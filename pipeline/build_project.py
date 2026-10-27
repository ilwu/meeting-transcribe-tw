#!/usr/bin/env python3
"""把 Gemini 分段逐字稿（[mm:ss] 講者N：內容）與 whisper verbose_json 合成校對專案 JSON。
用法: python pipeline/build_project.py <會議資料夾> <audio> [--whisper file.json] part1.txt:0 part2.txt:1568 ...
輸出 <會議資料夾>/project.json，音訊路徑以相對於會議資料夾的方式儲存。
"""
import json, os, re, sys
from pathlib import Path
from textutil import norm, cer

LINE = re.compile(r"\[(\d+):(\d\d)\]\s*(講者\d+)：(.*)")


HALLUCINATION_PHRASES = ("字幕由", "Amara.org", "請不吝點贊", "點贊訂閱", "訂閱我的頻道", "感謝觀看", "謝謝觀看",
                         "優優獨播劇場", "明鏡與點點", "字幕提供", "中文字幕")


def whisper_hallucinations(w, min_repeat=3, min_len=4):
    """whisper 幻覺的段落索引：同一句連續出現 min_repeat 次以上（模型卡在迴圈，實測一次可連續 20 幾段），
    或含常見的影片字幕幻覺字串。很短的「對」「嗯」連續出現是正常附和，不算。"""
    #====================================
    # 連續重複的同一句
    #====================================
    bad, i = set(), 0
    while i < len(w):
        t = w[i]["text"].strip()
        j = i
        while j + 1 < len(w) and w[j + 1]["text"].strip() == t:
            j += 1
        if j - i + 1 >= min_repeat and len(t) >= min_len:
            bad.update(range(i, j + 1))
        i = j + 1

    #====================================
    # 常見幻覺字串
    #====================================
    bad.update(k for k, x in enumerate(w) if any(p in x["text"] for p in HALLUCINATION_PHRASES))
    return bad


def correct_times(segs, w, sec_per_char=0.22, max_shift=180):
    """用 whisper 的時間戳校正 Gemini 每行的開始時間（Gemini 偶爾整批標錯時間）。
    兩邊文字正規化後串成字元流做序列比對；whisper 每字的時間由所在片段線性內插。
    每行取前 20 字中對上的字（至少 3 字）推回行首時間；對不上的行依前後已校正行的位移內插。原始時間留在 start_gemini。"""
    import difflib
    wc, wt = [], []
    for x in w:
        t = norm(x["text"])
        for i, ch in enumerate(t):
            wc.append(ch); wt.append(x["start"] + (x["end"] - x["start"]) * i / len(t))
    gtexts = [norm(s["text"]) for s in segs]
    sm = difflib.SequenceMatcher(None, "".join(gtexts), "".join(wc), autojunk=False)
    gmap = {}
    for a, b, n in sm.get_matching_blocks():
        if n >= 2:                                   # 單字巧合不算
            for i in range(n):
                gmap[a + i] = wt[b + i]
    shift, pos = {}, 0
    for k, (s, t) in enumerate(zip(segs, gtexts)):
        hits = [(i, gmap[pos + i]) for i in range(min(len(t), 20)) if pos + i in gmap]
        if len(hits) >= 3:
            est = hits[0][1] - hits[0][0] * sec_per_char
            if abs(est - s["start"]) < max_shift:
                shift[k] = est - s["start"]
        pos += len(t)
    if not shift:
        return
    ks = sorted(shift)
    prev_t = 0.0
    for k, s in enumerate(segs):
        if k in shift:
            d = shift[k]
        else:                                        # 依前後錨點的位移線性內插
            p = max((a for a in ks if a < k), default=None); q = min((a for a in ks if a > k), default=None)
            if p is None or q is None:
                d = shift[q if p is None else p]
            else:
                d = shift[p] + (shift[q] - shift[p]) * (s["start_gemini"] - segs[p]["start_gemini"]) / max(1, segs[q]["start_gemini"] - segs[p]["start_gemini"])
        s["start"] = round(max(prev_t, s["start_gemini"] + d), 1)   # 保持時間單調不減
        prev_t = s["start"]
    big = sum(1 for d in shift.values() if abs(d) > 10)
    print(f"  時間校正：{len(shift)}/{len(segs)} 行有 whisper 錨點，其中 {big} 行位移超過 10 秒")


def align_speakers(segs, w, min_share=0.3):
    """跨段講者對齊。各段的 Gemini 講者編號彼此獨立，但 whisper 是整場一次做講者分離，編號全場一致。
    每段內把 Gemini 講者一對一配給重疊時間最多的 whisper 講者（佔比至少 min_share），
    配到同一個 whisper 講者的各段標籤合併成同一個「講者A/B/C…」（依總發言時間排序）。配不到的保留原標籤。"""
    from collections import defaultdict
    ov = defaultdict(lambda: defaultdict(float))
    for s in segs:
        for x in w:
            o = min(s["end"], x["end"]) - max(s["start"], x["start"])
            if o > 0 and x.get("speaker") is not None:
                ov[s["speaker_orig"]][x["speaker"]] += o
    to_w = {}
    for part in sorted({s["part"] for s in segs}):
        labels = [l for l in ov if l.startswith(f"P{part}-")]
        pairs = sorted(((v, l, k) for l in labels for k, v in ov[l].items()), reverse=True)
        used = set()
        for v, l, k in pairs:
            if l not in to_w and k not in used and v >= min_share * sum(ov[l].values()):
                to_w[l] = k; used.add(k)
    talk = defaultdict(float)
    for s in segs:
        if s["speaker_orig"] in to_w:
            talk[to_w[s["speaker_orig"]]] += s["end"] - s["start"]
    letter = {k: f"講者{chr(65 + i)}" for i, k in enumerate(sorted(talk, key=lambda k: -talk[k]))}
    speaker_map = {l: letter[k] for l, k in sorted(to_w.items())}
    for l in sorted(ov):
        share = (ov[l][to_w[l]] if l in to_w else max(ov[l].values())) / sum(ov[l].values())
        print(f"  {l} → {speaker_map.get(l, '（未對齊，保留原標籤）')}  (whisper 講者 {to_w.get(l, '-')}，重疊佔比 {share:.0%})")
    return speaker_map


def main():
    args = sys.argv[1:]
    mdir, audio = Path(args[0]), args[1]
    name = mdir.resolve().name
    whisper = None
    parts = []
    i = 2
    while i < len(args):
        if args[i] == "--whisper":
            whisper = args[i + 1]
            i += 2
            continue
        f, off = args[i].rsplit(":", 1)
        parts.append((f, float(off)))
        i += 1

    segs = []
    for pi, (f, off) in enumerate(parts, 1):
        for l in Path(f).read_text(encoding="utf-8").splitlines():
            m = LINE.match(l.strip())
            if m:
                segs.append({"start": int(m[1]) * 60 + int(m[2]) + off,
                             "speaker": f"P{pi}-{m[3]}", "text": m[4].strip(), "part": pi})
    segs.sort(key=lambda s: s["start"])
    for s in segs:
        s["start_gemini"] = s["start"]
    w = json.load(open(whisper, encoding="utf-8"))["segments"] if whisper else None
    w_text = None                                    # 去掉幻覺後的 whisper，只用於文字比對與時間校正
    if w:
        bad = whisper_hallucinations(w)
        w_text = [x for i, x in enumerate(w) if i not in bad]
        if bad:
            print(f"  whisper 幻覺：{len(bad)} 段不採用（連續重複或常見幻覺字串）")
        correct_times(segs, w_text)
    for k, s in enumerate(segs):
        s["id"] = k
        s["end"] = segs[k + 1]["start"] if k + 1 < len(segs) else s["start"] + 10
        s.update(text_orig=s["text"], status="pending", text_whisper="", flag="",
                 whisper_cer=None, llm_suggestion="")

    if w:
        for s in segs:
            ov = [x for x in w_text
                  if min(x["end"], s["end"]) - max(x["start"], s["start"]) > 0.3 * max(0.1, x["end"] - x["start"])]
            s["text_whisper"] = "".join(x["text"].strip() for x in ov)
            if s["text_whisper"]:
                c = cer(norm(s["text"]), norm(s["text_whisper"]))
                s["whisper_cer"] = round(c, 2)
                if c > 0.5 and len(norm(s["text"])) >= 6:
                    s["flag"] = "suspect"

    for s in segs:
        s["speaker_orig"] = s["speaker"]
    speaker_map = align_speakers(segs, w) if whisper and len(parts) > 1 else {}
    for s in segs:
        s["speaker"] = speaker_map.get(s["speaker_orig"], s["speaker_orig"])

    # 若專案已存在，保留使用者的修改（以 Gemini 原始時間 + text_orig 對應），以及講者名稱與合併結果。
    # 講者標籤（講者A/B…）在重建後可能換人，所以舊標籤要先換算成新標籤：
    # 以同一批行的「舊標籤 → 新標籤」多數決建立對照，名稱與使用者改過的講者都依對照搬過去。
    from collections import Counter, defaultdict
    old_path = mdir / "project.json"; kept = 0; names = {}
    if old_path.exists():
        old = json.loads(old_path.read_text(encoding="utf-8"))
        idx = {(o.get("start_gemini", o["start"]), o["text_orig"]): o for o in old["segments"]}
        pairs = [(s, idx.get((s["start_gemini"], s["text_orig"]))) for s in segs]
        pairs = [(s, o) for s, o in pairs if o]
        def user_changed(o, s):
            o_orig = o.get("speaker_orig", s["speaker_orig"])
            return o["speaker"] not in (o_orig, old.get("speaker_map", {}).get(o_orig))
        votes, votes_all = defaultdict(Counter), defaultdict(Counter)
        for s, o in pairs:
            votes_all[o["speaker"]][s["speaker"]] += 1
            if not user_changed(o, s):
                votes[o["speaker"]][s["speaker"]] += 1
        for ol, c in votes_all.items():          # 只出現在使用者改過的行（例如合併後的標籤）才用全部行投票
            votes.setdefault(ol, c)
        old_to_new = {ol: c.most_common(1)[0][0] for ol, c in votes.items()}
        for ol, info in old.get("speakers", {}).items():
            nl = old_to_new.get(ol, ol)
            if info.get("name") and (nl not in names or votes[ol][nl] > names[nl][1]):
                names[nl] = (info["name"], votes[ol][nl], info.get("name_source", ""), info.get("voice_score"))
        for s, o in pairs:
            for k in ("text", "status", "llm_suggestion", "llm_tried", "fixes", "cleaned"):
                if k in o:
                    s[k] = o[k]
            if user_changed(o, s):
                s["speaker"] = old_to_new.get(o["speaker"], o["speaker"])
            kept += 1
    speakers = sorted({s["speaker"] for s in segs})
    proj = {"name": name, "audio": Path(os.path.relpath(Path(audio).resolve(), mdir.resolve())).as_posix(),
            "speakers": {sp: {"name": n[0], "name_source": n[2], "voice_score": n[3]}
                         for sp in speakers for n in [names.get(sp, ("", 0, "", None))]},
            "speaker_map": speaker_map, "segments": segs}
    if old_path.exists():                            # 專案層級的學習紀錄（接受過的錯字、忽略的詞彙表建議）原樣保留
        for k in ("learned", "glossary_ignored", "name_suggestions", "speaker_purity"):
            if k in old:
                proj[k] = old[k]
    if kept: print(f"kept {kept} previously edited/labelled segments")
    mdir.mkdir(parents=True, exist_ok=True)
    old_path.write_text(json.dumps(proj, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{name}: {len(segs)} segments, {sum(1 for s in segs if s['flag'])} suspect, speakers={speakers}")


if __name__ == "__main__":
    main()
