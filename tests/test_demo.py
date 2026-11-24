"""示範會議：放進 repo 的那一場是完整的，全新安裝第一次啟動會看到它。"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import app
import notes

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"
MEETING = next(DEMO.glob("*/project.json")).parent


def test_demo_meeting_is_complete():
    #====================================
    # 校對專案本身
    #====================================
    p = json.loads((MEETING / "project.json").read_text(encoding="utf-8"))
    segs = p["segments"]
    assert [s["id"] for s in segs] == list(range(len(segs))) and len(segs) > 5
    assert all(v.get("name") for v in p["speakers"].values())
    assert {s["speaker"] for s in segs} == set(p["speakers"])
    assert all(s["text_whisper"] for s in segs)                       # 每一行都有 whisper 的對照可以看

    #====================================
    # 校對工具要用到的檔案都在
    #====================================
    assert (MEETING / p["audio"]).stat().st_size > 100_000 and (MEETING / "source.m4a").exists()
    meta = json.loads((MEETING / "meeting.json").read_text(encoding="utf-8"))
    assert "示範" in meta["title"] and meta["source"]["duration_sec"] > 60
    assert meta["demo"] is True                                              # 聲紋靠這個記號不學示範會議的合成聲音
    assert (MEETING / "glossary.txt").read_text(encoding="utf-8").strip()      # 重聽、重新轉錄都會帶這場的詞彙表

    #====================================
    # 會議紀錄的時間點都對得到逐字稿
    #====================================
    n = json.loads((MEETING / "notes.json").read_text(encoding="utf-8"))
    _, bad = notes.snap_times(n, sorted(s["start"] for s in segs), max(s["end"] for s in segs))
    assert bad == 0 and n["action_items"] and n["review_items"]
    assert (MEETING / "notes.md").read_text(encoding="utf-8") == notes.to_markdown(n)


def test_nothing_in_demo_is_ignored_by_git():
    #====================================
    # 準備：只有在 git 工作目錄裡才查得到忽略規則
    #====================================
    if not (ROOT / ".git").exists() or not shutil.which("git"):
        pytest.skip("不是 git 工作目錄")

    #====================================
    # 列出 demo/ 裡被忽略規則擋掉的檔
    #====================================
    r = subprocess.run(["git", "-c", "core.quotepath=false", "ls-files", "--others", "--ignored", "--exclude-standard", "--", "demo"],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8")

    #====================================
    # 斷言
    #====================================
    # 根目錄的忽略規則（*.m4a、glossary.txt）是替真的會議資料寫的，示範會議的同名檔要另外放行；
    # 漏了的話本機測試照樣過，推上去的那一份卻少檔
    assert r.returncode == 0 and r.stdout.splitlines() == []


def test_script_only_uses_speakers_that_have_a_voice():
    #====================================
    # 讀劇本
    #====================================
    sys.path.insert(0, str(ROOT / "utils"))
    import make_demo_audio
    lines = make_demo_audio.read_script(make_demo_audio.SCRIPT)

    #====================================
    # 每位講者都有指定的聲音
    #====================================
    assert len(lines) > 5 and {who for who, _ in lines} == set(make_demo_audio.VOICES)


def test_first_start_gets_the_demo_meeting_once(tmp_path):
    meetings = tmp_path / "meetings"

    #====================================
    # 第一次啟動：放進示範會議，沒有留下暫存資料夾
    #====================================
    app.seed_meetings(meetings, DEMO)
    copied = meetings / MEETING.name
    assert (copied / "project.json").exists() and (copied / "audio").is_dir()
    assert [p.name for p in tmp_path.iterdir()] == ["meetings"]

    #====================================
    # 使用者刪掉示範會議之後，下次啟動不會再放回來
    #====================================
    shutil.rmtree(copied)
    app.seed_meetings(meetings, DEMO)
    assert list(meetings.iterdir()) == []


def test_interrupted_first_start_is_redone(tmp_path, monkeypatch):
    meetings = tmp_path / "meetings"
    real_copy = shutil.copytree

    #====================================
    # 第一次啟動：複製到一半出事
    #====================================
    def broken_copy(src, dst):
        Path(dst).mkdir(parents=True)
        (Path(dst) / "project.json").write_text("寫到一半", encoding="utf-8")
        raise OSError("磁碟滿了")
    monkeypatch.setattr(shutil, "copytree", broken_copy)
    with pytest.raises(OSError):
        app.seed_meetings(meetings, DEMO)
    assert not meetings.exists()

    #====================================
    # 下次啟動：重新來過，示範會議是完整的，暫存資料夾也清掉了
    #====================================
    monkeypatch.setattr(shutil, "copytree", real_copy)
    app.seed_meetings(meetings, DEMO)
    assert (meetings / MEETING.name / "project.json").exists() and (meetings / MEETING.name / "source.m4a").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["meetings"]


def test_importing_the_tool_does_not_create_the_meetings_folder(tmp_path):
    #====================================
    # 在全新的行程裡 import（跑測試、別的腳本引用時就是這樣），資料位置指到還不存在的地方
    #====================================
    code = ("import sys; from pathlib import Path; sys.path[:0] = [sys.argv[1], sys.argv[1] + '/pipeline']; "
            "import recorder; recorder.MEETINGS = Path(sys.argv[2]) / 'meetings'; import app; print(recorder.MEETINGS.exists())")
    r = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(ROOT), str(tmp_path)],
                       capture_output=True, text=True, encoding="utf-8")

    #====================================
    # 真的啟動（python app.py）才會建資料夾、放示範會議
    #====================================
    assert r.returncode == 0, r.stderr[-500:]
    assert r.stdout.strip() == "False"


def test_demo_meeting_opens_in_the_tool(client, data_root):
    #====================================
    # 準備：像全新安裝那樣放進示範會議
    #====================================
    (data_root / "meetings").rmdir()
    app.seed_meetings(data_root / "meetings", DEMO)

    #====================================
    # 會議清單、逐字稿、音檔、會議紀錄都讀得到
    #====================================
    ov = client.get("/api/projects/overview").get_json()
    assert [(m["name"], m["title"], m["silent"]) for m in ov] == [(MEETING.name, "產品週會（示範）", [])]
    assert len(client.get(f"/api/project/{MEETING.name}").get_json()["segments"]) == ov[0]["total"]
    audio = client.get(f"/audio/{MEETING.name}")
    assert audio.status_code == 200 and audio.mimetype == "audio/mp4"
    assert client.get(f"/api/project/{MEETING.name}/notes").get_json()["title"]
    assert client.get("/api/search", query_string={"q": "bpm 進度"}).get_json()["total"] >= 1
