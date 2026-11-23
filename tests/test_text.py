"""文字正規化與差異率、清語助詞、錯字建議的驗證與合併、會議紀錄的時間點對齊。"""
import pytest

import correct
import fillers
import notes
from helpers import seg
from textutil import cer, norm


#------------------------------------------------------------------------
# 正規化與差異率
#------------------------------------------------------------------------

def test_norm_ignores_script_punctuation_and_case():
    assert norm("費用請領單，OK！") == norm("费用请领单 ok")


def test_cer():
    assert cer("abcd", "abcd") == 0
    assert cer("abcd", "abxd") == 0.25
    assert cer("", "abc") == 1.0            # 沒有參考文字時當成全錯，可疑行比對才不會除以零


#------------------------------------------------------------------------
# 清語助詞：規則
#------------------------------------------------------------------------

@pytest.mark.parametrize("src, want", [
    ("嗯，我們先看上週的進度。", "我們先看上週的進度。"),
    ("好啊，那就這樣。", "好，那就這樣。"),
    ("我們，嗯，就是要確認。", "我們，就是要確認。"),
    ("對對對，沒錯。", "對，沒錯。"),
    ("對，對，對。", "對。"),
    ("針對對方的要求再討論。", "針對對方的要求再討論。"),     # 「對對」在詞裡面，不是附和
    ("嗯。", "嗯。"),                                         # 整行都是語助詞（回答）不動
    ("啊？", "啊？"),
    ("什麼啊？", "什麼？"),
    ("呃……這個欄位要改。", "這個欄位要改。"),
    ("欸你看這邊。", "你看這邊。"),
    ("PC507 嗯 要先送簽。", "PC507 要先送簽。"),
])
def test_clean_rules(src, want):
    assert fillers.clean_rules(src) == want


#------------------------------------------------------------------------
# 清語助詞：模型的結果只能是「刪掉贅詞」
#------------------------------------------------------------------------

@pytest.mark.parametrize("before, after, ok", [
    ("那個我覺得就是要先送簽", "我覺得要先送簽", True),
    ("我們我們先看畫面", "我們先看畫面", True),                       # 口吃
    ("上次講的那個... 那個補休", "上次講的那個補休", True),
    ("狀況是這樣子，就是說，比方說", "狀況是這樣子，比方說", True),
    ("你原...你原本的設定", "你原本的設定", True),
    ("修修改這個欄位", "修改這個欄位", True),
    ("我們沒有要改", "我們要改", False),                               # 刪掉「沒有」意思相反
    ("因為流程不同所以分開", "流程不同所以分開", False),
    ("主要的流程是這樣", "流程是這樣", False),
    ("我們先存檔然後送簽", "我們先存檔再送簽", False),                 # 改字
    ("我們先存檔", "我們先存檔，", False),                             # 加字
    ("PC507 要送簽", "要送簽", False),                                 # 英數字不能刪
    ("就是就是就是好", "好", False),                                   # 刪太多
    ("好", "", False),
])
def test_only_deletions(before, after, ok):
    assert fillers.only_deletions(before, after) is ok


def test_clean_applies_only_valid_model_output_to_pending_rows(monkeypatch, tmp_path):
    #====================================
    # 準備：一行已接受、三行未處理
    #====================================
    project = {"segments": [
        seg(0, "嗯，那個我覺得就是要先送簽。"),
        seg(1, "那個表單要先存檔，然後送簽。"),
        seg(2, "好啊，那就這樣。", status="accepted"),
        seg(3, "我們我們先看 PC507 的畫面。"),
    ]}
    sent = []

    def fake_model(parts, key, as_json=False):
        sent.append(parts[0]["text"])
        return {"lines": [
            {"segment": 0, "text": "我覺得要先送簽。"},
            {"segment": 1, "text": "表單要先存檔，再送簽。"},          # 改了字，不能採用
            {"segment": 3, "text": "我們先看 PC507 的畫面。"},
            {"segment": 99, "text": "不存在的行"},
        ]}
    monkeypatch.setattr(fillers, "gemini_text", fake_model)

    #====================================
    # 執行
    #====================================
    out = fillers.clean(tmp_path, project, key="test")

    #====================================
    # 已接受的行不送模型、不會被改
    #====================================
    assert "[2]" not in sent[0]
    assert 2 not in out

    #====================================
    # 只採用「只刪贅詞」的結果
    #====================================
    assert out[0] == ("嗯，那個我覺得就是要先送簽。", "我覺得要先送簽。")
    assert out[3][1] == "我們先看 PC507 的畫面。"
    assert 1 not in out                                   # 模型改了字，規則也沒東西可刪，這行不動


#------------------------------------------------------------------------
# 錯字建議
#------------------------------------------------------------------------

def test_fix_must_point_at_real_text_in_range():
    segs = {5: seg(5, "請填寫費用指引單再送出")}
    good = {"segment": 5, "original": "指引單", "suggestion": "請領單"}
    assert correct.valid(good, segs, 0, 10)
    assert not correct.valid({**good, "segment": 99}, segs, 0, 10)                 # 行號不存在
    assert not correct.valid(good, segs, 6, 10)                                    # 不在這一批
    assert not correct.valid({**good, "original": "繳庫單"}, segs, 0, 10)          # 原文片段不在那一行
    assert not correct.valid({**good, "original": "請", "suggestion": "请"}, segs, 0, 10)   # 只改繁簡
    assert not correct.valid({**good, "suggestion": "指引單"}, segs, 0, 10)        # 沒有改


def test_merge_replaces_pending_and_respects_decisions():
    #====================================
    # 準備：一則待處理、一則已忽略
    #====================================
    project = {"segments": [seg(0, "甲乙丙", fixes=[
        {"original": "甲", "suggestion": "子", "reason": "", "status": "pending"},
        {"original": "乙", "suggestion": "丑", "reason": "", "status": "dismissed"},
    ]), seg(1, "丁", fixes=[{"original": "丁", "suggestion": "卯", "reason": "", "status": "pending"}])]}
    found = {0: [{"original": "乙", "suggestion": "丑", "reason": "又建議一次"},
                 {"original": "丙", "suggestion": "寅", "reason": "新的"}]}

    #====================================
    # 執行
    #====================================
    n = correct.merge(project, found)

    #====================================
    # 忽略過的不再出現、舊的待處理換成新的
    #====================================
    assert n == 1
    assert [(f["original"], f["status"]) for f in project["segments"][0]["fixes"]] == [("乙", "dismissed"), ("丙", "pending")]
    assert "fixes" not in project["segments"][1]          # 這次沒有再找到，舊的待處理清掉


#------------------------------------------------------------------------
# 會議紀錄的時間點
#------------------------------------------------------------------------

def test_snap_times_aligns_to_existing_lines():
    starts = [0, 30, 95]
    obj = {"decisions": [{"time": "00:32", "text": "甲"}, {"time": "01:34", "text": "乙"}, {"time": "9:59:59", "text": "丙"}],
           "topics": [{"start": "00:29", "points": [{"time": "亂寫", "text": "丁"}]}]}
    out, bad = notes.snap_times(obj, starts, end_sec=120)
    assert out["decisions"][0]["time"] == "00:30"
    assert out["decisions"][1]["time"] == "01:35"          # 模型取整差 1～2 秒，算在下一行
    assert out["decisions"][2]["time"] == ""               # 超出會議長度
    assert out["topics"][0]["start"] == "00:30"
    assert out["topics"][0]["points"][0]["time"] == ""
    assert bad == 2


def test_notes_markdown_has_title_and_sections():
    md = notes.to_markdown({"title": "週會", "summary": "摘要文字", "decisions": [{"time": "00:30", "text": "照原計畫"}],
                            "_meta": {"template": "general", "edited_segments": 1, "total_segments": 2}})
    assert md.startswith("# 週會")
    assert "## 決議" in md and "- [00:30] 照原計畫" in md
