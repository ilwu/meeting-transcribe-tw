"""實體會議錄音：用 ffmpeg 從麥克風錄進 meetings/<日期>_<標題>/，校對工具的「錄音」頁面使用。

- 錄音時每 SEGMENT_SEC 秒存成一個 ADTS（.aac）小檔：ADTS 沒有檔頭，電腦當機或程式被關掉，
  已寫入的部分都還能用；結束時再無損合併成 source.m4a。
- 「結束並開始下一場」：先啟動下一場的 ffmpeg，再在背景合併上一場，兩場之間幾乎沒有空檔。
- 錄音期間呼叫 SetThreadExecutionState 防止 Windows 睡眠。
"""
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
MEETINGS = ROOT / "meetings"
SEGMENT_SEC = 300
SAMPLE_RATE, BITRATE = 48000, "96k"
_lock = threading.Lock()
_state = {"rec": None, "finalizing": {}, "transcribing": {}}
_qcond = threading.Condition()                           # 保護 _queue 與 _state["transcribing"]
_queue = []                                              # [(會議資料夾, 排入時間)]，由 _transcribe_loop 依序轉
_wait_other = [False]                                    # 佇列正在等別處開的 run.py 跑完


def list_devices():
    """DirectShow 的音訊輸入裝置名稱。"""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return re.findall(r'"([^"]+)" \(audio\)', r.stderr)


def _keep_awake_loop():
    """錄音期間每 30 秒重設系統閒置計時器，防止睡眠。SetThreadExecutionState 只對呼叫的執行緒有效，
    所以用一條專用執行緒；沒有在錄音時就結束，系統恢復原本的睡眠設定。"""
    if sys.platform != "win32":
        return
    import ctypes
    ES_SYSTEM_REQUIRED = 0x00000001
    while _state["rec"]:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_SYSTEM_REQUIRED)
        time.sleep(30)


def _safe_title(title):
    t = re.sub(r'[\\/:*?"<>|\s]+', "_", title.strip()).strip("_.")
    return t or "會議"


def _new_folder(title):
    base = f"{datetime.now():%Y-%m-%d}_{_safe_title(title)}"
    d, n = MEETINGS / base, 2
    while d.exists():
        d, n = MEETINGS / f"{base}-{n}", n + 1
    (d / "rec").mkdir(parents=True)
    return d


def start(title, device=None, auto_transcribe=False):
    with _lock:
        _start_locked(title, device, auto_transcribe)
    return status()


def _start_locked(title, device, auto_transcribe, replacing=False):
    """呼叫端必須持有 _lock。replacing=True 時由 stop() 用來無縫接下一場（目前那場由呼叫端收尾）。"""
    if _state["rec"] and not replacing:
        raise RuntimeError("已經在錄音中")
    if not device:                               # 已知裝置就不重新列舉（列舉要 0.5 秒，會拉長切換空檔）
        devices = list_devices()
        device = devices[0] if devices else None
    if not device:
        raise RuntimeError("找不到麥克風")
    d = _new_folder(title)
    meta = {"title": title.strip() or "會議", "date": f"{datetime.now():%Y-%m-%d}",
            "source": {"file": "source.m4a", "note": f"recorder.py 錄音，裝置：{device}"},
            "recording": {"started": datetime.now().isoformat(timespec="seconds"), "device": device},
            "participants": [], "transcripts": {}, "status": "錄音中",
            "auto_transcribe": bool(auto_transcribe)}
    (d / "meeting.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    # 音量：每 0.25 秒算一次 RMS，寫到 level.log（只讀最後一行）
    af = "asetnsamples=n=12000,astats=metadata=1:reset=1,ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=level.log:direct=1"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "dshow", "-i", f"audio={device}",
           "-af", af, "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "aac", "-b:a", BITRATE,
           "-f", "segment", "-segment_time", str(SEGMENT_SEC), "-segment_format", "adts",
           "-reset_timestamps", "1", "part%04d.aac"]
    log = open(d / "rec" / "ffmpeg.log", "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=d / "rec", stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT)
    _state["rec"] = {"proc": proc, "dir": d, "title": meta["title"], "device": device, "t0": time.time(),
                     "auto": bool(auto_transcribe), "log": log}
    if not any(t.name == "keep-awake" and t.is_alive() for t in threading.enumerate()):
        threading.Thread(target=_keep_awake_loop, name="keep-awake", daemon=True).start()


def _level_db(d):
    try:
        with open(d / "rec" / "level.log", "rb") as f:
            f.seek(0, 2); f.seek(max(0, f.tell() - 400))
            vals = re.findall(rb"RMS_level=(-?[\d.]+|-inf)", f.read())
        return None if not vals else (-120.0 if vals[-1] == b"-inf" else round(float(vals[-1]), 1))
    except OSError:
        return None


def status():
    r = _state["rec"]
    rec = None
    if r:
        alive = r["proc"].poll() is None
        rec = {"title": r["title"], "folder": r["dir"].name, "device": r["device"], "alive": alive,
               "elapsed": round(time.time() - r["t0"]), "level_db": _level_db(r["dir"]),
               "segments": len(list((r["dir"] / "rec").glob("part*.aac")))}
        if not alive:
            rec["error"] = (r["dir"] / "rec" / "ffmpeg.log").read_text(encoding="utf-8", errors="replace")[-300:]
    transcribing = {k: _transcribe_state(v) for k, v in _all_jobs().items()}
    return {"recording": rec, "finalizing": dict(_state["finalizing"]), "transcribing": transcribing,
            "unfinished": [d.parent.name for d in unfinished()], "pending": pending(transcribing)}


MEDIA_EXT = {".mp4", ".m4a", ".mp3", ".wav", ".webm", ".mkv", ".mov", ".aac", ".flac", ".ogg", ".wma"}


def pending(transcribing):
    """有錄音或影片、但還沒有校對專案、也沒在轉錄或錄音中的會議資料夾（例如直接丟進 Teams 下載檔的）。"""
    if not MEETINGS.is_dir():
        return []
    busy = {k for k, v in transcribing.items() if v["state"] in ("進行中", "排隊中")}
    if _state["rec"]:
        busy.add(_state["rec"]["dir"].name)
    busy |= {k for k, v in _state["finalizing"].items() if not v.startswith(("完成", "失敗"))}
    out = []
    for d in sorted(MEETINGS.iterdir()):
        if not d.is_dir() or d.name in busy or (d / "project.json").exists() or (d / "rec").exists():
            continue
        if any(f.is_file() and f.suffix.lower() in MEDIA_EXT for f in d.iterdir()):
            out.append(d.name)
    return out


RTF_WHISPER = 0.37                                      # 本機內顯跑 whisper 的速度（處理時間／音訊長度）


def _all_jobs():
    """這次啟動後開始的轉錄，加上 12 小時內有 run.log 的會議（app.py 重開過也看得到進度）。"""
    with _qcond:                                         # 一起取，才不會看到剛離開佇列、還沒標成進行中的那一場
        jobs = dict(_state["transcribing"])
        for i, (d, t0) in enumerate(_queue):            # 排隊中的可能留著上次失敗的 run.log，以排隊狀態為準
            jobs[d.name] = {"proc": None, "dir": d, "t0": t0, "queue_pos": i + 1, "wait_other": i == 0 and _wait_other[0]}
    for log in MEETINGS.glob("*/run.log"):
        d = log.parent
        if d.name not in jobs and time.time() - log.stat().st_mtime < 12 * 3600:
            jobs[d.name] = {"proc": None, "dir": d, "t0": log.stat().st_ctime}
    return jobs


def _transcribe_state(job):
    """轉錄進度：讀會議資料夾的 run.log（run.py 每一步都會寫一行），整理成步驟與預估時間。"""
    p, d = job["proc"], job["dir"]
    if "queue_pos" in job:
        return {"state": "排隊中", "queue_pos": job["queue_pos"], "wait_other": job["wait_other"],
                "elapsed": round(time.time() - job["t0"]), "steps": []}
    if "error" in job:
        return {"state": "失敗", "elapsed": 0, "steps": [], "error": f"run.py 沒有啟動：{job['error']}"}
    if job.get("starting"):
        return {"state": "進行中", "elapsed": round(time.time() - job["t0"]), "steps": []}
    try:
        lines = [l.strip() for l in (d / "run.log").read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    except OSError:
        lines = []
    if p is not None:
        state = "進行中" if p.poll() is None else ("完成" if p.returncode == 0 else f"失敗（代碼 {p.returncode}）")
    elif any("完成。" in l for l in lines):          # app.py 重開過，只能從 run.log 判斷
        state = "完成"
    elif any("Traceback" in l or "轉錄失敗" in l for l in lines):
        state = "失敗"
    else:
        state = "進行中"
    steps = [l for l in lines if re.match(r"\d\d:\d\d:\d\d ", l) and " → gemini" not in l]   # 略過每次呼叫模型的流水帳
    info = {"state": state, "elapsed": round(time.time() - job["t0"]), "steps": [s[9:] for s in steps[-6:]]}
    w_start = next((l for l in lines if "whisper（vibe-server" in l and "開始" in l), None)
    w_done = any("whisper 完成" in l or "whisper 失敗" in l for l in lines)
    if w_start and not w_done and state == "進行中":
        try:
            dur = json.loads((d / "meeting.json").read_text(encoding="utf-8"))["source"]["duration_sec"]
            hh, mm, ss = map(int, w_start[:8].split(":"))
            start = datetime.now().replace(hour=hh, minute=mm, second=ss)
            info["whisper_eta"] = (start.timestamp() + dur * RTF_WHISPER)
            info["whisper_eta_text"] = datetime.fromtimestamp(info["whisper_eta"]).strftime("%H:%M")
        except (OSError, KeyError, ValueError):
            pass
    if state.startswith("失敗"):
        info["error"] = "\n".join(lines[-5:])
    return info


def stop(next_title=None):
    """結束目前的錄音；給了 next_title 就立刻開始下一場（裝置與自動轉錄設定沿用）。"""
    with _lock:                                          # 在同一個鎖裡交接，狀態查詢不會看到中間的空檔
        r = _state["rec"]
        if not r:
            raise RuntimeError("目前沒有在錄音")
        _state["finalizing"][r["dir"].name] = "收尾中"
        next_error = None
        if next_title is not None:
            try:                                         # 先開下一場再換掉這一場，狀態不會出現「沒在錄」的空檔
                _start_locked(next_title, r["device"], r["auto"], replacing=True)
            except Exception as e:                       # 下一場開不起來也要先把這一場收好
                next_error = e
        if _state["rec"] is r:
            _state["rec"] = None
    try:
        r["proc"].communicate(b"q", timeout=10)          # 讓 ffmpeg 正常收尾
    except subprocess.TimeoutExpired:
        r["proc"].kill()
    r["log"].close()
    threading.Thread(target=finalize, args=(r["dir"], r["auto"]), daemon=True).start()
    if next_error:
        raise RuntimeError(f"這一場已保存，但下一場沒有開始：{next_error}")
    return status()


def unfinished():
    """有錄音小檔但還沒合併成 source.m4a 的資料夾（上次沒正常結束），不含正在錄的那場。"""
    cur = _state["rec"]["dir"] if _state["rec"] else None

    def still_writing(d):                    # 例如錄音中重開了 app.py，舊的 ffmpeg 可能還在寫
        return any(time.time() - p.stat().st_mtime < 30 for p in d.glob("part*.aac"))
    return [d for d in sorted(MEETINGS.glob("*/rec")) if d.parent != cur and any(d.glob("part*.aac"))
            and not (d.parent / "source.m4a").exists() and d.parent.name not in _state["finalizing"]
            and not still_writing(d)]


def finalize(d, auto_transcribe=False):
    """把 rec/part*.aac 無損合併成 source.m4a，確認長度後刪掉小檔；需要時接著自動轉錄。"""
    d = Path(d)
    _state["finalizing"][d.name] = "合併中"
    try:
        parts = sorted((d / "rec").glob("part*.aac"))
        parts = [p for p in parts if p.stat().st_size > 0]
        if not parts:
            raise RuntimeError("沒有錄到任何聲音")
        lst = d / "rec" / "list.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt",
                        "-c", "copy", "-bsf:a", "aac_adtstoasc", str(d / "source.m4a")], cwd=d / "rec", check=True)
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    str(d / "source.m4a")], capture_output=True, text=True, check=True).stdout)
        meta_p = d / "meeting.json"
        meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
        meta.setdefault("source", {})["duration_sec"] = round(dur)
        meta.setdefault("recording", {})["ended"] = datetime.now().isoformat(timespec="seconds")
        meta["status"] = "未處理"
        meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        for p in (d / "rec").iterdir():                  # 合併成功才刪小檔
            p.unlink()
        (d / "rec").rmdir()
        _state["finalizing"][d.name] = f"完成（{dur / 60:.1f} 分鐘）"
        if auto_transcribe or meta.get("auto_transcribe"):
            transcribe(d)
    except Exception as e:
        _state["finalizing"][d.name] = f"失敗：{e}"


def _running(name):
    """呼叫端必須持有 _qcond。"""
    job = _state["transcribing"].get(name)
    return bool(job) and (job.get("starting") or (job["proc"] is not None and job["proc"].poll() is None))


def transcribe(d):
    """排進轉錄佇列。一次只轉一場：同時跑會搶同一顆內顯跑 whisper，兩場都變慢，先轉完一場才能先校對。"""
    d = Path(d)
    with _qcond:
        if any(x.name == d.name for x, _ in _queue) or _running(d.name):
            return
        _queue.append((d, time.time()))
        _qcond.notify()


def _other_run_py():
    """系統裡有沒有不是這個佇列開的 run.py：app.py 重開前留下的子行程、命令列啟動的。查不到就當沒有，不卡住佇列。"""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | ForEach-Object CommandLine"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(re.search(r"pipeline[\\/]run\.py", out))


def _transcribe_loop():
    while True:
        try:
            #====================================
            # 等佇列有會議、別的 run.py 跑完
            #====================================
            with _qcond:
                while not _queue:
                    _qcond.wait()
            # 只在兩場之間檢查，這時這個佇列自己沒有在跑，查到的 run.py 一定是別處開的
            while _other_run_py():
                _wait_other[0] = True
                time.sleep(30)
            _wait_other[0] = False

            #====================================
            # 取出下一場
            #====================================
            with _qcond:                                 # 取出與標成進行中在同一個鎖內，中間不會被當成待轉錄再排一次
                d, _ = _queue.pop(0)
                _state["transcribing"][d.name] = {"proc": None, "dir": d, "t0": time.time(), "starting": True}

            #====================================
            # 執行 run.py 到結束
            #====================================
            try:
                with open(d / "run.log", "w", encoding="utf-8") as log:
                    proc = subprocess.Popen([sys.executable, str(ROOT / "pipeline" / "run.py"), str(d)],
                                            stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
                    _state["transcribing"][d.name] = {"proc": proc, "dir": d, "t0": time.time()}
                    proc.wait()
            except Exception as e:                       # 不寫進 run.log：資料夾可能已被改名或刪掉
                _state["transcribing"][d.name] = {"proc": None, "dir": d, "t0": time.time(), "error": str(e)}
        except Exception as e:                           # 這條執行緒死了佇列就停，任何錯都不能讓它結束
            print(f"轉錄佇列出錯：{e}", file=sys.stderr)
            time.sleep(5)


threading.Thread(target=_transcribe_loop, daemon=True).start()
