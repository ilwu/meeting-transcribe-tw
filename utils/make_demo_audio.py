#!/usr/bin/env python3
"""把 demo/劇本.txt 合成成示範會議的錄音（Gemini 語音合成，需要 .env 的金鑰與網路）。

用法: python utils/make_demo_audio.py [輸出檔，預設 demo/2026-01-15_產品週會-示範/source.m4a]
送出去的只有劇本文字。每次合成的語氣與長度會略有不同，重做之後整場示範會議要重新轉錄。
"""
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from bundled import use_bundled_ffmpeg  # noqa: E402
from transcribe_gemini import call, load_env  # noqa: E402

MODEL = "gemini-3.8-flash-tts"
VOICES = {"Amy": "Leda", "小明": "Charon"}              # 講者 → 聲音；這個模型一次最多兩位講者
LANGUAGE = "cmn-TW"
SCRIPT = ROOT / "demo/劇本.txt"
OUT = ROOT / "demo/2026-01-15_產品週會-示範/source.m4a"


def read_script(path):
    """回傳 [(講者, 台詞)]；空行與 # 開頭的說明略過。"""
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip() and not raw.startswith("#"):
            who, text = raw.split("：", 1)
            lines.append((who.strip(), text.strip()))
    return lines


def synthesize(lines, key):
    """回傳 (音訊資料, mimeType)。"""
    #====================================
    # 組請求
    #====================================
    # 這個模型的規矩：每一句各自標講者（speechMetadata.speaker），不接受 systemInstruction
    body = {"contents": [{"role": "user", "parts": [{"text": text, "speechMetadata": {"speaker": who}} for who, text in lines]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                "languageCode": LANGUAGE,
                "multiSpeakerVoiceConfig": {"speakerVoiceConfigs": [
                    {"speaker": who, "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}} for who, voice in VOICES.items()]}}}}

    #====================================
    # 呼叫模型
    #====================================
    resp, _ = call(MODEL, body, key)
    if "error" in resp:
        sys.exit("合成失敗：" + json.dumps(resp["error"], ensure_ascii=False)[:400])
    part = next((p for c in resp.get("candidates", []) for p in c.get("content", {}).get("parts", []) if "inlineData" in p), None)
    if part is None:
        sys.exit("模型沒有回傳音訊：" + json.dumps(resp, ensure_ascii=False)[:400])

    #====================================
    # 回傳
    #====================================
    return base64.b64decode(part["inlineData"]["data"]), part["inlineData"]["mimeType"]


def main():
    #====================================
    # 讀劇本
    #====================================
    out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else OUT
    lines = read_script(SCRIPT)
    unknown = sorted({who for who, _ in lines} - set(VOICES))
    if unknown:
        sys.exit("劇本裡有沒設定聲音的講者：" + "、".join(unknown))

    #====================================
    # 合成
    #====================================
    use_bundled_ffmpeg()
    load_env()
    data, mime = synthesize(lines, os.environ["GEMINI_API_KEY"])

    #====================================
    # 轉成 m4a
    #====================================
    # 回傳的可能是 WAV 檔，也可能是沒有檔頭的原始音訊（這時 mimeType 會帶取樣率）
    raw = ["-f", "s16le", "-ar", mime.split("rate=")[1].split(";")[0], "-ac", "1"] if "rate=" in mime else []
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", *raw, "-i", "pipe:0", "-c:a", "aac", "-b:a", "64k", str(out)], input=data, check=True)
    print(f"{len(lines)} 句 → {out}")


if __name__ == "__main__":
    main()
