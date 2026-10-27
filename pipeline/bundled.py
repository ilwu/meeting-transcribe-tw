"""專案內附的外部程式：安裝指南把 ffmpeg／ffprobe 放在 vendor/ffmpeg/bin，使用者不用改系統 PATH。"""
import os
from pathlib import Path

FFMPEG_BIN = Path(__file__).resolve().parent.parent / "vendor/ffmpeg/bin"


def use_bundled_ffmpeg():
    """有內附的 ffmpeg 就放到 PATH 最前面；之後開的子行程（run.py、whisper）也會繼承。"""
    path = os.environ.get("PATH", "")
    if FFMPEG_BIN.is_dir() and str(FFMPEG_BIN) not in path.split(os.pathsep):
        os.environ["PATH"] = str(FFMPEG_BIN) + os.pathsep + path
