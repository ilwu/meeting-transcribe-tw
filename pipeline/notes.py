#!/usr/bin/env python3
"""由校對專案產生會議紀錄：每一條都附逐字稿時間點，AI 推測的人名、名詞、期限列成「待確認」。
用法: python pipeline/notes.py meetings/<資料夾> [--template general|logic] [--answers answers.json]

- general：摘要、決議、待辦（負責人、期限）、未決問題、主題
- logic：業務邏輯（每個作業的觸發時機、執行者、輸入、系統動作、規則、例外、待確認），整理系統開發用的現行做法
- --answers：回答「待確認」後重新整理（{"題目": "回答", ...}），已回答的題目移除、時間點不變
輸出 <會議資料夾>/notes.json 與 notes.md。
"""
import json, os, re, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from transcribe_gemini import load_env  # noqa: E402
from run import gemini_text, glossary, log  # noqa: E402

MAX_REVIEW = 8

COMMON_RULES = """規則：
1. 只根據逐字稿與畫面紀錄的內容，不要補充會議中沒有講的事；講者的推測、不確定的說法要照實標明「待確認」。
2. 每一條都要附 time（mm:ss，取自逐字稿行首的時間；分鐘數可以超過 59，例如 75:20）。time 要填「具體講到這一條內容」的那一行——有細節、例子或規則的地方；
   會議開頭的議程、流程總覽只是帶過，除非這一條真的只在那裡出現，否則不要引用總覽的時間。使用者會點時間去聽原話，引用到概述就聽不到細節。
3. 名稱以詞彙表的寫法為準；人名用逐字稿中的講者名稱，不確定是誰時寫職稱或「待確認」。
4. 凡是你推測出來的人名、專有名詞寫法、負責人、期限、數字，都要另外列進 review_items（最多 {max_review} 題），每題附 time。
5. 輸出繁體中文（台灣用語），JSON 以外不要任何文字。"""

TEMPLATES = {
    "general": {
        "label": "一般會議",
        "schema": """{
 "title": "會議標題",
 "summary": "三到五句的摘要",
 "decisions": [{"text": "決議內容", "time": "mm:ss"}],
 "action_items": [{"owner": "負責人", "task": "要做的事", "due": "yyyy-MM-dd 或空字串", "time": "mm:ss"}],
 "open_questions": [{"text": "還沒有結論的問題", "time": "mm:ss"}],
 "topics": [{"title": "主題", "start": "mm:ss", "points": [{"text": "重點", "time": "mm:ss"}]}],
 "review_items": [{"question": "要使用者確認的事", "time": "mm:ss"}]
}""",
        "extra": "期限若是「下週三」這類說法，依會議日期換算成 yyyy-MM-dd，並列進 review_items。",
    },
    "logic": {
        "label": "業務邏輯",
        "schema": """{
 "title": "主題",
 "summary": "這場說明涵蓋的範圍（三到五句）",
 "processes": [{
   "name": "作業名稱", "code": "程式代碼（沒有就空字串）", "time": "mm:ss",
   "trigger": "觸發時機", "actors": "執行者（不同廠區不同時分開寫）",
   "inputs": "前置條件或輸入", "system_actions": "系統動作",
   "rules": [{"text": "業務規則", "time": "mm:ss"}],
   "exceptions": [{"text": "例外、現況問題或痛點", "time": "mm:ss"}],
   "open_points": [{"text": "會議中講到但還沒確認的事", "time": "mm:ss"}]
 }],
 "terms": [{"term": "名詞或代碼", "meaning": "意思", "time": "mm:ss"}],
 "review_items": [{"question": "要使用者確認的事", "time": "mm:ss"}]
}""",
        "extra": "依作業流程的先後排列 processes；同一個作業在不同時間被提到時合併成一筆，作業的 time 用詳細說明這個作業的那一行（不是開場總覽）。"
                 "現況的做法和未來想改的做法要分開寫清楚（未來的寫進 open_points 或 exceptions，不要寫成現行規則）。",
    },
}


def fmt(sec):
    sec = int(round(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def parse_time(s):
    m = re.fullmatch(r"\s*(\d+):(\d{1,2})(?::(\d{1,2}))?\s*", str(s or ""))
    if not m:
        return None
    if m.group(3):                                   # 模型偶爾回 h:mm:ss
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return int(m.group(1)) * 60 + int(m.group(2))


def build_transcript(p, timeline):
    """逐字稿一行一句「[mm:ss] 講者：文字」，畫面切換處插入「[mm:ss]（畫面）說明」。"""
    #====================================
    # 講者顯示名稱
    #====================================
    names = {k: (v.get("name") or k) for k, v in p.get("speakers", {}).items()}

    #====================================
    # 逐字稿與畫面依時間合併
    #====================================
    rows = [(s["start"], 1, f"[{fmt(s['start'])}] {names.get(s['speaker'], s['speaker'])}：{s['text']}") for s in p["segments"]]
    last = None
    for x in timeline:
        if x.get("same_as_prev") or x.get("desc") == last:
            continue
        rows.append((x["t"], 0, f"[{fmt(x['t'])}]（畫面）{x['desc']}"))
        last = x.get("desc")
    return "\n".join(r for _, _, r in sorted(rows))


def snap_times(obj, starts, end_sec):
    """把所有 time／start 欄位對齊到最近、不晚於它的逐字稿行；超出範圍或格式不對的改成空字串並計數。"""
    bad = 0

    def fix(v):
        nonlocal bad
        t = parse_time(v)
        if t is None or t > end_sec + 5:
            bad += 1
            return ""
        prev = [s for s in starts if s <= t + 2]     # 容許模型取整造成的 1～2 秒誤差
        return fmt(prev[-1] if prev else starts[0])

    def walk(o):
        if isinstance(o, dict):
            return {k: (fix(v) if k in ("time", "start") and isinstance(v, str) else walk(v)) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(x) for x in o]
        return o
    return walk(obj), bad


def generate(mdir, template="general", answers=None, previous=None):
    #====================================
    # 讀取校對專案、畫面時間軸、會議資料
    #====================================
    p = json.loads((mdir / "project.json").read_text(encoding="utf-8"))
    tl_path = mdir / "transcripts/screen_timeline.json"
    timeline = json.loads(tl_path.read_text(encoding="utf-8")) if tl_path.exists() else []
    meta_path = mdir / "meeting.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    tpl = TEMPLATES[template]

    #====================================
    # 組提示詞
    #====================================
    transcript = build_transcript(p, timeline)
    head = (f"會議：{meta.get('title', mdir.name)}（{meta.get('date', '')}）\n"
            f"講者：{'、'.join(sorted({(v.get('name') or k) for k, v in p.get('speakers', {}).items()}))}\n"
            f"詞彙表（正確寫法）：\n{glossary(mdir)}\n")
    if answers and previous:
        task = (f"以下是先前整理的{tpl['label']}紀錄，以及使用者對「待確認」的回答。請依回答修正紀錄："
                "已回答的題目從 review_items 移除，內容依回答更正，其他條目與時間點保持不變；仍不確定的可以保留或新增題目。\n\n"
                f"先前的紀錄：\n{json.dumps(previous, ensure_ascii=False)}\n\n使用者的回答：\n"
                + "\n".join(f"- 問：{q}\n  答：{a}" for q, a in answers.items()))
    else:
        task = f"請把以下會議逐字稿整理成{tpl['label']}紀錄。"
    prompt = (f"{task}\n\n輸出 JSON 格式：\n{tpl['schema']}\n\n{COMMON_RULES.format(max_review=MAX_REVIEW)}\n{tpl['extra']}\n\n"
              f"{head}\n逐字稿（行首是時間；「（畫面）」是螢幕分享的內容說明）：\n{transcript}")

    #====================================
    # 呼叫模型並驗證時間點
    #====================================
    t0 = time.time()
    load_env()
    notes = gemini_text([{"text": prompt}], os.environ["GEMINI_API_KEY"], as_json=True)
    if not isinstance(notes, dict):
        sys.exit("整理失敗：模型沒有回傳可用的 JSON")
    starts = sorted(s["start"] for s in p["segments"])
    notes, bad = snap_times(notes, starts, max(s["end"] for s in p["segments"]))
    notes["review_items"] = notes.get("review_items", [])[:MAX_REVIEW]
    notes["_meta"] = {"template": template, "generated": time.strftime("%Y-%m-%d %H:%M"),
                      "seconds": round(time.time() - t0), "invalid_times": bad,
                      "edited_segments": sum(1 for s in p["segments"] if s["status"] != "pending"),
                      "total_segments": len(p["segments"])}
    return notes


def item_line(x, key="text"):
    t = f"[{x['time']}] " if x.get("time") else ""
    return f"- {t}{x.get(key, '')}"


def to_markdown(n):
    #====================================
    # 標題與摘要
    #====================================
    m = n.get("_meta", {})
    out = [f"# {n.get('title', '')}", "",
           f"> {TEMPLATES[m.get('template', 'general')]['label']}紀錄，{m.get('generated', '')} 由 AI 整理；"
           f"逐字稿已校對 {m.get('edited_segments', 0)}/{m.get('total_segments', 0)} 行。方括號是逐字稿時間，可回校對工具聽原話。", "",
           "## 摘要", "", n.get("summary", ""), ""]

    #====================================
    # 依範本輸出各段
    #====================================
    def section(title, items, key="text"):
        if items:
            out.extend([f"## {title}", "", *[item_line(x, key) for x in items], ""])

    if m.get("template") == "logic":
        for pr in n.get("processes", []):
            code = f"{pr['code']} " if pr.get("code") else ""
            t = f"（[{pr['time']}]）" if pr.get("time") else ""
            out += [f"## {code}{pr.get('name', '')}{t}", ""]
            for label, k in (("觸發時機", "trigger"), ("執行者", "actors"), ("前置條件／輸入", "inputs"), ("系統動作", "system_actions")):
                if pr.get(k):
                    out.append(f"- **{label}**：{pr[k]}")
            for label, k in (("業務規則", "rules"), ("例外與現況問題", "exceptions"), ("待確認", "open_points")):
                if pr.get(k):
                    out += [f"- **{label}**", *["  " + item_line(x) for x in pr[k]]]
            out.append("")
        if n.get("terms"):
            out += ["## 名詞", "", "| 名詞 | 意思 | 出處 |", "|---|---|---|",
                    *[f"| {x.get('term', '')} | {x.get('meaning', '')} | {x.get('time', '')} |" for x in n["terms"]], ""]
    else:
        section("決議", n.get("decisions"))
        if n.get("action_items"):
            out += ["## 待辦", "", "| 負責人 | 事項 | 期限 | 出處 |", "|---|---|---|---|",
                    *[f"| {x.get('owner', '')} | {x.get('task', '')} | {x.get('due', '')} | {x.get('time', '')} |"
                      for x in n["action_items"]], ""]
        section("未決問題", n.get("open_questions"))
        for tp in n.get("topics", []):
            t = f"（[{tp['start']}]）" if tp.get("start") else ""
            out += [f"## {tp.get('title', '')}{t}", "", *[item_line(x) for x in tp.get("points", [])], ""]
    section("待確認（請回答後重新整理）", n.get("review_items"), key="question")
    return "\n".join(out).rstrip() + "\n"


def save(mdir, notes):
    (mdir / "notes.json").write_text(json.dumps(notes, ensure_ascii=False, indent=1), encoding="utf-8")
    (mdir / "notes.md").write_text(to_markdown(notes), encoding="utf-8")


def main():
    #====================================
    # 解析參數
    #====================================
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    mdir = Path(args[0]).resolve()
    if not (mdir / "project.json").exists():
        sys.exit(f"{mdir.name} 還沒有校對專案，請先執行 pipeline/run.py")
    answers = json.loads(Path(args[args.index("--answers") + 1]).read_text(encoding="utf-8")) if "--answers" in args else None
    previous = json.loads((mdir / "notes.json").read_text(encoding="utf-8")) if answers else None
    template = args[args.index("--template") + 1] if "--template" in args else (previous or {}).get("_meta", {}).get("template", "general")

    #====================================
    # 整理並存檔
    #====================================
    log(f"整理{TEMPLATES[template]['label']}紀錄：{mdir.name}")
    notes = generate(mdir, template, answers, previous)
    save(mdir, notes)
    m = notes["_meta"]
    log(f"完成：{m['seconds']}s，待確認 {len(notes.get('review_items', []))} 題，"
        f"無效時間點 {m['invalid_times']} 個 → notes.md")


if __name__ == "__main__":
    main()
