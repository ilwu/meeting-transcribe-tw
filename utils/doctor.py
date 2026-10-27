#!/usr/bin/env python3
"""安裝自我檢查：逐項列出必要、建議、選用元件裝好了沒，缺的寫怎麼補（對應 docs/install.md 的段落）。

用法: python utils/doctor.py            # 不連網
      python utils/doctor.py --online   # 另外打一次 Gemini API 確認金鑰可用（不花錢）
結束碼：必要項目全部通過為 0，否則為 1。不會印出金鑰的任何部分。
"""
import importlib
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from bundled import use_bundled_ffmpeg  # noqa: E402

WIN = sys.platform == "win32"
VIBE = ROOT / "vendor/vibe-server" / ("vibe-server.exe" if WIN else "vibe-server")
WHISPER_MODELS = ["ggml-large-v3-turbo.bin", "diar_streaming_sortformer_4spk-v2.q8_0.gguf", "ggml-silero-v6.2.0.bin"]
results = []                                             # (等級, 是否通過, 說明)；等級：必要／建議／選用


def add(level, ok, msg):
    results.append((level, ok, msg))


def first_line(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        return (r.stdout or r.stderr).splitlines()[0] if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def read_key():
    """只讀 .env 裡的 GEMINI_API_KEY，不印出來。沒有 .env 回傳 None。"""
    env = ROOT / ".env"
    if not env.exists():
        return None
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("GEMINI_API_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def check_key_online(key):
    """列模型清單：不產生費用，能確認金鑰有效、網路連得到 Google。"""
    req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
                                 headers={"x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            json.load(r)
        return True, "金鑰可用"
    except urllib.error.HTTPError as e:
        return False, f"Google 拒絕這把金鑰（HTTP {e.code}）：請向發金鑰的人確認金鑰是否正確、是否已停用"
    except (urllib.error.URLError, OSError) as e:
        return False, f"連不上 Google（{type(e).__name__}）：檢查網路或公司的 Proxy"


def gpus():
    """顯示卡名稱（只當參考：whisper 用 Vulkan／Metal，內顯也能跑）。"""
    if WIN:
        out = first_line(["powershell", "-NoProfile", "-Command",
                          "(Get-CimInstance Win32_VideoController | ForEach-Object Name) -join '；'"])
        return out or "查不到"
    if sys.platform == "darwin":
        return "Apple Silicon（Metal）" if platform.machine() == "arm64" else "Intel Mac"
    return "未偵測（Linux）"


def main():
    online = "--online" in sys.argv
    try:
        sys.stdout.reconfigure(errors="replace")        # 主控台是 cp950 時，印不出的字變成 ? 而不是整個當掉
    except AttributeError:
        pass
    use_bundled_ffmpeg()

    #====================================
    # 必要：Python、套件、ffmpeg、金鑰
    #====================================
    v = sys.version_info
    add("必要", v >= (3, 10), f"Python {platform.python_version()}" + ("" if v >= (3, 10) else "：需要 3.10 以上（見 docs/install.md「Python 環境」）"))
    missing = []
    for mod, pkg in (("flask", "flask"), ("opencc", "opencc-python-reimplemented"), ("numpy", "numpy")):
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)
    add("必要", not missing, "Python 套件齊全" if not missing else f"缺套件 {', '.join(missing)}：在專案資料夾執行 uv pip install --python .venv -r requirements.txt")
    for tool in ("ffmpeg", "ffprobe"):
        line = first_line([tool, "-version"])
        where = shutil.which(tool) or ""
        bundled = " （專案內附）" if "vendor" in where.replace("\\", "/") else ""
        add("必要", bool(line), f"{tool}：{line.split(' Copyright')[0]}{bundled}" if line else f"找不到 {tool}（見 docs/install.md「ffmpeg」）")
    key = read_key()
    if key is None:
        add("必要", False, "沒有 .env：從 .env.example 複製一份（見 docs/install.md「設定檔」）")
    elif not key:
        add("必要", False, ".env 裡的 GEMINI_API_KEY 是空的：請使用者自己打開 .env 填入金鑰")
    elif online:
        ok, msg = check_key_online(key)
        add("必要", ok, "Gemini " + msg)
    else:
        add("必要", True, "Gemini 金鑰已填（加 --online 可確認能用）")

    #====================================
    # 建議：詞彙表、PDF
    #====================================
    gl = ROOT / "glossary.txt"
    add("建議", gl.exists(), "詞彙表 glossary.txt" if gl.exists()
        else "沒有 glossary.txt（可以不用）：有人給你就放到專案根目錄；要自己建，格式參考 glossary.example.txt，範本裡的假詞不要留著")
    try:
        importlib.import_module("pymupdf")
        add("建議", True, "PyMuPDF（會議資料夾裡的 PDF 可建詞彙表）")
    except ImportError:
        add("建議", False, "沒有 PyMuPDF：PDF 不會建詞彙表；uv pip install --python .venv -r requirements.txt")

    #====================================
    # 選用：whisper、聲紋
    #====================================
    w_missing = [m for m in WHISPER_MODELS if not (ROOT / "vendor/models" / m).exists()]
    if VIBE.exists() and not w_missing:
        add("選用", True, f"whisper（vibe-server 與 3 個模型）；顯示卡：{gpus()}")
    elif not VIBE.exists() and len(w_missing) == len(WHISPER_MODELS):
        add("選用", None, f"whisper 未安裝（轉錄只用 Gemini，沒有可疑行標示）；顯示卡：{gpus()}")
    else:
        add("選用", False, "whisper 裝了一半：缺 " + "、".join(([VIBE.name] if not VIBE.exists() else []) + w_missing)
            + "（見 docs/install.md「whisper」）")
    try:
        import voiceprints
        vp = voiceprints.available()
    except ImportError:
        vp = False
    spk = (ROOT / "vendor/models/spk").exists()
    add("選用", True if vp else (False if spk else None),
        "聲紋（sherpa-onnx 與模型）" if vp else ("聲紋裝了一半（見 docs/install.md「聲紋」）" if spk else "聲紋未安裝"))

    #====================================
    # 校對工具是否已在執行
    #====================================
    try:                                                 # 只看埠有沒有人聽會誤判成別的程式，要確認回應的是這個工具
        with urllib.request.urlopen("http://127.0.0.1:5077/api/projects/overview", timeout=3) as r:
            running = isinstance(json.load(r), list)
    except (OSError, ValueError):
        running = False
    add("選用", True if running else None, "校對工具正在 http://127.0.0.1:5077 執行" if running else "校對工具還沒啟動（雙擊 start.bat；macOS／Linux 執行 ./start.sh）")

    #====================================
    # 輸出結論
    #====================================
    mark = {True: "[通過]", False: "[失敗]", None: "[未裝]"}
    for level in ("必要", "建議", "選用"):
        print(level)
        for lv, ok, msg in results:
            if lv == level:
                tag = "[注意]" if ok is False and level != "必要" else mark[ok]
                print(f"  {tag} {msg}")
    failed = [m for lv, ok, m in results if lv == "必要" and not ok]
    print("\n結論：" + ("必要項目全部通過，可以開始使用" if not failed else f"有 {len(failed)} 項必要項目沒通過"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
