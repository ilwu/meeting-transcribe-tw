#!/usr/bin/env python3
"""gemini-3.5-transcribe 專用模型測試：verbatim + 講者分離 + 字級時間戳。
先試 generateContent + generationConfig；失敗則改走 Files API 上傳 + /v1beta/interactions。
用法: python pipeline/gemini_transcribe.py <audio> <out_prefix> [lang] [vocab.txt]
"""
import base64, json, os, sys, time, urllib.request, urllib.error
from pathlib import Path
BASE = "https://generativelanguage.googleapis.com"

def load_env():
    for line in (Path(__file__).resolve().parent.parent / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())

def http(method, url, key, body=None, headers=None, raw=None):
    h = {"x-goog-api-key": key}; h.update(headers or {})
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None and raw is None: h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()

def upload_file(path, mime, key):
    size = Path(path).stat().st_size
    st, hd, _ = http("POST", f"{BASE}/upload/v1beta/files", key, body={"file": {"display_name": Path(path).name}},
        headers={"X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                 "X-Goog-Upload-Header-Content-Length": str(size), "X-Goog-Upload-Header-Content-Type": mime})
    assert st == 200, f"upload start {st}"
    up = {k.lower(): v for k, v in hd.items()}["x-goog-upload-url"]
    st, _, out = http("POST", up, key, raw=Path(path).read_bytes(),
        headers={"Content-Length": str(size), "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"})
    assert st == 200, f"upload finalize {st} {out[:300]}"
    f = json.loads(out)["file"]
    while f.get("state") == "PROCESSING":
        time.sleep(2); f = json.loads(http("GET", f"{BASE}/v1beta/{f['name']}", key)[2])
    return f["uri"], f["name"]

def main():
    load_env(); key = os.environ["GEMINI_API_KEY"]
    audio, prefix = sys.argv[1], sys.argv[2]
    lang = sys.argv[3] if len(sys.argv) > 3 else "cmn-Hans-CN"
    vocab = [l.strip() for l in Path(sys.argv[4]).read_text(encoding="utf-8").splitlines() if l.strip()] if len(sys.argv) > 4 else None
    mime = "audio/m4a"
    tcfg = {"language_codes": [lang]}
    if vocab:
        tcfg["custom_vocabulary"] = vocab
        tcfg["mode"] = "smart" if os.environ.get("MODE") == "smart" else {"type": "verbatim"}
    else: tcfg["mode"] = {"type": "verbatim", "diarization_mode": "speaker", "timestamp_granularities": ["word"]}

    # 第一種做法：generateContent，inline 音訊加 generationConfig.transcriptionConfig
    data = base64.b64encode(Path(audio).read_bytes()).decode()
    body = {"contents": [{"role": "user", "parts": [{"inline_data": {"mime_type": mime, "data": data}}]}],
            "generationConfig": {"transcriptionConfig": tcfg}}
    t0 = time.time(); st, _, out = http("POST", f"{BASE}/v1beta/models/gemini-3.5-transcribe:generateContent", key, body=body)
    print(f"generateContent+config -> HTTP {st} in {time.time()-t0:.0f}s")
    if st != 200:
        print("  ", out.decode(errors="replace")[:400])
        # 第二種做法：Files API 上傳後走 interactions
        uri, fname = upload_file(audio, mime, key); print("uploaded:", fname)
        body = {"model": "gemini-3.5-transcribe", "input": [{"type": "audio", "uri": uri, "mime_type": mime}],
                "generation_config": {"transcription_config": tcfg}}
        t0 = time.time(); st, _, out = http("POST", f"{BASE}/v1beta/interactions", key, body=body)
        print(f"interactions -> HTTP {st} in {time.time()-t0:.0f}s")
    Path(prefix + ".json").write_bytes(out)
    if st != 200: print(out.decode(errors="replace")[:1500]); return 1
    d = json.loads(out)
    # 兩種回應格式都取出文字與字級標註
    text, words = "", []
    if "candidates" in d:
        for p in d["candidates"][0]["content"]["parts"]:
            at = p.get("audioTranscription") or {}
            text += at.get("text", "") or p.get("text", "")
            words += at.get("annotations", []) or at.get("words", []) or at.get("wordInfos", [])
    else:
        text = d.get("output_text") or d.get("outputText") or ""
        for s in d.get("steps", []):
            for c in s.get("content", []):
                text = text or c.get("text", "")
                words += c.get("annotations", [])
    Path(prefix + ".txt").write_text(text, encoding="utf-8")
    print(f"text chars={len(text)} word annotations={len(words)}")
    print("top-level keys:", list(d.keys()))
    if words:
        print("sample annotations:", json.dumps(words[:5], ensure_ascii=False))
        # 組出依講者分段的逐字稿
        lines, cur, buf, start = [], None, [], None
        def off(x): return float(str(x).rstrip("s")) if x is not None else 0.0
        for w in words:
            spk = w.get("speaker"); 
            if spk != cur and buf:
                lines.append(f"[{int(start//60):02d}:{int(start%60):02d}] {cur}: {''.join(buf)}"); buf = []
            if not buf: start = off(w.get("start_offset") or w.get("startOffset"))
            cur = spk; buf.append(w.get("text", ""))
        if buf: lines.append(f"[{int(start//60):02d}:{int(start%60):02d}] {cur}: {''.join(buf)}")
        Path(prefix + ".turns.txt").write_text("\n".join(lines), encoding="utf-8")
        print(f"speaker turns={len(lines)}, speakers={sorted(set(w.get('speaker') for w in words))}")
    print("--- first 600 chars ---"); print(text[:600])
    return 0

if __name__ == "__main__": sys.exit(main())
