#!/usr/bin/env python3
"""升級到最新版：抓最新的程式、套件清單有變就重裝、最後跑自我檢查。

會議、詞彙表、聲紋資料庫、.env 都不在 git 裡，升級不會動到。
用法: python utils/update.py        Windows 可以雙擊 update.bat；macOS／Linux 執行 ./update.sh
結束碼：升級完成（或本來就是最新版）而且自我檢查的必要項目都通過為 0，否則為 1。
"""
import http.client
import json
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 5077
WIN = sys.platform == "win32"
# 問本機的校對工具不走 proxy：電腦設了 proxy、例外清單又沒列 127.0.0.1 時，Python 會把這個請求也送去 proxy
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def git(root, *args):
    return subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=root,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def other_run_py():
    sys.path.insert(0, str(ROOT))
    import recorder
    return recorder._other_run_py()


def busy(port=PORT):
    """現在不能升級的原因（一句話）；可以升級回傳 None。"""
    #====================================
    # 校對工具開著：新程式要重開才生效，而重開會中斷錄音與轉錄
    #====================================
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()   # 沒人在聽時 Windows 要等到逾時才回報，所以設短一點
        listening = True
    except OSError:
        listening = False
    if listening:
        try:
            with LOCAL.open(f"http://127.0.0.1:{port}/api/rec/status", timeout=10) as r:
                st = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code >= 500:                            # 有程式在、但狀態讀不到：可能就是校對工具自己出錯了，寧可不升級
                return f"{port} 埠上的程式有回應、但讀不到狀態（HTTP {e.code}）：校對工具如果還開著，先關掉再升級。"
            st = None                                    # 404 之類：佔用這個埠的是別的網頁程式
        except (OSError, ValueError, http.client.HTTPException) as e:
            if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
                return "校對工具沒有回應（可能正在忙）：等它忙完、關掉之後再升級。"
            st = None                                    # 回應不是這個工具的格式：佔用這個埠的是別的程式
        if isinstance(st, dict) and "transcribing" in st:
            doing = []
            if st.get("recording"):
                doing.append("正在錄音")
            # 合併完的項目會留在清單上給錄音頁顯示（「完成…」「失敗…」），不算還在合併
            if any(not str(v).startswith(("完成", "失敗")) for v in (st.get("finalizing") or {}).values()):
                doing.append("正在合併錄音")
            jobs = [v for v in st["transcribing"].values() if v.get("state") in ("進行中", "排隊中")]
            if jobs:
                doing.append(f"有 {len(jobs)} 場會議在轉錄或排隊")
            if doing:
                return "校對工具" + "、".join(doing) + "：等它結束、關掉校對工具之後再升級。"
            return "校對工具還開著：請先關掉（關掉 start.bat 開的黑色視窗），再升級。"

    #====================================
    # 命令列另外開的轉錄：中途換掉程式，後半段會用到新舊混合的版本
    #====================================
    if other_run_py():
        return "有轉錄正在跑（pipeline/run.py）：等它跑完再升級。"
    return None


def changed(root, old, new, path):
    return git(root, "diff", "--quiet", old, new, "--", path).returncode == 1


def venv_python(root):
    #====================================
    # 專案自己的 Python 在哪
    #====================================
    p = root / ".venv" / ("Scripts/python.exe" if WIN else "bin/python")

    #====================================
    # 沒有 .venv 就是用電腦原有的 Python
    #====================================
    return p if p.exists() else None


def find_uv():
    """uv 剛裝好時還不在 PATH 上，所以也看它預設的安裝位置。"""
    #====================================
    # PATH 上有就用
    #====================================
    exe = shutil.which("uv")
    if exe:
        return exe

    #====================================
    # 不然看預設的安裝位置
    #====================================
    p = Path.home() / ".local/bin" / ("uv.exe" if WIN else "uv")
    return str(p) if p.exists() else None


def install_packages(root):
    """照 requirements.txt 把 .venv 的套件補齊（已經齊全時不到一秒），裝好回傳 True。"""
    uv = find_uv()
    if uv:
        cmd = [uv, "pip", "install", "--python", ".venv", "-r", "requirements.txt"]
    else:
        cmd = [str(venv_python(root)), "-m", "pip", "install", "-r", "requirements.txt"]   # uv 建的 .venv 裡沒有 pip；這條路給 python -m venv 建的環境
    return subprocess.run(cmd, cwd=root).returncode == 0


def run_doctor(root):
    """跑自我檢查（結果直接顯示給使用者），回傳必要項目是否全部通過。"""
    #====================================
    # 用專案自己的 Python（沒有 .venv 就用現在這個）
    #====================================
    py = venv_python(root) or sys.executable

    #====================================
    # 結束碼 0 才是必要項目全過
    #====================================
    return subprocess.run([str(py), str(root / "utils" / "doctor.py")], cwd=root).returncode == 0


def main(root=ROOT):
    try:
        sys.stdout.reconfigure(errors="replace")        # 主控台是 cp950 時，印不出的字變成 ? 而不是整個當掉
    except (AttributeError, ValueError):
        pass

    #====================================
    # 檢查：程式是用 git 取得的、現在可以升級
    #====================================
    if not shutil.which("git") or not (root / ".git").exists():
        print("這個資料夾不是用 git clone 取得的（或這台電腦沒有 git），沒辦法自動升級："
              "請重新下載最新版，再把 meetings/、glossary.txt、voiceprints.json、.env 搬過去。")
        return 1
    why = busy()
    if why:
        print(why)
        return 1
    old = git(root, "rev-parse", "HEAD").stdout.strip()

    #====================================
    # 抓最新版
    #====================================
    print("抓最新版…", flush=True)
    r = git(root, "pull", "--ff-only")                   # 只接受單純往前：這個資料夾裡有自己的 commit 時不替使用者合併
    if r.returncode != 0:
        print((r.stderr or r.stdout).strip())
        print("沒有升級，程式維持原樣。常見原因：沒有網路；這個資料夾裡的程式被改過（git status 看得到改了哪些檔）。")
        return 1
    new = git(root, "rev-parse", "HEAD").stdout.strip()
    if new == old:
        print("已經是最新版。")
    else:
        print("這次更新：")
        for line in git(root, "log", "--format=%s", f"{old}..{new}").stdout.splitlines():
            print("  - " + line)

    #====================================
    # 補齊套件
    #====================================
    installed = True
    if venv_python(root):
        # 每次都對一次，不只在套件清單有變的時候：上次補裝失敗（當時沒網路）、或先自己 git pull 過，這裡才補得回來
        print("確認套件…", flush=True)
        installed = install_packages(root)
        if not installed:
            print("  套件沒有裝好（可能是連不到網路）：再執行一次升級會重試。")
    elif new != old and changed(root, old, new, "requirements.txt"):
        # 沒有 .venv：套件裝在電腦原有的 Python 裡，不替使用者決定要不要動它
        print("套件清單有更新，請自己補裝：pip install -r requirements.txt")
        installed = False

    #====================================
    # 自我檢查
    #====================================
    print(flush=True)
    passed = run_doctor(root)

    #====================================
    # 結論
    #====================================
    if new != old:
        print("程式已經換成新版，重新開啟校對工具（start.bat；macOS／Linux 用 ./start.sh）就會用到。")
    return 0 if installed and passed else 1


if __name__ == "__main__":
    sys.exit(main())
