#!/usr/bin/env python3
"""用 vibe-server（whisper.cpp 引擎）本機轉逐字稿的測試工具。
用法: python pipeline/transcribe_whisper.py <audio> <model.bin> <out_prefix> [--no-gpu] [--diarize] [--vad] [--field k=v ...]
會自行啟動 vendor/vibe-server/vibe-server.exe，載入模型，POST /v1/audio/transcriptions (verbose_json)，輸出 .json / .txt / .turns.txt
"""
import json, os, subprocess, sys, time, urllib.request, urllib.error, uuid
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "vendor/vibe-server" / ("vibe-server.exe" if sys.platform == "win32" else "vibe-server")
MODELS = ROOT / "vendor/models"

def post_multipart(url, fields, file_field, file_path, timeout=3600):
    b = uuid.uuid4().hex.encode(); body = bytearray()
    for k, v in fields.items():
        body += b"--" + b + b"\r\nContent-Disposition: form-data; name=\"" + k.encode() + b"\"\r\n\r\n" + str(v).encode() + b"\r\n"
    body += b"--" + b + b"\r\nContent-Disposition: form-data; name=\"" + file_field.encode() + b"\"; filename=\"" + Path(file_path).name.encode() + b"\"\r\nContent-Type: application/octet-stream\r\n\r\n" + Path(file_path).read_bytes() + b"\r\n--" + b + b"--\r\n"
    req = urllib.request.Request(url, data=bytes(body), method="POST", headers={"Content-Type": f"multipart/form-data; boundary={b.decode()}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return r.status, r.read()
    except urllib.error.HTTPError as e: return e.code, e.read()

def post_json(url, obj):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r: return r.status, r.read()
    except urllib.error.HTTPError as e: return e.code, e.read()

def speaker_of(s):
    """講者編號；0 是合法編號，不能用 or 串接。"""
    spk = s.get("speaker")
    return s.get("speaker_id") if spk is None else spk

def main():
    args = sys.argv[1:]; audio, model, prefix = args[0], str(Path(args[1]).resolve()), args[2]
    no_gpu = "--no-gpu" in args; diarize = "--diarize" in args; vad = "--vad" in args
    fields = {"response_format": "verbose_json"}
    for i, a in enumerate(args):
        if a == "--field": k, v = args[i + 1].split("=", 1); fields[k] = v
    if diarize: fields["diarize_model"] = str(MODELS / "diar_streaming_sortformer_4spk-v2.q8_0.gguf")
    if vad: fields["vad_model"] = str(MODELS / "ggml-silero-v6.2.0.bin")
    port = 18124
    log = open(prefix + ".server.log", "w", encoding="utf-8")
    srv = subprocess.Popen([str(EXE), "serve", "--port", str(port), "--exit-with-parent", "false"], stdout=log, stderr=subprocess.STDOUT, cwd=EXE.parent)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(60):
            time.sleep(1)
            try: urllib.request.urlopen(base + "/health", timeout=2); break
            except Exception: pass
        t0 = time.time(); st, out = post_json(base + "/v1/models/load", {"path": model, "no_gpu": no_gpu})
        t_load = time.time() - t0
        print(f"load model -> {st} in {t_load:.1f}s  {out[:200].decode(errors='replace')}")
        if st != 200: return 1
        t0 = time.time(); st, out = post_multipart(base + "/v1/audio/transcriptions", fields, "file", audio)
        t_tr = time.time() - t0
        Path(prefix + ".json").write_bytes(out)
        if st != 200: print(f"transcribe -> {st}: {out[:500].decode(errors='replace')}"); return 1
        d = json.loads(out)
        segs = d.get("segments", [])
        text = d.get("text") or "".join(s.get("text", "") for s in segs)
        Path(prefix + ".txt").write_text(text, encoding="utf-8")
        lines = []
        for s in segs:
            st_ = float(s.get("start", 0)); spk = speaker_of(s)
            tag = f" spk{spk}:" if spk not in (None, "") else ""
            lines.append(f"[{int(st_//60):02d}:{int(st_%60):02d}]{tag} {s.get('text','').strip()}")
        Path(prefix + ".turns.txt").write_text("\n".join(lines), encoding="utf-8")
        dur = float(segs[-1]["end"]) if segs else 0
        print(f"transcribe ok in {t_tr:.0f}s (audio {dur:.0f}s, RTF={t_tr/max(dur,1):.2f})  segments={len(segs)} chars={len(text)} "
              f"speakers={sorted(set(str(speaker_of(s)) for s in segs))}")
        print("--- first 8 segments ---"); print("\n".join(lines[:8]))
        return 0
    finally:
        srv.terminate(); log.close()

if __name__ == "__main__": sys.exit(main())
