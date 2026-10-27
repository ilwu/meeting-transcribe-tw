#!/usr/bin/env python3
"""錯字建議：請 Gemini 找出逐字稿中可能聽錯的字，只回傳「最短的原文片段 → 建議」，由程式驗證後交給使用者接受。
用法: python pipeline/correct.py meetings/<資料夾>   （只列出建議，不改 project.json；校對工具用 /api/project/<名>/fixes）

設計：LLM 整行改寫容易過度修飾，所以只要「哪一行、哪幾個字、改成什麼、為什麼」；
程式確認原文片段真的在那一行、改動夠小、不是只改標點或繁簡，不符合的丟掉；最後由使用者按接受才改。
"""
import json, os, re, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from transcribe_gemini import load_env  # noqa: E402
from run import gemini_text, glossary, log  # noqa: E402
from textutil import norm  # noqa: E402

CHUNK = 150                  # 一次送幾行：太多容易漏看，太少要呼叫很多次
MAX_ORIGINAL, MAX_SUGGESTION = 15, 20

PROMPT = """以下是一場會議的機器逐字稿（語音辨識結果），可能有聽錯的字：同音或近音字、專有名詞寫錯、人名寫錯。
請找出這些錯字，輸出 JSON：{{"fixes": [{{"segment": 行號, "original": "原文片段", "suggestion": "改成", "reason": "理由"}}]}}

規則：
1. 只改聽錯的字。不要潤飾、不要補字、不要刪字、不要改標點、不要把口語改成書面語、不要改繁簡。
2. original 必須逐字出現在該行裡，而且越短越好（通常 2 到 6 個字，只包含錯的部分和必要的上下文）。
3. 寧可漏掉也不要改錯：沒有把握的不要列。詞彙表裡的正確寫法、講者名稱可以當依據。
4. reason 用一句話說明（例如「同音字，依詞彙表應為成品繳庫單」）。
5. 沒有錯字就輸出 {{"fixes": []}}。JSON 以外不要任何文字。

會議：{title}
講者：{speakers}
詞彙表（正確寫法）：
{glossary}

逐字稿（[行號] 講者：內容）：
{lines}"""


def valid(fix, segs_by_id, lo, hi):
    """程式驗證：行號在這一批、原文片段真的在該行、改動夠小、不是只改標點或繁簡。"""
    try:
        sid = int(fix.get("segment"))
    except (TypeError, ValueError):
        return False
    o, s = str(fix.get("original", "")), str(fix.get("suggestion", ""))
    return (lo <= sid < hi and sid in segs_by_id and o and s and o != s
            and o in segs_by_id[sid]["text"]
            and len(o) <= MAX_ORIGINAL and len(s) <= MAX_SUGGESTION
            and norm(o) != norm(s))


def suggest(mdir, p=None, key=None):
    """回傳 {segment_id: [{"original", "suggestion", "reason"}]}，已驗證、已去重。"""
    #====================================
    # 準備會議資料與分批
    #====================================
    p = p or json.loads((mdir / "project.json").read_text(encoding="utf-8"))
    meta_path = mdir / "meeting.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    names = {k: (v.get("name") or k) for k, v in p.get("speakers", {}).items()}
    segs = p["segments"]
    segs_by_id = {s["id"]: s for s in segs}
    batches = [segs[i:i + CHUNK] for i in range(0, len(segs), CHUNK)]
    gl = glossary(mdir)
    if key is None:
        load_env(); key = os.environ["GEMINI_API_KEY"]

    #====================================
    # 每批送模型（並行）
    #====================================
    def one(batch):
        lines = "\n".join(f"[{s['id']}] {names.get(s['speaker'], s['speaker'])}：{s['text']}" for s in batch)
        prompt = PROMPT.format(title=meta.get("title", mdir.name), speakers="、".join(sorted(set(names.values()))),
                               glossary=gl, lines=lines)
        res = gemini_text([{"text": prompt}], key, as_json=True) or {}
        return batch[0]["id"], batch[-1]["id"] + 1, res.get("fixes", []) if isinstance(res, dict) else []

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(one, batches))

    #====================================
    # 驗證並去重
    #====================================
    out, dropped = {}, 0
    for lo, hi, fixes in results:
        for f in fixes:
            if not isinstance(f, dict) or not valid(f, segs_by_id, lo, hi):
                dropped += 1
                continue
            sid = int(f["segment"])
            item = {"original": f["original"], "suggestion": f["suggestion"], "reason": str(f.get("reason", ""))[:80]}
            if all((x["original"], x["suggestion"]) != (item["original"], item["suggestion"]) for x in out.get(sid, [])):
                out.setdefault(sid, []).append(item)
    log(f"錯字建議：{sum(len(v) for v in out.values())} 則（{len(out)} 行），驗證不通過丟掉 {dropped} 則")
    return out


def merge(p, found):
    """把新建議併進 project.json：舊的待處理建議換掉；接受過或忽略過的同一組（原文→建議）不再出現。"""
    n = 0
    for s in p["segments"]:
        old = s.get("fixes", [])
        decided = {(x["original"], x["suggestion"]) for x in old if x.get("status") in ("accepted", "dismissed", "stale")}
        keep = [x for x in old if x.get("status") != "pending"]
        new = [dict(x, status="pending") for x in found.get(s["id"], []) if (x["original"], x["suggestion"]) not in decided]
        if keep or new:
            s["fixes"] = keep + new
        elif "fixes" in s:
            del s["fixes"]
        n += len(new)
    return n


NAME_PROMPT = """以下是一場會議的逐字稿，講者用代號標示（例如 講者A）。請根據對話內容推測每個代號的真實姓名。
輸出 JSON：{{"suggestions": [{{"speaker": "代號", "name": "姓名", "reason": "根據", "time": "mm:ss"}}]}}

規則：
1. 只在有明確根據時才推測：被叫名字後下一句由他回應、自我介紹、主持人點名後由他發言、別人說「謝謝○○的報告」而前一段是他在報告。
2. 只有職稱或稱謂（例如「主管」「老師」「經理」）不算名字。
3. 同一個名字只能給一位講者；沒有把握的代號不要列。
4. time 填最能佐證的那一行行首時間；reason 一句話說明（例如「03:12 被叫 Amy 後下一句回答」）。
5. 下面的「可能出席的人」只是參考名單，不能只因為名單上有就指定給某位講者。
6. JSON 以外不要任何文字。

會議：{title}
可能出席的人：{candidates}

逐字稿（[mm:ss] 代號：內容）：
{lines}"""


def guess_speaker_names(mdir, p=None, key=None):
    """回傳 [{"speaker", "name", "reason", "time"}]：已驗證代號存在、名字長度合理、一名一人、不是已經取好的名字。"""
    #====================================
    # 準備逐字稿與參考名單
    #====================================
    p = p or json.loads((mdir / "project.json").read_text(encoding="utf-8"))
    meta_path = mdir / "meeting.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    labels = set(p.get("speakers", {}))
    lines = "\n".join(f"[{int(s['start']) // 60:02d}:{int(s['start']) % 60:02d}] {s['speaker']}：{s['text']}" for s in p["segments"])
    cand = []
    for line in glossary(mdir).splitlines():          # 詞彙表的人名、畫面上抓到的與會者姓名
        if "人名" in line or "與會者" in line:
            cand.append(line.split("）", 1)[-1].split("：", 1)[-1].strip())
    if key is None:
        load_env(); key = os.environ["GEMINI_API_KEY"]

    #====================================
    # 呼叫模型
    #====================================
    res = gemini_text([{"text": NAME_PROMPT.format(title=meta.get("title", mdir.name), candidates="；".join(cand) or "（無）",
                                                   lines=lines)}], key, as_json=True) or {}

    #====================================
    # 驗證：名字（或它的別名）要真的出現在證據時間前後 90 秒的逐字稿裡
    #====================================
    aliases = name_aliases(glossary(mdir))
    out, used, dropped = [], set(), 0
    for x in res.get("suggestions", []) if isinstance(res, dict) else []:
        sp, name = str(x.get("speaker", "")).strip(), str(x.get("name", "")).strip()
        current = (p["speakers"].get(sp) or {}).get("name", "")
        names = aliases.get(name, {name})
        m = re.fullmatch(r"\s*(\d+):(\d{1,2})\s*", str(x.get("time", "")))
        t = int(m.group(1)) * 60 + int(m.group(2)) if m else None
        near = "".join(s["text"] for s in p["segments"] if t is not None and abs(s["start"] - t) <= 90)
        ok = (sp in labels and 1 <= len(name) <= 30 and name not in used
              and current not in names                  # 已經取了同一人的名字（或別名）就不用再建議
              and any(n.lower() in near.lower() for n in names))
        if not ok:
            dropped += 1
            continue
        out.append({"speaker": sp, "name": name, "reason": str(x.get("reason", ""))[:100], "time": str(x.get("time", ""))})
        used.add(name)
    log(f"講者名字建議：{len(out)} 則（證據不符或重複丟掉 {dropped} 則）")
    return out


def name_aliases(gl):
    """從詞彙表抓人名別名：「Amy（常誤辨為 Emmy）」「王小明（英文名 Sam／Samuel；常誤辨為 黃小明）」→ 名字對應到所有寫法。"""
    out = {}
    for line in gl.splitlines():
        body = line.split("：", 1)[1] if "：" in line[:12] else line
        for name, paren in re.findall(r"([^、：（）\n]+)(?:（([^）]*)）)?", body):
            name = name.strip()
            if not name:
                continue
            words = {w for w in re.split(r"[/／；;、,，\s]+", re.sub(r"常誤辨為|英文名|常說", " ", paren or "")) if len(w) >= 2}
            out[name] = {name} | words
    return out


IGNORED_FILE = "glossary_ignored.txt"    # 放在 repo 根目錄，可能含人名，已列入 .gitignore


def glossary_suggestions(root, limit=12):
    """從所有會議接受過的錯字建議與講者名稱，找出還不在全域詞彙表、也沒被忽略的詞。
    回傳 [{"term", "variants": [聽錯的寫法], "count", "meetings", "kind"}]，依出現次數排序。"""
    #====================================
    # 讀取已有的詞彙表與忽略清單
    #====================================
    gl_path, ign_path = root / "glossary.txt", root / IGNORED_FILE
    known = gl_path.read_text(encoding="utf-8") if gl_path.exists() else ""
    ignored = set(ign_path.read_text(encoding="utf-8").split("\n")) if ign_path.exists() else set()

    #====================================
    # 彙整各會議的接受紀錄與講者名稱
    #====================================
    cand = {}
    for pj in (root / "meetings").glob("*/project.json"):
        p = json.loads(pj.read_text(encoding="utf-8"))
        for x in p.get("learned", []):
            c = cand.setdefault(x["suggestion"], {"term": x["suggestion"], "variants": [], "count": 0, "meetings": [], "kind": "錯字"})
            c["count"] += 1
            if x["original"] not in c["variants"]:
                c["variants"].append(x["original"])
            if pj.parent.name not in c["meetings"]:
                c["meetings"].append(pj.parent.name)
        for v in p.get("speakers", {}).values():
            n = (v.get("name") or "").strip()
            if n:
                c = cand.setdefault(n, {"term": n, "variants": [], "count": 0, "meetings": [], "kind": "講者名稱"})
                c["count"] += 1
                if pj.parent.name not in c["meetings"]:
                    c["meetings"].append(pj.parent.name)

    #====================================
    # 排除已在詞彙表、已忽略、太短的詞
    #====================================
    def in_meeting_glossaries(c):                    # 已經加進它出現過的每一場的本場詞彙表
        files = [root / "meetings" / m / "glossary.txt" for m in c["meetings"]]
        return all(f.exists() and c["term"] in f.read_text(encoding="utf-8") for f in files)
    out = [c for t, c in cand.items() if len(t) >= 2 and t not in known and t not in ignored and not in_meeting_glossaries(c)]
    return sorted(out, key=lambda c: (-c["count"], c["term"]))[:limit]


def add_glossary_line(path, term, variants=()):
    """在詞彙表檔尾加一行「正確寫法（常誤辨為 a/b）」；已經有這個詞就不重複加。"""
    path = Path(path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if term in text:
        return False
    line = term + (f"（常誤辨為 {'/'.join(variants)}）" if variants else "")
    path.write_text(text.rstrip("\n") + ("\n" if text.strip() else "") + line + "\n", encoding="utf-8")
    return True


def ignore_term(root, term):
    f = root / IGNORED_FILE
    text = f.read_text(encoding="utf-8") if f.exists() else ""
    if term not in text.split("\n"):
        f.write_text(text + term + "\n", encoding="utf-8")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    mdir = Path(sys.argv[1]).resolve()
    p = json.loads((mdir / "project.json").read_text(encoding="utf-8"))
    found = suggest(mdir, p)
    by_id = {s["id"]: s for s in p["segments"]}
    for sid in sorted(found):
        for f in found[sid]:
            print(f"[{sid}] {f['original']} → {f['suggestion']}（{f['reason']}）　原句：{by_id[sid]['text'][:50]}")


if __name__ == "__main__":
    main()
