"""需要真的音效裝置的錄音測試：錄幾秒到暫存資料夾，不會轉錄。沒有麥克風的機器（例如 GitHub Actions）會跳過。"""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import recorder
import run

ROOT = Path(__file__).resolve().parent.parent


def has_microphone():
    if sys.platform != "win32":
        return False
    try:
        return bool(recorder.list_devices())
    except OSError:                                   # 沒有 ffmpeg
        return False


pytestmark = pytest.mark.skipif(not has_microphone(), reason="需要 Windows 與麥克風")


def test_microphone_recording_becomes_source_file(data_root, monkeypatch):
    #====================================
    # 準備：每 2 秒存一個小檔、錄完不轉錄
    #====================================
    monkeypatch.setattr(recorder, "SEGMENT_SEC", 2)
    monkeypatch.setattr(recorder, "transcribe", lambda d: None)
    recorder._state["finalizing"].clear()

    #====================================
    # 錄 3 秒
    #====================================
    st = recorder.start("測試錄音")
    folder = data_root / "meetings" / st["recording"]["folder"]
    time.sleep(3)
    assert recorder.status()["recording"]["alive"]
    recorder.stop()
    for _ in range(80):
        if recorder._state["finalizing"].get(folder.name, "").startswith(("完成", "失敗")):
            break
        time.sleep(0.25)

    #====================================
    # 小檔合併成 source.m4a，並記下結束時間
    #====================================
    assert recorder._state["finalizing"][folder.name].startswith("完成")
    assert run.ffprobe_duration(folder / "source.m4a") > 1
    assert not (folder / "rec").exists()
    meta = json.loads((folder / "meeting.json").read_text(encoding="utf-8"))
    assert meta["status"] == "未處理" and meta["recording"]["ended"] and meta["recording"]["loopback"] is False


# 照校對工具實際的情況開錄：音效套件先在一條很快就結束的執行緒裡載入（像網頁請求），再從另一條執行緒開始錄。
# 要用全新的行程才重現得出來：測試行程的主執行緒一旦載入過音效套件，其他執行緒就借得到它的初始化。
LOOPBACK_SCENARIO = """
import json, sys, threading, time
from pathlib import Path
root, tmp = Path(sys.argv[1]), Path(sys.argv[2])
sys.path[:0] = [str(root), str(root / "pipeline")]
import recorder
recorder.MEETINGS, recorder.SEGMENT_SEC, recorder.transcribe = tmp, 2, lambda d: None

def in_thread(f):
    t = threading.Thread(target=f); t.start(); t.join()

out = {}
def start():
    try:
        recorder.start("loopback", loopback=True)
    except Exception as e:
        out["start_error"] = str(e)

in_thread(recorder.loopback_available)
in_thread(start)
time.sleep(2.5)
st = recorder.status()["recording"] or {}
out.update(alive=st.get("alive"), error=st.get("error"), loopback_db=st.get("loopback_db"))
if "start_error" not in out:
    in_thread(recorder.stop)
for _ in range(80):
    if recorder._state["finalizing"] and all(v.startswith(("完成", "失敗")) for v in recorder._state["finalizing"].values()):
        break
    time.sleep(0.25)
out["finalizing"] = list(recorder._state["finalizing"].values())
print(json.dumps(out, ensure_ascii=False))
"""


def test_recording_computer_audio_works_from_request_threads(tmp_path):
    pytest.importorskip("soundcard")
    r = subprocess.run([sys.executable, "-X", "utf8", "-c", LOOPBACK_SCENARIO, str(ROOT), str(tmp_path)],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert "start_error" not in out, out
    assert out["alive"] is True and out["error"] is None, out
    assert out["loopback_db"] is not None                      # 錄音頁靠這個顯示電腦聲音有沒有收到
    assert out["finalizing"] and out["finalizing"][0].startswith("完成"), out
    assert run.ffprobe_duration(next(tmp_path.iterdir()) / "source.m4a") > 1
