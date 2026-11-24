"""轉錄時決定用不用電腦那一軌（run.computer_audio、run.prepare_audio）：用合成的雙聲道檔，
左聲道（麥克風）是 440 Hz、右聲道（電腦播出的聲音）是 880 Hz，看轉出來的單聲道裡有沒有 880 Hz。"""
import pytest

import run
from helpers import make_audio, make_two_tracks, needs_ffmpeg, tone_db

pytestmark = needs_ffmpeg
HEARD, ABSENT = -30, -60                                 # 聽得到的純音在 -20 dB 上下，沒有的在 -90 dB 以下


@pytest.fixture(autouse=True)
def short_files(monkeypatch):
    """測試用 8 秒的音檔（合成 30 秒的要兩秒多），「有聲音超過多久才算」的門檻跟著縮成 3 秒。"""
    monkeypatch.setattr(run, "COMPUTER_MIN_SEC", 3)


def meeting(tmp_path, computer_sec, **recording):
    """一場 8 秒的錄音頁錄音：電腦那一軌前 computer_sec 秒有聲音。回傳（會議資料夾, meeting.json 的內容）。"""
    d = tmp_path / "2026-01-01_測試"
    d.mkdir()
    make_two_tracks(d / "source.m4a", 8, computer_sec)
    return d, {"recording": {"channels": ["mic", "computer"], **recording}}


def test_remote_voices_are_mixed_in(tmp_path):
    d, meta = meeting(tmp_path, 5)

    out = run.prepare_audio(d, meta, False)

    #====================================
    # 記下判斷的結果
    #====================================
    ca = meta["recording"]["computer_audio"]
    assert ca["used"] is True and ca["by"] == "auto" and 4 <= ca["active_sec"] <= 6

    #====================================
    # 轉出來的單聲道裡兩邊的聲音都有
    #====================================
    mic, computer = tone_db(out, 440, 880)
    assert run.audio_channels(out) == 1 and mic > HEARD and computer > HEARD


@pytest.mark.parametrize("computer_sec, why", [(0, "現場會議：電腦完全沒播聲音"), (1, "只有一聲提示音")])
def test_computer_track_is_left_out_when_it_has_no_real_sound(tmp_path, computer_sec, why):
    d, meta = meeting(tmp_path, computer_sec)

    #====================================
    # 轉檔
    #====================================
    out = run.prepare_audio(d, meta, False)

    #====================================
    # 記下「沒有採用」，轉出來的單聲道裡只有麥克風
    #====================================
    ca = meta["recording"]["computer_audio"]
    assert ca["used"] is False and ca["by"] == "auto" and abs(ca["active_sec"] - computer_sec) <= 1, why
    mic, computer = tone_db(out, 440, 880)
    assert mic > HEARD and computer < ABSENT, why


def test_the_user_can_overrule_the_automatic_choice(tmp_path):
    #====================================
    # 聲音很短，但使用者說要用
    #====================================
    d, meta = meeting(tmp_path, 1, use_computer=True)
    out = run.prepare_audio(d, meta, False)
    assert meta["recording"]["computer_audio"]["used"] is True and meta["recording"]["computer_audio"]["by"] == "manual"
    assert tone_db(out, 880) > HEARD - 15                 # 只有 1 秒，整段平均下來比較小聲

    #====================================
    # 聲音很長，但使用者說不要（例如那是開會時自己在聽的音樂）；改了要重做，所以加 force
    #====================================
    meta["recording"]["use_computer"] = False
    make_two_tracks(d / "source.m4a", 8, 5)
    out = run.prepare_audio(d, meta, True)
    assert meta["recording"]["computer_audio"]["used"] is False and meta["recording"]["computer_audio"]["by"] == "manual"
    mic, computer = tone_db(out, 440, 880)
    assert mic > HEARD and computer < ABSENT


def test_files_from_elsewhere_are_downmixed_as_before(tmp_path):
    #====================================
    # 別處來的雙聲道檔（例如 Teams 錄影）：沒有錄音頁的記號，兩個聲道照常混在一起
    #====================================
    d, _ = meeting(tmp_path, 5)
    meta = {}
    assert run.computer_audio(d / "source.m4a", meta) is None
    out = run.prepare_audio(d, meta, False)
    mic, computer = tone_db(out, 440, 880)
    assert meta == {} and mic > HEARD - 10 and computer > HEARD - 10

    #====================================
    # 有記號但檔案是單聲道：不當成兩軌
    #====================================
    make_audio(d / "source.m4a", ("tone", 5))
    assert run.computer_audio(d / "source.m4a", {"recording": {"channels": ["mic", "computer"]}}) is None


def test_existing_audio_is_kept_unless_forced(tmp_path):
    #====================================
    # 第一次轉檔
    #====================================
    d, meta = meeting(tmp_path, 5)
    out = run.prepare_audio(d, meta, False)
    before = out.stat().st_mtime_ns

    #====================================
    # 重跑（例如上次 Gemini 失敗）：不會重做這一步，上次記下的結果也還在
    #====================================
    assert run.prepare_audio(d, meta, False).stat().st_mtime_ns == before
    assert meta["recording"]["computer_audio"]["used"] is True
