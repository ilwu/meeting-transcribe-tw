#!/usr/bin/env python3
"""用 Gemini API 轉逐字稿的測試工具（inline audio, 無外部依賴）。
用法: python pipeline/transcribe_gemini.py <audio> <model> [prompt|-] [out_prefix]
"""
import base64, json, os, sys, time, urllib.request, urllib.error
from pathlib import Path

def load_env():
    p = Path(__file__).resolve().parent.parent / ".env"
    for line in p.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())

def call(model, body, key):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            return json.load(r), time.time() - t0
    except urllib.error.HTTPError as e:
        return {"error": json.loads(e.read().decode() or "{}"), "status": e.code}, time.time() - t0

def main():
    load_env(); key = os.environ["GEMINI_API_KEY"]
    audio, model = sys.argv[1], sys.argv[2]
    prompt = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "-" else None
    prefix = sys.argv[4] if len(sys.argv) > 4 else str(Path(audio).with_suffix("")) + f".{model}"
    mime = {"m4a": "audio/m4a", "mp3": "audio/mp3", "wav": "audio/wav", "flac": "audio/flac"}[Path(audio).suffix[1:]]
    if Path(audio).stat().st_size > 18_000_000 or os.environ.get("FORCE_UPLOAD"):      # 超過 inline 上限改走 Files API
        from gemini_transcribe import upload_file
        uri, fname = upload_file(audio, mime, key); print("uploaded:", fname)
        parts = [{"file_data": {"mime_type": mime, "file_uri": uri}}]
    else:
        data = base64.b64encode(Path(audio).read_bytes()).decode()
        parts = [{"inline_data": {"mime_type": mime, "data": data}}]
    if prompt: parts.append({"text": prompt})
    body = {"contents": [{"role": "user", "parts": parts}]}
    if os.environ.get("SAFETY_OFF"):
        body["safetySettings"] = [{"category": c, "threshold": "BLOCK_NONE"} for c in
            ("HARM_CATEGORY_HARASSMENT","HARM_CATEGORY_HATE_SPEECH","HARM_CATEGORY_SEXUALLY_EXPLICIT","HARM_CATEGORY_DANGEROUS_CONTENT","HARM_CATEGORY_CIVIC_INTEGRITY")]
    extra = os.environ.get("GEN_CONFIG")
    if extra: body["generationConfig"] = json.loads(extra)
    resp, dt = call(model, body, key)
    Path(prefix + ".json").write_text(json.dumps(resp, ensure_ascii=False, indent=1), encoding="utf-8")
    if "error" in resp:
        print(f"[{model}] ERROR {resp.get('status')} in {dt:.0f}s: {json.dumps(resp['error'], ensure_ascii=False)[:800]}"); return 1
    text = "".join(p.get("text", "") for c in resp.get("candidates", []) for p in c["content"].get("parts", []))
    Path(prefix + ".txt").write_text(text, encoding="utf-8")
    u = resp.get("usageMetadata", {})
    print(f"[{model}] ok in {dt:.0f}s  tokens in={u.get('promptTokenCount')} out={u.get('candidatesTokenCount')} "
          f"thinking={u.get('thoughtsTokenCount')}  chars={len(text)}  -> {prefix}.txt")
    print("--- first 1200 chars ---"); print(text[:1200])
    return 0

if __name__ == "__main__": sys.exit(main())
