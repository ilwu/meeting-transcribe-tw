"""校對工具的 API：只接受自己頁面的請求、會議清單、搜尋、改逐字稿、匯出、錯字建議、清語助詞。"""
import json

import pytest

import app
import recorder
from helpers import read_project, seg, write_meeting

ME, EVIL = "http://127.0.0.1:5077", "https://evil.example"
SAME = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}


#------------------------------------------------------------------------
# 只接受自己的頁面發來的請求
#------------------------------------------------------------------------

@pytest.fixture
def fake_recorder(monkeypatch):
    called = []
    monkeypatch.setattr(recorder, "start", lambda *a, **k: called.append("start") or {"ok": True})
    monkeypatch.setattr(recorder, "stop", lambda *a, **k: called.append("stop") or {"ok": True})
    return called


@pytest.mark.parametrize("label, method, url, base, kw", [
    ("自己開網址", "GET", "/", ME, {"headers": {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"}}),
    ("自己的頁面讀清單", "GET", "/api/projects/overview", ME, {"headers": SAME}),
    ("命令列工具（沒有瀏覽器標頭）", "GET", "/api/projects/overview", ME, {}),
    ("從別的網站點連結過來", "GET", "/record", ME, {"headers": {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}}),
    ("自己的頁面開始錄音", "POST", "/api/rec/start", ME, {"json": {"title": "x"}, "headers": {**SAME, "Origin": ME}}),
    ("沒有內容但有 JSON 標頭", "POST", "/api/rec/stop", ME, {"headers": {**SAME, "Origin": ME, "Content-Type": "application/json"}}),
    ("用 localhost 開的頁面", "POST", "/api/rec/stop", "http://localhost:5077", {"json": {}, "headers": {**SAME, "Origin": "http://localhost:5077"}}),
    ("測試站在別的埠", "GET", "/api/rec/status", "http://127.0.0.1:5078", {"headers": SAME}),
])
def test_own_page_is_allowed(client, fake_recorder, label, method, url, base, kw):
    assert client.open(url, method=method, base_url=base, **kw).status_code != 403, label


@pytest.mark.parametrize("label, method, url, base, kw", [
    ("外面的網域指到本機", "GET", "/api/projects/overview", "http://evil.example:5077", {}),
    ("別的網站用 fetch 讀清單", "GET", "/api/projects/overview", ME,
     {"headers": {"Origin": EVIL, "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"}}),
    ("別的網站把錄音嵌進去", "GET", "/audio/x", ME, {"headers": {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"}}),
    ("同一台電腦別的埠上的網頁", "GET", "/api/projects/overview", ME,
     {"headers": {"Origin": "http://127.0.0.1:3000", "Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "cors"}}),
    ("別的網站送純文字開始錄音", "POST", "/api/rec/start", ME,
     {"data": '{"title":"x","loopback":true}', "content_type": "text/plain",
      "headers": {"Origin": EVIL, "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"}}),
    ("別的網站用表單結束錄音", "POST", "/api/rec/stop", ME,
     {"data": {"a": "b"}, "headers": {"Origin": EVIL, "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}}),
    ("舊瀏覽器沒有來源標頭、送純文字", "POST", "/api/rec/start", ME, {"data": '{"title":"x"}', "content_type": "text/plain"}),
    ("來源是 null", "POST", "/api/rec/stop", ME, {"json": {}, "headers": {"Origin": "null"}}),
])
def test_other_origins_are_blocked(client, fake_recorder, label, method, url, base, kw):
    assert client.open(url, method=method, base_url=base, **kw).status_code == 403, label
    assert fake_recorder == [], "被擋的請求不能真的執行"


#------------------------------------------------------------------------
# 會議清單與搜尋
#------------------------------------------------------------------------

def test_status_works_before_any_meeting_exists(client, data_root, monkeypatch):
    # 全新安裝還沒有 meetings/：狀態 API 壞掉的話，「處理中」區塊和會議清單的自動更新都會不見
    monkeypatch.setattr(recorder, "MEETINGS", data_root / "還沒有這個資料夾")
    r = client.get("/api/rec/status")
    assert r.status_code == 200 and r.get_json()["pending"] == []


def test_overview_lists_newest_first_with_progress(client, data_root):
    #====================================
    # 準備
    #====================================
    write_meeting(data_root, "2026-01-05_週會", ["甲", {"text": "乙", "status": "accepted"}],
                  meta={"title": "週會", "date": "2026-01-05", "source": {"duration_sec": 600}, "silent_ranges": [[100, 400]]})
    write_meeting(data_root, "2026-01-09_2026-01-09-主管會議", ["丙"], meta={"title": "2026-01-09-主管會議", "date": "2026-01-09"})
    write_meeting(data_root, "2026-01-02-舊會議", ["丁"],
                  meta={"title": "舊會議", "date": "2026-01-02", "source": {"duration_sec": 900}, "silent_tail_from_sec": 60})
    write_meeting(data_root, "experiment", ["戊"])

    #====================================
    # 執行
    #====================================
    ov = client.get("/api/projects/overview").get_json()

    #====================================
    # 日期開頭的由新到舊，不是日期開頭的排最後
    #====================================
    assert [m["name"] for m in ov] == ["2026-01-09_2026-01-09-主管會議", "2026-01-05_週會", "2026-01-02-舊會議", "experiment"]

    #====================================
    # 標題、進度、無聲警告
    #====================================
    by = {m["name"]: m for m in ov}
    assert by["2026-01-09_2026-01-09-主管會議"]["title"] == "主管會議"       # 標題自己又帶日期時不重複顯示
    assert (by["2026-01-05_週會"]["done"], by["2026-01-05_週會"]["total"]) == (1, 2)
    assert by["2026-01-05_週會"]["silent"] == [[100, 400]]
    assert by["2026-01-02-舊會議"]["silent"] == [[60, 900]]                  # 舊資料只記了結尾無聲的起點
    assert by["experiment"]["title"] == "experiment" and by["experiment"]["silent"] == []


def test_search_needs_all_terms_and_ignores_case(client, data_root):
    write_meeting(data_root, "2026-01-05_週會", ["BPM 流程要送簽", "流程圖還沒畫", "bpm 的簽核流程"])
    write_meeting(data_root, "2026-01-09_主管會議", ["預算討論", "Bpm 流程改版"])

    r = client.get("/api/search", query_string={"q": "bpm 流程"}).get_json()
    assert r["total"] == 3
    assert [(x["project"], x["id"]) for x in r["results"]] == [("2026-01-09_主管會議", 1), ("2026-01-05_週會", 0), ("2026-01-05_週會", 2)]
    assert client.get("/api/search", query_string={"q": ""}).get_json()["results"] == []
    assert client.get("/api/search", query_string={"q": "不存在的詞"}).get_json()["total"] == 0


def test_search_sees_edits_immediately(client, data_root):
    # 搜尋結果依檔案的修改時間快取，改完字要馬上搜得到新的字
    d = write_meeting(data_root, "2026-01-05_週會", ["原本的字"])
    assert client.get("/api/search", query_string={"q": "原本"}).get_json()["total"] == 1
    client.post(f"/api/project/{d.name}/segment/0", json={"text": "改過的字"})
    assert client.get("/api/search", query_string={"q": "改過"}).get_json()["total"] == 1


#------------------------------------------------------------------------
# 改逐字稿、匯出
#------------------------------------------------------------------------

def test_editing_a_segment_tracks_status(client, data_root):
    d = write_meeting(data_root, "2026-01-05_週會", ["原本的字", {"text": "清過", "text_orig": "嗯，清過", "cleaned": True}])
    url = f"/api/project/{d.name}/segment"

    assert client.post(f"{url}/0", json={"text": "改過的字"}).get_json()["status"] == "edited"
    assert client.post(f"{url}/0", json={"status": "accepted"}).get_json()["status"] == "accepted"
    # 改回原文：不再算清過語助詞
    back = client.post(f"{url}/1", json={"text": "嗯，清過", "status": "pending"}).get_json()
    assert back["status"] == "pending" and "cleaned" not in back
    assert read_project(d)["segments"][0]["text"] == "改過的字"


def test_export_returns_what_it_writes(client, data_root):
    d = write_meeting(data_root, "2026-01-05_週會", [{"text": "改過", "text_orig": "原本", "status": "edited"}, "沒改"])
    r = client.post(f"/api/project/{d.name}/export", json={}).get_json()
    assert r["text"] == (d / "corrected.md").read_text(encoding="utf-8")
    assert "**[00:00] 講者A**：改過" in r["text"]
    assert (r["corrections"], r["done"], r["total"]) == (1, 1, 2)
    assert json.loads((d / "corrections.json").read_text(encoding="utf-8"))["corrections"][0]["orig"] == "原本"


def test_merging_speakers_moves_all_lines(client, data_root):
    d = write_meeting(data_root, "2026-01-05_週會", ["甲", {"text": "乙", "speaker": "講者B"}, {"text": "丙", "speaker": "講者B"}])
    speakers = client.post(f"/api/project/{d.name}/speakers", json={"op": "merge", "src": "講者B", "dst": "講者A"}).get_json()
    assert list(speakers) == ["講者A"]
    assert {s["speaker"] for s in read_project(d)["segments"]} == {"講者A"}


#------------------------------------------------------------------------
# 錯字建議
#------------------------------------------------------------------------

def fix(original, suggestion, status="pending"):
    return {"original": original, "suggestion": suggestion, "reason": "測試", "status": status}


def test_accepting_a_fix_everywhere(client, data_root):
    #====================================
    # 準備：同一組建議出現在三行，其中一行的原文已經被手動改掉
    #====================================
    d = write_meeting(data_root, "2026-01-05_週會", [
        {"text": "費用指引單要送簽", "fixes": [fix("指引單", "請領單")]},
        {"text": "指引單、指引單都要", "fixes": [fix("指引單", "請領單")]},
        {"text": "已經手動改好了", "fixes": [fix("指引單", "請領單")]},
    ])

    #====================================
    # 執行：全部接受
    #====================================
    r = client.post(f"/api/project/{d.name}/segment/0/fix", json={"index": 0, "action": "accept", "all": True}).get_json()

    #====================================
    # 有原文的行改掉並記進 learned，原文不在的標成 stale
    #====================================
    p = read_project(d)
    assert [s["text"] for s in p["segments"]] == ["費用請領單要送簽", "請領單、請領單都要", "已經手動改好了"]
    assert [s["fixes"][0]["status"] for s in p["segments"]] == ["accepted", "accepted", "stale"]
    assert [s["status"] for s in p["segments"]] == ["edited", "edited", "pending"]
    assert len(p["learned"]) == 2 and len(r["segments"]) == 3


def test_dismissing_a_fix_changes_nothing_else(client, data_root):
    d = write_meeting(data_root, "2026-01-05_週會", [{"text": "費用指引單", "fixes": [fix("指引單", "請領單")]}])
    client.post(f"/api/project/{d.name}/segment/0/fix", json={"index": 0, "action": "dismiss"})
    s = read_project(d)["segments"][0]
    assert (s["text"], s["status"], s["fixes"][0]["status"]) == ("費用指引單", "pending", "dismissed")


#------------------------------------------------------------------------
# 清語助詞
#------------------------------------------------------------------------

def test_fillers_only_touch_untouched_pending_rows_and_can_be_undone(client, data_root, monkeypatch):
    #====================================
    # 準備：模型回來之前，第 2 行被使用者改過、第 3 行已接受
    #====================================
    d = write_meeting(data_root, "2026-01-05_週會", [
        {"text": "嗯，要先送簽", "fixes": [fix("嗯，要", "嗯，需要")]},
        "啊這行等一下會被改",
        {"text": "呃已接受", "status": "accepted"},
        "沒有語助詞",
    ])

    def fake_clean(mdir, p):
        client.post(f"/api/project/{d.name}/segment/1", json={"text": "使用者剛改的"})      # 模擬模型執行期間的編輯
        return {0: ("嗯，要先送簽", "要先送簽"), 1: ("啊這行等一下會被改", "這行等一下會被改"), 2: ("呃已接受", "已接受")}
    monkeypatch.setattr(app.fillers_mod, "clean", fake_clean)

    #====================================
    # 清理：只套用到還沒處理、而且這段時間沒被動過的行
    #====================================
    r = client.post(f"/api/project/{d.name}/fillers", json={}).get_json()
    segs = read_project(d)["segments"]
    assert r["cleaned"] == 1
    assert (segs[0]["text"], segs[0]["cleaned"], segs[0]["status"]) == ("要先送簽", True, "pending")
    assert segs[0]["fixes"][0]["status"] == "stale"             # 錯字建議的原文片段被清掉了
    assert segs[1]["text"] == "使用者剛改的" and "cleaned" not in segs[1]
    assert segs[2]["text"] == "呃已接受"

    #====================================
    # 復原：改回原文
    #====================================
    u = client.post(f"/api/project/{d.name}/fillers/undo", json={}).get_json()
    segs = read_project(d)["segments"]
    assert u["undone"] == 1
    assert segs[0]["text"] == "嗯，要先送簽" and "cleaned" not in segs[0]


def test_undo_keeps_rows_the_user_edited_after_cleaning(client, data_root):
    d = write_meeting(data_root, "2026-01-05_週會", [{"text": "清過又改過", "text_orig": "嗯，清過", "cleaned": True, "status": "edited"}])
    assert client.post(f"/api/project/{d.name}/fillers/undo", json={}).get_json()["undone"] == 0
    assert read_project(d)["segments"][0]["text"] == "清過又改過"


#------------------------------------------------------------------------
# 會議紀錄
#------------------------------------------------------------------------

def test_notes_markdown_comes_from_notes_json(client, data_root):
    d = write_meeting(data_root, "2026-01-05_週會", ["甲"])
    assert client.get(f"/api/project/{d.name}/notes/md").status_code == 404
    (d / "notes.json").write_text(json.dumps({"title": "週會", "summary": "摘要", "_meta": {"template": "general"}}, ensure_ascii=False),
                                  encoding="utf-8")
    assert client.get(f"/api/project/{d.name}/notes/md").get_json()["text"].startswith("# 週會")
