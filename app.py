#!/usr/bin/env python3
"""逐字稿校對工具（本機網頁）。啟動: python app.py  然後開 http://127.0.0.1:5077"""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "pipeline"))
from run import glossary  # noqa: E402  共用詞彙表讀取
import recorder  # noqa: E402  實體會議錄音
import notes as notes_mod  # noqa: E402  會議紀錄
import correct as correct_mod  # noqa: E402  錯字建議
import fillers as fillers_mod  # noqa: E402  清語助詞
import voiceprints  # noqa: E402  聲紋
from bundled import use_bundled_ffmpeg  # noqa: E402
use_bundled_ffmpeg()
recorder.MEETINGS.mkdir(exist_ok=True)               # 全新安裝還沒有會議資料夾；錄音、待轉錄清單都要它在
# 校對專案：每個資料夾一個 project.json，專案名稱即資料夾名
PROJECT_GLOBS = ["meetings/*/project.json", "research/projects/*/project.json"]
app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
_lock = threading.Lock()


def project_dirs():
    return {f.parent.name: f.parent for g in PROJECT_GLOBS for f in sorted(ROOT.glob(g))}


def pdir(name):
    d = project_dirs().get(name)
    if d is None:
        abort(404)
    return d


def audio_path(name, p):
    """project.json 的 audio 是相對於專案資料夾的路徑。"""
    return pdir(name) / p["audio"]


def load(name):
    return json.loads((pdir(name) / "project.json").read_text(encoding="utf-8"))


def save(name, p):
    (pdir(name) / "project.json").write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")


def spk_name(p, label):
    return p["speakers"].get(label, {}).get("name") or label


def fmt(s):
    return f"{int(s) // 60:02d}:{int(s) % 60:02d}"


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/projects")
def projects():
    return jsonify(sorted(project_dirs()))


_overview_cache = {}                                     # 專案資料夾 → ((project.json, meeting.json 的 mtime), 摘要)


def _sort_key(kv):
    # 日期開頭的會議由新到舊，research/projects 的實驗專案（不是日期開頭）放最後
    return (kv[0][:4].isdigit(), kv[0])


@app.get("/api/projects/overview")
def projects_overview():
    """左欄會議清單：標題、日期、長度、校對進度（依 mtime 快取，校對中每次存檔只重讀那一場）。"""
    out = []
    for name, d in sorted(project_dirs().items(), key=_sort_key, reverse=True):
        mj = d / "meeting.json"
        key = ((d / "project.json").stat().st_mtime, mj.stat().st_mtime if mj.exists() else 0)
        cached = _overview_cache.get(d)
        if not cached or cached[0] != key:
            p = json.loads((d / "project.json").read_text(encoding="utf-8"))
            meta = json.loads(mj.read_text(encoding="utf-8")) if mj.exists() else {}
            dated = name[:4].isdigit()
            title = meta.get("title") or (name[11:] if dated else name)
            # 錄音頁的標題可能自己又打了日期；清單另有日期欄，開頭的日期不重複顯示
            title = re.sub(r"^\d{4}-\d{2}-\d{2}[-_ ]*", "", title) or title
            cached = (key, {"name": name, "title": title,
                            "date": meta.get("date") or (name[:10] if dated else ""),
                            "duration": meta.get("source", {}).get("duration_sec"),
                            "done": sum(1 for s in p["segments"] if s["status"] != "pending"),
                            "total": len(p["segments"])})
            _overview_cache[d] = cached
        out.append(cached[1])
    return jsonify(out)


_search_cache = {}                                     # 專案資料夾 → (project.json 的 mtime, 每行 (id, start, 講者, 文字))
SEARCH_LIMIT = 300


@app.get("/api/search")
def search():
    """跨會議搜尋校對後的文字：空白分開的每個詞都要出現（不分大小寫），新的會議在前。"""
    #====================================
    # 解析關鍵字
    #====================================
    terms = [t.lower() for t in request.args.get("q", "").split()]
    if not terms:
        return jsonify({"results": [], "total": 0, "limit": SEARCH_LIMIT})

    #====================================
    # 逐場比對
    #====================================
    results, total = [], 0
    for name, d in sorted(project_dirs().items(), key=_sort_key, reverse=True):
        f = d / "project.json"
        mtime = f.stat().st_mtime
        cached = _search_cache.get(d)
        if not cached or cached[0] != mtime:          # 每打一個字就搜一次，沒改過的專案不重讀
            p = json.loads(f.read_text(encoding="utf-8"))
            cached = (mtime, [(s["id"], s["start"], spk_name(p, s["speaker"]), s["text"]) for s in p["segments"]])
            _search_cache[d] = cached
        for sid, start, speaker, text in cached[1]:
            low = text.lower()
            if all(t in low for t in terms):
                total += 1
                if len(results) < SEARCH_LIMIT:
                    results.append({"project": name, "id": sid, "start": start, "speaker": speaker, "text": text})

    #====================================
    # 回傳
    #====================================
    return jsonify({"results": results, "total": total, "limit": SEARCH_LIMIT})


@app.get("/api/project/<name>")
def project(name):
    p = load(name)
    tl = pdir(name) / "transcripts/screen_timeline.json"   # 影片會議的畫面時間軸（run.py 產生）
    p["timeline"] = json.loads(tl.read_text(encoding="utf-8")) if tl.exists() else []
    p["voiceprint_available"] = voiceprints.available()
    return jsonify(p)


@app.get("/api/project/<name>/notes")
def get_notes(name):
    f = pdir(name) / "notes.json"
    return jsonify(json.loads(f.read_text(encoding="utf-8")) if f.exists() else None)


@app.get("/api/project/<name>/notes/md")
def notes_md(name):
    """另存用的 Markdown：從 notes.json 現產，和面板顯示的一定是同一份。"""
    f = pdir(name) / "notes.json"
    if not f.exists():
        abort(404)
    return jsonify({"text": notes_mod.to_markdown(json.loads(f.read_text(encoding="utf-8")))})


@app.post("/api/project/<name>/notes")
def make_notes(name):
    """整理會議紀錄（約 30～60 秒，同步執行）。帶 answers 時是依「待確認」的回答重新整理。"""
    #====================================
    # 解析參數
    #====================================
    b = request.get_json(silent=True) or {}
    d = pdir(name)
    answers = {q: a.strip() for q, a in (b.get("answers") or {}).items() if a and a.strip()}
    previous = json.loads((d / "notes.json").read_text(encoding="utf-8")) if answers and (d / "notes.json").exists() else None
    template = b.get("template") or (previous or {}).get("_meta", {}).get("template", "general")
    if template not in notes_mod.TEMPLATES:
        abort(400)

    #====================================
    # 整理並存檔
    #====================================
    try:
        n = notes_mod.generate(d, template, answers or None, previous)
    except SystemExit as e:                          # generate 失敗時用 sys.exit 帶訊息
        return jsonify({"error": str(e)}), 502
    notes_mod.save(d, n)
    return jsonify(n)


@app.get("/record")
def record_page():
    return send_from_directory(app.static_folder, "record.html")


def _rec_call(fn, *a, **k):
    try:
        return jsonify(fn(*a, **k))
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409


@app.get("/api/rec/devices")
def rec_devices():
    return jsonify(recorder.list_devices())


@app.get("/api/rec/status")
def rec_status():
    return jsonify(recorder.status())


@app.post("/api/rec/start")
def rec_start():
    b = request.get_json(force=True)
    return _rec_call(recorder.start, b.get("title", ""), b.get("device"), b.get("auto_transcribe", False))


@app.post("/api/rec/stop")
def rec_stop():
    b = request.get_json(silent=True) or {}
    return _rec_call(recorder.stop, b.get("next_title"))


@app.post("/api/rec/transcribe/<folder>")
def rec_transcribe(folder):
    """對還沒轉逐字稿的會議資料夾執行 run.py（原始檔由 run.py 自動整理）。"""
    if folder not in recorder.pending(recorder.status()["transcribing"]):
        abort(404)
    recorder.transcribe(recorder.MEETINGS / folder)
    return jsonify(recorder.status())


@app.post("/api/rec/finalize/<folder>")
def rec_finalize(folder):
    """合併上次沒正常結束的錄音。"""
    d = next((x.parent for x in recorder.unfinished() if x.parent.name == folder), None)
    if d is None:
        abort(404)
    threading.Thread(target=recorder.finalize, args=(d,), daemon=True).start()
    return jsonify(recorder.status())


@app.get("/frame/<name>/<path:fname>")
def frame(name, fname):
    return send_from_directory(pdir(name) / "frames", fname)


@app.get("/audio/<name>")
def audio(name):
    a = str(audio_path(name, load(name)))
    mime = "audio/mp4" if a.endswith((".m4a", ".mp4")) else None
    return send_file(a, conditional=True, mimetype=mime)


@app.post("/api/project/<name>/segment/<int:sid>")
def update_segment(name, sid):
    body = request.get_json(force=True)
    with _lock:
        p = load(name)
        s = p["segments"][sid]
        for k in ("text", "speaker", "status", "llm_suggestion"):
            if k in body:
                s[k] = body[k]
        if body.get("llm_suggestion") == "":       # 使用者忽略或還原了建議：批次重聽不要再送這行
            s["llm_tried"] = True
        if "text" in body and body["text"] != s["text_orig"] and s["status"] == "pending":
            s["status"] = "edited"
        if "text" in body and body["text"] == s["text_orig"]:
            s.pop("cleaned", None)                 # 還原成原文，就不再算清過語助詞
        s["updated"] = time.time()
        save(name, p)
    return jsonify(s)


@app.post("/api/project/<name>/speakers")
def speakers(name):
    body = request.get_json(force=True)
    with _lock:
        p = load(name)
        if body.get("op") == "rename":
            sp = p["speakers"].setdefault(body["label"], {})
            sp.update(name=body.get("name", ""), name_source="manual" if body.get("name") else "", voice_score=None,
                      voice_warning="")
            if voiceprints.available():              # 取名就記住這個人的聲音（背景算，約幾秒）
                threading.Thread(target=_enroll, args=(name, body["label"], body.get("name", "")), daemon=True).start()
        elif body.get("op") == "merge":
            src, dst = body["src"], body["dst"]
            for s in p["segments"]:
                if s["speaker"] == src:
                    s["speaker"] = dst
            p["speakers"].pop(src, None)
        save(name, p)
    return jsonify(p["speakers"])


@app.post("/api/project/<name>/export")
def export(name):
    p = load(name)
    lines = [f"# {name} 校對版逐字稿", ""]
    corrections = []
    for s in p["segments"]:
        # 每行之間空一行，Markdown 才會分段顯示（單一換行會被併成同一段）
        lines += [f"**[{fmt(s['start'])}] {spk_name(p, s['speaker'])}**：{s['text']}", ""]
        if s["text"] != s["text_orig"]:
            corrections.append({"id": s["id"], "start": s["start"], "orig": s["text_orig"],
                                "fixed": s["text"], "whisper": s.get("text_whisper", "")})
    d = pdir(name)
    md = "\n".join(lines)
    # 會議資料夾裡一律留一份，頁面另存到使用者選的位置用回傳的 text
    (d / "corrected.md").write_text(md, encoding="utf-8")
    (d / "corrections.json").write_text(
        json.dumps({"speakers": p["speakers"], "corrections": corrections}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    done = sum(1 for s in p["segments"] if s["status"] != "pending")
    return jsonify({"md": (d / "corrected.md").relative_to(ROOT).as_posix(), "text": md, "corrections": len(corrections),
                    "done": done, "total": len(p["segments"])})


def _relisten(name, sid):
    """剪出該行前後 2 秒的音訊，連同上下文與詞彙表請 Gemini 重聽這一行。"""
    from transcribe_gemini import load_env, call
    load_env()
    key = os.environ["GEMINI_API_KEY"]
    p = load(name)
    segs = p["segments"]
    s = segs[sid]
    lo, hi = max(0, s["start"] - 2), s["end"] + 2
    tmp = Path(tempfile.gettempdir()) / f"relisten_{name}_{sid}.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(lo), "-t", str(hi - lo), "-i", str(audio_path(name, p)),
                    "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "48k", str(tmp)], check=True)
    ctx_prev = "\n".join(f"{spk_name(p, x['speaker'])}：{x['text']}" for x in segs[max(0, sid - 3):sid])
    ctx_next = "\n".join(f"{spk_name(p, x['speaker'])}：{x['text']}" for x in segs[sid + 1:sid + 3])
    gl = glossary(pdir(name))  # 全域詞彙表 + 本場詞彙表
    whisper_line = f"另一個引擎聽到的是：{s['text_whisper']}\n\n" if s.get("text_whisper") else ""
    prompt = ("這是一場會議逐字稿的校對。附上的音訊片段涵蓋「目前這一行」及其前後各約 2 秒。\n"
              "請仔細聽，只重新轉寫「目前這一行」講的內容，輸出繁體中文（台灣用語）一行，"
              "保留原話、不要摘要、不要加任何說明。\n\n"
              f"前文：\n{ctx_prev}\n\n目前這一行（機器辨識，可能有誤）：{s['text']}\n\n"
              f"{whisper_line}後文：\n{ctx_next}\n\n專有名詞表：\n{gl}")
    data = base64.b64encode(tmp.read_bytes()).decode()
    tmp.unlink(missing_ok=True)
    body = {"contents": [{"role": "user", "parts": [
        {"inline_data": {"mime_type": "audio/m4a", "data": data}}, {"text": prompt}]}]}
    text, model, dt, resp, error = "", "", 0, {}, ""
    for model in ("gemini-3.6-flash", "gemini-3.8-flash"):
        try:
            resp, dt = call(model, body, key)
        except OSError as e:                 # 連線中斷、逾時
            resp, dt = {"error": {"message": repr(e)}}, 0
        text = "".join(pt.get("text", "") for c in resp.get("candidates", [])
                       for pt in c.get("content", {}).get("parts", [])).strip()
        if text:
            break
        error = resp.get("error", {}).get("message", "")[:200]
    text = text.splitlines()[0].strip() if text else ""
    if "：" in text[:12]:
        text = text.split("：", 1)[1]
    blocked = resp.get("promptFeedback", {}).get("blockReason")
    # 只有拿到建議才覆寫；連線或 HTTP 錯誤時保留原本的建議，也不標記為已重聽，下次批次會再試
    if text or blocked:
        with _lock:
            p = load(name)
            if text:
                p["segments"][sid]["llm_suggestion"] = text
            p["segments"][sid]["llm_tried"] = True
            save(name, p)
    return {"suggestion": text, "model": model, "seconds": round(dt, 1), "blocked": blocked,
            "error": "" if text else error}


@app.post("/api/project/<name>/relisten/<int:sid>")
def relisten(name, sid):
    return jsonify(_relisten(name, sid))


@app.post("/api/project/<name>/fixes")
def find_fixes(name):
    """AI 找錯字（約 20～60 秒）：模型呼叫在鎖外做，回來後再在鎖內讀最新的專案合併，避免蓋掉這段時間的修改。"""
    #====================================
    # 呼叫模型找錯字
    #====================================
    found = correct_mod.suggest(pdir(name), load(name))

    #====================================
    # 合併進專案
    #====================================
    with _lock:
        p = load(name)
        n = correct_mod.merge(p, found)
        save(name, p)
    return jsonify({"new": n, "segments": sum(1 for s in p["segments"] if any(x.get("status") == "pending" for x in s.get("fixes", [])))})


@app.post("/api/project/<name>/fillers")
def clean_fillers(name):
    """清語助詞（規則加模型，約 20～60 秒）：模型在鎖外跑，回來後只套用到這段時間沒被動過、仍未處理的行。"""
    #====================================
    # 規則與模型清理
    #====================================
    found = fillers_mod.clean(pdir(name), load(name))

    #====================================
    # 合併進專案
    #====================================
    with _lock:
        p = load(name)
        n = 0
        for s in p["segments"]:
            before, after = found.get(s["id"], (None, None))
            if before is None or s["status"] != "pending" or s["text"] != before:
                continue
            s["text"], s["cleaned"] = after, True
            for f in s.get("fixes", []):
                if f.get("status") == "pending" and f["original"] not in after:
                    f["status"] = "stale"             # 錯字建議的原文片段被清掉了
            n += 1
        save(name, p)
    return jsonify({"cleaned": n, "segments": p["segments"]})


@app.post("/api/project/<name>/fillers/undo")
def undo_fillers(name):
    """復原清語助詞：只改回還沒處理的行；清過之後使用者又改過或接受的，以使用者為準不動。"""
    with _lock:
        p = load(name)
        n = 0
        for s in p["segments"]:
            if s.get("cleaned") and s["status"] == "pending":
                s["text"] = s["text_orig"]
                del s["cleaned"]
                n += 1
        save(name, p)
    return jsonify({"undone": n, "segments": p["segments"]})


@app.post("/api/project/<name>/segment/<int:sid>/fix")
def apply_fix(name, sid):
    """接受或忽略一則錯字建議；all=true 時同一組「原文→建議」在全部的行一起處理。
    接受過的記進 project["learned"]，供詞彙表建議使用。"""
    #====================================
    # 解析參數
    #====================================
    b = request.get_json(force=True)
    action, idx = b.get("action"), int(b.get("index", -1))
    if action not in ("accept", "dismiss"):
        abort(400)

    with _lock:
        #====================================
        # 找出要處理的建議
        #====================================
        p = load(name)
        segs = p["segments"]
        fixes = segs[sid].get("fixes", [])
        if not 0 <= idx < len(fixes):
            abort(404)
        pair = (fixes[idx]["original"], fixes[idx]["suggestion"])
        targets = [(s, f) for s in segs for f in s.get("fixes", [])
                   if f.get("status") == "pending" and (f["original"], f["suggestion"]) == pair
                   and (b.get("all") or s["id"] == sid)]

        #====================================
        # 套用
        #====================================
        changed = []
        for s, f in targets:
            if action == "dismiss":
                f["status"] = "dismissed"
            elif f["original"] in s["text"]:
                s["text"] = s["text"].replace(f["original"], f["suggestion"])   # 同一行出現幾次就改幾次
                f["status"] = "accepted"
                if s["status"] == "pending":
                    s["status"] = "edited"
                p.setdefault("learned", []).append({"original": f["original"], "suggestion": f["suggestion"],
                                                    "reason": f.get("reason", ""), "segment": s["id"]})
            else:
                f["status"] = "stale"                  # 使用者已經手動改過這段，原文片段不在了
            s["updated"] = time.time()
            changed.append(s)
        save(name, p)
    return jsonify({"segments": changed})


@app.post("/api/project/<name>/guess_speakers")
def guess_speakers(name):
    """AI 從對話內容猜講者名字（只當建議，存在 project["name_suggestions"]，套用由前端呼叫改名）。"""
    found = correct_mod.guess_speaker_names(pdir(name), load(name))
    with _lock:
        p = load(name)
        p["name_suggestions"] = found
        save(name, p)
    return jsonify(found)


@app.post("/api/project/<name>/name_suggestions/dismiss")
def dismiss_name_suggestion(name):
    b = request.get_json(force=True)
    with _lock:
        p = load(name)
        p["name_suggestions"] = [x for x in p.get("name_suggestions", [])
                                 if not (x["speaker"] == b.get("speaker") and x["name"] == b.get("name"))]
        save(name, p)
    return jsonify(p["name_suggestions"])


_vp_lock = threading.Lock()                         # 聲紋資料庫是單一檔案，學習與比對不要同時寫


def _enroll(name, label, person):
    with _vp_lock:
        _, warn = voiceprints.enroll(pdir(name), load(name), label, person)
    # 背景算完才知道有沒有學；警告寫進專案，講者欄重新載入時顯示
    with _lock:
        p = load(name)
        sp = p["speakers"].get(label)
        if sp is not None and sp.get("name") == person and sp.get("voice_warning", "") != warn:
            sp["voice_warning"] = warn
            save(name, p)


@app.post("/api/project/<name>/voiceprint/match")
def voiceprint_match(name):
    """聲紋比對：相似度夠高的直接帶入名字（標「自動」），次高的列為名字建議；同時更新各講者的一致性（偵測混到別人）。"""
    if not voiceprints.available():
        return jsonify({"error": "需要 pip install sherpa-onnx 與聲紋模型（見 README）"}), 501
    with _vp_lock:
        found, purity = voiceprints.match(pdir(name), load(name))
    with _lock:
        p = voiceprints.apply_match(load(name), found, purity)
        save(name, p)
    return jsonify({"speakers": p["speakers"], "name_suggestions": p["name_suggestions"], "speaker_purity": purity,
                    "auto": voiceprints.auto_count(found)})


@app.post("/api/project/<name>/voiceprint/reject")
def voiceprint_reject(name):
    """「不是他」：清掉自動帶入的名字，之後這場這位講者不再帶入這個人。"""
    label = request.get_json(force=True).get("label")
    with _lock:
        p = load(name)
        sp = p["speakers"].get(label) or abort(404)
        if sp.get("name"):
            voiceprints.reject(pdir(name), label, sp["name"])
        sp.update(name="", name_source="", voice_score=None)
        save(name, p)
    return jsonify(p["speakers"])


@app.get("/api/glossary/suggestions")
def glossary_suggestions():
    return jsonify(correct_mod.glossary_suggestions(ROOT))


@app.post("/api/glossary/suggestions")
def glossary_suggestion_action():
    """詞彙表建議：global＝加入全域詞彙表、meeting＝加入這場的詞彙表、ignore＝之後不再建議。"""
    b = request.get_json(force=True)
    term, action = (b.get("term") or "").strip(), b.get("action")
    if not term or action not in ("global", "meeting", "ignore"):
        abort(400)
    if action == "ignore":
        correct_mod.ignore_term(ROOT, term)
    elif action == "global":
        correct_mod.add_glossary_line(ROOT / "glossary.txt", term, b.get("variants") or [])
    else:
        correct_mod.add_glossary_line(pdir(b.get("project", "")) / "glossary.txt", term, b.get("variants") or [])
    return jsonify(correct_mod.glossary_suggestions(ROOT))


@app.post("/api/project/<name>/prepass")
def prepass(name):
    """對可疑、尚未處理、也還沒重聽過的行批次重聽（並行 4 個）。?limit=N 控制一次處理幾行。
    已重聽過（含使用者忽略或還原過建議）、已接受或已修改的行都不再送。"""
    p = load(name)
    ids = [s["id"] for s in p["segments"] if s.get("flag") == "suspect" and s["status"] == "pending"
           and not s.get("llm_suggestion") and not s.get("llm_tried")]
    limit = int(request.args.get("limit", 20))

    def one(sid):
        try:
            return _relisten(name, sid)
        except Exception as e:               # 單行失敗（例如 ffmpeg 錯誤）不影響整批
            return {"suggestion": "", "error": repr(e)[:200]}

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, ids[:limit]))
    failed = sum(1 for r in results if r.get("error"))
    return jsonify({"processed": len(results), "failed": failed,
                    "suggested": sum(1 for r in results if r.get("suggestion")),
                    "remaining": max(0, len(ids) - limit) + failed})


if __name__ == "__main__":
    if "--open" in sys.argv:                         # 啟動捷徑用：伺服器起來後再開瀏覽器，免得先看到連不上
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:5077/")).start()
    # .env 由 transcribe_gemini.load_env 自己讀；關掉 Flask 的 python-dotenv 提示，免得使用者以為少裝了東西
    app.run(host="127.0.0.1", port=5077, debug=False, threaded=True, load_dotenv=False)
