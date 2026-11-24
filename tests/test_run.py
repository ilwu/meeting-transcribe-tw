"""轉錄流程裡不需要模型的部分：整理原始檔、無聲偵測、切段、檢查模型的回應。音檔用 ffmpeg 合成。"""
import json
import subprocess

import pytest

import run
from helpers import make_audio, make_long_tone, needs_ffmpeg, tone_db

MIN = 60


def response(text, finish="STOP"):
    return {"candidates": [{"finishReason": finish, "content": {"parts": [{"text": text}]}}]}


#------------------------------------------------------------------------
# 檢查模型的回應
#------------------------------------------------------------------------

def test_check_accepts_well_formed_transcript():
    text, problem = run.check(response("[00:00] 講者1：甲\n[00:05] 講者2：乙"))
    assert problem == "" and text.count("\n") == 1


@pytest.mark.parametrize("resp, speech_sec, expect", [
    ({"error": {"message": "過載"}, "status": 503}, None, "HTTP 503"),
    ({"promptFeedback": {"blockReason": "OTHER"}}, None, "被擋"),
    (response("[00:00] 講者1：甲", finish="MAX_TOKENS"), None, "上限"),
    (response("沒有時間也沒有講者\n就只是文字"), None, "格式不符"),
    # 模型偶爾轉幾分鐘就回 STOP：有聲音到 26 分鐘卻只轉到 1 分鐘
    (response("[00:00] 講者1：甲\n[01:00] 講者2：乙"), 26 * MIN, "只轉到"),
])
def test_check_reports_problems(resp, speech_sec, expect):
    assert expect in run.check(resp, speech_sec)[1]


#------------------------------------------------------------------------
# 整理直接丟進資料夾的原始檔
#------------------------------------------------------------------------

@needs_ffmpeg
def test_import_raw_organizes_dropped_files(tmp_path):
    #====================================
    # 準備：Teams 下載的檔名
    #====================================
    d = tmp_path / "2026-01-02-週會"
    d.mkdir()
    make_audio(d / "週會-20260102_083000-會議錄製.m4a", ("tone", 2))
    (d / "字幕.vtt").write_text("WEBVTT\n", encoding="utf-8")

    #====================================
    # 執行兩次：第二次不能再動
    #====================================
    run.import_raw(d)
    first = (d / "meeting.json").read_text(encoding="utf-8")
    run.import_raw(d)

    #====================================
    # 檢查
    #====================================
    meta = json.loads(first)
    assert (d / "source.m4a").exists() and (d / "transcripts" / "teams.vtt").exists()
    assert (meta["title"], meta["date"]) == ("週會", "2026-01-02")
    assert meta["source"]["original_name"] == "週會-20260102_083000-會議錄製.m4a" and meta["source"]["duration_sec"] == 2
    assert (d / "meeting.json").read_text(encoding="utf-8") == first


@needs_ffmpeg
def test_import_raw_refuses_to_guess_between_two_recordings(tmp_path):
    make_audio(tmp_path / "a.m4a", ("tone", 1))
    make_audio(tmp_path / "b.m4a", ("tone", 1))
    with pytest.raises(SystemExit):
        run.import_raw(tmp_path)


#------------------------------------------------------------------------
# 無聲偵測
#------------------------------------------------------------------------

@needs_ffmpeg
def test_silent_tail_is_not_sent_for_transcription(tmp_path):
    audio = make_audio(tmp_path / "a.m4a", ("tone", 120), ("silence", 180))
    speech, full = run.speech_end(audio)
    assert 120 <= speech <= 130 and 299 <= full <= 301


@needs_ffmpeg
def test_short_pause_at_the_end_is_kept(tmp_path):
    audio = make_audio(tmp_path / "a.m4a", ("tone", 120), ("silence", 30))
    speech, full = run.speech_end(audio)
    assert speech == full


@needs_ffmpeg
def test_silent_ranges_finds_long_digital_silence_anywhere(tmp_path):
    audio = make_audio(tmp_path / "a.m4a", ("tone", 30), ("silence", 90), ("tone", 30), ("silence", 20))
    ranges = run.silent_ranges(audio)
    assert len(ranges) == 1                                           # 結尾 20 秒不到 1 分鐘，不算
    assert abs(ranges[0][0] - 30) <= 1 and abs(ranges[0][1] - 120) <= 1
    assert run.silent_ranges(make_audio(tmp_path / "b.m4a", ("tone", 90))) == []


@needs_ffmpeg
def test_whisper_only_hears_the_part_with_sound(tmp_path):
    (tmp_path / "audio").mkdir()
    audio = make_audio(tmp_path / "audio" / "16k.m4a", ("tone", 40))
    assert run.whisper_input(tmp_path, audio, {}, False) == audio
    cut = run.whisper_input(tmp_path, audio, {"silent_tail_from_sec": 20}, False)
    assert cut.name == "speech.m4a" and abs(run.ffprobe_duration(cut) - 20) < 1


#------------------------------------------------------------------------
# 切段
#------------------------------------------------------------------------

@needs_ffmpeg
def test_quietest_point_prefers_a_pause(tmp_path):
    audio = make_audio(tmp_path / "a.m4a", ("tone", 20), ("silence", 1), ("tone", 20))
    assert 20 <= run.quietest_point(audio, center=18) <= 21
    assert run.quietest_point(make_audio(tmp_path / "b.m4a", ("tone", 40)), center=18) == 18   # 到處一樣吵就照原定位置切


CH = 300      # 測試用的切段長度；真的是 26 分鐘，照那個長度合成音檔一次測試要跑一分多鐘


@needs_ffmpeg
@pytest.mark.parametrize("seconds, offsets, why", [
    (CH, [0], "不超過單段上限就不切"),
    (2 * CH, [0, CH], "剛好整除"),
    (2 * CH + 90, [0, CH], "尾段不到 2 分鐘，併進前一段"),
    (2 * CH + 145, [0, CH, 2 * CH], "尾段超過 2 分鐘，自己一段"),
])
def test_split_boundaries(tmp_path, monkeypatch, seconds, offsets, why):
    #====================================
    # 準備
    #====================================
    monkeypatch.setattr(run, "CHUNK", CH)
    monkeypatch.setattr(run, "SINGLE_MAX", CH + 20)
    (tmp_path / "audio").mkdir()
    audio = make_long_tone(tmp_path / "audio" / "16k.m4a", seconds)
    total = run.ffprobe_duration(audio)

    #====================================
    # 執行
    #====================================
    parts = run.split(tmp_path, audio, {}, False)

    #====================================
    # 切在預定位置附近、各段首尾相接、加起來等於全長
    #====================================
    # 切點會落在預定位置前後 10 秒內最安靜的地方（合成音檔每 8 秒的接縫有極短的安靜），所以不是整數
    assert len(parts) == len(offsets), why
    # 容許 10.5 秒：回傳的是最安靜那半秒的中點，會比範圍邊界多出一點
    assert all(abs(off - want) <= 10.5 * i for i, ((_, off), want) in enumerate(zip(parts, offsets))), why
    if len(parts) > 1:
        lengths = [run.ffprobe_duration(p) for p, _ in parts]
        assert abs(sum(lengths) - total) < 1
        for (_, off), (_, nxt), length in zip(parts, parts[1:], lengths):
            assert abs(off + length - nxt) < 0.5


@needs_ffmpeg
def test_split_stops_at_the_silent_tail_and_remembers_it(tmp_path):
    (tmp_path / "audio").mkdir()
    audio = make_audio(tmp_path / "audio" / "16k.m4a", ("tone", 80), ("silence", 600))
    meta = {}
    parts = run.split(tmp_path, audio, meta, False)
    assert len(parts) == 1 and parts[0][1] == 0
    assert 80 <= run.ffprobe_duration(parts[0][0]) <= 90
    assert 80 <= meta["silent_tail_from_sec"] <= 90                   # 校對工具靠這個顯示警告


def test_split_reuses_recorded_offsets(tmp_path):
    # meeting.json 已經記了切段位置就沿用：位移一變，校對專案就對不上
    meta = {"parts": [{"file": "audio/part1.m4a", "offset_sec": 0}, {"file": "audio/part2.m4a", "offset_sec": 1543.25}]}
    assert run.split(tmp_path, tmp_path / "audio" / "16k.m4a", meta, False) == [
        (tmp_path / "audio/part1.m4a", 0), (tmp_path / "audio/part2.m4a", 1543.25)]


@needs_ffmpeg
def test_forced_rerun_recuts_the_parts_from_the_new_audio(tmp_path, monkeypatch):
    #====================================
    # 準備：一場切成三段的會議，第一次轉錄時聲音是 440 Hz
    #====================================
    monkeypatch.setattr(run, "CHUNK", CH)
    monkeypatch.setattr(run, "SINGLE_MAX", CH + 20)
    (tmp_path / "audio").mkdir()
    audio = make_long_tone(tmp_path / "audio" / "16k.m4a", 2 * CH + 145)
    parts = run.split(tmp_path, audio, {}, False)
    meta = {"parts": [{"file": p.relative_to(tmp_path).as_posix(), "offset_sec": off} for p, off in parts]}
    assert len(parts) == 3 and tone_db(parts[1][0], 440, seconds=2) > -30

    #====================================
    # 音檔換了內容（例如改成把電腦那一軌混進來），這裡用 880 Hz 代表
    #====================================
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=880:sample_rate=16000:duration={2 * CH + 145}",
                    "-ac", "1", "-c:a", "aac", "-b:a", "32k", str(audio)], check=True)

    #====================================
    # 一般的重跑：沿用已經切好的段，不重切
    #====================================
    assert run.split(tmp_path, audio, meta, False) == parts
    assert tone_db(parts[1][0], 880, seconds=2) < -60

    #====================================
    # 強制重轉：每一段都換成新的聲音，位移不變、各段仍然首尾相接
    #====================================
    assert run.split(tmp_path, audio, meta, True) == parts
    assert all(tone_db(p, 880, seconds=2) > -30 for p, _ in parts)
    lengths = [run.ffprobe_duration(p) for p, _ in parts]
    assert abs(sum(lengths) - run.ffprobe_duration(audio)) < 1
    for (_, off), (_, nxt), length in zip(parts, parts[1:], lengths):
        assert abs(off + length - nxt) < 0.5


def test_computer_audio_option_must_be_valid_and_come_with_force():
    #====================================
    # 正確的用法：記下使用者的指定
    #====================================
    for value, want in (("on", True), ("off", False), ("auto", None)):
        meta = {"recording": {"use_computer": "舊的"}}
        run.set_computer_choice(["會議", "--computer-audio", value, "--force"], meta, True)
        assert meta["recording"]["use_computer"] is want

    #====================================
    # 沒給這個參數：什麼都不動
    #====================================
    meta = {}
    run.set_computer_choice(["會議", "--force"], meta, True)
    assert meta == {}

    #====================================
    # 打錯字、漏了值、忘了 --force：說明後結束，什麼都不記
    #====================================
    for args, force in ((["會議", "--computer-audio", "ON", "--force"], True), (["會議", "--force", "--computer-audio"], True),
                        (["會議", "--computer-audio", "off"], False)):
        meta = {}
        with pytest.raises(SystemExit) as e:
            run.set_computer_choice(args, meta, force)
        assert meta == {} and "--computer-audio" in str(e.value)
