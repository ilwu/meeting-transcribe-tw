import json, os, sys, urllib.request, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline"))  # 共用 pipeline 的 Gemini 函式
from transcribe_gemini import load_env, call
load_env(); key=os.environ["GEMINI_API_KEY"]
model, textfile, start, end, out = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
t=Path(textfile).read_text(encoding="utf-8")[start:end]
body={"contents":[{"role":"user","parts":[{"text":"以下是一段會議逐字稿，請轉成繁體中文並修正錯字，其餘不要改動：\n\n"+t}]}]}
resp,dt=call(model, body, key)
Path(out).write_text(json.dumps(resp,ensure_ascii=False,indent=1),encoding="utf-8")
br=resp.get("promptFeedback",{}).get("blockReason"); fin=[c.get("finishReason") for c in resp.get("candidates",[])]
n=sum(len(p.get("text","")) for c in resp.get("candidates",[]) for p in c.get("content",{}).get("parts",[]))
print(f"{model} text[{start}:{end}] len={end-start}: block={br} finish={fin} out_chars={n} ({dt:.0f}s)")
