"""兩個頁面的 JavaScript：語法正確，而且程式裡用 id 去找的元素都存在。"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
PAGES = ["index.html", "record.html"]


def page_and_script(page):
    html = (STATIC / page).read_text(encoding="utf-8")
    return html, "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))


@pytest.mark.parametrize("page", PAGES)
@pytest.mark.skipif(not shutil.which("node"), reason="需要 node")
def test_script_parses(page, tmp_path):
    js = tmp_path / "page.js"
    js.write_text(page_and_script(page)[1], encoding="utf-8")
    r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("page", PAGES)
def test_every_element_the_script_looks_up_exists(page):
    # 改版面時最容易漏的：元素改名或拿掉了，程式還在用 $('#舊名字')，要到瀏覽器按下去才會壞
    html, js = page_and_script(page)
    ids = set(re.findall(r"\$\('#([A-Za-z][\w-]*)'\)", js))
    assert ids, "沒有找到任何 $('#…')，檢查的寫法可能和頁面對不上了"
    assert sorted(i for i in ids if f'id="{i}"' not in html) == []


def test_pages_load_the_shared_stylesheet():
    for page in PAGES:
        assert '<link rel="stylesheet" href="/static/app.css">' in page_and_script(page)[0]
    assert (STATIC / "app.css").stat().st_size > 10_000
