"""用 /v1beta/interactions 送音訊給一般模型（測試是否避開 generateContent 的 OTHER 擋下）。
用法: python gemini_interactions.py <audio> <model> <prompt> <out_prefix>"""
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline"))  # 共用 pipeline 的 Gemini 函式
from gemini_transcribe import load_env, http, upload_file, BASE
load_env(); key=os.environ["GEMINI_API_KEY"]
audio, model, prompt, prefix = sys.argv[1:5]
uri, fname = upload_file(audio, "audio/m4a", key)
body={"model": model, "input":[{"type":"audio","uri":uri,"mime_type":"audio/m4a"},{"type":"text","text":prompt}]}
t0=time.time(); st,_,out=http("POST", f"{BASE}/v1beta/interactions", key, body=body); dt=time.time()-t0
Path(prefix+".json").write_bytes(out)
if st!=200: print(f"HTTP {st} in {dt:.0f}s: {out[:300].decode(errors='replace')}"); sys.exit(1)
d=json.loads(out); text=d.get("output_text") or "".join(c.get("text","") for s in d.get("steps",[]) for c in s.get("content",[]) if c.get("type")=="text")
Path(prefix+".txt").write_text(text,encoding="utf-8")
print(f"HTTP 200 in {dt:.0f}s status={d.get('status')} chars={len(text)} keys={list(d.keys())[:8]}")
