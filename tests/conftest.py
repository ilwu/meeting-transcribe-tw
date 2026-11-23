"""測試共用設定：匯入路徑，以及把資料位置導到暫存資料夾的校對工具。

測試不碰真的會議資料、聲紋資料庫與詞彙表，也不呼叫模型：要用模型的地方一律換成假函式。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "pipeline"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from bundled import use_bundled_ffmpeg  # noqa: E402

use_bundled_ffmpeg()                       # 測試裡的 ffmpeg 和使用者安裝後的一樣：有內附的就先用內附的


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """把校對工具、錄音、聲紋的資料位置都導到暫存資料夾，並關掉會在背景學聲紋的功能。"""
    import app
    import recorder
    import voiceprints
    (tmp_path / "meetings").mkdir()
    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr(recorder, "MEETINGS", tmp_path / "meetings")
    monkeypatch.setattr(voiceprints, "DB", tmp_path / "voiceprints.json")
    monkeypatch.setattr(voiceprints, "available", lambda: False)
    return tmp_path


@pytest.fixture
def client(data_root):
    import app
    return app.app.test_client()
