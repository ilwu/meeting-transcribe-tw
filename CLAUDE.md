# CLAUDE.md

給在這個 repo 工作的 Claude Code（以及人類開發者）的開發指引。

**新安裝的電腦**（使用者請你安裝、或這台還沒有 `.venv`／`.env`）：照 [`docs/install.md`](docs/install.md) 做，下面的開發指引先不用讀。

## 文件地圖

| 文件 | 內容 |
|---|---|
| `README.md` | 功能、安裝、使用方式、已知限制 |
| `docs/install.md` | 安裝步驟（給 Claude Code 照做；下載網址、版本、檢查碼只寫在這裡） |
| `docs/design.md` | 每個設計的實驗依據與沒採用的方案 |
| 本檔 | 架構、資料模型與不變量、開發慣例、驗證方式 |

- **寫文件規則**：只寫現在為真的內容，推翻的移出（過程寫進 commit 訊息）；同一件事只在一處當本體，其他處一句＋指標；門檻等數值以程式常數為準，文件只寫它的依據（`docs/design.md`）。repo 裡的文件是要公開的最終狀態：待辦、草案、討論中的傾向不寫進來。
- 若有 `CLAUDE.local.md`（不進 git），那是這台電腦自己的狀態與筆記指標，先讀它。

## 專案在做什麼

把會議錄音／影片轉成繁體中文逐字稿，再用本機網頁工具逐行聽、逐行校對：

```
source.* ─ffmpeg→ audio/16k.m4a ─(>28 分鐘切段)→ Gemini 加詞彙表 ─┐
                                └─ whisper（vibe-server，選用）────┤
                                                                   ↓
         build_project.py：合併各段、以 whisper 校正時間戳、跨段講者對齊、標可疑行
                                                                   ↓
                     project.json ←→ app.py + static/index.html（校對工具）→ corrected.md
```

## 檔案職責

| 檔案 | 職責 |
|---|---|
| `pipeline/run.py` | 一個指令跑完整流程；開頭 `import_raw()` 把直接丟進資料夾的原檔整理好（唯一的媒體檔 → `source.*`、`*.vtt` → `transcripts/teams.vtt`、補建 `meeting.json`）；每步產出已存在就跳過（`--force` 重跑），可中斷後重跑。影片來源會先抽關鍵畫面建畫面詞彙表、PDF 建資料詞彙表；結尾超過 1 分鐘的完全無聲不送轉錄 |
| `pipeline/transcribe_gemini.py` | Gemini generateContent 呼叫（`call`、`load_env`），也可單獨當 CLI 測試 |
| `pipeline/gemini_transcribe.py` | Gemini 3.5 Transcribe 專用模型與 Files API 上傳（`upload_file`） |
| `pipeline/transcribe_whisper.py` | 啟動 vibe-server、送 whisper 轉錄（講者分離 + VAD） |
| `pipeline/build_project.py` | 由各段稿與 whisper JSON 建立／重建 `project.json` |
| `pipeline/notes.py` | 會議紀錄：`generate()` 把逐字稿（校對後文字、講者名稱）與畫面時間軸合併送 Gemini（JSON 輸出），範本 `general`／`logic`；`snap_times()` 把所有 time 對齊到不晚於它的逐字稿行、超出範圍的清空並計數；`--answers` 依「待確認」回答重整。輸出 `notes.json`、`notes.md`；校對工具 `/api/project/<名>/notes` 讀寫 |
| `pipeline/correct.py` | 校對輔助：`suggest()`／`merge()` 錯字建議（只收「最短原文片段 → 建議」，程式驗證後才給使用者）；`glossary_suggestions()` 彙整所有會議的 `learned` 與講者名稱；`guess_speaker_names()` 從對話推測講者名字，名字或別名（`name_aliases()` 從詞彙表抓）必須出現在證據時間附近 |
| `pipeline/fillers.py` | 清語助詞：`clean_rules()` 規則刪純語助詞；`clean()` 再請 Gemini 刪要看上下文的贅詞，`only_deletions()` 驗證只刪不改、每段刪除都是贅詞清單裡的詞或與緊鄰的字重複、英數字不刪、刪除比例有上限；只處理 `status` 為 `pending` 的行。校對工具 `/api/project/<名>/fillers`、`/fillers/undo` |
| `pipeline/voiceprints.py` | 聲紋（sherpa-onnx + ERes2Net，`vendor/models/spk/`）：`speaker_prints()` 每個講者的聲紋與一致性 `purity`（剔除不像的段落）；`enroll()` 取名時學，聲音明顯更像另一個已知的人就不學、回傳警告（存成 `speakers[標籤].voice_warning`）；`match()`／`apply_match()` 很像的自動帶入、次像的列為名字建議，每位講者只考慮自己最像的人、一人一場只自動帶入一位（兩位都最像同一人時列「合併」建議 `merge_into`），不跟同一場比；`reject()`「不是他」。門檻見程式常數與 `docs/design.md`；資料庫 `voiceprints.json` 在根目錄（不進 git） |
| `pipeline/textutil.py` | 共用的文字正規化 `norm` 與 `cer` |
| `pipeline/compare.py` | 多份逐字稿兩兩 CER（支援 txt、Teams vtt、whisper json） |
| `app.py`、`static/index.html` | 校對工具（Flask + 單頁 HTML，三欄：會議清單／逐字稿／講者・AI 工具・會議紀錄），port 5077；`/api/projects/overview` 給左欄（標題、日期、進度，依 mtime 快取）；`/api/project` 另附 `transcripts/screen_timeline.json`（不寫回 project.json），`/frame/<專案>/<檔名>` 提供截圖；`/api/search` 跨會議搜尋 `text`（依 project.json 的 mtime 快取） |
| `recorder.py`、`static/record.html` | 實體會議錄音（`/record`）：ffmpeg dshow 錄麥克風，每 `SEGMENT_SEC` 秒存一個 ADTS 小檔到 `<會議>/rec/`，結束時無損合併成 `source.m4a` 並刪掉小檔；當機留下的 `rec/` 由 `unfinished()` 偵測、`finalize()` 合併；`pending()` 列出有媒體檔但沒有 `project.json` 的會議供頁面一鍵轉錄；`transcribe()` 只是排進佇列，一次只跑一場 `run.py`（同時跑會搶同一顆內顯跑 whisper），排隊的狀態是「排隊中」、佇列不跨 app.py 重開；開始下一場前會等系統裡別處開的 `run.py`（app.py 重開前留下的、命令列開的）跑完；轉錄進度一律從各會議的 `run.log` 解析（app.py 重開後仍看得到） |
| `static/tailwind.css` → `static/app.css` | 兩個頁面共用的樣式：Tailwind v4，`tailwind.css` 是來源（逐字稿行、按鈕等 JS 動態產生的元件定義成 class），編好的 `app.css` 進 git，使用者不用建置 |
| `docs/install.md`、`utils/doctor.py`、`start.bat`／`start.sh` | 安裝：給 Claude Code 照做的安裝指南（固定下載網址與 SHA-256、要停下來問使用者的點）；自我檢查（必要／建議／選用，`--online` 驗金鑰，不印金鑰）；啟動捷徑（優先用 `.venv`，`app.py --open` 起來後開瀏覽器）。下載的網址、版本、檢查碼只寫在 `install.md` |
| `pipeline/bundled.py` | `use_bundled_ffmpeg()`：有 `vendor/ffmpeg/bin` 就放到 PATH 最前面；`app.py`、`run.py`、`voiceprints.py` 的入口會呼叫 |
| `utils/` | 其他與主流程無關的小工具 |
| `research/scripts/` | 9 月引擎比較的一次性實驗腳本，封存不維護 |

## 資料模型與不變量（改程式前必讀）

會議資料夾 `meetings/<YYYY-MM-DD>_<標題>/`（日期後面用 `-` 或 `_` 都可以，`import_raw()` 兩種都認）：`source.*`（原檔，不動）、`audio/`（可重建）、`transcripts/`（各引擎原始稿）、`meeting.json`（分段位移與說明）、`project.json`（校對狀態）、`glossary.txt`（本場詞彙，選用）。

`project.json` 每行（segment）的欄位：

| 欄位 | 意義 |
|---|---|
| `start`、`end` | 播放用時間；有 whisper 時 `start` 是校正後的時間 |
| `start_gemini` | Gemini 原始時間。**重建時以 `(start_gemini, text_orig)` 對應舊專案**，不能改用 `start` |
| `text`、`text_orig` | 校對後文字、原始文字 |
| `speaker`、`speaker_orig` | 目前講者、Gemini 原始標籤（`P<段>-講者N`）；頂層 `speaker_map` 存跨段對齊表 |
| `status` | `pending`／`edited`／`accepted` |
| `text_whisper`、`whisper_cer`、`flag` | whisper 對照、差異率、`suspect`（CER > 0.5） |
| `llm_suggestion`、`llm_tried` | LLM 重聽建議、是否已重聽過（批次重聽據此跳過） |
| `cleaned` | 清過語助詞（`text` 是清過的版本、`status` 仍是 `pending`；改回原文時拿掉） |
| `fixes` | 錯字建議 `[{original, suggestion, reason, status}]`，status：`pending`／`accepted`／`dismissed`／`stale`（原文片段已被手動改掉） |

專案層級：`learned`（接受過的錯字，供詞彙表建議）、`name_suggestions`（AI 猜的講者名字與不夠像的聲紋建議）、`speaker_purity`（各講者聲音一致性）、`speaker_map`。`speakers[標籤]` 有 `name`、`name_source`（`manual`／`voiceprint`）、`voice_score`、`voice_warning`（取名時聲紋沒學進去的原因）。根目錄 `glossary_ignored.txt` 是詞彙表建議的忽略清單（第一次按「忽略」才產生；含人名，不進 git）。

必須維持的不變量：

1. **重建不能遺失使用者的修改**：`text`、`status`、`llm_suggestion`、`llm_tried`、`fixes`、`cleaned`、使用者改過的講者，以及專案層級的 `learned`、`name_suggestions` 都要保留。
2. **講者名稱跟著人走，不是跟著字母**：重建後「講者A/B」可能換人，`build_project.py` 用同一批行的多數決把舊標籤換算成新標籤。
3. **寫 `project.json` 一律在 `app._lock` 內「讀 → 改 → 寫」**，因為批次重聽是多執行緒。
4. **切段首尾相接、不重疊**：合併時按時間排序，重疊會產生重複行；跨段講者靠 whisper 對齊，不靠重疊區。
5. **重聽失敗不覆寫既有建議**：只有拿到建議或被擋（blockReason）才寫入並標 `llm_tried`。

## 開發慣例

- **語言**：文件、註解、輸出一律繁體中文（台灣用語）。
- **Windows 編碼**：系統是 cp950，檔案是 UTF-8。`open()`、`read_text()`、`subprocess` 一律指定 `encoding="utf-8"`；遇到 `UnicodeEncodeError: 'cp950'` 就加 `PYTHONUTF8=1`。
- **換行**：`.gitattributes` 是 `* text=auto`，repo 內存 LF。用 Python 改檔時注意 `write_text` 在 Windows 會寫成 CRLF；以字串比對做取代時，先把 `\r\n` 正規化。
- **Windows 上的 gcloud**：參數含中文會讓 `gcloud.cmd` 解析失敗，顯示名稱用英文。
- **詞彙表**：`glossary.txt` 一行一組，正確寫法在前、括號內寫常見誤辨。合併順序：全域 → 本場 `glossary.txt` → `transcripts/glossary_screen.txt` → `transcripts/glossary_materials.txt`（`run.glossary()`，校對工具的重聽也共用）。Gemini 提示詞整份附上；whisper 的 prompt 只取括號外的詞（避免把誤辨詞教給它）。
- **畫面資訊只用來建詞彙表與畫面時間軸，不把整段影片送 Gemini**（從 Teams 截圖辨識說話者也試過，不可靠）：實測整段影片貴 3.5 倍、沒有更準，還會把講到一半的代碼自行補完（見 `docs/design.md`）。
- **樣式**：改了 `index.html`、`record.html` 的 class 或 `tailwind.css`，要重新產生 `app.css` 一起 commit（指令見「常用指令」）。Tailwind 只產生掃到的 class，JS 裡的 class 要寫成完整字串，不能拼接。CLI 是單一執行檔，放 `vendor/tailwindcss/tailwindcss.exe`（不進 git，從 Tailwind 的 GitHub 發行頁下載 v4.3.3 的 `tailwindcss-windows-x64.exe`）。
- 臨時檔案放系統暫存或 scratchpad，不要寫進會議資料夾。

## 安全與隱私（硬性規則）

- **絕不輸出 API 金鑰的任何部分**，包括「遮罩後」的版本：金鑰格式會變（例如 `AIza…` 改成 `AQ.…`），遮罩規則一失效就整把外洩。要確認兩把金鑰是否相同，由程式內部比較，只印出「相同／不同」。
- **不 commit 會議內容或人名**：`meetings/`、`glossary.txt`、`.env`、`research/` 底下的私人資料都在 `.gitignore`。新增檔案前先確認不含真實姓名、公司內部資訊或會議原話。
- **改寫 git 歷史前**：先 `git rm --cached` 並 commit，再改寫歷史；否則 `filter-branch` 最後的 checkout 會刪掉工作目錄裡的檔案。改寫前先打備份 tag。

## 注意事項（實跑踩過的坑）

- **改了 `app.py`、`recorder.py`、`pipeline/*.py` 要請使用者重開 `app.py`**：靜態頁每次從磁碟讀、後端不會重載，新頁面配舊後端會讓清單畫不出來（例如錄音清單、左欄會議清單）。轉錄進行中別叫使用者 Ctrl+C（會連子行程一起中斷）；`run.py` 從命令列另外啟動的不受影響。
- **Gemini 會「提前結束」**：回 STOP 但只轉了幾分鐘（例如 26 分鐘的音檔只轉了 1 分鐘）；`run.check()` 的涵蓋率檢查就是為此，別拿掉。也會隨機回 503，等 30 秒重試即可。
- **會議資料夾裡的檔案要先核對**：可能放錯別場的影片或 `teams.vtt`。比對長度、md5、畫面內容後再處理。
- **Teams 錄影可能有一段完全無聲**（-91 dB）：`run.speech_end()` 會略過結尾無聲；中段無聲要提醒使用者。
- **測試別碰真資料**：UI 測試在另一個 port、用 `meetings/_xxx` 暫存複本，測完刪；會寫入 `voiceprints.json`、`glossary.txt` 的測試要先備份、測完還原。
- **Windows 的 Bash heredoc 會吃掉反斜線**：多行 Python 先寫成檔再跑。

## 驗證方式

- **後端**：用 Flask test client（`app.app.test_client()`），並把 `transcribe_gemini.call` 換成假函式，不實際呼叫 API。測試專案放在臨時資料夾，測完刪除。
- **重建**：先建到暫存資料夾，比對舊專案的 `text`、`status`、`llm_suggestion` 沒有遺失，講者名稱沒有換人。
- **切段**：用 `ffmpeg -f lavfi -i anullsrc` 產生指定長度的靜音檔測邊界（剛好整除、尾段很短、尾段剛好 2 分鐘）。
- **前端**：在另一個 port 起測試站（不要動使用者正在用的 5077），到瀏覽器實際操作；有時間競態的動作在同一段 JavaScript 內觸發。
- **錄音**：測試時把 `recorder.MEETINGS` 改到暫存資料夾、`SEGMENT_SEC` 改小，並停用 `recorder.transcribe`（避免拿測試錄音呼叫 Gemini）；當機情境用 `proc.kill()` 模擬。
- **AI 建議類功能**（找錯字、猜名字、會議紀錄）：先用假的 `correct_mod.suggest` 等函式測接受／全部接受／忽略／stale 與重建保留，再用真的模型跑一場確認品質；在 UI 測試用 `meetings/_ui_test` 這類暫存複本，測完刪除。
- **CER 回歸**：`python pipeline/compare.py a=<稿1> b=<稿2>`，重構後數字要和重構前相同。

## 常用指令

```bash
python pipeline/run.py meetings/<資料夾>            # 一個指令跑完（--no-whisper、--force、--model）
python app.py                                        # 校對工具 http://127.0.0.1:5077
python pipeline/build_project.py <資料夾> <audio> --whisper <json> p1.txt:0 p2.txt:1560 ...
python pipeline/compare.py gemini=<txt> whisper=<json> teams=<vtt>
vendor/tailwindcss/tailwindcss.exe -i static/tailwind.css -o static/app.css --minify   # 改 UI 後重產樣式
```
