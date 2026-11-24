"""錄音（recorder.py）：麥克風與電腦播出的聲音分存左右聲道。

音效裝置換成假的（麥克風是 440 Hz、喇叭播的是 880 Hz），ffmpeg 是真的，所以從抓聲音、分段存檔到合併成
source.m4a 都實際跑過；沒有麥克風的機器（GitHub Actions）也能測。真的裝置在 test_recorder_audio.py。
"""
import json
import threading
import time
from collections import deque

import numpy as np
import pytest

import recorder
import run
from helpers import needs_ffmpeg, tone_db

RATE = recorder.SAMPLE_RATE
HEARD, ABSENT = -30, -60                                 # 聽得到的純音在 -20 dB 上下，沒有的在 -90 dB 以下


class FakeDevice:
    """假的音效裝置：照真實的時間吐出固定頻率的聲音。broken 有值就打不開。"""

    def __init__(self, name, freq, channels=1, broken=None):
        self.name, self.freq, self.channels, self.broken = name, freq, channels, broken

    def recorder(self, samplerate, channels=None, blocksize=None):
        if self.broken:
            raise RuntimeError(self.broken)
        return FakeStream(self)


class FakeStream:
    def __init__(self, dev):
        self.dev = dev

    def __enter__(self):
        self.t0, self.sent = time.time(), 0
        return self

    def __exit__(self, *exc):
        return False

    def record(self, numframes=None):
        #====================================
        # 要給幾個樣本
        #====================================
        if numframes is None:                            # 像喇叭：這段時間累積了多少就給多少
            n = int((time.time() - self.t0) * RATE) - self.sent
        else:                                            # 像麥克風：等到湊滿才回來
            n = numframes
            time.sleep(max(0.0, self.t0 + (self.sent + n) / RATE - time.time()))

        #====================================
        # 接著上次的相位產生聲音
        #====================================
        x = 0.2 * np.sin(2 * np.pi * self.dev.freq * (self.sent + np.arange(n)) / RATE)
        self.sent += n
        return np.repeat(x.astype(np.float32)[:, None], self.dev.channels, axis=1)


@pytest.fixture
def devices(data_root, monkeypatch):
    """回傳 (麥克風, 喇叭清單)：測試可以改這兩個東西來模擬裝置出狀況。喇叭清單的每一項是 (id, 名稱, 假裝置)。"""
    mic = FakeDevice("假麥克風", 440)
    speakers = [("spk-1", "假喇叭", FakeDevice("假喇叭", 880, channels=2))]
    monkeypatch.setattr(recorder, "available", lambda: True)
    monkeypatch.setattr(recorder, "_open_mic", lambda device: mic)
    monkeypatch.setattr(recorder, "_list_speakers", lambda: list(speakers))
    monkeypatch.setattr(recorder, "SEGMENT_SEC", 2)
    monkeypatch.setattr(recorder, "SPEAKER_SCAN_SEC", 0.3)
    monkeypatch.setattr(recorder, "transcribe", lambda d: None)
    recorder._state["finalizing"].clear()
    yield mic, speakers
    if recorder._state["rec"]:                           # 測試中途失敗時把錄音收掉，不拖累後面的測試
        recorder.stop()


def wait_saved(folder):
    """等這場合併完，回傳最後的狀態文字。"""
    for _ in range(120):
        state = recorder._state["finalizing"].get(folder.name, "")
        if state.startswith(("完成", "失敗")):
            return state
        time.sleep(0.1)
    return state


def record(data_root, seconds, title="測試"):
    """錄 seconds 秒。回傳 (會議資料夾, 快結束時錄音頁看到的狀態)。"""
    st = recorder.start(title)
    folder = data_root / "meetings" / st["recording"]["folder"]
    time.sleep(seconds)
    during = recorder.status()["recording"]
    recorder.stop()
    assert wait_saved(folder).startswith("完成")
    return folder, during


pytestmark = needs_ffmpeg


#------------------------------------------------------------------------
# 從開錄到存成檔
#------------------------------------------------------------------------
def test_microphone_and_computer_audio_go_to_separate_channels(devices, data_root):
    folder, during = record(data_root, 3)

    #====================================
    # 錄音頁看得到兩軌的音量
    #====================================
    assert during["alive"] and during["level_db"] > HEARD and during["loopback_db"] > HEARD
    assert during["loopback_sec"] >= 2 and during["speakers"] == 1 and not during["notes"]

    #====================================
    # 存成雙聲道：左是麥克風、右是電腦播出的聲音，互相沒有滲漏
    #====================================
    src = folder / "source.m4a"
    left_mic, left_pc = tone_db(src, 440, 880, channel=0)
    right_mic, right_pc = tone_db(src, 440, 880, channel=1)
    assert run.audio_channels(src) == 2 and run.ffprobe_duration(src) > 2.5
    assert left_mic > HEARD and right_pc > HEARD
    assert left_pc < ABSENT and right_mic < ABSENT

    #====================================
    # 小檔合併後刪掉；meeting.json 記著這是兩軌的檔，轉錄流程靠它認
    #====================================
    meta = json.loads((folder / "meeting.json").read_text(encoding="utf-8"))
    assert not (folder / "rec").exists()
    assert meta["recording"]["channels"] == ["mic", "computer"] and meta["recording"]["ended"] and meta["status"] == "未處理"
    assert run.computer_audio(src, meta)["active_sec"] >= 2


def test_speaker_that_shows_up_during_the_meeting_is_recorded(devices, data_root):
    _, speakers = devices
    late = speakers.pop()                                # 開錄時一個喇叭都沒有

    #====================================
    # 錄到一半才接上（例如接了螢幕或耳機）
    #====================================
    st = recorder.start("中途接上")
    folder = data_root / "meetings" / st["recording"]["folder"]
    time.sleep(1.2)
    before = recorder.status()["recording"]
    speakers.append(late)
    time.sleep(2.3)
    after = recorder.status()["recording"]
    recorder.stop()
    assert wait_saved(folder).startswith("完成")

    #====================================
    # 接上之前沒有電腦聲音，接上之後有
    #====================================
    assert before["speakers"] == 0 and before["loopback_sec"] == 0
    assert after["speakers"] == 1 and after["loopback_sec"] >= 1
    assert tone_db(folder / "source.m4a", 880, channel=1) > HEARD - 10


def test_a_speaker_that_cannot_be_opened_does_not_stop_the_recording(devices, data_root):
    _, speakers = devices
    speakers.insert(0, ("spk-0", "被獨占的喇叭", FakeDevice("被獨占的喇叭", 660, broken="裝置使用中")))

    folder, during = record(data_root, 2.5)

    #====================================
    # 錄音照常，打得開的那個喇叭照錄；錄音頁看得到哪個喇叭錄不到
    #====================================
    assert during["alive"] and during["speakers"] == 1
    assert "被獨占的喇叭" in during["notes"] and "裝置使用中" in during["notes"] and "error" not in during
    mic = tone_db(folder / "source.m4a", 440, channel=0)
    pc = tone_db(folder / "source.m4a", 880, channel=1)
    assert mic > HEARD and pc > HEARD


def test_warning_goes_away_when_the_speaker_works_again(devices, data_root, monkeypatch):
    _, speakers = devices
    monkeypatch.setattr(recorder, "SPEAKER_RETRY_SEC", 0.5)
    speakers[0][2].broken = "裝置使用中"                 # 開錄時唯一的喇叭被別的程式佔住

    #====================================
    # 一開始錄不到：錄音頁有提示
    #====================================
    st = recorder.start("暫時錄不到")
    folder = data_root / "meetings" / st["recording"]["folder"]
    time.sleep(1.0)
    before = recorder.status()["recording"]
    assert "假喇叭" in before["notes"] and before["speakers"] == 0

    #====================================
    # 佔用的程式放開了：自動重試、開始錄到，提示消失
    #====================================
    speakers[0][2].broken = None
    time.sleep(2.5)
    after = recorder.status()["recording"]
    recorder.stop()
    assert wait_saved(folder).startswith("完成")
    assert after["notes"] == "" and after["speakers"] == 1 and after["loopback_sec"] >= 1


def test_meeting_without_any_computer_audio_still_records_the_microphone(devices, data_root):
    #====================================
    # 準備：這台電腦一個喇叭都沒有
    #====================================
    _, speakers = devices
    speakers.clear()

    #====================================
    # 錄音
    #====================================
    folder, during = record(data_root, 2.5)

    #====================================
    # 麥克風照錄；檔案還是兩軌，右聲道是靜音，轉錄時不會用它
    #====================================
    assert during["alive"] and during["speakers"] == 0 and during["loopback_sec"] == 0
    src = folder / "source.m4a"
    assert run.audio_channels(src) == 2                  # 還是兩軌，右聲道是靜音：轉錄時就不會用它
    assert tone_db(src, 440, channel=0) > HEARD and tone_db(src, 880, channel=1) < ABSENT
    meta = json.loads((folder / "meeting.json").read_text(encoding="utf-8"))
    assert run.computer_audio(src, meta) == {"active_sec": 0, "used": False, "by": "auto"}


def test_ending_one_meeting_can_start_the_next(devices, data_root):
    #====================================
    # 第一場錄到一半，按「結束並開始下一場」
    #====================================
    st = recorder.start("第一場")
    first = data_root / "meetings" / st["recording"]["folder"]
    time.sleep(1.5)
    st = recorder.stop(next_title="第二場")
    second = data_root / "meetings" / st["recording"]["folder"]
    assert second != first and st["recording"]["alive"] and st["recording"]["device"] == "假麥克風"

    #====================================
    # 兩場各自存成檔
    #====================================
    time.sleep(1.5)
    recorder.stop()
    assert wait_saved(first).startswith("完成") and wait_saved(second).startswith("完成")
    assert run.ffprobe_duration(first / "source.m4a") > 1 and run.ffprobe_duration(second / "source.m4a") > 1


#------------------------------------------------------------------------
# 開不了錄的情況
#------------------------------------------------------------------------
def test_broken_microphone_is_reported_before_anything_is_created(devices, data_root):
    #====================================
    # 準備：麥克風打不開
    #====================================
    mic, _ = devices
    mic.broken = "找不到裝置"

    #====================================
    # 開錄：說清楚原因，不留下空的會議資料夾
    #====================================
    with pytest.raises(RuntimeError, match="打不開麥克風.*找不到裝置"):
        recorder.start("開不了")
    assert list((data_root / "meetings").iterdir()) == [] and recorder.status()["recording"] is None


def test_computer_that_cannot_record_says_so(devices, data_root, monkeypatch):
    #====================================
    # 準備：這台電腦不能錄音（不是 Windows，或沒有 soundcard）
    #====================================
    monkeypatch.setattr(recorder, "available", lambda: False)

    #====================================
    # 錄音頁知道不能錄；硬要開錄會說明原因
    #====================================
    assert recorder.status()["can_record"] is False and recorder.list_devices() == []
    with pytest.raises(RuntimeError, match="不能錄音"):
        recorder.start("不能錄")
    assert list((data_root / "meetings").iterdir()) == []


#------------------------------------------------------------------------
# 標題開頭又打了日期
#------------------------------------------------------------------------
@pytest.mark.parametrize("typed, kept", [
    ("2026-10-21-週會", "週會"),
    ("2026/10/21 週會", "週會"),
    ("2026.10.21_週會", "週會"),
    ("20261021週會", "週會"),
    ("2026年10月21日　週會", "週會"),
    ("2026-10-21", ""),                                  # 只打了日期：標題是空的，會用預設的「會議」
    ("  週會  ", "週會"),
    ("2026-10-20 補錄昨天的會", "2026-10-20 補錄昨天的會"),   # 不是今天：是有意義的日期，留著
    ("2026 年度計畫", "2026 年度計畫"),
    ("20261021號機台", "號機台"),
    ("202610215 號訂單", "202610215 號訂單"),            # 後面還有數字：不是日期
    ("週會 2026-10-21", "週會 2026-10-21"),              # 日期不在開頭：不動
])
def test_todays_date_at_the_start_of_the_title_is_dropped(typed, kept):
    from datetime import datetime
    assert recorder._without_today(typed, datetime(2026, 10, 21)) == kept


def test_folder_name_does_not_repeat_the_date(devices, data_root):
    from datetime import datetime
    today = f"{datetime.now():%Y-%m-%d}"

    #====================================
    # 標題自己又打了今天的日期
    #====================================
    st = recorder.start(f"{today}-週會")
    folder = data_root / "meetings" / st["recording"]["folder"]
    recorder.stop()
    assert wait_saved(folder).startswith("完成")

    #====================================
    # 資料夾與標題都只有一個日期
    #====================================
    assert folder.name == f"{today}_週會" and st["recording"]["title"] == "週會"
    assert json.loads((folder / "meeting.json").read_text(encoding="utf-8"))["title"] == "週會"


#------------------------------------------------------------------------
# 把各個喇叭的聲音湊成一軌（不開裝置，直接餵資料）
#------------------------------------------------------------------------
@pytest.fixture
def mix(tmp_path):
    """回傳 (混音器, 餵資料的函式)。"""
    m = recorder._SpeakerMix(threading.Event(), tmp_path / "capture.log")

    def feed(sid, samples):
        st = m.streams.setdefault(sid, {"new": deque(), "pending": np.zeros(0, dtype=np.float32), "primed": False})
        st["new"].append(np.asarray(samples, dtype=np.float32))
    return m, feed


def test_mix_adds_speakers_and_fills_gaps_with_silence(mix):
    #====================================
    # 準備：兩個喇叭各播了 0.3 秒
    #====================================
    m, feed = mix
    n = recorder.MIX_BLOCK
    feed("a", np.full(n * 3, 0.25))
    feed("b", np.full(n * 3, 0.5))

    #====================================
    # 取四次，每次 0.1 秒
    #====================================
    assert np.allclose(m.take(n), 0.75)                  # 兩個喇叭同時在播：加總
    assert np.allclose(m.take(n), 0.75)
    assert np.allclose(m.take(n), 0.75)
    assert np.allclose(m.take(n), 0)                     # 都沒有新的聲音：靜音


def test_mix_keeps_sound_continuous_when_data_arrives_unevenly(mix):
    m, feed = mix
    n = recorder.MIX_BLOCK
    ramp = np.arange(1, n * 20 + 1, dtype=np.float32)    # 每個樣本的值都不同，才看得出有沒有缺、有沒有亂序

    #====================================
    # 資料一包一包進來，有時少一點、有時多一點（真的裝置就是這樣）
    #====================================
    out, sent = [], 0
    for size in [n * 9 // 10, n * 11 // 10] * 10:
        feed("a", ramp[sent:sent + size])
        sent += size
        out.append(m.take(n))
    for _ in range(3):                                   # 播完了，把剩下的取完
        out.append(m.take(n))
    got = np.concatenate(out)

    #====================================
    # 聲音一開始之後就沒有中斷，也沒有少任何一個樣本
    #====================================
    heard = got[got > 0]
    first = int(np.argmax(got > 0))
    assert np.array_equal(heard, ramp)
    assert np.all(got[first:first + len(ramp)] > 0)


def test_mix_plays_a_short_blip_and_drops_a_long_backlog(mix):
    m, feed = mix
    n = recorder.MIX_BLOCK

    #====================================
    # 很短的提示音：不夠一次取的量，也不會再有新的，下一輪就給出來
    #====================================
    feed("a", np.full(n // 4, 0.5))
    assert np.allclose(m.take(n), 0)
    blip = m.take(n)
    assert np.count_nonzero(blip) == n // 4

    #====================================
    # 積了太多（兩個裝置的時鐘不同步）：丟掉舊的，從最新的接下去
    #====================================
    backlog = np.arange(1, recorder.LOOPBACK_MAX_LAG + n * 5 + 1, dtype=np.float32)
    feed("a", backlog)
    got = m.take(n)
    assert got[0] > backlog[len(backlog) // 2] and got[-1] <= backlog[-1]
