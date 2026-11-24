"""升級（utils/update.py）：抓得到新版、不該升級的時候不動程式、每次都把套件對一次。
用暫存資料夾裡的兩個 git repo 當「GitHub」與使用者電腦上 clone 下來的那一份。"""
import http.server
import json
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "utils"))
import update  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="沒有 git")


def sh(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
                       cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def publish(upstream, files, msg):
    """在「GitHub」那一份改檔並 commit。"""
    for name, text in files.items():
        (upstream / name).write_text(text, encoding="utf-8")
    sh(upstream, "add", "-A")
    sh(upstream, "commit", "-q", "-m", msg)


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """兩個 repo 的範本，整個檔案只建一次：git 在 Windows 上每開一次行程要零點幾秒，每個測試各建一份太慢。"""
    base = tmp_path_factory.mktemp("update-template")
    (base / "upstream").mkdir()
    sh(base / "upstream", "init", "-q", "-b", "main")
    publish(base / "upstream", {"app.py": "v1", "requirements.txt": "flask"}, "第一版")
    sh(base, "clone", "-q", "upstream", "installed")
    sh(base / "installed", "remote", "set-url", "origin", "../upstream")     # 改成相對路徑：整組複製到別處還連得到
    return base


@pytest.fixture
def repos(template, tmp_path, monkeypatch):
    """upstream 是「GitHub」，installed 是使用者 clone 下來、放著自己資料的那一份（當成有 .venv）；
    不真的裝套件、不真的跑自我檢查。"""
    #====================================
    # 兩個 repo（從範本複製）
    #====================================
    upstream, installed = tmp_path / "upstream", tmp_path / "installed"
    shutil.copytree(template / "upstream", upstream)
    shutil.copytree(template / "installed", installed)

    #====================================
    # 使用者自己的資料（都不在 git 裡）
    #====================================
    (installed / "meetings" / "週會").mkdir(parents=True)
    (installed / "meetings" / "週會" / "project.json").write_text("我的校對", encoding="utf-8")
    (installed / ".env").write_text("GEMINI_API_KEY=x", encoding="utf-8")

    #====================================
    # 假的裝套件與自我檢查，記下被呼叫幾次
    #====================================
    calls = {"install": 0, "doctor": 0, "install_ok": True}

    def fake_install(root):
        calls["install"] += 1
        return calls["install_ok"]

    def fake_doctor(root):
        calls["doctor"] += 1
        return True

    monkeypatch.setattr(update, "busy", lambda: None)
    monkeypatch.setattr(update, "venv_python", lambda root: root / ".venv")
    monkeypatch.setattr(update, "install_packages", fake_install)
    monkeypatch.setattr(update, "run_doctor", fake_doctor)
    return upstream, installed, calls


#------------------------------------------------------------------------
# 升級本身
#------------------------------------------------------------------------
def test_already_latest(repos, capsys):
    _, installed, calls = repos

    #====================================
    # 升級
    #====================================
    code = update.main(installed)

    #====================================
    # 沒有新版，但套件與自我檢查照樣對一次
    #====================================
    assert code == 0 and "已經是最新版" in capsys.readouterr().out
    assert (calls["install"], calls["doctor"]) == (1, 1)


def test_gets_new_version_and_keeps_user_data(repos, capsys):
    upstream, installed, calls = repos
    publish(upstream, {"app.py": "v2"}, "第二版：修好錄音")

    #====================================
    # 升級
    #====================================
    assert update.main(installed) == 0

    #====================================
    # 程式換成新版，並告訴使用者這次改了什麼、要重開
    #====================================
    assert (installed / "app.py").read_text(encoding="utf-8") == "v2"
    out = capsys.readouterr().out
    assert "第二版：修好錄音" in out and "start.bat" in out

    #====================================
    # 使用者的資料沒被動到
    #====================================
    assert (installed / "meetings" / "週會" / "project.json").read_text(encoding="utf-8") == "我的校對"
    assert (installed / ".env").read_text(encoding="utf-8") == "GEMINI_API_KEY=x"
    assert (calls["install"], calls["doctor"]) == (1, 1)


def test_packages_that_failed_to_install_are_retried_next_time(repos, capsys):
    upstream, installed, calls = repos
    publish(upstream, {"requirements.txt": "flask\nnumpy"}, "多一個套件")

    #====================================
    # 抓到新版，但套件沒裝起來（例如當下沒網路）：程式已經換了，要回報沒完成
    #====================================
    calls["install_ok"] = False
    assert update.main(installed) == 1
    assert "套件沒有裝好" in capsys.readouterr().out
    assert sh(installed, "rev-parse", "HEAD") == sh(upstream, "rev-parse", "HEAD")

    #====================================
    # 再執行一次：已經是最新版，還是會再裝一次，這次成功
    #====================================
    calls["install_ok"] = True
    assert update.main(installed) == 0
    assert calls["install"] == 2 and "已經是最新版" in capsys.readouterr().out


def test_without_venv_packages_are_left_to_the_user(repos, capsys, monkeypatch):
    upstream, installed, calls = repos
    monkeypatch.setattr(update, "venv_python", lambda root: None)

    #====================================
    # 套件清單沒變：不用做什麼
    #====================================
    publish(upstream, {"app.py": "v2"}, "第二版")
    assert update.main(installed) == 0 and calls["install"] == 0

    #====================================
    # 套件清單變了：不動電腦原有的 Python，請使用者自己裝
    #====================================
    publish(upstream, {"requirements.txt": "flask\nnumpy"}, "多一個套件")
    assert update.main(installed) == 1
    assert calls["install"] == 0 and "請自己補裝" in capsys.readouterr().out


def test_does_not_touch_anything_while_the_tool_is_running(repos, capsys, monkeypatch):
    upstream, installed, calls = repos
    publish(upstream, {"app.py": "v2"}, "第二版")
    monkeypatch.setattr(update, "busy", lambda: "校對工具正在錄音：等它結束")

    #====================================
    # 升級
    #====================================
    code = update.main(installed)

    #====================================
    # 說明原因，什麼都不動
    #====================================
    assert code == 1 and "正在錄音" in capsys.readouterr().out
    assert (installed / "app.py").read_text(encoding="utf-8") == "v1" and (calls["install"], calls["doctor"]) == (0, 0)


def test_local_edits_stop_the_upgrade_and_are_kept(repos, capsys):
    upstream, installed, calls = repos
    publish(upstream, {"app.py": "v2"}, "第二版")
    before = sh(installed, "rev-parse", "HEAD")
    (installed / "app.py").write_text("使用者自己改的", encoding="utf-8")

    #====================================
    # 升級
    #====================================
    code = update.main(installed)

    #====================================
    # 沒有升級，使用者改的還在
    #====================================
    assert code == 1 and "沒有升級" in capsys.readouterr().out
    assert (installed / "app.py").read_text(encoding="utf-8") == "使用者自己改的"
    assert sh(installed, "rev-parse", "HEAD") == before and calls["doctor"] == 0


def test_folder_that_is_not_a_clone(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(update, "busy", lambda: None)

    #====================================
    # 升級
    #====================================
    code = update.main(tmp_path)

    #====================================
    # 說明沒辦法自動升級
    #====================================
    assert code == 1 and "不是用 git clone 取得的" in capsys.readouterr().out


#------------------------------------------------------------------------
# 現在能不能升級
#------------------------------------------------------------------------
@pytest.fixture
def fake_tool():
    """假的校對工具：在一個空的埠回應錄音狀態的查詢。回傳 (埠, reply)：reply["body"] 是字串就原樣回（當成別的程式），
    reply["code"] 是 HTTP 狀態碼。"""
    reply = {"body": {"recording": None, "finalizing": {}, "transcribing": {}}, "code": 200}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = reply["body"] if isinstance(reply["body"], str) else json.dumps(reply["body"])
            self.send_response(reply["code"])
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], reply
    server.shutdown()
    server.server_close()


def closed_port():
    """一個確定沒有人在聽的埠。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_busy_tells_why(fake_tool, monkeypatch):
    port, reply = fake_tool
    monkeypatch.setattr(update, "other_run_py", lambda: False)

    #====================================
    # 校對工具開著但閒著：請使用者先關掉
    #====================================
    assert "還開著" in update.busy(port)

    #====================================
    # 正在錄音、正在轉錄：說出在忙什麼
    #====================================
    reply["body"] = {"recording": {"title": "週會"}, "finalizing": {}, "transcribing": {}}
    assert "正在錄音" in update.busy(port)
    reply["body"] = {"recording": None, "finalizing": {},
                     "transcribing": {"a": {"state": "進行中"}, "b": {"state": "排隊中"}, "c": {"state": "完成"}}}
    assert "有 2 場會議在轉錄或排隊" in update.busy(port)

    #====================================
    # 合併錄音：合併中才算；合併完的項目會一直留在清單上，不能讓使用者等一個早就結束的合併
    #====================================
    reply["body"] = {"recording": None, "finalizing": {"a": "合併中"}, "transcribing": {}}
    assert "正在合併錄音" in update.busy(port)
    reply["body"] = {"recording": None, "finalizing": {"a": "完成（3.2 分鐘）", "b": "失敗：沒有錄到任何聲音"}, "transcribing": {}}
    assert "合併" not in update.busy(port) and "還開著" in update.busy(port)


def test_busy_is_not_fooled_by_other_programs_or_a_proxy(fake_tool, monkeypatch):
    port, reply = fake_tool
    monkeypatch.setattr(update, "other_run_py", lambda: False)

    #====================================
    # 電腦設了 proxy、例外清單沒列 127.0.0.1：還是直接問本機，看得到校對工具開著
    #====================================
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{closed_port()}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert "還開著" in update.busy(port)

    #====================================
    # 佔用這個埠的是別的網頁程式：不擋升級
    #====================================
    reply["body"] = "<html>別的程式</html>"
    assert update.busy(port) is None
    reply["body"] = "[1, 2]"
    assert update.busy(port) is None
    reply["code"] = 404
    assert update.busy(port) is None

    #====================================
    # 有程式在、但回的是伺服器錯誤：可能就是校對工具自己出錯了，不升級
    #====================================
    reply["code"] = 500
    assert "讀不到狀態" in update.busy(port)


def test_busy_survives_a_port_that_does_not_speak_http(monkeypatch):
    monkeypatch.setattr(update, "other_run_py", lambda: False)

    #====================================
    # 準備：一個連得上、但回的不是 HTTP 的程式
    #====================================
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)

    def answer():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            conn.recv(1024)
            conn.sendall(b"hello\r\n")
            conn.close()
    threading.Thread(target=answer, daemon=True).start()

    #====================================
    # 當成別的程式，不擋升級，也不會出錯
    #====================================
    try:
        assert update.busy(server.getsockname()[1]) is None
    finally:
        server.close()


def test_busy_when_the_tool_is_closed(monkeypatch):
    closed = closed_port()

    #====================================
    # 沒有東西在跑：可以升級
    #====================================
    monkeypatch.setattr(update, "other_run_py", lambda: False)
    assert update.busy(closed) is None

    #====================================
    # 命令列另外開的轉錄還在跑：不能升級
    #====================================
    monkeypatch.setattr(update, "other_run_py", lambda: True)
    assert "pipeline/run.py" in update.busy(closed)
