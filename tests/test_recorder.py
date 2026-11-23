"""轉錄佇列：一次只轉一場、不重複排、一場失敗不卡住後面；以及從 run.log 判斷進度。

run.py 換成一支只睡一下的假腳本，真的用子行程跑，所以排隊與等待的行為和實際一樣。
"""
import json
import shutil
import time

import pytest

import recorder

JOB_SEC = 0.5
FAKE_RUN = f"""import sys, time
from pathlib import Path
with open(Path(__file__).parent.parent / "started.txt", "a", encoding="utf-8") as f:
    f.write(Path(sys.argv[1]).name + " " + str(time.time()) + "\\n")
time.sleep({JOB_SEC})
print("12:00:00 完成。")
"""


def wait_idle(timeout=20):
    """等佇列清空、沒有轉錄在跑。"""
    end = time.time() + timeout
    while time.time() < end:
        with recorder._qcond:
            busy = bool(recorder._queue) or any(recorder._running(k) for k in list(recorder._state["transcribing"]))
        if not busy:
            return
        time.sleep(0.05)
    raise AssertionError("佇列沒有在時間內跑完")


@pytest.fixture
def queue(data_root, monkeypatch):
    """回傳 (建立會議資料夾的函式, 讀取啟動紀錄的函式)。"""
    (data_root / "pipeline").mkdir()
    (data_root / "pipeline" / "run.py").write_text(FAKE_RUN, encoding="utf-8")
    monkeypatch.setattr(recorder, "ROOT", data_root)
    monkeypatch.setattr(recorder, "_other_run_py", lambda: False)
    recorder._state["transcribing"].clear()

    def meeting(name):
        d = data_root / "meetings" / name
        d.mkdir()
        (d / "source.m4a").write_bytes(b"x")
        return d

    def started():
        f = data_root / "started.txt"
        rows = [l.split() for l in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []
        return [(name, float(t)) for name, t in rows]

    yield meeting, started
    wait_idle()
    recorder._state["transcribing"].clear()


def states():
    return {k: v["state"] for k, v in recorder.status()["transcribing"].items()}


#------------------------------------------------------------------------
# 佇列
#------------------------------------------------------------------------

def test_one_meeting_at_a_time_in_order(queue):
    #====================================
    # 排三場
    #====================================
    meeting, started = queue
    a, b, c = meeting("2026-01-01_a"), meeting("2026-01-02_b"), meeting("2026-01-03_c")
    assert recorder.status()["pending"] == [a.name, b.name, c.name]
    for d in (a, b, c):
        recorder.transcribe(d)
    time.sleep(JOB_SEC / 2)

    #====================================
    # 第一場在跑，其他排隊，都不再列為待轉錄
    #====================================
    st = recorder.status()
    assert {k: (v["state"], v.get("queue_pos")) for k, v in st["transcribing"].items()} == {
        a.name: ("進行中", None), b.name: ("排隊中", 1), c.name: ("排隊中", 2)}
    assert st["pending"] == []

    #====================================
    # 跑完：照順序，而且每一場都等前一場結束才開始
    #====================================
    wait_idle()
    runs = started()
    assert [n for n, _ in runs] == [a.name, b.name, c.name]
    assert all(t2 - t1 >= JOB_SEC * 0.9 for (_, t1), (_, t2) in zip(runs, runs[1:]))
    assert set(states().values()) == {"完成"}


def test_asking_twice_does_not_run_twice(queue):
    meeting, started = queue
    a, b = meeting("2026-01-01_a"), meeting("2026-01-02_b")
    recorder.transcribe(a)
    recorder.transcribe(b)
    recorder.transcribe(b)                      # 還在排隊時又按一次
    time.sleep(JOB_SEC / 2)
    recorder.transcribe(a)                      # 正在跑時又按一次
    wait_idle()
    assert sorted(n for n, _ in started()) == [a.name, b.name]


def test_one_failure_does_not_block_the_queue(queue):
    #====================================
    # 第二場還沒輪到，資料夾就被刪掉
    #====================================
    meeting, started = queue
    a, b, c = meeting("2026-01-01_a"), meeting("2026-01-02_b"), meeting("2026-01-03_c")
    for d in (a, b, c):
        recorder.transcribe(d)
    shutil.rmtree(b)
    wait_idle()

    #====================================
    # 那一場顯示失敗的原因，後面的照常轉完
    #====================================
    assert [n for n, _ in started()] == [a.name, c.name]
    st = recorder.status()["transcribing"]
    assert st[b.name]["state"] == "失敗" and "沒有啟動" in st[b.name]["error"]
    assert st[c.name]["state"] == "完成"


def test_queued_meeting_shows_as_queued_even_with_an_old_failed_log(queue):
    meeting, _ = queue
    a, b = meeting("2026-01-01_a"), meeting("2026-01-02_b")
    (b / "run.log").write_text("Traceback 上次失敗\n", encoding="utf-8")
    recorder.transcribe(a)
    recorder.transcribe(b)
    time.sleep(JOB_SEC / 2)
    assert states()[b.name] == "排隊中"


#------------------------------------------------------------------------
# 校對工具重開後，進度只能從 run.log 判斷
#------------------------------------------------------------------------

def log_only_state(d, log):
    (d / "run.log").write_text(log, encoding="utf-8")
    return recorder._transcribe_state({"proc": None, "dir": d, "t0": time.time()})


def test_progress_is_read_from_the_log(queue):
    meeting, _ = queue
    d = meeting("2026-01-01_a")

    done = log_only_state(d, "16:46:16 轉檔 source.mp4 → audio/16k.m4a\n16:53:49 完成。python app.py 後在校對工具選「x」\n")
    assert done["state"] == "完成" and done["steps"][0] == "轉檔 source.mp4 → audio/16k.m4a"

    failed = log_only_state(d, "16:46:16 轉檔\nTraceback (most recent call last):\n  File x\nKeyError: 'y'\n")
    assert failed["state"] == "失敗" and "KeyError" in failed["error"]

    running = log_only_state(d, "16:46:16 轉檔\n16:46:20 part1.m4a → gemini-3.6-flash\n")
    assert running["state"] == "進行中" and running["steps"] == ["轉檔"]        # 呼叫模型的流水帳不列為步驟


def test_whisper_finish_time_is_estimated_from_meeting_length(queue):
    meeting, _ = queue
    d = meeting("2026-01-01_a")
    (d / "meeting.json").write_text(json.dumps({"source": {"duration_sec": 1200}}), encoding="utf-8")
    info = log_only_state(d, "16:46:45 whisper（vibe-server，內顯）開始\n")
    assert "whisper_eta_text" in info
    finished = log_only_state(d, "16:46:45 whisper（vibe-server，內顯）開始\n16:53:45 whisper 完成：420s\n")
    assert "whisper_eta_text" not in finished
