"""會議錄音：把麥克風與電腦播出的聲音錄進 meetings/<日期>_<標題>/，校對工具的「錄音」頁面使用。

- 兩軌分開存：source.m4a 的左聲道是麥克風、右聲道是電腦播出的聲音（線上會議的對方、播放的影片）。
  筆電麥克風陣列的回音消除會把喇叭播出的聲音消掉，只靠麥克風錄不到線上會議的對方；每一場都錄，
  要不要用右聲道留到轉錄時再決定（run.py），錄音當下不用選、也就不會忘。
- 聲音由 Python（soundcard 套件，Windows 的 WASAPI）抓，交給 ffmpeg 編碼；錄音時每 SEGMENT_SEC 秒存成一個
  ADTS（.aac）小檔：ADTS 沒有檔頭，電腦當機或程式被關掉，已寫入的部分都還能用；結束時再無損合併成 source.m4a。
- 「結束並開始下一場」：先開始下一場，再在背景合併上一場，兩場之間幾乎沒有空檔。
- 錄音期間呼叫 SetThreadExecutionState 防止 Windows 睡眠。
"""
import contextlib
import json
import math
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
MEETINGS = ROOT / "meetings"
SEGMENT_SEC = 300
SAMPLE_RATE = 48000
CHANNELS = ["mic", "computer"]                           # source.m4a 的左、右聲道；run.py 靠 meeting.json 裡的這一項認得這種檔
# 兩軌各約 96 kbps。實測一軌有聲音、另一軌靜音時，解碼後靜音的那一軌仍是完全靜音，不會互相滲漏
ENCODE = ["-c:a", "aac", "-b:a", "192k"]
# 向 Windows 要 1 秒的緩衝：程式偶爾慢一下（電腦忙）時聲音留在緩衝裡不會掉。實測每秒卡 0.3 秒，緩衝 10 毫秒只收到八成，1 秒的全收到
BUFFER = SAMPLE_RATE
MIX_BLOCK = SAMPLE_RATE // 10                            # 每 0.1 秒寫一次，以麥克風為時鐘
CUSHION = SAMPLE_RATE // 20                              # 電腦聲音先存 0.05 秒再開始取：資料晚到一點點才不會斷成一截一截
LOOPBACK_MAX_LAG = SAMPLE_RATE * 2                       # 電腦聲音積超過 2 秒就丟掉舊的：兩個裝置的時鐘不同步，不丟會越差越遠
SPEAKER_SCAN_SEC, SPEAKER_RETRY_SEC = 5, 30              # 多久重新找一次喇叭；打不開的喇叭隔多久再試
SOUND_DB = -60                                           # 電腦那一軌超過這個音量才算「有聲音」
_lock = threading.Lock()
_state = {"rec": None, "finalizing": {}, "transcribing": {}}
_qcond = threading.Condition()                           # 保護 _queue 與 _state["transcribing"]
_queue = []                                              # [(會議資料夾, 排入時間)]，由 _transcribe_loop 依序轉
_wait_other = [False]                                    # 佇列正在等別處開的 run.py 跑完


def available():
    """這台電腦能不能錄音：靠 soundcard 套件抓麥克風與喇叭播出的聲音，只支援 Windows。"""
    if sys.platform != "win32":
        return False
    try:
        import soundcard  # noqa: F401
        return True
    except Exception:                                    # 沒裝、或音效裝置初始化失敗
        return False


@contextlib.contextmanager
def _com():
    """soundcard 底層是 Windows 的 COM：每一條用到它的執行緒都要先初始化。soundcard 只在第一次 import 時替那條執行緒做，
    app.py 裡那條常常是早已結束的網頁請求執行緒，之後開錄音的新執行緒一碰裝置就出 0x800401f0（CO_E_NOTINITIALIZED）。"""
    if sys.platform != "win32":
        yield
        return
    import ctypes
    hr = ctypes.windll.ole32.CoInitializeEx(None, 0)   # 0 = COINIT_MULTITHREADED
    try:
        yield
    finally:
        if hr in (0, 1):                                 # S_OK、S_FALSE 都要配對釋放；換模式失敗的不釋放
            ctypes.windll.ole32.CoUninitialize()


def list_devices():
    """麥克風的名稱，Windows 的預設裝置排第一個。"""
    if not available():
        return []

    #====================================
    # 問 Windows 有哪些麥克風
    #====================================
    try:
        with _com():
            import soundcard as sc
            names = [m.name for m in sc.all_microphones()]
            first = sc.default_microphone().name if names else None
    except Exception:                                    # 一個錄音裝置都沒有時 soundcard 會丟錯
        return []

    #====================================
    # 預設的排最前面
    #====================================
    return sorted(dict.fromkeys(names), key=lambda n: n != first)


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


def _without_today(title, today=None):
    """標題開頭又打了今天的日期就拿掉：資料夾名稱與會議清單本來就有日期，再打一次只是變成 2026-10-21_2026-10-21-週會。
    只拿掉今天的日期：打別的日期（例如補錄前一天的會）是有意義的，留著。"""
    #====================================
    # 開頭像不像日期
    #====================================
    today = today or datetime.now()
    t = title.strip()
    m = re.match(r"(\d{4})\s*[-/._年]?\s*(\d{1,2})\s*[-/._月]?\s*(\d{1,2})(?!\d)\s*日?[\s\-_/.:：、，,]*", t)

    #====================================
    # 是今天才拿掉
    #====================================
    if m and tuple(map(int, m.groups())) == (today.year, today.month, today.day):
        return t[m.end():]
    return t


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
    #====================================
    # 檢查：沒有在錄、這台能錄、麥克風打得開
    #====================================
    if _state["rec"] and not replacing:
        raise RuntimeError("已經在錄音中")
    if not available():
        raise RuntimeError("這台電腦不能錄音：需要 Windows 與 soundcard 套件（uv pip install --python .venv -r requirements.txt）")
    if not replacing:                            # 接下一場時裝置剛剛還在用，不重試（試開要零點幾秒，會拉長兩場之間的空檔）
        device = _probe(device)

    #====================================
    # 建資料夾與 meeting.json
    #====================================
    title = _without_today(title)
    d = _new_folder(title)
    meta = {"title": title or "會議", "date": f"{datetime.now():%Y-%m-%d}",
            "source": {"file": "source.m4a", "note": f"recorder.py 錄音：左聲道是麥克風（{device}），右聲道是電腦播出的聲音"},
            "recording": {"started": datetime.now().isoformat(timespec="seconds"), "device": device, "channels": CHANNELS},
            "participants": [], "transcripts": {}, "status": "錄音中",
            "auto_transcribe": bool(auto_transcribe)}
    (d / "meeting.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    #====================================
    # ffmpeg 負責編碼與分段存檔，聲音從 stdin 進來
    #====================================
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "2", "-i", "pipe:0",
           *ENCODE, "-f", "segment", "-segment_time", str(SEGMENT_SEC), "-segment_format", "adts",
           "-reset_timestamps", "1", "part%04d.aac"]
    log = open(d / "rec" / "ffmpeg.log", "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=d / "rec", stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT)

    #====================================
    # 開始抓聲音
    #====================================
    stop_evt = threading.Event()
    meter = {"mic_db": None, "loopback_db": None, "loopback_sec": 0.0, "speakers": 0, "notes": ""}
    th = threading.Thread(target=_capture, args=(proc, device, stop_evt, d / "rec" / "capture.log", meter), daemon=True)
    th.start()
    _state["rec"] = {"proc": proc, "dir": d, "title": meta["title"], "device": device, "t0": time.time(),
                     "auto": bool(auto_transcribe), "log": log, "stop": stop_evt, "capture": th, "meter": meter}
    if not any(t.name == "keep-awake" and t.is_alive() for t in threading.enumerate()):
        threading.Thread(target=_keep_awake_loop, name="keep-awake", daemon=True).start()


def _open_mic(device):
    import soundcard as sc
    return next((m for m in sc.all_microphones() if m.name == device), None) or sc.default_microphone()


def _list_speakers():
    """[(喇叭 id, 名稱, 能錄到它播出聲音的裝置)]。錄「所有」喇叭：會議軟體可以自己指定喇叭，不一定是 Windows 的預設
    （實測 Teams 從筆電喇叭出聲時，預設卻是外接螢幕，只錄預設的會整段沒有對方的聲音）。"""
    import soundcard as sc
    return [(str(spk.id), spk.name, sc.get_microphone(id=str(spk.id), include_loopback=True)) for spk in sc.all_speakers()]


def _probe(device):
    """開錄前先實際打開麥克風讀一小段，打不開就在建資料夾之前說清楚。回傳實際要用的麥克風名稱。
    喇叭不在這裡試：錄不到電腦的聲音不該擋住現場會議的錄音，錄音頁會另外顯示。"""
    with _com():
        try:
            mic = _open_mic(device)
            with mic.recorder(samplerate=SAMPLE_RATE, channels=1, blocksize=BUFFER) as r:
                r.record(numframes=None)
            return mic.name                              # 問名稱也會用到 COM，要在 _com() 的範圍裡問
        except Exception as e:
            raise RuntimeError(f"打不開麥克風：{type(e).__name__}: {e}") from e


class _SpeakerMix:
    """所有喇叭播出的聲音，加總成一軌。每個喇叭一條讀取執行緒把聲音放進自己的緩衝，錄音的主迴圈用 take() 取走。"""

    def __init__(self, stop, log_path):
        self.stop, self.log_path = stop, log_path
        self.lock = threading.Lock()
        self.streams = {}                                # 喇叭 id → {"new": 剛讀到的, "pending": 還沒取走的, "primed": 已經開始取}
        self.failed = {}                                 # 喇叭 id → {"at": 上次打不開的時間, "why": 給使用者看的原因}；恢復了就拿掉
        self.scan_error = ""                             # 找喇叭這件事本身出的錯
        self.logged = set()

    def log(self, msg):
        if msg in self.logged:                           # 同一個喇叭反覆打不開，只記一次
            return
        self.logged.add(msg)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")

    def problems(self):
        """現在還錄不到的喇叭（給錄音頁看）。只講現在的狀態：一開始打不開、後來恢復的不再提。"""
        with self.lock:
            return "；".join(filter(None, [self.scan_error, *(f["why"] for f in self.failed.values())]))

    def watch(self):
        """（自己一條執行緒）每隔幾秒找一次喇叭，還沒在錄的就開始錄。錄音中途才出現的喇叭也要錄：
        接上螢幕或耳機之後，會議軟體可能改從新的裝置出聲。"""
        import numpy as np
        with _com():
            while True:
                try:
                    found, self.scan_error = _list_speakers(), ""
                except Exception as e:
                    found, self.scan_error = [], f"找不到喇叭：{type(e).__name__}: {e}"
                    self.log(self.scan_error)
                for sid, name, lb in found:
                    with self.lock:
                        if sid in self.streams or time.time() - self.failed.get(sid, {"at": 0})["at"] < SPEAKER_RETRY_SEC:
                            continue
                        self.streams[sid] = {"new": deque(), "pending": np.zeros(0, dtype=np.float32), "primed": False}
                    threading.Thread(target=self.read, args=(sid, name, lb), daemon=True).start()
                if self.stop.wait(SPEAKER_SCAN_SEC):
                    return

    def read(self, sid, name, lb):
        """（每個喇叭一條執行緒）一直讀這個喇叭播出的聲音。讀不到（被別的程式獨占、裝置被拔掉）就記一筆並結束，其他喇叭照錄。"""
        new = self.streams[sid]["new"]
        try:
            with _com(), lb.recorder(samplerate=SAMPLE_RATE, blocksize=BUFFER) as r:
                with self.lock:
                    self.failed.pop(sid, None)           # 打得開了：之前錄不到的提示可以收掉
                while not self.stop.is_set():
                    x = r.record(numframes=None)         # 一次只給一小包，要一直讀才讀得完
                    if len(x):
                        with self.lock:
                            new.append(x.mean(axis=1) if x.ndim > 1 else x)
                    else:
                        time.sleep(0.005)
        except Exception as e:
            why = f"錄不到喇叭「{name}」播出的聲音：{type(e).__name__}: {e}"
            self.log(why)
            with self.lock:
                self.streams.pop(sid, None)
                self.failed[sid] = {"at": time.time(), "why": why}

    def take(self, n):
        """取出接下來 n 個樣本（所有喇叭加總）。沒有聲音在播的時候 Windows 不送資料，缺的就是靜音。"""
        import numpy as np
        out = np.zeros(n, dtype=np.float32)
        with self.lock:
            for st in self.streams.values():
                arrived = bool(st["new"])
                if arrived:
                    st["pending"] = np.concatenate([st["pending"], *st["new"]]).astype(np.float32, copy=False)
                    st["new"].clear()
                p = st["pending"]
                if len(p) > LOOPBACK_MAX_LAG:
                    p = p[-(n + CUSHION):]
                # 剛開始播：先存一小段再開始取。資料還在陸續進來卻不夠一次取的量，就等下一輪；已經沒有新的（很短的提示音）就直接給
                if not st["primed"] and arrived and len(p) < n + CUSHION:
                    st["pending"] = p
                    continue
                k = min(n, len(p))
                out[:k] += p[:k]
                st["pending"], st["primed"] = p[k:], k == n
        return out


def _db(x):
    import numpy as np
    #====================================
    # 這一段的平均音量
    #====================================
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if len(x) else 0.0

    #====================================
    # 換成 dB；完全沒聲音時取不了對數，給一個很小的值
    #====================================
    return round(20 * math.log10(rms), 1) if rms > 1e-6 else -120.0


def _capture(proc, device, stop, log_path, meter):
    """麥克風放左聲道、電腦播出的聲音放右聲道，寫進 ffmpeg 的 stdin；結束或出錯都會關掉 stdin 讓 ffmpeg 收尾。
    兩軌以麥克風為時鐘：麥克風每來 0.1 秒，就配上這段時間裡電腦播出的聲音。meter 是給錄音頁看的音量。"""
    try:
        import warnings
        import numpy as np
        warnings.filterwarnings("ignore", message="data discontinuity")
        #====================================
        # 開裝置
        #====================================
        com = contextlib.ExitStack()
        com.enter_context(_com())                        # 這條執行緒結束（finally）時才釋放
        mic = _open_mic(device)
        speakers = _SpeakerMix(stop, log_path)
        threading.Thread(target=speakers.watch, daemon=True).start()

        #====================================
        # 一次寫 0.1 秒的兩軌
        #====================================
        with mic.recorder(samplerate=SAMPLE_RATE, channels=1, blocksize=BUFFER) as mr:
            while not stop.is_set():
                m = mr.record(numframes=MIX_BLOCK)[:, 0].astype(np.float32)
                pc = np.clip(speakers.take(len(m)), -1, 1)
                meter["mic_db"], meter["loopback_db"], meter["speakers"] = _db(m), _db(pc), len(speakers.streams)
                meter["notes"] = speakers.problems()
                if meter["loopback_db"] > SOUND_DB:
                    meter["loopback_sec"] += len(m) / SAMPLE_RATE
                proc.stdin.write(np.column_stack([m, pc]).astype("<f4").tobytes())
    except Exception as e:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"錄音失敗：{type(e).__name__}: {e}\n")
    finally:
        stop.set()                                       # 主迴圈出錯時，喇叭的讀取執行緒也要跟著停
        try:
            proc.stdin.close()
        except OSError:
            pass
        if "com" in locals():
            com.close()


def status():
    r = _state["rec"]
    rec = None
    if r:
        alive, m = r["proc"].poll() is None, r["meter"]
        rec = {"title": r["title"], "folder": r["dir"].name, "device": r["device"], "alive": alive,
               "elapsed": round(time.time() - r["t0"]), "level_db": m["mic_db"],
               "loopback_db": m["loopback_db"], "loopback_sec": round(m["loopback_sec"]), "speakers": m["speakers"],
               "segments": len(list((r["dir"] / "rec").glob("part*.aac")))}
        # 還在錄：現在錄不到哪個喇叭（不影響錄音）；停了：紀錄檔的結尾是停下來的原因
        if alive:
            rec["notes"] = m["notes"]
        else:
            rec["error"] = "".join(f.read_text(encoding="utf-8", errors="replace") for f in (r["dir"] / "rec" / n for n in ("capture.log", "ffmpeg.log"))
                                   if f.exists())[-300:]
    transcribing = {k: _transcribe_state(v) for k, v in _all_jobs().items()}
    return {"recording": rec, "finalizing": dict(_state["finalizing"]), "transcribing": transcribing,
            "unfinished": [d.parent.name for d in unfinished()], "pending": pending(transcribing),
            "screen_pending": screen_pending(transcribing),
            "can_record": available()}



MEDIA_EXT = {".mp4", ".m4a", ".mp3", ".wav", ".webm", ".mkv", ".mov", ".aac", ".flac", ".ogg", ".wma"}


def _busy(transcribing):
    """正在轉錄、排隊、錄音或合併的會議資料夾：這些先不列進「等著處理」的清單。"""
    #====================================
    # 轉錄中或排隊中
    #====================================
    busy = {k for k, v in transcribing.items() if v["state"] in ("進行中", "排隊中")}

    #====================================
    # 錄音中、合併中
    #====================================
    if _state["rec"]:
        busy.add(_state["rec"]["dir"].name)
    return busy | {k for k, v in _state["finalizing"].items() if not v.startswith(("完成", "失敗"))}


VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm"}


def screen_pending(transcribing):
    """已經有逐字稿、之後才放進影片的會議（會議軟體的錄影通常晚一點才下載得到）：只差畫面還沒處理。
    只看副檔名，不去問 ffprobe：錄音頁每秒都會來問一次。"""
    if not MEETINGS.is_dir():
        return []
    busy, out = _busy(transcribing), []
    for d in sorted(MEETINGS.iterdir()):
        if not d.is_dir() or d.name in busy or not (d / "project.json").exists() or (d / "transcripts/screen_timeline.json").exists():
            continue
        # source.* 本身是影片的不算：那種的畫面在第一次轉錄時就處理了（或使用者指定不處理）
        if any(f.is_file() and f.suffix.lower() in VIDEO_EXT and f.stem != "source" for f in d.iterdir()):
            out.append(d.name)
    return out


def pending(transcribing):
    """有錄音或影片、但還沒有校對專案、也沒在轉錄或錄音中的會議資料夾（例如直接丟進 Teams 下載檔的）。"""
    if not MEETINGS.is_dir():
        return []
    busy = _busy(transcribing)
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
        r["stop"].set()                                  # 停止抓聲音、關掉 stdin，ffmpeg 讀到結尾就收尾
        r["capture"].join(timeout=5)
        r["proc"].wait(timeout=10)
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
