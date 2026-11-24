"""另外放進來的錄影當畫面來源：認檔、用聲音對齊時間、截圖用錄音的時間、錄影晚到時列在錄音頁。

聲音用內附示範會議的合成語音（要有像說話那樣不規則的聲音才對得起來），影片是合成的一片藍。
"""
import json
import shutil
from pathlib import Path

import pytest

import recorder
import run
from helpers import make_audio, make_video, needs_ffmpeg, write_meeting

ROOT = Path(__file__).resolve().parent.parent
SPEECH = next((ROOT / "demo").glob("*/source.m4a"))      # 81 秒


@pytest.fixture
def meeting(tmp_path):
    """錄音頁錄好的一場會議：只有聲音。回傳 (資料夾, meeting.json 的內容)。"""
    d = tmp_path / "2026-01-15_線上會議"
    d.mkdir()
    shutil.copy(SPEECH, d / "source.m4a")
    meta = {"title": "線上會議", "date": "2026-01-15", "source": {"file": "source.m4a"},
            "recording": {"started": "2026-01-15T10:00:00", "channels": ["mic", "computer"]}}
    (d / "meeting.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return d, meta


def read_meta(d):
    return json.loads((d / "meeting.json").read_text(encoding="utf-8"))


#------------------------------------------------------------------------
# 認檔
#------------------------------------------------------------------------
@needs_ffmpeg
def test_video_dropped_next_to_a_recording_becomes_the_screen_source(meeting):
    d, _ = meeting
    make_video(d / "週會-20260115_100030-會議錄製.mp4", 5)

    #====================================
    # 整理檔案
    #====================================
    run.import_raw(d)

    #====================================
    # 影片變成畫面來源，聲音來源不動，原檔名記下來
    #====================================
    assert sorted(f.name for f in d.iterdir() if f.is_file()) == ["meeting.json", "screen.mp4", "source.m4a"]
    assert read_meta(d)["screen"] == {"file": "screen.mp4", "original_name": "週會-20260115_100030-會議錄製.mp4"}
    assert read_meta(d)["source"]["file"] == "source.m4a"

    #====================================
    # 再整理一次：不會再動
    #====================================
    before = (d / "meeting.json").read_text(encoding="utf-8")
    run.import_raw(d)
    assert (d / "meeting.json").read_text(encoding="utf-8") == before


@needs_ffmpeg
def test_extra_videos_are_left_alone_when_it_is_not_clear_what_they_are(tmp_path, meeting):
    #====================================
    # 聲音來源本身就是影片：另一個影片不知道是什麼，不動
    #====================================
    a = tmp_path / "2026-01-16_錄影"
    a.mkdir()
    make_video(a / "source.mp4", 3)
    make_video(a / "另一段.mp4", 3)
    run.import_raw(a)
    assert sorted(f.name for f in a.glob("*.mp4")) == ["source.mp4", "另一段.mp4"] and "screen" not in read_meta(a)

    #====================================
    # 放了兩個影片：不知道哪一個是這場的畫面，都不動
    #====================================
    d, _ = meeting
    make_video(d / "上半場.mp4", 3)
    make_video(d / "下半場.mp4", 3)
    run.import_raw(d)
    assert sorted(f.name for f in d.glob("*.mp4")) == ["上半場.mp4", "下半場.mp4"] and "screen" not in read_meta(d)


#------------------------------------------------------------------------
# 對齊時間
#------------------------------------------------------------------------
@needs_ffmpeg
@pytest.mark.parametrize("delay, skip, want, why", [
    (7, 0, -7, "錄影比錄音早 7 秒開始"),
    (0, 10, 10, "錄影比錄音晚 10 秒開始"),
])
def test_offset_is_found_by_matching_the_sound(meeting, delay, skip, want, why):
    d, meta = meeting
    video = make_video(d / "screen.mp4", 60, SPEECH, delay=delay, skip=skip)

    #====================================
    # 對齊
    #====================================
    off = run.screen_offset(d, meta, video)

    #====================================
    # 差不到 0.2 秒，並記下是用聲音對出來的
    #====================================
    assert abs(off - want) < 0.2, why
    assert meta["screen"]["aligned_by"] == "audio" and meta["screen"]["offset_sec"] == off


@needs_ffmpeg
def test_offset_falls_back_to_recorded_times_when_the_sound_does_not_match(meeting, tmp_path):
    d, meta = meeting

    #====================================
    # 錄影整段沒有聲音，但檔名裡有開錄的時間：比錄音晚 30 秒
    #====================================
    video = make_video(d / "screen.mp4", 20)
    meta["screen"] = {"file": "screen.mp4", "original_name": "週會-20260115_100030-會議錄製.mp4"}
    assert run.screen_offset(d, meta, video) == 30 and meta["screen"]["aligned_by"] == "timestamps"

    #====================================
    # 算過就沿用，不重算
    #====================================
    meta["screen"]["offset_sec"] = 12.5
    assert run.screen_offset(d, meta, video) == 12.5

    #====================================
    # 聲音不相干（錄影裡是一段純音）、也查不到時間：當成同時開始，並標明對不上
    #====================================
    tone = make_audio(tmp_path / "tone.m4a", ("tone", 20))
    video = make_video(d / "screen.mp4", 20, tone)
    meta["screen"] = {"file": "screen.mp4", "original_name": "會議錄製.mp4"}
    meta["recording"].pop("started")
    assert run.screen_offset(d, meta, video) == 0 and meta["screen"]["aligned_by"] == "none"


@needs_ffmpeg
def test_screen_offset_can_be_given_by_hand(meeting):
    d, meta = meeting

    #====================================
    # 沒有另外放進來的影片：這個參數用不上，說明後結束
    #====================================
    with pytest.raises(SystemExit):
        run.set_screen_offset(["會議", "--screen-offset", "5"], meta, d)

    #====================================
    # 有影片：記下手動指定的值，並回報畫面要重做；沒給參數就什麼都不做
    #====================================
    make_video(d / "screen.mp4", 3)
    assert run.set_screen_offset(["會議"], meta, d) is False and "screen" not in meta
    assert run.set_screen_offset(["會議", "--screen-offset", "-12.5"], meta, d) is True
    assert meta["screen"] == {"file": "screen.mp4", "offset_sec": -12.5, "aligned_by": "manual"}

    #====================================
    # 不是數字、漏了值：說明後結束
    #====================================
    for args in (["會議", "--screen-offset", "很晚"], ["會議", "--screen-offset"]):
        with pytest.raises(SystemExit):
            run.set_screen_offset(args, meta, d)


#------------------------------------------------------------------------
# 截圖與畫面時間軸
#------------------------------------------------------------------------
@needs_ffmpeg
def test_frames_and_timeline_use_the_recording_clock(meeting, monkeypatch):
    d, meta = meeting
    (d / "transcripts").mkdir()
    (d / "frames").mkdir()
    (d / "frames" / "t00000.jpg").write_bytes(b"old")    # 上次對齊留下的截圖

    #====================================
    # 準備：250 秒的錄影比 81 秒的錄音早 100 秒開始；模型換成假的，每張圖都回一行說明
    #====================================
    make_video(d / "screen.mp4", 250)
    meta["screen"] = {"file": "screen.mp4", "offset_sec": -100, "aligned_by": "manual"}

    def fake_gemini(parts, key, as_json=False):
        shown = [int(p["text"][2:]) for p in parts if p.get("text", "").startswith("t=")]
        return {"glossary": ["畫面上的詞"], "timeline": [{"t": t, "desc": f"第 {t} 秒的畫面"} for t in shown]}
    monkeypatch.setattr(run, "gemini_text", fake_gemini)

    #====================================
    # 分析畫面
    #====================================
    run.screen_analysis(d, meta, "key", False)

    #====================================
    # 每 2 分鐘一張照錄音的時間排：81 秒的錄音只有開頭那一張，取的是錄影第 100 秒的畫面；舊的同名截圖被換掉
    #====================================
    assert [f.name for f in (d / "frames").iterdir()] == ["t00000.jpg"]
    assert (d / "frames" / "t00000.jpg").stat().st_size > 500
    timeline = json.loads((d / "transcripts" / "screen_timeline.json").read_text(encoding="utf-8"))
    assert [(r["t"], r["frame"]) for r in timeline] == [(0, "t00000.jpg")]
    assert (d / "transcripts" / "glossary_screen.txt").read_text(encoding="utf-8").strip() == "畫面上的詞"

    #====================================
    # 錄影在錄音結束之後才開始：沒有畫面可用，但留下空的結果，不會一直被列成「畫面還沒處理」
    #====================================
    meta["screen"]["offset_sec"] = 500
    run.screen_analysis(d, meta, "key", True)
    assert list((d / "frames").iterdir()) == []
    assert json.loads((d / "transcripts" / "screen_timeline.json").read_text(encoding="utf-8")) == []


#------------------------------------------------------------------------
# 錄影晚到：逐字稿已經轉完，只差畫面
#------------------------------------------------------------------------
def test_meetings_that_got_a_video_later_are_listed_for_screen_processing(client, data_root, monkeypatch):
    asked = []
    monkeypatch.setattr(recorder, "transcribe", asked.append)

    #====================================
    # 準備：三場已經有逐字稿的會議
    #====================================
    late = write_meeting(data_root, "2026-01-03_影片晚到", ["甲"])
    (late / "Teams 錄影.mp4").write_bytes(b"x")
    done = write_meeting(data_root, "2026-01-02_畫面處理過了", ["乙"])
    (done / "screen.mp4").write_bytes(b"x")
    (done / "transcripts").mkdir()
    (done / "transcripts" / "screen_timeline.json").write_text("[]", encoding="utf-8")
    video_only = write_meeting(data_root, "2026-01-01_本來就是錄影", ["丙"])
    (video_only / "source.mp4").write_bytes(b"x")

    #====================================
    # 只有「影片晚到」那一場等著處理畫面；它不算「還沒轉逐字稿」
    #====================================
    st = client.get("/api/rec/status").get_json()
    assert st["screen_pending"] == ["2026-01-03_影片晚到"] and st["pending"] == []

    #====================================
    # 錄音頁按「處理畫面」：排進去跑；別的資料夾不能這樣觸發
    #====================================
    assert client.post("/api/rec/transcribe/2026-01-03_影片晚到", json={}).status_code == 200
    assert [d.name for d in asked] == ["2026-01-03_影片晚到"]
    assert client.post("/api/rec/transcribe/2026-01-02_畫面處理過了", json={}).status_code == 404
