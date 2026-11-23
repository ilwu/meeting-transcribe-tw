"""重建校對專案的不變量：不遺失使用者的修改；講者名稱跟著人走，不是跟著「講者A／B」的字母。"""
import json
import sys

import build_project
from helpers import read_project


def build(monkeypatch, mdir, parts, whisper=None):
    """照命令列的方式執行 build_project。parts 是 [(逐字稿檔, 位移秒數)]。"""
    argv = ["build_project.py", str(mdir), str(mdir / "audio" / "16k.m4a")]
    if whisper:
        argv += ["--whisper", str(whisper)]
    monkeypatch.setattr(sys, "argv", argv + [f"{f}:{off}" for f, off in parts])
    build_project.main()
    return read_project(mdir)


def save(mdir, project):
    (mdir / "project.json").write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")


def test_first_build(tmp_path, monkeypatch):
    part = tmp_path / "p1.txt"
    part.write_text("[00:00] 講者1：第一句話要先確認\n這一行格式不對會被略過\n[00:10] 講者2：第二句話是回答\n[00:25] 講者1：第三句話做結論\n",
                    encoding="utf-8")
    p = build(monkeypatch, tmp_path, [(part, 0)])

    segs = p["segments"]
    assert [s["id"] for s in segs] == [0, 1, 2]                      # id 等於位置，前端用 id 直接取那一行
    assert [(s["start"], s["end"]) for s in segs] == [(0, 10), (10, 25), (25, 35)]
    assert [s["speaker"] for s in segs] == ["P1-講者1", "P1-講者2", "P1-講者1"]
    assert all(s["text"] == s["text_orig"] and s["status"] == "pending" for s in segs)
    assert p["audio"] == "audio/16k.m4a"


def test_rebuild_keeps_user_work(tmp_path, monkeypatch):
    #====================================
    # 第一次建立
    #====================================
    part = tmp_path / "p1.txt"
    lines = ["[00:00] 講者1：第一句話要先確認", "[00:10] 講者2：第二句話是回答", "[00:25] 講者1：第三句話做結論"]
    part.write_text("\n".join(lines), encoding="utf-8")
    p = build(monkeypatch, tmp_path, [(part, 0)])

    #====================================
    # 使用者校對：改字、接受、清語助詞、錯字建議、改講者、取名
    #====================================
    s0, s1, s2 = p["segments"]
    s0.update(text="第一句話已經確認", status="edited", cleaned=True,
              fixes=[{"original": "確認", "suggestion": "確定", "reason": "", "status": "dismissed"}])
    s1.update(status="accepted", llm_suggestion="第二句是回答", llm_tried=True, speaker="P1-講者1")
    s2.update(text="第三句做結論", status="edited", cleaned=True)
    p["speakers"]["P1-講者1"].update(name="王小明", name_source="manual")
    p.update(learned=[{"original": "甲", "suggestion": "乙"}], name_suggestions=[{"speaker": "P1-講者2", "name": "Amy"}],
             speaker_purity={"P1-講者1": 0.9})
    save(tmp_path, p)

    #====================================
    # 重轉後第三句的原文變了，再重建
    #====================================
    part.write_text("\n".join(lines[:2] + ["[00:25] 講者1：第三句話重新轉出來不一樣"]), encoding="utf-8")
    p = build(monkeypatch, tmp_path, [(part, 0)])
    s0, s1, s2 = p["segments"]

    #====================================
    # 對得上的行保留所有修改
    #====================================
    assert (s0["text"], s0["status"], s0["text_orig"]) == ("第一句話已經確認", "edited", "第一句話要先確認")
    assert s0["cleaned"] is True and s0["fixes"][0]["status"] == "dismissed"
    assert (s1["status"], s1["llm_suggestion"], s1["llm_tried"]) == ("accepted", "第二句是回答", True)
    assert s1["speaker"] == "P1-講者1"                               # 使用者改過的講者
    assert p["speakers"]["P1-講者1"]["name"] == "王小明"
    assert p["learned"] and p["name_suggestions"] and p["speaker_purity"]

    #====================================
    # 原文變了的行是新的一行，不沿用舊的修改
    #====================================
    assert (s2["text"], s2["status"]) == ("第三句話重新轉出來不一樣", "pending")
    assert "cleaned" not in s2 and "fixes" not in s2


def test_names_follow_people_when_labels_swap(tmp_path, monkeypatch):
    #====================================
    # 兩段逐字稿，各段的講者編號彼此獨立
    #====================================
    p1, p2 = tmp_path / "p1.txt", tmp_path / "p2.txt"
    p1.write_text("[00:00] 講者1：甲先報告上週的進度\n[00:10] 講者2：乙提出一個問題\n"
                  "[00:15] 講者1：甲回答這個問題\n[00:25] 講者2：乙說明白了\n", encoding="utf-8")
    p2.write_text("[00:00] 講者1：第二段比較長的說明內容\n[00:40] 講者2：第二段最後的補充\n", encoding="utf-8")
    times = [(0, 10, "甲先報告上週的進度"), (10, 15, "乙提出一個問題"), (15, 25, "甲回答這個問題"), (25, 30, "乙說明白了"),
             (30, 70, "第二段比較長的說明內容"), (70, 80, "第二段最後的補充")]

    def whisper(name, who):
        f = tmp_path / name
        f.write_text(json.dumps({"segments": [{"start": a, "end": b, "text": t, "speaker": w} for (a, b, t), w in zip(times, who)]},
                                ensure_ascii=False), encoding="utf-8")
        return f

    #====================================
    # 第一次：whisper 認為第二段的長篇是甲講的，甲講最久所以是「講者A」
    #====================================
    p = build(monkeypatch, tmp_path, [(p1, 0), (p2, 30)], whisper("w1.json", [0, 1, 0, 1, 0, 1]))
    assert [s["speaker"] for s in p["segments"]] == ["講者A", "講者B", "講者A", "講者B", "講者A", "講者B"]

    #====================================
    # 使用者取名，並把最後一行改成甲講的
    #====================================
    p["speakers"]["講者A"].update(name="甲", name_source="manual")
    p["speakers"]["講者B"].update(name="乙", name_source="manual")
    p["segments"][5]["speaker"] = "講者A"
    save(tmp_path, p)

    #====================================
    # 重建：這次 whisper 認為第二段的長篇是乙講的，乙變成講最久的「講者A」，字母對調
    #====================================
    p = build(monkeypatch, tmp_path, [(p1, 0), (p2, 30)], whisper("w2.json", [0, 1, 0, 1, 1, 0]))
    name_of = lambda i: p["speakers"][p["segments"][i]["speaker"]]["name"]

    #====================================
    # 名字跟著原本那些行走
    #====================================
    assert p["segments"][0]["speaker"] == "講者B"                    # 甲的行現在標成講者B
    assert (name_of(0), name_of(2)) == ("甲", "甲")
    assert (name_of(1), name_of(3)) == ("乙", "乙")
    assert name_of(5) == "甲"                                        # 使用者指定給甲的那一行還是甲
