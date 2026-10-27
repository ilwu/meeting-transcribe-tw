#!/usr/bin/env python3
"""聲紋：替講者取名一次，之後的會議自動帶入名字。
用法: python pipeline/voiceprints.py --enroll-all          # 從所有會議已取名的講者建立聲紋
      python pipeline/voiceprints.py --match meetings/<資料夾>  # 對這場未命名的講者比對並寫入名字

- 引擎：sherpa-onnx + 3D-Speaker ERes2Net（中文，CPU）；聲紋是 192 個數字，不是錄音。
- 資料庫：repo 根目錄 voiceprints.json（含同事的聲音特徵，不進 git，刪掉這個檔即全部清除）。
- 相似度 ≥ 0.75 自動帶入、0.6～0.75 只列建議；每人最多 30 份、少於 8 秒不學：依 14 場會議的實驗（docs/design.md）。
- 每組聲紋只用「和平均相似度 ≥ 0.6」的段落，避免講者標籤混到別人時把別人的聲音一起學進去。
"""
import json, subprocess, sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MODEL = ROOT / "vendor/models/spk/3dspeaker_speech_eres2net_base_200k_sv_zh-cn_16k-common.onnx"
DB = ROOT / "voiceprints.json"
THRESHOLD = 0.6          # 列為建議的相似度門檻（按「套用」才改）
AUTO = 0.75              # 直接自動帶入的門檻：實測正確的多在 0.84 以上，帶錯的在 0.65～0.69
PURE = 0.6               # 段落和平均相似度低於這個就不用（多半是混到別人）
MIN_SECONDS = 8          # 少於這個秒數的聲紋不可靠，不學也不比對
MAX_PER_PERSON = 30
DUPLICATE = 0.95         # 和既有某份幾乎一樣就不重複存
_ext = None


def available():
    """有裝 sherpa-onnx 而且模型在，才提供聲紋功能。"""
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return False
    return MODEL.exists()


def _extractor():
    global _ext
    if _ext is None:
        import sherpa_onnx
        _ext = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(MODEL), num_threads=4))
    return _ext


def _embed(x):
    ext = _extractor()
    s = ext.create_stream(); s.accept_waveform(16000, x); s.input_finished()
    e = np.array(ext.compute(s))
    return e / np.linalg.norm(e)


def _decode(audio):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(audio), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16)


def speaker_prints(mdir, p, labels=None):
    """每個講者標籤一組聲紋：{label: {"emb", "secs", "purity"}}。purity 是各段與平均相似度的最低值（偏低＝可能混到別人）。"""
    #====================================
    # 解碼音訊
    #====================================
    wav = _decode(mdir / p["audio"])
    out = {}
    for label in labels or p.get("speakers", {}):
        #====================================
        # 挑這位講者最長的幾段（去頭尾 0.5 秒，避免抓到前後的人）
        #====================================
        segs = sorted([s for s in p["segments"] if s["speaker"] == label and s["end"] - s["start"] >= 3],
                      key=lambda s: -(s["end"] - s["start"]))
        embs, secs = [], []
        for s in segs[:8]:
            a, b = int((s["start"] + 0.5) * 16000), int(min(s["end"] - 0.5, s["start"] + 15) * 16000)
            if b - a >= 2 * 16000:
                embs.append(_embed(wav[a:b].astype(np.float32) / 32768))
                secs.append((b - a) / 16000)
        if not embs:
            continue

        #====================================
        # 剔除不像這個人的段落，再平均
        #====================================
        mean = np.mean(embs, axis=0); mean /= np.linalg.norm(mean)
        sims = [float(e @ mean) for e in embs]
        keep = [i for i, c in enumerate(sims) if c >= PURE]
        if keep:
            mean = np.mean([embs[i] for i in keep], axis=0); mean /= np.linalg.norm(mean)
        out[label] = {"emb": mean, "secs": round(sum(secs[i] for i in keep)), "purity": round(min(sims), 2)}
    return out


def load_db():
    return json.loads(DB.read_text(encoding="utf-8")) if DB.exists() else {"people": {}, "rejected": []}


def save_db(db):
    DB.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")


def enroll(mdir, p, label, name, prints=None):
    """把這場這位講者的聲紋記到 name 底下（同一場同一標籤先前記在別人名下的會移掉，因為使用者改了名）。
    回傳 (是否有學到, 警告)；聲音明顯更像另一個已知的人時不學，警告說明像誰。"""
    #====================================
    # 算聲紋，太短不學
    #====================================
    pr = (prints or speaker_prints(mdir, p, [label])).get(label)
    db = load_db()
    for person in db["people"].values():
        person[:] = [x for x in person if not (x["meeting"] == mdir.name and x["label"] == label)]
    db["people"] = {k: v for k, v in db["people"].items() if v}
    if not name or not pr or pr["secs"] < MIN_SECONDS:
        save_db(db)
        return False, ""

    #====================================
    # 檢查是不是更像別人
    #====================================
    # 錯的樣本比門檻更危險：A 的聲音存在 B 名下，之後 A 被切成兩位講者時，其中一位會被很有把握地
    # 自動標成 B（依據見 docs/design.md），所以寧可不學
    def best(refs):
        refs = [x for x in refs if x["meeting"] != mdir.name]
        return max((float(np.array(x["emb"]) @ pr["emb"]) for x in refs), default=0.0)
    own = best(db["people"].get(name, []))
    other, other_score = max(((n, best(r)) for n, r in db["people"].items() if n != name), key=lambda x: x[1], default=("", 0.0))
    if other_score >= AUTO and other_score > own:
        save_db(db)
        return False, (f"這位講者的聲音很像 {other}（相似度 {other_score:.2f}），沒有記成 {name} 的聲紋；"
                       "可能混到別人的聲音，或轉錄時把人分錯了")

    #====================================
    # 加入（和既有的幾乎一樣就不重複存，超過上限丟掉最舊的）
    #====================================
    person = db["people"].setdefault(name, [])
    if all(float(np.array(x["emb"]) @ pr["emb"]) < DUPLICATE for x in person):
        person.append({"meeting": mdir.name, "label": label, "secs": pr["secs"], "emb": [round(float(v), 5) for v in pr["emb"]]})
        del person[:-MAX_PER_PERSON]
    save_db(db)
    return True, ""


def match(mdir, p):
    """對這場還沒取名（或之前自動帶入）的講者找最像的已知的人。
    回傳 ({label: {"name", "score"}}, {label: purity})；一人一場只對一位講者，被按過「不是他」的組合不再帶入。"""
    #====================================
    # 算這場每位講者的聲紋
    #====================================
    db = load_db()
    prints = speaker_prints(mdir, p)
    rejected = {(x["meeting"], x["label"], x["name"]) for x in db.get("rejected", [])}
    todo = [l for l, v in p.get("speakers", {}).items() if not v.get("name") or v.get("name_source") == "voiceprint"]

    #====================================
    # 每對（講者, 已知的人）取該人各份聲紋的最高相似度（不跟這場自己的聲紋比）
    #====================================
    cand = []
    for label in todo:
        pr = prints.get(label)
        if not pr or pr["secs"] < MIN_SECONDS:
            continue
        for name, refs in db["people"].items():
            refs = [x for x in refs if x["meeting"] != mdir.name]
            if refs and (mdir.name, label, name) not in rejected:
                score = max(float(np.array(x["emb"]) @ pr["emb"]) for x in refs)
                if score >= THRESHOLD:
                    cand.append((score, label, name))

    #====================================
    # 由高到低分配，每位講者只考慮自己最像的那個人
    #====================================
    # 最像的人已經是同場另一位講者時，多半是同一人被分成兩位：建議合併，不退而改配第二像的人
    # （退而求其次曾把使用者自己整段標成別人）
    top = {}
    for score, label, name in cand:
        if score > top.get(label, (0, ""))[0]:
            top[label] = (score, name)
    taken = {v["name"]: l for l, v in p["speakers"].items() if v.get("name") and v.get("name_source") != "voiceprint"}
    out = {}
    for score, label, name in sorted(cand, reverse=True):
        if label in out or top[label][1] != name:
            continue
        if name in taken:
            out[label] = {"name": name, "score": round(score, 2), "same_as": taken[name]}
        else:
            out[label] = {"name": name, "score": round(score, 2)}
            taken[name] = label
    return out, {l: v["purity"] for l, v in prints.items()}


def auto_count(found):
    """會直接帶入名字的講者數（同一人被分成兩位的只列建議，不算）。"""
    return sum(1 for m in found.values() if m["score"] >= AUTO and not m.get("same_as"))


def apply_match(p, found, purity):
    """把比對結果寫進專案：≥ AUTO 直接帶入（name_source=voiceprint，畫面標「自動」）；
    THRESHOLD～AUTO 只加進 name_suggestions（和 AI 猜名字同一個流程，按套用才改）；比對不到的舊自動名字清掉。"""
    #====================================
    # 清掉上次自動帶入、這次沒比對到的
    #====================================
    for label, v in p["speakers"].items():
        if v.get("name_source") == "voiceprint" and (label not in found or found[label]["score"] < AUTO):
            v.update(name="", name_source="", voice_score=None)

    #====================================
    # 自動帶入或列為建議
    #====================================
    sugg = [x for x in p.get("name_suggestions", []) if not x.get("voiceprint")]
    for label, m in found.items():
        if m.get("same_as"):
            other = p["speakers"].get(m["same_as"], {}).get("name") or m["same_as"]
            sugg.append({"speaker": label, "name": m["name"], "merge_into": m["same_as"], "time": "", "voiceprint": True,
                         "reason": f"聲音和 {other}（{m['same_as']}）一樣最像 {m['name']}（相似度 {m['score']}），"
                                   "可能是同一人被分成兩位"})
        elif m["score"] >= AUTO:
            p["speakers"][label].update(name=m["name"], name_source="voiceprint", voice_score=m["score"])
        else:
            sugg.append({"speaker": label, "name": m["name"], "reason": f"聲紋相似度 {m['score']}（不夠高，請確認）",
                         "time": "", "voiceprint": True})
    p["name_suggestions"] = sugg
    p["speaker_purity"] = purity
    return p


def reject(mdir, label, name):
    """「不是他」：記下這場這位講者不是 name，之後不再自動帶入。"""
    db = load_db()
    db.setdefault("rejected", []).append({"meeting": mdir.name, "label": label, "name": name})
    save_db(db)


def main():
    from bundled import use_bundled_ffmpeg
    use_bundled_ffmpeg()
    args = sys.argv[1:]
    if not available():
        sys.exit("需要 pip install sherpa-onnx，並把模型放在 " + str(MODEL))
    if "--enroll-all" in args:
        n = 0
        for pj in sorted((ROOT / "meetings").glob("*/project.json")):
            p = json.loads(pj.read_text(encoding="utf-8"))
            named = {l: v["name"] for l, v in p.get("speakers", {}).items()
                     if v.get("name") and v.get("name_source") != "voiceprint"}
            if not named or not (pj.parent / p["audio"]).exists():
                continue
            prints = speaker_prints(pj.parent, p, list(named))
            for label, name in named.items():
                ok, warn = enroll(pj.parent, p, label, name, prints)
                n += ok
                pur = prints.get(label, {}).get("purity")
                print(f"{pj.parent.name[:28]:28s} {label:9s} {name:8s} {'已學' if ok else '略過：' + (warn or '太短')}"
                      f"{'  ← 可能混到別人（一致性 %.2f）' % pur if pur is not None and pur < PURE else ''}")
        db = load_db()
        print(f"\n共學 {n} 組；資料庫 {len(db['people'])} 人：" + "、".join(f"{k}（{len(v)}）" for k, v in db["people"].items()))
    elif "--match" in args:
        mdir = Path(args[args.index("--match") + 1]).resolve()
        pj = mdir / "project.json"
        p = json.loads(pj.read_text(encoding="utf-8"))
        found, purity = match(mdir, p)
        apply_match(p, found, purity)
        pj.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
        print("聲紋帶入：" + ("、".join(f"{l} → {m['name']}（{m['score']}）" + (f"，和 {m['same_as']} 可能是同一人" if m.get("same_as") else "")
                                      for l, m in found.items()) or "沒有比對到已知的人"))


if __name__ == "__main__":
    main()
