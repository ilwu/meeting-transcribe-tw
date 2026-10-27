#!/usr/bin/env python3
"""移除 Markdown 檔案中的圖片。

支援移除:
  - Markdown 圖片語法: ![alt](url)  (含內嵌 base64 data URI)
  - 參考式圖片: ![alt][ref] 及其定義行
  - HTML <img> 標籤

用法:
  python remove_md_images.py input.md              # 就地修改, 原檔備份為 input.md.bak
  python remove_md_images.py input.md -o out.md    # 輸出到新檔, 不動原檔
  python remove_md_images.py input.md --no-backup  # 就地修改, 不留備份
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

# ![alt](url "title")
INLINE_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# ![alt][ref] 或 ![alt]
REF_IMG = re.compile(r"!\[[^\]]*\](?:\[[^\]]*\])?")
# <img ...> / <img ... />
HTML_IMG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
# 三個以上連續換行縮成兩個 (清掉移除圖片後留下的空行)
EXTRA_BLANK = re.compile(r"\n{3,}")


def remove_images(text: str) -> str:
    text = INLINE_IMG.sub("", text)
    text = HTML_IMG.sub("", text)
    text = REF_IMG.sub("", text)
    # 移除圖片後行尾殘留的空白
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
    text = EXTRA_BLANK.sub("\n\n", text)
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="移除 Markdown 檔案中的圖片")
    parser.add_argument("input", type=Path, help="輸入的 .md 檔案")
    parser.add_argument("-o", "--output", type=Path, default=None,
                        help="輸出檔案 (預設: 就地修改輸入檔)")
    parser.add_argument("--no-backup", action="store_true",
                        help="就地修改時不建立 .bak 備份")
    args = parser.parse_args()

    if not args.input.is_file():
        print(f"找不到檔案: {args.input}", file=sys.stderr)
        return 1

    original = args.input.read_text(encoding="utf-8")
    count = (len(INLINE_IMG.findall(original))
             + len(HTML_IMG.findall(original)))
    cleaned = remove_images(original)

    target = args.output or args.input
    if target == args.input and not args.no_backup:
        backup = args.input.with_suffix(args.input.suffix + ".bak")
        shutil.copy2(args.input, backup)
        print(f"原檔已備份: {backup}")

    target.write_text(cleaned, encoding="utf-8")
    print(f"已移除 {count} 張圖片 -> {target}")
    print(f"檔案大小: {len(original.encode('utf-8')):,} bytes"
          f" -> {len(cleaned.encode('utf-8')):,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
