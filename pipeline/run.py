#!/usr/bin/env python3
"""一個指令跑完：原檔 →（影片抽畫面詞彙表、PDF 抽詞彙表）→ 16k 音訊 →（切段）→ Gemini 逐字稿 + whisper 對照 → 校對專案。
用法: python pipeline/run.py meetings/<日期>_<標題> [--no-whisper] [--no-screen] [--force] [--model gemini-3.6-flash]

每一步的產出已存在就跳過（--force 全部重跑），所以中斷後重跑會接著做。
完成後用 python app.py 開校對工具。
"""
import base64, json, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from transcribe_gemini import load_env, call  # noqa: E402
from bundled import use_bundled_ffmpeg  # noqa: E402

# Google 已停用 3.5-flash 並自動導向 3.6，所以降級改用 3.8（音訊偶爾被 OTHER 隨機擋下，所以只當備援）
FALLBACK_MODEL = "gemini-3.8-flash"
SINGLE_MAX = 28 * 60          # 28 分鐘以內不切段
CHUNK = STEP = 1560           # 切段長度（秒），首尾相接不重疊（重疊會在合併時產生重複行；跨段講者靠 whisper 對齊）
INLINE_MAX = 18_000_000       # 超過改走 Files API
LINE = re.compile(r"^\[(\d+):(\d\d)\]\s*講者\d+：")
WHISPER_MODEL = ROOT / "vendor/models/ggml-large-v3-turbo.bin"

PROMPT = """這是一段會議錄音。請完整逐字轉寫成繁體中文（台灣用語），要求：
1. 每次換人說話、或同一人講了一段較長的話後，另起一行，格式嚴格為：[mm:ss] 講者N：內容
   - mm:ss 是這一行在本音檔中實際開始的時間（從 00:00 起算），請依聽到的時間標記，不要估算
   - 依聲音區分講者，編號從 講者1 開始，同一人全程使用同一個編號
2. 逐字保留原話，包括口語、重複與中英夾雜（英文保留原文），不要摘要、不要潤飾、不要省略任何段落
3. 聽不清楚的地方寫 [聽不清]，不要猜測編造
4. 只輸出逐字稿本身，不要任何前言或說明

以下是專有名詞表，聽到相近發音時請使用表中寫法：
{glossary}"""


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def ffprobe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def glossary(mdir):
    """全域詞彙表 + 本場人工詞彙表 + 自動抽出的畫面／資料詞彙表（存在才用）。"""
    files = (ROOT / "glossary.txt", mdir / "glossary.txt",
             mdir / "transcripts/glossary_screen.txt", mdir / "transcripts/glossary_materials.txt")
    return "\n".join(f.read_text(encoding="utf-8").strip() for f in files if f.exists())


GL_RULE = ("輸出格式：一行一組詞，正確寫法在前；同類的詞用頓號放同一行，行首可用括號註明類別；"
           "名稱若有口語簡稱（例如「成品繳庫單」常說「繳庫」），在同一行補上簡稱。"
           "只列實際出現的詞，不要解釋、不要編造，不要任何前言。")
SCREEN_PROMPT = """這些是一場會議螢幕分享的截圖，每張圖前面標了它在影片中的秒數（t=秒）。請輸出 JSON：
{"glossary": [字串, ...], "timeline": [{"t": 秒數, "desc": 字串}, ...]}

glossary：畫面上出現、會議中可能被口頭提到的專有名詞——程式代碼、系統與功能名稱、表單與欄位名稱、部門與職稱、廠區與地點、品牌與客戶、會議軟體畫面上顯示的與會者姓名。
- 每個元素是一行：正確寫法在前，同類的詞用頓號放同一行，行首可用括號註明類別；名稱若有口語簡稱（例如「成品繳庫單」常說「繳庫」），在同一行補上簡稱。
- 排除與會議內容無關的介面元素：瀏覽器分頁與書籤、作業系統工作列與通知、天氣、股價、聊天軟體、檔案總管的一般資料夾名稱。
- 只列實際出現的詞，不要解釋、不要編造。

timeline：每張圖一筆，desc 用一句繁體中文說明畫面主要在顯示什麼（例如「ERP 的 PC510 生產掃描紀錄查詢畫面，篩選大登分廠」「簡報：作業總表（二）繳庫到出貨」），
要寫出畫面上的程式代碼、單據或頁面標題；與前一張相同的畫面寫「同上」。"""
MATERIAL_PROMPT = "以下是會議相關資料的文字。請整理出會議中可能被口頭提到的專有名詞：程式代碼、作業名稱、系統名稱、部門與職稱、地點、人名。" + GL_RULE


def gemini_text(parts, key, as_json=False):
    """一般文字（或 JSON）輸出的 Gemini 呼叫（主模型試兩次再降級），失敗回傳空字串／None。"""
    body = {"contents": [{"role": "user", "parts": parts}]}
    if as_json:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    for m in ("gemini-3.6-flash", "gemini-3.6-flash", FALLBACK_MODEL):  # 主模型試兩次，再降級
        try:
            resp, _ = call(m, body, key)
        except OSError:
            resp = {}
        text = "".join(p.get("text", "") for c in resp.get("candidates", []) for p in c.get("content", {}).get("parts", []))
        if text.strip():
            if not as_json:
                return text.strip()
            try:
                return json.loads(text)
            except ValueError:
                log(f"{m} 回傳的 JSON 無法解析，重試")
        time.sleep(10)
    return None if as_json else ""


def source_file(mdir):
    src = next(mdir.glob("source.*"), None)
    if src is None:
        sys.exit(f"找不到 {mdir}/source.*")
    return src


MEDIA_EXT = {".mp4", ".m4a", ".mp3", ".wav", ".webm", ".mkv", ".mov", ".aac", ".flac", ".ogg", ".wma"}


def import_raw(mdir):
    """直接丟進資料夾的原始檔（例如 Teams 下載的 mp4 與 vtt）整理成標準結構：
    媒體檔 → source.*、字幕 → transcripts/teams.vtt、沒有 meeting.json 就建立。已整理過的不會再動。"""
    #====================================
    # 媒體檔改名為 source.*
    #====================================
    if not any(mdir.glob("source.*")):
        media = [f for f in mdir.iterdir() if f.is_file() and f.suffix.lower() in MEDIA_EXT]
        if not media:
            sys.exit(f"{mdir} 裡沒有錄音或影片檔")
        if len(media) > 1:
            sys.exit("資料夾裡有多個媒體檔，不知道要用哪個：" + "、".join(f.name for f in media))
        original = media[0].name
        media[0].rename(mdir / f"source{media[0].suffix.lower()}")
        log(f"原檔 {original} → source{media[0].suffix.lower()}")
    else:
        original = None

    #====================================
    # 字幕搬進 transcripts/
    #====================================
    (mdir / "transcripts").mkdir(exist_ok=True)
    vtts = []
    for f in sorted(mdir.glob("*.vtt")):
        dst, n = mdir / "transcripts/teams.vtt", 2
        while dst.exists():                       # 一場會議有多份字幕時依序編號，不覆蓋
            dst, n = mdir / f"transcripts/teams-{n}.vtt", n + 1
        f.rename(dst)
        vtts.append((f.name, dst.name))
        log(f"字幕 {f.name} → transcripts/{dst.name}")

    #====================================
    # 補建或更新 meeting.json
    #====================================
    meta_path = mdir / "meeting.json"
    if meta_path.exists() and not original and not vtts:
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    m = re.match(r"(\d{4}-\d{2}-\d{2})[-_ ]*(.*)", mdir.name)    # 資料夾名稱：日期_標題（分隔用 - 或 _ 都可以）
    meta.setdefault("title", (m.group(2) if m else mdir.name).replace("_", " ").strip() or mdir.name)
    meta.setdefault("date", m.group(1) if m else time.strftime("%Y-%m-%d"))
    src = source_file(mdir)
    s = meta.setdefault("source", {})
    s.setdefault("file", src.name)
    s.setdefault("duration_sec", round(ffprobe_duration(src)))
    if original:
        s["original_name"] = original
    for orig, name in vtts:
        meta.setdefault("transcripts", {})[name] = f"Teams 自動字幕（原檔名 {orig}）"
    meta.setdefault("participants", [])
    meta.setdefault("transcripts", {})
    meta.setdefault("status", "未處理")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def has_video(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=codec_name",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
    return bool(out) and out not in ("mjpeg", "png")      # 音檔內嵌封面圖不算影片


def screen_analysis(mdir, key, force, batch=60):
    """影片來源：抽關鍵畫面（畫面切換點 + 每 2 分鐘一張），請 Gemini 同時產出
    畫面詞彙表（transcripts/glossary_screen.txt）與畫面時間軸（transcripts/screen_timeline.json）。
    實測（docs/design.md）：比整段影片直接送 Gemini 便宜 3.5 倍且更準。截圖留在 frames/，校對工具與筆記會用到。"""
    out, tl_out = mdir / "transcripts/glossary_screen.txt", mdir / "transcripts/screen_timeline.json"
    src = source_file(mdir)
    if (out.exists() and tl_out.exists() and not force) or not has_video(src):
        return
    dur = ffprobe_duration(src)
    r = subprocess.run(["ffmpeg", "-hide_banner", "-skip_frame", "nokey", "-i", str(src), "-an",
                        "-vf", "scale=320:-1,select='gt(scene,0.06)',showinfo", "-fps_mode", "vfr", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    scenes = {round(float(t)) for t in re.findall(r"pts_time:([\d.]+)", r.stderr)}
    times = sorted(scenes | set(range(0, int(dur), 120)))
    fdir = mdir / "frames"; fdir.mkdir(exist_ok=True)
    log(f"畫面詞彙表：抽 {len(times)} 張關鍵畫面（畫面切換 {len(scenes)} 次）")
    frames = []
    for t in times:
        f = fdir / f"t{t:05d}.jpg"
        if not f.exists():
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(min(t + 1, dur - 1)), "-i", str(src),
                            "-frames:v", "1", "-vf", "scale=1280:-1", "-q:v", "4", str(f)], check=True)
        frames.append(f)
    gl_lines, timeline = [], []
    for i in range(0, len(frames), batch):
        parts = []
        for t, f in zip(times[i:i + batch], frames[i:i + batch]):
            parts += [{"text": f"t={t}"},
                      {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(f.read_bytes()).decode()}}]
        res = gemini_text(parts + [{"text": SCREEN_PROMPT}], key, as_json=True) or {}
        gl_lines += [str(x).strip() for x in res.get("glossary", []) if str(x).strip()]
        timeline += [x for x in res.get("timeline", []) if isinstance(x, dict) and "t" in x]
    if gl_lines:
        out.write_text("\n".join(dict.fromkeys(gl_lines)) + "\n", encoding="utf-8")
        log(f"畫面詞彙表完成：{len(gl_lines)} 行 → transcripts/glossary_screen.txt")
    if timeline:
        names = {t: f.name for t, f in zip(times, frames)}
        rows, last = [], None
        for x in sorted(timeline, key=lambda x: int(x["t"])):
            t = int(x["t"]); desc = str(x.get("desc", "")).strip()
            if t not in names:
                continue
            if desc in ("", "同上"):
                desc = last or ""
            rows.append({"t": t, "frame": names[t], "desc": desc, "same_as_prev": desc == last})
            last = desc
        tl_out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"畫面時間軸完成：{len(rows)} 張，{sum(not r['same_as_prev'] for r in rows)} 個不同畫面 → transcripts/screen_timeline.json")


def materials_glossary(mdir, key, force):
    """會議資料夾裡的 PDF（簡報、文件）→ 詞彙表。"""
    out = mdir / "transcripts/glossary_materials.txt"
    pdfs = sorted(mdir.glob("*.pdf"))
    if (out.exists() and not force) or not pdfs:
        return
    try:
        import pymupdf
    except ImportError:
        log("略過 PDF 詞彙表：未安裝 PyMuPDF（pip install pymupdf）")
        return
    text = "\n".join(p.get_text() for f in pdfs for p in pymupdf.open(f))[:200_000]
    gl = gemini_text([{"text": MATERIAL_PROMPT + "\n\n" + text}], key)
    if gl:
        out.write_text(gl + "\n", encoding="utf-8")
        log(f"資料詞彙表完成（{len(pdfs)} 個 PDF）：{len(gl.splitlines())} 行 → transcripts/glossary_materials.txt")


def speech_end(audio):
    """結尾若有超過 1 分鐘的完全無聲（例如錄影後段沒錄到聲音），回傳有聲音的結束時間，避免把空白送去轉錄。"""
    dur = ffprobe_duration(audio)
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(audio), "-af", "silencedetect=n=-50dB:d=10", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    # 最後一段靜音一直延續到結尾（新版 ffmpeg 在檔尾也會印 silence_end，舊版不會）
    runs_to_end = len(starts) > len(ends) or (ends and ends[-1] >= dur - 1)
    if starts and runs_to_end and dur - starts[-1] > 60:
        return starts[-1] + 5, dur
    return dur, dur


def prepare_audio(mdir, force):
    """source.* → audio/16k.m4a（16 kHz 單聲道 AAC 48 kbps）。"""
    out = mdir / "audio/16k.m4a"
    if out.exists() and not force:
        return out
    src = source_file(mdir)
    out.parent.mkdir(exist_ok=True)
    log(f"轉檔 {src.name} → audio/16k.m4a")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "aac", "-b:a", "48k", str(out)], check=True)
    return out


def split(mdir, audio, meta, force):
    """回傳 [(音檔, 位移秒數)]。meeting.json 已有 parts 就沿用，以免位移改變導致校對專案對不上。"""
    if meta.get("parts"):
        return [(mdir / p["file"], p["offset_sec"]) for p in meta["parts"]]
    dur, full = speech_end(audio)
    if dur < full:
        log(f"結尾 {full - dur:.0f} 秒完全無聲，只轉錄前 {dur:.0f} 秒")
        meta["silent_tail_from_sec"] = round(dur)
    if dur == full and dur <= SINGLE_MAX:
        return [(audio, 0)]
    parts, off, i = [], 0, 1
    while off < dur:
        last = dur - (off + CHUNK) < 120     # 剩不到 2 分鐘就併進這一段，避免切出太短的尾段（或漏掉）
        cut = dur if last else quietest_point(audio, off + CHUNK)
        length = cut - off
        p = mdir / f"audio/part{i}.m4a"
        if not p.exists() or force:
            log(f"切段 part{i}：{off}s 起 {length:.0f}s")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(off), "-t", f"{length:.3f}", "-i", str(audio),
                            "-c", "copy", str(p)], check=True)
        parts.append((p, off))
        if last:
            break
        off, i = cut, i + 1
    return parts


def quietest_point(audio, center, radius=10, win=0.5):
    """在 center 前後 radius 秒內找最安靜的 win 秒，回傳它的中點（秒，取到小數第二位），避免把一句話切成兩半。
    整段一樣安靜（例如沒有聲音）時回傳 center。"""
    #====================================
    # 每 win 秒算一次音量
    #====================================
    lo = max(0, center - radius)
    r = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(lo), "-t", str(2 * radius), "-i", str(audio), "-af",
                        f"asetnsamples=n={int(16000 * win)},astats=metadata=1:reset=1,"
                        "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    times = [float(x) for x in re.findall(r"pts_time:([\d.]+)", r.stderr)]
    rms = [(-200.0 if v == "-inf" else float(v)) for v in re.findall(r"RMS_level=(-?[\d.]+|-inf)", r.stderr)]

    #====================================
    # 取最安靜的一格
    #====================================
    if not rms or max(rms) - min(rms) < 1:           # 沒有明顯差異就照原定位置切
        return center
    k = min(range(min(len(times), len(rms))), key=lambda j: rms[j])
    return round(lo + times[k] + win / 2, 2)


def audio_part(path, key):
    if path.stat().st_size > INLINE_MAX:
        from gemini_transcribe import upload_file
        uri, _ = upload_file(str(path), "audio/m4a", key)
        return {"file_data": {"mime_type": "audio/m4a", "file_uri": uri}}
    return {"inline_data": {"mime_type": "audio/m4a", "data": base64.b64encode(path.read_bytes()).decode()}}


def check(resp, speech_sec=None):
    """回傳 (逐字稿文字, 問題描述)。問題描述為空代表可用。
    speech_sec：這段音檔有聲音的長度；給了就檢查逐字稿有沒有涵蓋到結尾（模型偶爾會轉一小段就 STOP）。"""
    if "error" in resp:
        st = resp.get("status")
        return "", f"{st if isinstance(st, str) else f'HTTP {st}'} {json.dumps(resp['error'], ensure_ascii=False)[:200]}"
    if resp.get("promptFeedback", {}).get("blockReason"):
        return "", f"被擋：{resp['promptFeedback']['blockReason']}"
    cands = resp.get("candidates", [])
    text = "".join(p.get("text", "") for c in cands for p in c.get("content", {}).get("parts", []))
    finish = cands[0].get("finishReason") if cands else None
    lines = [l for l in text.splitlines() if l.strip()]
    if finish == "MAX_TOKENS":
        return text, "輸出撞到上限（可能陷入重複迴圈）"
    if not lines or sum(1 for l in lines if LINE.match(l.strip())) < len(lines) * 0.8:
        return text, "格式不符（少於八成的行是 [mm:ss] 講者N：內容）"
    if max(len(l) for l in lines) > 1500:
        return text, "有單行超過 1500 字（可能陷入重複迴圈）"
    if speech_sec and speech_sec > 300:
        stamps = [int(m[1]) * 60 + int(m[2]) for m in (LINE.match(l.strip()) for l in lines) if m]
        if max(stamps) < speech_sec - 180:
            return text, f"只轉到 {max(stamps) // 60} 分 {max(stamps) % 60:02d} 秒就結束（有聲音到 {speech_sec / 60:.0f} 分鐘）"
    return text, ""


def transcribe_part(path, prefix, model, gl, key, force):
    txt = Path(str(prefix) + ".txt")
    if txt.exists() and not force:
        log(f"已有 {txt.name}，跳過")
        return txt
    body = {"contents": [{"role": "user", "parts": [audio_part(path, key), {"text": PROMPT.format(glossary=gl)}]}]}
    speech_sec, _ = speech_end(path)
    for m in (model, model, FALLBACK_MODEL):  # 主模型試兩次，再降級
        log(f"{path.name} → {m}")
        try:
            resp, dt = call(m, body, key)
        except OSError as e:                 # 連線中斷、逾時（URLError、socket.timeout 都是 OSError）
            resp, dt = {"error": {"message": repr(e)}, "status": "連線錯誤"}, 0
        text, problem = check(resp, speech_sec)
        if not problem:
            Path(str(prefix) + ".json").write_text(json.dumps(resp, ensure_ascii=False, indent=1), encoding="utf-8")
            txt.write_text(text, encoding="utf-8")
            u = resp.get("usageMetadata", {})
            log(f"{path.name} 完成：{m} {dt:.0f}s，{len(text.splitlines())} 行，"
                f"tokens in={u.get('promptTokenCount')} out={u.get('candidatesTokenCount')}")
            return txt
        log(f"{path.name} {m} 失敗（{dt:.0f}s）：{problem}")
        if resp.get("status") in (429, 500, 503, "連線錯誤"):   # 過載、限流或斷線：等一下再試
            time.sleep(30)
    log(f"{path.name} 轉錄失敗，三次都不可用")
    return None


def run_whisper(mdir, audio, gl, force):
    prefix = mdir / "transcripts/whisper-turbo"
    out = Path(str(prefix) + ".json")
    if out.exists() and not force:
        log("已有 whisper-turbo.json，跳過")
        return out
    # whisper 的 prompt 只放正確詞彙（去掉括號裡的誤辨說明），太長會被截掉
    terms = [t.strip() for t in re.split(r"[、\n：，,]", re.sub(r"（[^）]*）", "", gl)) if t.strip()]
    hint = "以下是繁體中文的會議錄音。" + "、".join(t for t in terms if t not in ("人名", "OA 串接相關"))
    log("whisper（vibe-server，內顯）開始")
    t0 = time.time()
    r = subprocess.run([sys.executable, str(HERE / "transcribe_whisper.py"), str(audio), str(WHISPER_MODEL), str(prefix),
                        "--diarize", "--vad", "--field", "language=zh", "--field", f"prompt={hint[:400]}"],
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode or not out.exists():
        log(f"whisper 失敗，略過對照：{(r.stdout + r.stderr)[-300:]}")
        return None
    log(f"whisper 完成：{time.time() - t0:.0f}s")
    return out


def auto_name_speakers(mdir):
    """用聲紋替新會議的講者帶入名字（沒裝 sherpa-onnx 或沒有聲紋資料就略過，不影響轉錄結果）。"""
    try:
        import voiceprints
        if not voiceprints.available() or not voiceprints.load_db()["people"]:
            return
        pj = mdir / "project.json"
        p = json.loads(pj.read_text(encoding="utf-8"))
        found, purity = voiceprints.match(mdir, p)
        voiceprints.apply_match(p, found, purity)
        pj.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
        auto = [f"{l}→{m['name']}" for l, m in found.items() if m["score"] >= voiceprints.AUTO and not m.get("same_as")]
        log("聲紋帶入：" + ("、".join(auto) if auto else "沒有夠像的已知講者")
            + (f"；另有 {len(found) - len(auto)} 則名字建議" if len(found) > len(auto) else ""))
    except Exception as e:                           # 聲紋只是輔助，出錯不能讓整個流程失敗
        log(f"聲紋比對略過：{e}")


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    use_bundled_ffmpeg()
    mdir = Path(args[0]).resolve()
    force, use_whisper = "--force" in args, "--no-whisper" not in args
    model = args[args.index("--model") + 1] if "--model" in args else "gemini-3.6-flash"
    load_env()
    key = os.environ["GEMINI_API_KEY"]
    import_raw(mdir)
    meta_path = mdir / "meeting.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    (mdir / "transcripts").mkdir(exist_ok=True)
    if "--no-screen" not in args:
        screen_analysis(mdir, key, force)
    materials_glossary(mdir, key, force)
    gl = glossary(mdir)

    audio = prepare_audio(mdir, force)
    parts = split(mdir, audio, meta, force)
    with ThreadPoolExecutor(max_workers=1) as pool:  # whisper 在本機跑，與 Gemini 同時進行
        wfut = pool.submit(run_whisper, mdir, audio, gl, force) if use_whisper else None
        single = len(parts) == 1
        txts = [transcribe_part(p, mdir / f"transcripts/{model}" if single else mdir / f"transcripts/{model}.p{i}",
                                model, gl, key, force) for i, (p, _) in enumerate(parts, 1)]
        if None in txts and wfut and not wfut.done():
            log("Gemini 轉錄失敗；等本機 whisper 跑完再結束（它的結果會保留，重跑時會跳過）")
        wjson = wfut.result() if wfut else None
    if None in txts:
        sys.exit("轉錄失敗，未建立校對專案。稍後重跑同一個指令即可（已完成的部分會跳過）。")

    cmd = [sys.executable, str(HERE / "build_project.py"), str(mdir), str(audio)]
    if wjson:
        cmd += ["--whisper", str(wjson)]
    cmd += [f"{t}:{off}" for t, (_, off) in zip(txts, parts)]
    log("建立校對專案")
    subprocess.run(cmd, check=True)
    auto_name_speakers(mdir)

    rel = lambda p: p.relative_to(mdir).as_posix()
    meta["audio"] = {"file": rel(audio), "note": "16 kHz 單聲道 AAC 48 kbps，校對工具與重聽用"}
    if not single:
        meta["parts"] = [{"file": rel(p), "offset_sec": off} for p, off in parts]
    tr = meta.setdefault("transcripts", {})
    tr[model if single else f"{model}.p1~p{len(parts)}"] = f"{model}（失敗時降級 {FALLBACK_MODEL}）+ 詞彙表提示詞，pipeline/run.py（{time.strftime('%Y-%m-%d')}）"
    if wjson:
        tr["whisper-turbo"] = f"Vibe large-v3-turbo，講者分離 + VAD，pipeline/run.py（{time.strftime('%Y-%m-%d')}）"
    for f, desc in (("glossary_screen.txt", "由關鍵畫面（frames/）抽出的詞彙表，已併入轉錄提示詞"),
                    ("glossary_materials.txt", "由會議資料（PDF）抽出的詞彙表，已併入轉錄提示詞"),
                    ("screen_timeline.json", "畫面時間軸：每張關鍵畫面的時間與內容說明，校對工具在畫面切換處顯示縮圖")):
        if (mdir / "transcripts" / f).exists():
            tr[f] = desc
    meta["status"] = "已轉錄，待校對"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    log(f"完成。python app.py 後在校對工具選「{mdir.name}」")


if __name__ == "__main__":
    main()
