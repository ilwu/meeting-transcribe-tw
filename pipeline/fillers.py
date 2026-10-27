#!/usr/bin/env python3
"""清語助詞：先用規則刪掉純語助詞（嗯、呃、啊…），再請 Gemini 刪掉要看上下文的贅詞（那個、就是、然後…）。
用法: python pipeline/fillers.py meetings/<資料夾>   （只列出會怎麼改，不改 project.json；校對工具用 /api/project/<名>/fillers）

設計：只動還沒處理的行；模型只能刪字，程式確認清完的結果是原文刪掉幾個字（不能加字、不能改字、
刪的量有上限、英數字不能刪），不符合的整行不採用。原文留在 text_orig，可以一鍵復原。
"""
import json, os, re, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from transcribe_gemini import load_env  # noqa: E402
from run import gemini_text, log  # noqa: E402

CHUNK = 150                  # 一次送幾行，和找錯字一樣
MAX_REMOVED = 0.4            # 一行最多刪掉幾成：刪更多多半是模型在改寫，不是清贅詞

PURE = "嗯呃欸喔蛤唉啊"       # 幾乎不會有實際意思的語助詞，規則直接刪
PUNCT = "，、。？！…"

PROMPT = """以下是一場會議的機器逐字稿。請刪掉口語贅詞，讓逐字稿好讀，但不能改變意思。

可以刪的：
- 當作停頓、沒有實際意思的「那個」「就是」「就是說」「然後」「這樣子」「的話」「對」「好」「所以說」。
- 口吃或重複：例如「我們我們」「這個這個」「對對對」只留一個。
- 刪完多出來的逗號。

不能刪的（有意思）：
- 指稱用的「那個」（例如「那個表單」「那個欄位」）、表示順序的「然後」（例如「先存檔，然後送簽」）、
  表示同意或回答的「對」「好」（例如問「這樣可以嗎？」答「對」）、條件句的「的話」（例如「沒有的話就退回」）。
- 正常的疊詞（例如「研究研究」「看看」「常常」）。
- 任何專有名詞、數字、英文、代碼。

規則：
1. 只能刪字，不能加字、不能改字、不能換順序、不能改標點（只能刪掉多出來的逗號）。
2. 沒把握的不要刪。沒有要刪的行不要列出來。
3. 輸出 JSON：{{"lines": [{{"segment": 行號, "text": "刪完的整行"}}]}}，JSON 以外不要任何文字。

逐字稿（[行號] 內容）：
{lines}"""


def clean_rules(text):
    """純語助詞直接刪：在句首或停頓後的連同後面的逗號一起刪；整行都是語助詞（例如回答「嗯。」）就不動。"""
    s = re.sub(f"(^|[{PUNCT}])[{PURE}]+[，、…]*", r"\1", text)   # 句首、停頓後：「嗯，我們」→「我們」
    s = re.sub(f"[{PURE}]+", "", s)                                # 句中、句尾：「好啊，」→「好，」
    # 「對對對」→「對」；要前後都是停頓，不然「針對對方」會被吃掉一個字
    s = re.sub(f"(^|[{PUNCT}])對(?:[，、]?對)+(?=[{PUNCT}]|$)", r"\1對", s)
    s = re.sub(f"[，、]+(?=[{PUNCT}])", "", s)                      # 刪完留下的連續標點
    s = re.sub(" {2,}", " ", s)
    s = re.sub(f"^[{PUNCT}\\s]+", "", s).strip()
    return s if re.sub(f"[{PUNCT}\\s]", "", s) else text


FILLER_WORDS = ("就是說", "所以說", "這樣子", "那個", "就是", "然後", "的話", "對", "好", "那") + tuple(PURE)
SKIP = PUNCT + " .…"


def _only_fillers(core):
    """core 能不能完全由贅詞拼成（例如「就是說那個」）。"""
    while core:
        w = next((w for w in FILLER_WORDS if core.startswith(w)), None)
        if not w:
            return False
        core = core[len(w):]
    return True


def only_deletions(before, after):
    """after 是不是 before 只刪掉贅詞：順序不變、沒加沒改字、刪的量在上限內，
    而且刪掉的每一段都是贅詞清單裡的詞、標點，或和緊鄰的字重複（口吃）。
    只檢查「只刪不改」不夠：實測模型會刪掉「沒有」「因為」這類有意思的字，意思可能整個相反。"""
    words = lambda t: sum(1 for c in t if c not in SKIP)   # 比例只算文字：短句刪掉「...」不該算成刪很多
    if not after or len(after) >= len(before) or words(before) - words(after) > words(before) * MAX_REMOVED:
        return False
    #====================================
    # 對齊：找出被刪掉的每一段（連續的字）
    #====================================
    spans, i, start = [], 0, None
    for k, ch in enumerate(before):
        if i < len(after) and ch == after[i]:
            i += 1
            if start is not None:
                spans.append((start, k)); start = None
        elif start is None:
            start = k
    if start is not None:
        spans.append((start, len(before)))
    if i != len(after):
        return False
    #====================================
    # 每一段都要是贅詞、標點或重複
    #====================================
    for a, b in spans:
        core = "".join(c for c in before[a:b] if c not in SKIP)
        if any(c.isascii() and c.isalnum() for c in core):
            return False
        nxt, prev = before[b:].lstrip(SKIP), before[:a].rstrip(SKIP)
        if core and not (_only_fillers(core) or nxt.startswith(core) or prev.endswith(core)):
            return False
    return True


def clean(mdir, p=None, key=None):
    """回傳 {segment_id: (清之前的文字, 清完的文字)}，只含還沒處理、而且有變的行。"""
    #====================================
    # 挑出還沒處理的行，先用規則清
    #====================================
    p = p or json.loads((mdir / "project.json").read_text(encoding="utf-8"))
    todo = [s for s in p["segments"] if s["status"] == "pending"]
    ruled = {s["id"]: clean_rules(s["text"]) for s in todo}
    if key is None:
        load_env(); key = os.environ["GEMINI_API_KEY"]

    #====================================
    # 要看上下文的贅詞交給模型（並行分批）
    #====================================
    def one(batch):
        lines = "\n".join(f"[{s['id']}] {ruled[s['id']]}" for s in batch)
        res = gemini_text([{"text": PROMPT.format(lines=lines)}], key, as_json=True) or {}
        return res.get("lines", []) if isinstance(res, dict) else []

    batches = [todo[i:i + CHUNK] for i in range(0, len(todo), CHUNK)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = [x for r in pool.map(one, batches) for x in r]

    #====================================
    # 驗證模型的結果：只刪不改，不符合就保留規則清完的版本
    #====================================
    final, dropped = dict(ruled), 0
    for x in results:
        try:
            sid, after = int(x.get("segment")), str(x.get("text", "")).strip()
        except (AttributeError, TypeError, ValueError):
            dropped += 1
            continue
        if sid in final and after != final[sid]:
            if only_deletions(final[sid], after):
                final[sid] = after
            else:
                dropped += 1

    #====================================
    # 只回傳有變的行
    #====================================
    before = {s["id"]: s["text"] for s in todo}
    out = {sid: (before[sid], t) for sid, t in final.items() if t != before[sid]}
    rule_n = sum(1 for sid, t in ruled.items() if t != before[sid])
    log(f"清語助詞：{len(out)} 行有清（規則 {rule_n} 行），模型結果驗證不通過丟掉 {dropped} 則")
    return out


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    mdir = Path(sys.argv[1]).resolve()
    for sid, (a, b) in sorted(clean(mdir).items()):
        print(f"[{sid}] {a}\n   → {b}")


if __name__ == "__main__":
    main()
