# meeting-transcribe-tw

**會議錄音／影片 → 繁體中文逐字稿 → 逐行播放校對 → 匯出 Markdown**

> **English summary** — A pipeline for turning Traditional Chinese (Taiwan) meeting recordings into accurate, speaker-labelled transcripts. Gemini (with a custom glossary) produces the text; a local whisper run serves as an independent reference to flag suspicious lines, correct timestamps and align speakers across chunks. A small local web app lets you replay and fix each line, then export Markdown. MIT licensed.

> **用 Claude Code 安裝**：把這個 repo 的網址丟給 Claude Code，請它「照 docs/install.md 安裝」。它會裝好 Python 環境、ffmpeg 與選用元件，金鑰那一步會請你自己填，最後跑自我檢查並開好校對工具。

## 特色

- **專有名詞準**：把公司名、系統名、人名寫進詞彙表，Gemini 在實測的 8 個關鍵詞拿到 8/8（不加詞彙表只有 2/8，本機 whisper 5/8）。
- **一個指令跑完**：轉檔、超過 28 分鐘自動切段、轉錄、比對、建立校對專案；中斷後重跑會接著做。
- **看得懂螢幕分享**：來源是影片時，自動抽出關鍵畫面，把畫面上的程式代碼、表單、欄位名稱整理成本場詞彙表；會議資料夾裡的 PDF（簡報、文件）也會被讀進詞彙表。結尾若有一大段沒錄到聲音，會自動略過。
- **兩個引擎互相檢查**（whisper 為選用）：
  - 與 whisper 差異大的行標為「可疑」，校對時優先看
  - 用 whisper 的時間校正 Gemini 偶爾整批標錯的時間戳
  - 長會議切段後，各段的講者編號自動對齊成同一組人
- **逐行校對工具**：點一行就播那一句、改字、改講者、替講者命名、請 LLM 帶著前後文重聽單一行；改過的內容重建專案也不會遺失。
- **AI 輔助校對**：AI 找錯字（只列最短的錯字片段，按接受才改）、從接受過的修正建議詞彙表、從對話猜講者名字、**聲紋**（取名一次，之後的會議自動帶入）。
- **會議紀錄**：一般會議或業務邏輯兩種範本，每一條都附逐字稿時間點，AI 推測的內容列成「待確認」讓你回答。
- **跨會議搜尋**：在所有會議校對後的逐字稿裡找字，點結果直接跳到那一場的那一行。
- **實體會議錄音**：網頁一鍵開始、結束並開始下一場，分段存檔不怕當機，錄完自動轉逐字稿。
- **繁體中文、台灣用語**，支援中英夾雜。

為什麼這樣設計、各方案實測比較，見 [`docs/design.md`](docs/design.md)。

## 流程

```
錄音／影片 ─ffmpeg→ 16 kHz 單聲道 ─(>28 分鐘切段)→ Gemini 3.6-flash + 詞彙表 ─┐
                                └─ whisper large-v3-turbo（本機，選用）─────────┤
                                                                               ↓
                 合併各段 → 以 whisper 校正時間戳 → 跨段講者對齊 → 標可疑行
                                                                               ↓
                             校對工具（瀏覽器）→ corrected.md
```

## 需求

| 項目 | 說明 |
|---|---|
| Python 3.10 以上 | `pip install -r requirements.txt` |
| ffmpeg / ffprobe | 在 PATH 上，或放在 `vendor/ffmpeg/bin/` |
| Gemini API 金鑰 | 到 [Google AI Studio](https://aistudio.google.com/) 申請；建議開啟付費（見下方「成本與隱私」） |
| whisper（選用） | [Vibe](https://github.com/thewh1teagle/vibe) 專案的 `vibe-server`（MIT）加 whisper.cpp 模型，安裝方式見下方；沒有的話用 `--no-whisper`，只是少了可疑行、時間校正與跨段講者對齊 |

## 安裝

用 Claude Code 的話，照 [`docs/install.md`](docs/install.md) 讓它裝（下載網址與檢查碼都寫在那裡）。手動安裝：

```bash
git clone https://github.com/ilwu/meeting-transcribe-tw.git
cd meeting-transcribe-tw
pip install -r requirements.txt
cp .env.example .env                    # 填入 GEMINI_API_KEY
python utils/doctor.py --online         # 自我檢查：必要項目全部通過就能用
```

ffmpeg 不在 PATH 上時，可以把 `ffmpeg`、`ffprobe` 放到 `vendor/ffmpeg/bin/`，程式會優先用那裡的。之後用 `start.bat`（macOS／Linux 用 `./start.sh`）啟動，會自動打開瀏覽器。

### 選用元件：whisper 與聲紋

- **whisper**（約 1.8 GB）：Vibe 專案的 `vibe-server`（MIT）加三個模型（whisper large-v3-turbo、NVIDIA Sortformer 講者分離最多 4 人、Silero VAD），放在 `vendor/`。沒有的話轉錄只用 Gemini，少了可疑行、時間校正與跨段講者對齊。速度參考：Intel Iris Xe 內顯（Vulkan）RTF 約 0.37（`recorder.RTF_WHISPER`），20 分鐘的會議約 7 分鐘；只用 CPU 時比即時還慢，不建議。
- **聲紋**（約 40 MB）：`sherpa-onnx` 加一個聲紋模型，替講者取名一次，之後的會議自動帶入名字；CPU 即可。已經取過名的會議，可以用 `python pipeline/voiceprints.py --enroll-all` 一次建立聲紋。

下載網址、放的位置與 SHA-256 都在 [`docs/install.md`](docs/install.md)「選用元件」。各模型的授權請見其模型卡（whisper、VAD 為 MIT，Sortformer 為 CC-BY-4.0）。`vendor/` 已在 `.gitignore`。

## 快速開始

```bash
# 1. 每場會議一個資料夾（日期_標題），把原檔直接放進去即可（m4a、mp3、mp4、wav… ffmpeg 讀得了的都可以）
#    Teams 下載的 .vtt 字幕、會議簡報 PDF 也一起放；run.py 會自動把原檔改名為 source.*、字幕搬進 transcripts/、建立 meeting.json
mkdir -p meetings/2026-10-01_週會
cp ~/Downloads/週會錄音.m4a meetings/2026-10-01_週會/

# 2. 一個指令跑完（20 分鐘的會議：Gemini 約 1 分鐘，whisper 在內顯約 7 分鐘）
python pipeline/run.py meetings/2026-10-01_週會
#    --no-whisper  不跑 whisper    --no-screen  不抽畫面詞彙表    --force  全部重跑    --model <名稱>  換 Gemini 模型
#    會議資料（簡報 PDF 等）直接放進同一個資料夾即可

# 3. 開校對工具
python app.py        # 瀏覽器開 http://127.0.0.1:5077
```

## 錄音（實體會議）

`python app.py` 後開 http://127.0.0.1:5077/record （校對工具左欄最下面「錄一場實體會議」）：

- 輸入會議標題按「開始錄音」，會自動建立 `meetings/<日期>_<標題>/`，錄完存成 `source.m4a`，不用再搬檔改名。
- 「結束並開始下一場」：一鍵切到下一場會議，兩場之間只有約 2 秒空檔。
- 錄音中顯示時間與音量；依最近 30 秒的音量分布提醒「沒收到聲音」「聲音太小」「背景太吵」。
- 每 5 分鐘存一個小檔，電腦當機或程式被關掉，最多損失最後幾分鐘；下次開頁面會列出「上次沒有正常結束的錄音」，按一下就能合併保存。
- 錄音期間防止 Windows 睡眠；勾選「錄完自動轉逐字稿」會在結束後直接執行 `run.py`。
- 頁面下方「還沒轉逐字稿的會議」會列出放了錄音或影片、但還沒處理的會議資料夾，按「開始轉逐字稿」即可，不用開命令列。一次只轉一場，其他的顯示「排隊中」，前一場轉完自動接著轉；「最近的錄音」會顯示每場轉錄的步驟、已執行時間與 whisper 預估完成時間。
- 錄音在伺服器端進行，關掉瀏覽器分頁不會中斷。目前錄的是麥克風（DirectShow 裝置），不包含喇叭播出的聲音；線上會議請用會議軟體本身的錄影。

## 校對工具

- 三欄版面：左欄是會議清單（每場附校對進度）、搜尋與處理中的會議；中間是逐字稿，上方分頁篩選「未處理／可疑／已修改／LLM 建議／錯字建議」（附行數），下方播放列可調速度、切換 whisper 對照；右欄分頁切換「講者／AI 工具／會議紀錄」。
- 左欄「搜尋所有會議」：在所有會議校對後的文字裡找字（不分大小寫；空白分開的詞要同時出現），結果依會議分組、新的在前，點一筆就切到那場、捲到那一行（按 Ctrl+Space 播放）。
- 清單只列出已轉好逐字稿的會議。放進 `meetings/` 但還沒轉的會議、轉錄中的會議，會在左欄「處理中」列出（待轉錄、轉錄中、排隊、失敗），可以直接按「開始轉錄」、看目前步驟；轉好後自動加進清單並提示。
- 每行可以（滑過或選到那一行時出現按鈕）：播放該句、直接改字（自動儲存）、改講者、接受、還原成原文、請 LLM 重聽。
- 影片會議：畫面切換的那一行會顯示當時的螢幕截圖與一句說明（例如「ERP 的 PC507 產量掃描頁面」），點縮圖開原圖。
- 右欄「講者」分頁：替「講者A」命名、把兩個標籤合併成同一人。「AI 猜名字」會從對話推測名字（例如被叫名字後下一句回應、自我介紹），附證據時間，按「套用」才改；程式會確認名字（或詞彙表裡的別名、常誤辨寫法）真的出現在證據時間前後，沒根據的建議會被丟掉。
- 聲紋：在講者欄替講者取名時會記住他的聲音。新會議轉完自動比對，**很像的直接帶入名字**（標「自動」，認錯按「不是他」），**不夠像的列為建議**（按「套用」才改）；也可以隨時按「聲紋比對」。某位講者各段聲音不一致時會提示「可能混到別人的聲音」，代表轉錄時把兩個人分成同一位，值得檢查。兩位講者都最像同一個人時（轉錄把一個人切成兩位），不自動帶入，改成建議「合併到 …」。取名時如果聲音明顯更像另一個已知的人，不會記進聲紋資料庫，講者欄會顯示警告，避免錯的聲紋讓之後的會議被標錯人。整位講者被標錯時，直接在講者欄改名字，整場這位講者的行會一次改好。聲紋（每人一組 192 個數字，不是錄音）只存在本機的 `voiceprints.json`，刪掉就全部清除。
- 「AI 找錯字」（右欄「AI 工具」分頁）：請 AI 掃整份逐字稿，只列出「原文片段 → 建議（理由）」，不潤飾、不改寫；每則可「接受」「全部接受」（同一組在全場一起改）或「忽略」，篩選「錯字建議」可以只看這些行。
- 「詞彙表建議」（右欄「AI 工具」分頁）：從所有會議接受過的錯字與講者名稱，建議還不在詞彙表裡的詞，可加入全域或本場詞彙表（會連同常誤辨的寫法一起寫入）或忽略。校對越多，下一場轉錄越準。
- 「LLM 先修可疑行」（右欄「AI 工具」分頁）：一次對 20 行可疑行請 LLM 重聽並給建議，採用與否由你決定。
- 「清語助詞」（右欄「AI 工具」分頁）：先用規則刪掉嗯、啊、呃這類純語助詞，再請 AI 刪掉當作停頓的「那個／就是／然後」與口吃重複。AI 只能刪字不能改字，刪掉的每一段都必須是贅詞或重複，否則整行不採用；只動還沒處理的行，清過的行下方會顯示「清語助詞前」的原文，也可以按「復原清理」全部改回。
- 「匯出」：跳出另存新檔視窗選位置，存成 `<會議>_校對版.md`（每句一段，含時間與講者；Chrome／Edge 會記住上次的資料夾，其他瀏覽器改成一般下載）。會議資料夾也一律留一份 `corrected.md` 與 `corrections.json`（修改紀錄）。

| 快捷鍵 | 動作 |
|---|---|
| Ctrl + Space | 播放／暫停目前這一行 |
| Ctrl + Enter | 接受並跳到下一行 |
| Alt + ↑ / ↓ | 上一行／下一行 |
| Ctrl + L | LLM 重聽目前這一行 |
| Esc | 停止播放 |

## 會議紀錄

校對工具右欄「會議紀錄」分頁，選範本後按「整理」（約 30～60 秒），結果存成會議資料夾的 `notes.json` 與 `notes.md`：

| 範本 | 內容 |
|---|---|
| 一般會議 | 摘要、決議、待辦（負責人、期限）、未決問題、各主題重點 |
| 業務邏輯 | 依作業流程整理每個作業的程式代碼、觸發時機、執行者、前置條件、系統動作、業務規則、例外與現況問題、待確認，加上名詞表；現行做法與未來規劃分開寫，適合整理系統開發用的現況說明 |

- **每一條都附逐字稿時間點**，點了就跳到那一行播放原話；時間點由程式對齊到實際存在的逐字稿行。
- AI 推測出來的人名、名詞、負責人、期限、數字列在「**待確認**」（最多 8 題），在面板上回答後按「依回答重新整理」，已回答的題目移除、內容依回答更正。
- 面板上的「另存」：跳出另存新檔視窗，存成 `<會議>_會議紀錄.md`（和「匯出」共用上次選的資料夾；不支援的瀏覽器改成下載）。
- 先校對再整理效果最好；紀錄會註明整理時逐字稿已校對幾行。命令列：`python pipeline/notes.py meetings/<資料夾> --template general|logic`。

## 詞彙表

`glossary.txt`（全域，選用；沒有也能轉錄，格式可參考 `glossary.example.txt`，但範本裡的假詞要刪掉，因為整份會送進提示詞）、`meetings/<會議>/glossary.txt`（本場，選用），以及自動產生的 `transcripts/glossary_screen.txt`（畫面）與 `transcripts/glossary_materials.txt`（PDF）會合併使用。自動產生的兩份可以手動修改，重跑時不會被覆蓋（`--force` 除外）。一行一組，正確寫法在前、括號內寫常見誤辨：

```
範例公司（公司名；常誤辨為 範列公司/範例工司）
費用請領單（常誤辨為 費用指引單/支領單）
人名：王小明、Amy（常誤辨為 Emmy）
```

Gemini 會拿到整份詞彙表；whisper 只拿括號外的正確寫法。詞彙表最好依會議主題分：同一份放太多無關的詞，對該場會議沒有幫助。

## 會議資料夾結構

```
meetings/<YYYY-MM-DD>_<標題>/
  source.*          原始錄音／錄影（不會被修改）
  meeting.json      標題、分段位移、各轉錄稿說明、處理狀態（run.py 會更新）
  glossary.txt      本場詞彙（選用）
  *.pdf             會議資料（選用，會被讀進詞彙表）
  audio/            16 kHz 轉檔與切段（可重建）
  frames/           影片的關鍵畫面截圖（自動產生）
  transcripts/      各引擎原始稿（gemini-*.txt/json、whisper-turbo.*）
  project.json      校對狀態（你的修改都在這裡，請備份）
  corrected.md      匯出的校對版
```

`meetings/` 預設在 `.gitignore` 裡，會議內容不會被 commit。

## 成本與隱私

- **成本**：Gemini 3.6-flash 處理 78 分鐘會議約新台幣 5 元，20 分鐘約 1 到 2 元（2026 年 10 月價格）。whisper 在本機執行，不另計費。
- **音訊會上傳到 Google**。Gemini API 免費層的輸入可能被用於改進 Google 的產品；處理公司會議請開啟付費，並在 Google Cloud 設定預算警示。
- 金鑰只放在 `.env`（已在 `.gitignore`），不要貼進 issue、截圖或對話紀錄。
- 校對工具只在本機（127.0.0.1）提供服務。

## 已知限制

- whisper 的講者分離最多 4 人；超過時講者以 Gemini 為準，跨段對齊可能不完整。
- Gemini 偶爾會把口語平順化（刪掉重複、補上語氣），逐字程度不如 whisper；有疑慮時用「播放」聽原音。
- whisper 偶爾會在安靜或雜訊段落產生重複幻覺；連續重複的段落已排除，但其他幻覺仍可能造成可疑行誤報。
- 聲紋與跨段講者對齊都依賴轉錄時的講者分離；若把兩個人分成同一位，聲紋會不準（講者欄會提示「可能混到別人的聲音」）。
- whisper 流程只在 Windows（Intel 內顯）上實測過；`vibe-server` 也有 macOS 與 Linux 版，歡迎回報使用結果。
- 錄音頁（實體會議）只支援 Windows；macOS／Linux 可以用其他錄音軟體錄好，再把檔案放進會議資料夾。
- 安裝指南只在 Windows 上實測過。

## 開發

開發規則、資料模型與不變量、驗證方式寫在 [`CLAUDE.md`](CLAUDE.md)（也是給 Claude Code 讀的）。歡迎開 issue 或 PR；請勿在 issue 中附上真實會議內容或金鑰。

## 授權

[MIT](LICENSE)
