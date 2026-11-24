"""測試用的小工具：假會議、合成音檔。匯入路徑由 conftest.py 先設好。"""
import json
import shutil
import subprocess

import pytest

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="需要 ffmpeg／ffprobe")


def seg(i, text, status="pending", speaker="講者A", **extra):
    """一行逐字稿：每行 10 秒，id 等於在清單裡的位置（前端靠這一點）。"""
    return {"id": i, "start": i * 10.0, "end": i * 10.0 + 10, "start_gemini": i * 10.0, "part": 1,
            "speaker": speaker, "speaker_orig": speaker, "text": text, "text_orig": text, "status": status,
            "flag": "", "text_whisper": "", "whisper_cer": None, "llm_suggestion": "", **extra}


def write_meeting(root, name, texts, meta=None, **project_extra):
    """在 root/meetings/<name>/ 建一場假會議，回傳資料夾。texts 的每一項是文字，或 seg() 的參數 dict。"""
    d = root / "meetings" / name
    (d / "audio").mkdir(parents=True)
    segs = [seg(i, **t) if isinstance(t, dict) else seg(i, t) for i, t in enumerate(texts)]
    speakers = {s["speaker"]: {"name": ""} for s in segs}
    project = {"name": name, "audio": "audio/16k.m4a", "speakers": speakers, "speaker_map": {}, "segments": segs, **project_extra}
    (d / "project.json").write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
    if meta is not None:
        (d / "meeting.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return d


def read_project(d):
    return json.loads((d / "project.json").read_text(encoding="utf-8"))


def make_long_tone(path, seconds, unit=8):
    """長時間的測試音：只編碼一小段，再不重新編碼地接起來（直接編十分鐘要六秒，這樣一秒多）。"""
    tmp = path.parent / f"_{path.stem}_units"
    tmp.mkdir()
    names = []
    for name, sec, count in (("u.m4a", unit, seconds // unit), ("r.m4a", seconds % unit, 1 if seconds % unit else 0)):
        if count:
            make_audio(tmp / name, ("tone", sec))
            names += [name] * count
    (tmp / "list.txt").write_text("".join(f"file '{n}'\n" for n in names), encoding="utf-8")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt"),
                    "-c", "copy", str(path)], check=True)
    shutil.rmtree(tmp)
    return path


def make_audio(path, *pieces, rate=16000):
    """用 ffmpeg 合成測試音檔。pieces 依序是 ("tone", 秒數) 或 ("silence", 秒數)；靜音是數位靜音（全是 0）。"""
    inputs, labels = [], []
    for i, (kind, sec) in enumerate(pieces):
        src = f"sine=frequency=440:sample_rate={rate}:duration={sec}" if kind == "tone" else f"anullsrc=r={rate}:cl=mono:d={sec}"
        inputs += ["-f", "lavfi", "-i", src]
        labels.append(f"[{i}:a]")
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex",
                    f"{''.join(labels)}concat=n={len(pieces)}:v=0:a=1",
                    "-ac", "1", "-ar", str(rate), "-c:a", "aac", "-b:a", "32k", str(path)], check=True)
    return path


def make_two_tracks(path, seconds, computer_sec):
    """錄音頁錄出來的那種雙聲道檔，用同一組編碼參數：左聲道（麥克風）整段是 440 Hz，
    右聲道（電腦播出的聲音）前 computer_sec 秒是 880 Hz、之後是數位靜音。"""
    import recorder
    expr = f"0.2*sin(2*PI*440*t)|0.2*sin(2*PI*880*t)*lt(t,{computer_sec})"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d={seconds}",
                    *recorder.ENCODE, str(path)], check=True)
    return path


def tone_db(path, *freqs, channel=0, seconds=None):
    """音檔某個聲道裡，各個頻率的成分有多強（dB）。振幅 0.2 的純音整段都在大約是 -14；完全沒有會在 -90 以下。
    給一個頻率回傳一個數字，給好幾個回傳清單（只解碼一次：這台電腦每開一次 ffmpeg 要零點幾秒）。
    seconds 是只看開頭幾秒：用很多小段接起來的長音檔，各段的相位接不上，整段一起量會互相抵消。"""
    import math
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", *(["-t", str(seconds)] if seconds else []), "-i", str(path),
                          "-af", f"pan=mono|c0=c{channel}", "-ar", "48000",
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype="<f4")
    t = np.arange(len(x)) / 48000
    out = [20 * math.log10(2 * abs(np.mean(x * np.exp(-2j * np.pi * f * t))) + 1e-12) for f in freqs]
    return out[0] if len(out) == 1 else out
