# 安裝指南（給 Claude Code 照著做）

這份是寫給 **Claude Code** 的：使用者把 repo 網址丟給 Claude，Claude 照這份從頭裝到能用。人也可以照著手動做。

## 目標與完成標準

- `utils/doctor.py --online` 的「必要」全部通過。
- 校對工具在獨立的視窗裡跑起來，瀏覽器看得到 http://127.0.0.1:5077 。
- 最後告訴使用者：以後怎麼啟動、怎麼開始轉第一場會議（第 7 節）。

## 規則（每一步都要守）

- **不用、也不要求管理員權限**。所有東西裝在專案資料夾或使用者目錄；不改系統層級的設定（系統 PATH、需要管理員的登錄機碼）。uv 安裝器會改「使用者」PATH，這可以。
- **金鑰不經過對話**：不要請使用者把金鑰貼給你，不要讀出、印出、檢查 `.env` 的內容。金鑰由使用者自己打開 `.env` 填；你只用 `doctor.py` 判斷填好沒（它不會印出金鑰）。
- **只用這份列出的網址與版本**。有列 SHA-256 的檔案，下載後要核對；不一致就停下來告訴使用者，不要改用別的來源。
- **已經有的就跳過**（重裝、補裝、裝到一半重來時）：`.venv`、`.env`、`glossary.txt`、`vendor/` 裡已核對過的檔案都不要刪掉重建。不要動 `meetings/`、`glossary.txt`、`voiceprints.json` 裡已經有的東西。
- **不要 commit、不要 push**：安裝和日常使用都不需要改 repo；協作者帳號可能有寫入權限，推上去會改到大家的版本。要更新程式用 `git pull`。
- 回報用繁體中文。
- 下面的指令以 Windows PowerShell 為準；在 Git Bash 裡把 `$env:USERPROFILE` 換成 `~`、路徑的 `\` 換成 `/`。

## 0. 開始前一次問完（停下來問）

一次問完下面幾題再開始，後面就不用再打斷使用者：

1. **裝在哪裡**（還沒 clone 時才問）：建議「文件」資料夾底下。
2. **要不要裝 whisper**、**要不要裝聲紋**：先說明用途與代價。可以先跑 `nvidia-smi` 或看「裝置管理員」的顯示卡；不確定就等第 3 節裝好 Python 後跑 `doctor.py` 看「顯示卡」那一欄。

   | 元件 | 用途 | 代價 |
   |---|---|---|
   | whisper | 本機第二個轉錄引擎：標出「可疑行」（兩個引擎差很多的行）、校正時間、長會議跨段的講者對齊 | 下載約 1.8 GB；需要顯示卡（Intel 內顯也可以），只有 CPU 會比即時還慢，不建議 |
   | 聲紋 | 替講者取名一次，之後的會議自動帶入名字 | 約 40 MB，CPU 即可 |

3. **手上有沒有別人給的 `glossary.txt`**（公司名、系統名、人名的詞彙表）。

同時告訴使用者會裝哪些東西、大約多久：只裝必要的約 5 分鐘（要下載 Python 約 20 MB、ffmpeg 約 115 MB）；加 whisper 要多下載約 1.8 GB。

## 1. 取得程式

- `git clone <網址>` 到使用者指定的位置。repo 是 private 的話，使用者的 GitHub 帳號要先被加成協作者；clone 失敗先確認這件事。
- 之後所有指令都在專案資料夾裡執行。

## 2. 設定檔與金鑰（先請使用者填，不用等）

1. 沒有 `.env` 才建：把 `.env.example` 複製成 `.env`。
2. **請使用者自己填金鑰**：用記事本打開專案資料夾裡的 `.env`，在 `GEMINI_API_KEY=` 後面貼上金鑰、存檔。**不用等他填好**，繼續做第 3～5 節；第 6 節檢查時才需要。
   - 金鑰哪裡來：有人（例如專案維護者）發給他的就用那把；沒有的話到 https://aistudio.google.com/ 申請，**處理公司會議要開付費**，免費層上傳的內容可能被 Google 用來改進產品。
3. 詞彙表：使用者有別人給的 `glossary.txt` 就請他放到專案根目錄；沒有就**不要建**（沒有也能轉錄）。不要把 `glossary.example.txt` 原樣複製成 `glossary.txt`：詞彙表會整份送進每一場的轉錄提示詞，範本裡的假詞會誤導轉錄。告訴使用者之後可以照範本的格式自己建。

## 3. Python 環境（用 uv，不需要先裝 Python）

1. 試 `uv --version`，也看看預設位置（PowerShell `$env:USERPROFILE\.local\bin\uv.exe`，macOS／Linux `~/.local/bin/uv`）有沒有檔案；有一個能用就跳過安裝。沒有就裝（裝到使用者目錄）：
   - Windows：`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/0.13.5/install.ps1 | iex"`
   - macOS／Linux：`curl -LsSf https://astral.sh/uv/0.13.5/install.sh | sh`
   - 安裝器改的使用者 PATH 要重開終端機（Claude Code）才生效，所以**這次安裝一律用完整路徑**呼叫：用安裝器最後印出的安裝位置（通常就是上面的預設位置）。下面寫 `uv` 的地方都換成完整路徑。
2. 沒有 `.venv` 才建：`uv venv .venv --python 3.13 --managed-python`
   （`--managed-python` 讓 uv 自己下載 Python 放在使用者目錄，不依附電腦上原有的 Python；之後 IT 升級或移除那份 Python，這裡也不會壞。）
3. `uv pip install --python .venv -r requirements.txt`
   （一律加 `--python .venv`：電腦上有 Anaconda、或終端機已經啟用別的環境時，不加會裝到那邊去。）
4. 之後執行 Python 一律用 `.venv` 裡的：Windows `.venv\Scripts\python.exe`、其他 `.venv/bin/python`。

## 4. ffmpeg

先試 `ffmpeg -version` 和 `ffprobe -version`，兩個都能跑就跳過這節。

**Windows**：只取壓縮檔裡的 `ffmpeg.exe`、`ffprobe.exe` 放到 `vendor\ffmpeg\bin\`（程式會自動優先用這裡的，不必改 PATH）：

```powershell
$zip = "$env:TEMP\ffmpeg-9.0.2-essentials_build.zip"
curl.exe -L --fail -o $zip https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip
if ((Get-FileHash $zip -Algorithm SHA256).Hash -ne '60F467265B1E312373DBCD92200C2618A74850F98D3D078E94296BB3FA2047BA') { Remove-Item $zip; throw 'SHA-256 不符，停止' }
New-Item -ItemType Directory -Force "$PWD\vendor\ffmpeg\bin" | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem
$z = [IO.Compression.ZipFile]::OpenRead($zip)
foreach ($n in 'ffmpeg.exe', 'ffprobe.exe') {
  $e = $z.Entries | Where-Object { $_.FullName -like "*/bin/$n" }
  [IO.Compression.ZipFileExtensions]::ExtractToFile($e, "$PWD\vendor\ffmpeg\bin\$n", $true)   # 要用完整路徑，.NET 的目前目錄不一定是 $PWD
}
$z.Dispose(); Remove-Item $zip
```

- **macOS**：有 Homebrew 就 `brew install ffmpeg`；沒有的話停下來問使用者要不要裝 Homebrew（裝 Homebrew 需要管理員密碼）。
- **Linux**：用套件管理員（例如 `sudo apt install ffmpeg`）；沒有 sudo 權限就停下來問使用者。

## 5. 選用元件（照第 0 節的回答）

每個檔案都照這個模式下載並核對（`--create-dirs` 會自動建好 `vendor\...` 底下還不存在的資料夾；macOS／Linux 用 `curl` 與 `shasum -a 256`）：

```powershell
function Get-Verified($url, $dest, $sha) {
  curl.exe -L --fail --create-dirs -o $dest $url
  if ((Get-FileHash $dest -Algorithm SHA256).Hash -ne $sha.ToUpper()) { Remove-Item $dest; throw "SHA-256 不符，停止：$dest" }
}
```

### whisper

| 檔案 | 下載 | 放到 | SHA-256 |
|---|---|---|---|
| vibe-server（Windows） | https://github.com/thewh1teagle/vibe/releases/download/server-v0.6.10/vibe-server-windows-amd64-with-ffmpeg.zip | 下載到 `$env:TEMP`，核對後 `Expand-Archive <zip> -DestinationPath "$PWD\vendor\vibe-server"`（壓縮檔裡直接是 `vibe-server.exe` 和 `ffmpeg.exe`，沒有外層資料夾），解完刪掉壓縮檔 | `f5193e9407acf8f963983e4c1eaf37e40664d71657c25476d72b218a8a8db9e9` |
| whisper 模型 | https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin | `vendor/models/` | `1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69` |
| 講者分離模型 | https://huggingface.co/nvidia/diar_streaming_sortformer_4spk-v2/resolve/main/diar_streaming_sortformer_4spk-v2.q8_0.gguf | `vendor/models/` | `0679cfeb1ce356d0dea9470b31274f4bfc7eb927497d82005483770666da998a` |
| VAD 模型 | https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin | `vendor/models/` | `2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987` |

macOS／Linux 的 vibe-server 從同一個發行頁 `https://github.com/thewh1teagle/vibe/releases/download/server-v0.6.10/<檔名>` 下載，放成 `vendor/vibe-server/vibe-server` 並 `chmod +x`（這兩個平台還沒實測過，裝完告訴使用者）：

| 平台 | 檔名 | SHA-256 |
|---|---|---|
| macOS Apple Silicon | `vibe-server-darwin-arm64-with-ffmpeg.tar.gz` | `faae324da3c01069d7af76eefaa72063dc066386e1c1c13ba3f42e7488aa3614` |
| macOS Intel | `vibe-server-darwin-amd64-with-ffmpeg.tar.gz` | `421bb1dc5a618e1d1336d4ca06e56f5b1f3a3ec48c709c21f4e346906fa233e9` |
| Linux x86_64 | `vibe-server-linux-amd64` | `c6fa295d59f30799dfcc3ab466ff4efd6ce407e1ea36c218b180d3c7c18a2719` |
| Linux ARM64 | `vibe-server-linux-arm64` | `00fdfebae44aa33314c1edcf66c27e2a0795afc77b3fa1bd1bb3f7c0ff83da92` |

### 聲紋

1. `uv pip install --python .venv sherpa-onnx==1.13.7`
2. 下載 https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_200k_sv_zh-cn_16k-common.onnx
   到 `vendor/models/spk/`，SHA-256 `e2d2048292e055f7b61cdec3db010503f35369b245bf0b3bbad021c9a91e4053`。

## 6. 檢查與啟動

1. 用 `.venv` 的 Python 執行 `utils/doctor.py --online`。金鑰那項沒通過就提醒使用者填 `.env`、等他說好再跑；其他「必要」的 `[失敗]` 照它寫的補，直到「必要項目全部通過」。「建議」「選用」的 `[注意]` 照實告訴使用者。
2. 啟動：**不要在工具裡前景執行 `start.bat`／`start.sh`**（伺服器不會結束，指令會一直卡住；放背景的話它是 Claude Code 的子行程，使用者關掉 Claude Code 就跟著停）。
   - Windows：`Start-Process -FilePath "$PWD\start.bat" -WorkingDirectory $PWD`，會另開一個獨立的黑色視窗並自動打開瀏覽器。
   - macOS／Linux：請使用者自己在終端機執行 `./start.sh`。
3. 再跑一次 `doctor.py`，「校對工具正在 http://127.0.0.1:5077 執行」要是 `[通過]`。左欄會議清單是空的是正常的，還沒有會議。
4. 不要幫使用者關掉那個視窗，除非他要求。

## 7. 交代使用者

- **以後啟動**：雙擊專案資料夾裡的 `start.bat`（macOS／Linux 執行 `start.sh`）。黑色視窗開著時工具才能用，關掉視窗就停止。
- **第一場會議**：校對工具左欄最下面「錄一場實體會議」可以直接錄（只支援 Windows）；Teams 錄影則是在專案資料夾的 `meetings/`（第一次啟動時自動建立）底下建一個「日期_標題」資料夾（例如 `2026-10-27_週會`），把錄影（和 .vtt 字幕、簡報 PDF）放進去，左欄「處理中」就會出現，按「開始轉錄」。
- **詞彙表**：`glossary.txt` 越完整轉錄越準，格式參考 `glossary.example.txt`。
- 有問題可以直接問 Claude（在專案資料夾開 Claude Code，它會讀 `CLAUDE.md`）。

## 疑難排解

| 狀況 | 做法 |
|---|---|
| 公司網路擋下載、或 `doctor.py --online` 連不上 Google | 問使用者公司是否要設 Proxy；設定 `HTTPS_PROXY` 後重試 |
| 中文輸出變亂碼、或 `UnicodeEncodeError: 'cp950'` | 指令前加環境變數 `PYTHONUTF8=1` |
| 下載的 `.bat` 被 Windows 擋下（SmartScreen） | 檔案右鍵「內容」勾「解除封鎖」，或從終端機執行 |
| `uv venv` 說環境已經存在 | 已經建過了，跳過；不要加 `--clear` 重建 |
| 5077 埠被別的程式佔用 | `doctor.py` 會顯示校對工具沒在執行；請使用者關掉占用的程式 |
