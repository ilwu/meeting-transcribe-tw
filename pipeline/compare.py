#!/usr/bin/env python3
"""比較多份逐字稿：正規化（去時間戳/講者/標點、轉簡體）後算兩兩 CER。
用法: python pipeline/compare.py 名稱=檔案 名稱=檔案 ...（支援 .txt/.md、Teams .vtt、whisper .json）
"""
import re, sys, json
from pathlib import Path
import textutil
from textutil import cer

def load(path):
    t = Path(path).read_text(encoding="utf-8")
    if path.endswith(".vtt"):                                     # Teams 字幕：只留 <v 講者>內容</v> 的內容
        return "\n".join(re.findall(r"<v [^>]*>(.*?)</v>", t, flags=re.S))
    if path.endswith(".json"):                                    # whisper verbose_json
        return "".join(s["text"] for s in json.loads(t)["segments"])
    t = re.sub(r"^這裡為您.*?\n\n", "", t, flags=re.S)          # 去掉模型的開場白
    return t

def norm(t):
    """去掉時間戳與講者前綴後，再做共用的正規化。"""
    t = re.sub(r"\[\d+:\d\d\]\s*", "", t)
    t = re.sub(r"(講者\d+|讲者\d+|spk:\d+|Speaker \d+)\s*[:：]\s*", "", t)
    return textutil.norm(t)

def main():
    files = dict(arg.split("=", 1) for arg in sys.argv[1:])
    texts = {k: norm(load(v)) for k, v in files.items()}
    names = list(texts)
    print("字數：", {k: len(v) for k, v in texts.items()})
    print("\n兩兩 CER（列＝參考稿、欄＝比對稿）：")
    print(" " * 12 + "".join(f"{n:>12s}" for n in names))
    for a in names:
        print(f"{a:>12s}" + "".join(f"{cer(texts[a], texts[b]):12.3f}" for b in names))


if __name__ == "__main__":
    main()
