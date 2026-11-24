"""聲紋：自動帶入名字的規則，以及不讓錯的樣本進資料庫。聲紋用手寫的向量，不需要模型與音檔。"""
import json
import math

import numpy as np
import pytest

import voiceprints as vp


def voice(deg):
    """和「甲」的聲音夾 deg 度的聲紋：0 度是甲、90 度是乙，相似度是夾角的 cos。"""
    return np.array([math.cos(math.radians(deg)), math.sin(math.radians(deg))])


def ref(meeting, label, deg):
    return {"meeting": meeting, "label": label, "secs": 60, "emb": [float(x) for x in voice(deg)]}


def prints(**degs):
    return {label: {"emb": voice(deg), "secs": 30, "purity": 0.9} for label, deg in degs.items()}


@pytest.fixture
def db(tmp_path, monkeypatch):
    """資料庫裡有甲（0 度）和乙（90 度），都是別場會議學來的。回傳寫入資料庫的函式。"""
    monkeypatch.setattr(vp, "DB", tmp_path / "voiceprints.json")

    def write(people=None, rejected=()):
        people = people if people is not None else {"甲": [ref("別場", "S1", 0)], "乙": [ref("別場", "S2", 90)]}
        vp.DB.write_text(json.dumps({"people": people, "rejected": list(rejected)}, ensure_ascii=False), encoding="utf-8")
    write()
    return write


@pytest.fixture
def meeting(tmp_path):
    d = tmp_path / "2026-01-05_週會"
    d.mkdir()
    return d


def project(*labels, **named):
    speakers = {l: {"name": ""} for l in labels}
    speakers.update({l: {"name": n, "name_source": "manual"} for l, n in named.items()})
    return {"speakers": speakers, "segments": []}


#------------------------------------------------------------------------
# 比對
#------------------------------------------------------------------------

def test_each_speaker_gets_only_their_best_match(db, meeting, monkeypatch):
    # B 最像甲（0.77），第二像乙（0.64）。甲已經配給更像的 A：B 不能退而被標成乙，而是提示可能和 A 是同一人
    monkeypatch.setattr(vp, "speaker_prints", lambda mdir, p: prints(A=5, B=40, C=80))
    found, purity = vp.match(meeting, project("A", "B", "C"))
    assert found["A"] == {"name": "甲", "score": 1.0}
    assert found["B"] == {"name": "甲", "score": 0.77, "same_as": "A"}
    assert found["C"] == {"name": "乙", "score": 0.98}
    assert purity == {"A": 0.9, "B": 0.9, "C": 0.9}


def test_match_skips_what_it_should_not_touch(db, meeting, monkeypatch):
    #====================================
    # 準備：A 已經由使用者取名為甲；C 被按過「不是乙」；D 的聲音太短
    #====================================
    db(rejected=[{"meeting": meeting.name, "label": "C", "name": "乙"}])
    all_prints = prints(A=0, B=5, C=85, D=88)
    all_prints["D"]["secs"] = 3
    monkeypatch.setattr(vp, "speaker_prints", lambda mdir, p: all_prints)

    #====================================
    # 執行
    #====================================
    found, _ = vp.match(meeting, project("B", "C", "D", A="甲"))

    #====================================
    # 檢查
    #====================================
    assert "A" not in found                                        # 使用者取的名字不重新比對
    assert found["B"] == {"name": "甲", "score": 1.0, "same_as": "A"}   # 甲在這場已經有人了
    assert "C" not in found and "D" not in found


def test_match_never_compares_against_the_same_meeting(db, meeting, monkeypatch):
    db(people={"甲": [ref(meeting.name, "A", 0)]})
    monkeypatch.setattr(vp, "speaker_prints", lambda mdir, p: prints(A=0))
    assert vp.match(meeting, project("A"))[0] == {}


#------------------------------------------------------------------------
# 寫進專案
#------------------------------------------------------------------------

def test_apply_match_fills_in_names_or_suggestions():
    #====================================
    # 準備：Z 是上次自動帶入、這次比對不到的
    #====================================
    p = project("A", "B", "C", "Z")
    p["speakers"]["Z"].update(name="舊名字", name_source="voiceprint", voice_score=0.8)
    p["name_suggestions"] = [{"speaker": "A", "name": "Amy", "reason": "AI 猜的", "time": "01:00"},
                             {"speaker": "A", "name": "過期", "reason": "上次的聲紋建議", "voiceprint": True}]
    found = {"A": {"name": "甲", "score": 0.9}, "B": {"name": "甲", "score": 0.77, "same_as": "A"}, "C": {"name": "乙", "score": 0.65}}

    #====================================
    # 執行
    #====================================
    vp.apply_match(p, found, {"A": 0.9})

    #====================================
    # 很像的直接帶入；可能是同一人的、不夠像的只列建議
    #====================================
    assert (p["speakers"]["A"]["name"], p["speakers"]["A"]["name_source"]) == ("甲", "voiceprint")
    assert p["speakers"]["B"]["name"] == "" and p["speakers"]["C"]["name"] == ""
    assert p["speakers"]["Z"]["name"] == ""                        # 這次比對不到，舊的自動名字清掉
    by = {(x["speaker"], x["name"]): x for x in p["name_suggestions"]}
    assert by[("B", "甲")]["merge_into"] == "A"
    assert "merge_into" not in by[("C", "乙")]
    assert ("A", "Amy") in by and ("A", "過期") not in by          # AI 猜的建議保留，舊的聲紋建議換掉
    assert vp.auto_count(found) == 1 and p["speaker_purity"] == {"A": 0.9}


#------------------------------------------------------------------------
# 取名時學聲紋
#------------------------------------------------------------------------

def people():
    return json.loads(vp.DB.read_text(encoding="utf-8"))["people"]


def test_enroll_refuses_a_voice_that_belongs_to_someone_else(db, meeting):
    # 這位講者的聲音幾乎就是甲，卻被取名為丙：學進去的話，之後甲會被自動標成丙
    learned, warning = vp.enroll(meeting, {}, "C", "丙", prints(C=5))
    assert not learned and "甲" in warning
    assert "丙" not in people()


def test_enroll_learns_a_normal_voice(db, meeting):
    learned, warning = vp.enroll(meeting, {}, "A", "甲", prints(A=30))
    assert learned and warning == ""
    assert [r["meeting"] for r in people()["甲"]] == ["別場", meeting.name]

    learned, _ = vp.enroll(meeting, {}, "N", "新同事", prints(N=45))       # 誰都不夠像的新人照學
    assert learned and "新同事" in people()


def test_enroll_skips_the_demo_meeting(db, meeting):
    #====================================
    # 準備：這場是內附的示範會議
    #====================================
    (meeting / "meeting.json").write_text('{"title": "產品週會（示範）", "demo": true}', encoding="utf-8")

    #====================================
    # 它的聲音是語音合成的，取了名也不學
    #====================================
    learned, warning = vp.enroll(meeting, {}, "N", "新同事", prints(N=45))
    assert not learned and "示範會議" in warning
    assert "新同事" not in people()

    #====================================
    # 加上記號之前就複製出去的那些沒有記號：資料夾名稱和內附的示範會議相同也算
    #====================================
    old_copy = meeting.parent / next((vp.ROOT / "demo").glob("*/project.json")).parent.name
    old_copy.mkdir()
    (old_copy / "meeting.json").write_text('{"title": "產品週會（示範）"}', encoding="utf-8")
    learned, warning = vp.enroll(old_copy, {}, "N", "新同事", prints(N=45))
    assert not learned and "示範會議" in warning
    assert "新同事" not in people()


def test_enroll_ignores_voices_that_are_too_short(db, meeting):
    short = prints(A=30)
    short["A"]["secs"] = 3
    assert vp.enroll(meeting, {}, "A", "甲", short) == (False, "")


def test_renaming_moves_the_voiceprint(db, meeting):
    vp.enroll(meeting, {}, "N", "打錯的名字", prints(N=45))
    vp.enroll(meeting, {}, "N", "正確的名字", prints(N=45))
    assert "打錯的名字" not in people() and "正確的名字" in people()
