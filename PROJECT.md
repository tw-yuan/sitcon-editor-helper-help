# SITCON 編輯組小石：專案說明

## 目的與背景

編輯組是持續運作的長期組別，工作會跨越年會、Camp、Hackathon 及日常社群內容。本專案以 Python agent 取代兩份 n8n 工作流，讓成員從 Telegram 建立工作、安排負責人、查卡、送審及查詢編輯組知識。年度 bot 提供可重用的模型與搜尋介面；編輯組的人員、知識、看板及文件位置則各自設定。

使用者已授權依口頭補充實作。開發期間可唯讀查證，新增／刪除遠端資源須另取得確認；這個限制不套用到未來機器人處理明確使用者指令的日常流程。

## 系統架構

```text
Telegram 群組／topic
        │ 驗證授權群組、觸發方式、操作者
        ▼
Gateway ── /ta、管理指令 ──┐
        │                  │
        ▼                  ▼
Agent + 主模型        編輯組工具
（經 CF AI Gateway）
        │                  │
        ├─ ask_user         ├─ 名冊：Google Sheets（唯一人員來源）
        ├─ 工具參數驗證      ├─ 知識：GitLab Wiki + Wiki 連結的 Google Docs
        ├─ 依序執行工具      ├─ 卡片：固定 GitLab 專案
        └─ 敘述結果         ├─ 文件：固定 Drive 根目錄 + Docs 範本
                           └─ 網路：獨立 Anthropic 搜尋服務
        │
        ▼
SQLite：授權、群組記憶、事件、操作步驟、資源對照、通知佇列、稽核
        │
        ▼
原群組／topic 回覆與 review 通知
```

單一 Compose `bot` 服務使用 Telegram long polling，不開 HTTP port。Google、GitLab、Telegram 與 AI API 都是對外 HTTPS 連線。SQLite 放在持久化 `data/`，service account JSON 以唯讀掛載提供。

## 技術棧與選型

| 技術 | 用途與理由 |
| --- | --- |
| Python 3.12 | 沿用年度 bot 的 async 服務及模型介面，降低整合差異。 |
| python-telegram-bot 22 | 群組、topic、按鈕、long polling 與限流。 |
| Pydantic Settings／models | 環境變數與工具參數驗證，拒絕額外參數。 |
| httpx | 固定專案的 GitLab REST API，可測試每一筆請求，不提供任意 API 工具。 |
| google-api-python-client／google-auth | 直接使用 service account 操作 Sheets、Shared Drive 與 Docs。 |
| OpenAI／Anthropic SDK | 主模型 `dynamic/sitcon` 經 Cloudflare 的 OpenAI 相容端點；Anthropic 搜尋另有自己的服務與憑證。 |
| SQLite／aiosqlite | 單機部署的授權、持久記憶、開卡步驟及通知紀錄，免額外維護資料庫服務。 |
| uv／Docker Compose | 鎖定依賴與部署環境；容器 UID/GID 固定為 10001。 |
| pytest／respx／Ruff | 模擬遠端錯誤與重試，測試容器無網路、無正式憑證。格式沿用來源專案。 |

正式依賴以 `uv.lock` 為準。來源 lock 的 `cryptography` 已依弱點稽核更新至修補版本；未更動年度 bot 的環境或 lock。

## 資料來源與責任

| 資料 | 權威來源 | 系統如何使用 |
| --- | --- | --- |
| 成員、Telegram／GitLab 對照、預設負責人、總副召 | 指定 Google Sheet 的 `gid=0` | 快取 300 秒；不從 Wiki 舊名單推斷。 |
| 可用 label、卡片狀態與內容 | `sitcon-tw/editorial/board` | 操作前查證既有 label，操作後讀回。 |
| 編輯規範與參考連結 | GitLab Wiki | 快取 900 秒；回答附來源，文件過長可分段讀取。 |
| 文案與附件 | 指定 Drive 根目錄與 Docs 範本 | 選擇建立文案的卡片各自擁有 `MMDD_TITLE` 子資料夾。 |
| 群組慣例 | 使用者明確要求保存的本群記憶 | 以 `chat_id` 隔離，持久儲存；不能變更權限。 |
| 公開、時效資訊 | 獨立網路搜尋 | 附來源，不傳內部卡片或完整名冊作為查詢。 |

實際名冊欄位是 `Telegram ID`、`gitlab_id`、`Nickname`、`default`、`note`。`Telegram ID` 的現行資料是 username，不是數字 ID；程式也接受純數字 Telegram ID。`default=yes` 必須唯一；`note` 中的「總召／副召」決定 review 通知。GitLab ID 在指派前再查證是否存在，錯誤資料不猜測替代值。

Wiki 首頁含較舊的人員區段，知識服務會移除該段，提示改查名冊。快取到期後讀取失敗會回報不可用，不以過期人員資料執行指派或群體通知。

## 使用與權限

- `/authorize`：設定的管理員授權本群。授權資料存 SQLite。
- `/revoke`：管理員撤銷本群；`/reload` 更新名冊及 Wiki 快取。
- `/help`：用法與「@ 所有編輯組員」按鈕；`/ta` 直接讀名冊、分批標註。
- 一般業務功能提供給已授權群組內的成員；名冊決定人員對照，不是使用者白名單。
- 私訊、未授權群組、其他 bot 的訊息不執行 agent；未授權群組允許設定的管理員 `/authorize`。
- 一般訊息僅有四種觸發方式：以小石的 `@bot帳號` 開頭、以「小石」開頭、以獨立單字 `review` 開頭，或回覆小石訊息。開頭從第一個字元比對，不略過前置空白或換行；名稱與 tag 出現在中途不觸發，避免一般聊天被當成指令。Tag 大小寫不拘且需完整比對帳號，不能誤認更長的其他帳號；後接中文或標點不需空白。`review` 大小寫不拘，後面需空白或訊息結束，保留既有送審快捷用法。既有斜線指令、補問選項與簽到按鈕維持原本用途。
- 通知回原群組與 topic，不另外私訊被標註者。
- 補問可點選原問題的按鈕或以 reply chain 回答；transcript 仍沿 reply chain 續接。只限同群、同 topic、同操作者。TTL 預設 30 分鐘，重啟後失效；長期記憶不失效。
- 補問工具與其他工具同輪出現時，其他工具一律不執行，避免資訊尚未完整就先寫入。

## 補問按鈕與確認

Agent 的 ask_user.options 會保存在 Pending，最多十個候選。問題本體仍為純文字「選項 1：…」，Telegram inline keyboard 每列最多三個數字按鈕；使用者點擊後，系統把完整候選文字填回原 ask_user 的工具結果，讓 agent 接續原需求。直接回覆編號也會換成對應候選文字；自由文字仍可作答。

確實需要同意的問題使用「同意」、「不同意」兩個候選，按鈕直接顯示這兩個標籤。不同意在 gateway 直接取消，不會呼叫模型或業務工具。明確且資訊完整的指令照常執行，不增加通用確認關卡。

questions.py 在記憶體保存隨機識別碼、原提問者、群組、topic、問題 message_id 與原對話，最多 500 筆並套用 CONTEXT_TTL_SECONDS。Callback 只攜帶識別碼及選項索引，沒有可任意指定的工具或操作參數；每次回呼先驗群組授權，再檢查原提問者、原群組、已綁定的問題訊息、期限和索引。Telegram 的 message_id 在群組內唯一；callback 的 message_thread_id 可能是問題的回覆串，不拿來比對原話題。接續的事件、工具、處理鎖與回覆沿用問題保存的 topic，同群其他話題的轉傳／複製訊息因 message_id 不符仍會拒絕。文字回答保留同話題檢查。按鈕與文字共用一次性狀態，同一題無法重複續接。作答後盡力移除按鈕；Telegram 確認回呼／移除鍵盤失敗不會丟掉已接受的答案，重按仍會被拒絕。

訊息 outbox 的 JSON body 只記錄問題識別碼，鍵盤放在問題最後一段；延遲送達後仍綁定實際送達的 message_id，支援文字及按鈕作答，不需資料庫 migration。重啟或過期後識別碼無效，舊按鈕會提示重新提出需求。既有歷史訊息不會補上按鈕。

文字補問及一般追問只在 Telegram 訊息的 `is_topic_message=true` 時採用 `message_thread_id`；一般群組一律以無 topic 保存／查詢 session、處理鎖、事件及回覆目的地，避免原需求、問題與使用者回答各自出現不同回覆串 ID，造成脈絡被當成失效。論壇 topic 仍精確比對；同群其他使用者無法接續原 Pending。欄位定義依 [Telegram Message](https://core.telegram.org/bots/api#message)。

Agent 續接時，除原始使用者訊息，也依原 `ask_user` 呼叫 ID 取回每次工具結果中的使用者回答，供日期等程式層驗證。只將已對應補問的答案視為輸入，不將 GitLab、Wiki 等其他工具結果當作使用者授權或日期證據。提示詞要求只補缺少的欄位，已提供的標題、日期及負責人不得重複追問。

## 開卡資料流

`create_card.document` 預設為 `true`，一般開卡建立 GitLab Issue、Drive 資料夾與範本 Docs。只有使用者明確要求「僅開卡／只開卡／不要資料夾、文案或文件」時才設為 `false`。不因標題、圖片或 label 自行略過文件；兩種模式仍使用 Sheets 名冊查核負責人。這個預設依 2026-10-05 的需求調整，既有操作保存的 `document=false` 不會被覆寫，重試仍遵守當時的選擇。

1. 使用者提供標題與到期日。支援獨立四位數 MMDD；「小石開卡 0925 test」直接解析為當年 09/25 到期、標題 test，無需補問。MMDD 使用真實日曆驗證，卡號及較長識別碼不當作日期。缺年份用台灣當前年份；「今天／今日、明天／明日、後天（也接受后天）」依本輪台灣日期加 0、1、2 日直接換算，跨月、跨年及閏年依日曆處理，不另問同意。「下週某天、過幾天」等無法唯一確定日期才補問。日期必須來自使用者的指令／續接脈絡，不接受模型編造的日期。
2. 將指定 Telegram username 對應名冊 GitLab ID；未指定時採唯一 `default=yes`，回覆明示。
3. 驗證所有 label 已存在。未指定 label 時，文案卡套 `社群文案`、任務卡套 `編輯組專案`；沒有指定狀態時加 `Status::Inbox`。不自動加年度 label。
4. 僅開卡略過所有 Drive／Docs 呼叫。建立文案時才預檢 Shared Drive 根目錄、建立權限、範本可複製及必要欄位。
5. 建立文案時，在固定根目錄建立到期日 `MMDD_TITLE` 資料夾並複製範本到其中，文件名稱同為 `MMDD_TITLE`。
6. 建立 GitLab Issue。description 包含原說明與操作識別，不自動附加建立者署名；只有實際存在的資料夾／文案才加入連結。各項以空行分段，避免 GitLab 將一般換行合併顯示。
7. 讀回 Issue，驗證 labels 與所有負責人。GitLab 若不接受多人，不把部分成功說成全部成功。
8. 若有建立文案，在文件填入 `TITTLE`、`DATE`、`GITLAB_LINK`、`DIR_LINK`，並設定兩個 URL 為可點擊連結。保留其他範本正文。
9. 保存 Issue／資料夾／文件對照，回覆實際網址與操作 ID；僅開卡的 `folder_id`、`document_id` 與對應 URL 為空值，不產生空連結。

新卡片不自動寫入 `建立者：@username`，也不附加 Telegram username／ID 署名，開卡不再為署名查詢提問者的 GitLab 帳號。負責人的名冊驗證與指派維持原流程。真正的操作者仍保存於 SQLite operations、events 及 audit_log，供權限與操作追蹤；GitLab Issue 的系統作者仍是執行 API 的 service account。舊操作 payload 中的 requester 欄位可以保留，接續建立新卡時不會將它寫入說明；已存在的卡片內容不批次清除。

每輪 ToolContext 保存一次 `reference_time`，PromptBuilder 將它轉成設定時區（預設 Asia/Taipei），列出今天、明天與後天對應的 ISO 日期。開卡與修改到期日共用同一時間基準驗證，避免模型處理跨過午夜後，提示與後端各用不同日期。工具與 GitLab 仍傳 YYYY-MM-DD；相對日期只在理解輸入時換算，既有操作接續沿用已保存的日期。清除日期仍須明確使用原有 `clear_due_date`。

建立文案時，每張卡的資料夾獨立，不以名稱查找合併：相同 `MMDD_TITLE` 仍可能是不同年度或不同工作。重試以操作識別及 Drive `appProperties` 對應資源。既有卡片的資料夾不會刪除；接續舊版純任務操作時，保留 checkpoint 內已建立的資料夾，不再呼叫 Drive。若舊版 Drive 寫入仍為結果不明，停止並要求人工查核，不跳過未知步驟後直接開新卡。

## 失敗與重試

### 既有卡片補建資料夾與文案

`add_card_document(iid)` 處理既有卡片的補建需求，包括開卡後追問「資料夾跟文案呢」。Agent 從同一人的回覆脈絡或查詢確認卡號，呼叫專用工具；不能重開卡，也不能編造操作 ID 呼叫接續工具。補建使用原卡標題與已設定的到期日，沒有日期時先請使用者提供明確日期並更新卡片。

1. 查詢原卡、SQLite 資源對照及已有補建操作。若原開卡尚未完成，回報原操作 ID，要求先接續原流程。
2. 比對卡片連結與本地紀錄；有多個或衝突資源時停止。已有文案時只回傳既有連結，不複製範本或改寫正文。
3. 預檢 Shared Drive 及範本。既有資料夾必須位於核准根目錄下；沒有資料夾時依原卡的 `MMDD_TITLE` 建立。
4. 沿用開卡的 checkpoint 與 Drive 操作識別機制，複製範本並填入卡片／資料夾連結。
5. 重新讀取原卡，在最新說明後追加缺少的資源連結，保留原說明、建立者、標籤、負責人與到期日；若期間已連結其他資源則停止並回報本次已建資源。GitLab 更新沿用既有回讀驗證。
6. 更新資源對照及完成收據。GitLab 回應遺失後，重試先讀取連結；已附上的連結不重複寫入。

補建的操作種類為 `add_card_document`，操作 ID 依固定專案內的卡號穩定產生，同一張卡跨 Telegram 請求與同時請求共用一組資源。未完成操作限原操作者在原群接續；完成操作回傳既有收據。`resume_operation` 可接續開卡與補建，服務重啟的恢復摘要也包含已完成補建的連結。沿用既有 operations／resources／outbox，無新依賴、環境變數或 migration。

補建不會把已存在的文案重新套版，不移動已有文件，也不自動回填所有歷史卡片。已完成補建的收據用於去重；若之後有人刪除或搬走資源，需人工查核，不因再次要求補建而自動建立第二份。

### 跨服務操作接續

建立文案是跨 Google／GitLab 的分步流程，不是原子交易；僅開卡只執行 GitLab 步驟。每次新增前先記錄 pending，成功後保存資源 ID。若連線中斷，先用操作識別查找遠端資源；找到則接續，找不到且結果仍不明則停下，避免盲目重建。

失敗回覆會列出操作 ID、完成步驟與已知連結。使用者可要求 `接續操作 ID`，只允許原操作者在原群接續。不會自動刪除已建立資源。相同 Telegram update 不再執行；同輪相同工具參數有持久收據，開卡未完成時也不能以不同參數另開一份。

已成功的文案替換在接續時先檢查連結，避免重複替換使用者標題中的文字。只允許在核准根目錄內、且屬於本次資料夾的文件上填入範本。

## 狀態與 review

現行看板使用 `Status::Inbox`、`Status::To Do`、`Status::Doing`、`Status::Waiting`、`Status::Review`、`Status::Report`。系統使用這些 label，不以原生 work item status 驅動看板。

改狀態只加入選定狀態並移除同 scope 舊狀態，保留年度及其他分類。Review 仍是 opened。關閉／重新開啟由獨立工具處理，需使用者明確要求。

「未完成／待辦／我還有什麼工作」不包含 Review，也不把 Review 計為逾期未完成。`gitlab_search_issues` 預設 `open_only=true, include_review=false`，後端取得完整查詢結果後排除設定的 Review label，再回傳清單供模型統計；這個排除與負責人、日期及分類篩選可同時使用。明確查全部未關閉時設 `include_review=true`；明確指定 Review 狀態或 label 也會包含 Review。`open_only=false` 仍列全部狀態。這是待辦查詢的分類規則，不會關閉或更動送審卡片。

批次 review 會先辨識全部目標，若某張標題模糊、已關閉、文案卡缺連結，則在寫入前回報。有文案時先完成全部 PDF 匯出與簽到欄位檢查，再逐卡切換狀態、讀回、保存通知內容，由 Telegram gateway 發送。通知列卡號、卡名、作者、總副召，連結依「文案：完整 URL」「卡片：完整 URL」各自換行；不附資料夾連結，沒有文案時不列空欄。格式由工具固定產生並保存至操作收據，讓通知送達與當機恢復沿用同樣標示；一般 AI 回覆也透過 prompt 要求標示連結種類。單張 API 失敗不會把全部卡片說成成功。舊卡可從 description 取連結，不會自動幫舊卡建資料夾。

使用者只要求 review／送審／審稿且所有文案成功時，以含 PDF 的通知作為唯一回覆，不另發完成摘要或版本提醒。自然語言由 Agent 的提示詞辨識是否還有其他需求，僅在文案送審工具確認本批全部成功後允許空白最終回覆；明確的 `review 676`、`review #676 #677` 等卡號指令，gateway 再核對全部成功的 PDF 收據後直接省略模型摘要。混合指令仍完成並回覆其他需求，部分失敗、補問及無 PDF 的純任務通知維持回覆。

## PDF 快照與多人簽到

PDF 附件、review 通知、「已看過：」與簽到按鈕合併在同一則訊息，以 `sendDocument` 的 caption 及 inline keyboard 送出。尚無簽到者時冒號後留白，不加「尚無」。附件說明上限 1,024 字元，程式保守依 UTF-16 計算 HTML 解析後的顯示長度，通知正文最多 900 單位，預留名單及分頁空間；過長在匯出 PDF 與修改卡片狀態前回報，不截斷卡名或連結。PDF 送達結果不明時不盲目重送。純任務卡沒有文案時沿用文字通知；已知僅開卡紀錄優先於標題／label 的文案推測，避免把「文案規劃」等任務誤判為缺少文件。後來手動附上的文件連結仍可送審。

`ReviewDocuments` 使用 Google Drive 的 `files.export` 取得完整 Docs PDF，沒有另建 Drive 檔案，也不提供公開下載端點；最大 10 MB，檢查 PDF 檔頭。匯出及簽到都先核對 Google 文件位於核准根目錄或直接子資料夾，以及 service account 的編輯／下載能力。每次 review 以操作識別保存快照，同輪重試沿用相同 PDF；不同 review 產生新的快照與各自的已讀名單。匯出時間保留於資料庫，不顯示 PDF 標題、匯出時間或版本提醒。文件修改後重新 review 才會取得新版 PDF。

`ReviewPackets` 管理通知及簽到：`review_packets` 保存 PDF BLOB、原始通知、文件與群組／topic；`document_signatures` 以文件 ID＋Telegram 數字 ID 凍結首次簽名字串；`review_reads` 保存各份快照的已讀者；`review_messages` 保存實際送達訊息 ID、名單頁碼與待更新狀態。任一已授權群內使用者均可按原通知簽到，不限發起 review 的人，也不套用短期補問的一次性／30 分鐘限制。Callback 必須符合原群及已綁定的原訊息。Telegram 的 message_id 在群組內唯一，跨 topic 也不重複；message_thread_id 則可能是通知本身的回覆串，與原始 review 指令不同，因此不拿 callback 的 thread 值比對授權。通知及後續回覆仍沿用保存的群組／topic；轉傳到別群或同群其他話題會有不同訊息識別，不能簽到。沒有 Telegram username 時用 Telegram ID 作為簽名。

簽到以文件鎖序列化，定位跨 tabs 的唯一「校稿簽到串：」段落，只插入新名字，保留既有名字、占位頓號及正文。依 UTF-16 計算位置，使用 `requiredRevisionId` 防止寫入過期位置；衝突或結果不明會先重讀，已找到簽名就不再插入。只有確認文件已有簽名後，才以 SQLite 交易保存已讀紀錄及通知更新需求。簽名未確認成功不顯示已看過；中斷後再次點擊會沿用同一身分接續，不代其他人簽到。

更新以通知鎖保護，PDF 原訊息透過 `editMessageCaption` 更新「已看過：…」，成功不另發群組訊息。名單超過長度時提供分頁，不遺漏名字。網路失敗保留 dirty 標記，每 15 秒重試；限流遵守 Retry-After；訊息刪除／權限問題記錄明確失敗，不持續刷錯誤。再次點擊可重新嘗試更新；文件簽到不會因此重複。恢復時可由已送達 outbox 紀錄重建按鈕綁定，恢復通知不會重改 Review 或重新匯出 PDF。

PDF 不因簽到而重產；「已看過」表示使用者主動按下簽到，不偵測實際讀完內容，也不代表審稿核准。Google Docs 的簽到串可累積跨版本參與者，Telegram 名單則分送審快照計算。新通知的 outbox kind 為 `review_document`，同一筆送達收據同時供按鈕綁定及訊息類型判斷，無需資料庫 schema 異動。舊版 `review_pdf`／`review_notice` 待送佇列仍依原順序接續，避免重送附件；已送達的舊文字通知仍以 `editMessageText` 更新名單，不主動刪除或合併群組歷史訊息。

API 依據：[Google PDF 匯出](https://developers.google.com/workspace/drive/api/guides/manage-downloads)、[Google Docs 版本寫入保護](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/batchUpdate)、[Telegram 文件附件](https://core.telegram.org/bots/api#senddocument)。

## 回覆格式

系統 prompt 明確要求一般回覆、補問與候選文字使用純文字：不輸出 Markdown 標題、清單、粗體、表格、程式碼區塊、格式化連結，也不輸出 HTML 標籤或實體編碼。使用句子與換行整理資訊，URL 直接列出。從 Wiki 或搜尋取得的格式化文字須轉述成純文字。

Agent 文字原本即以 `parse_mode=None` 發送；補問選項改用「選項 1：」避免產生 Markdown 編號清單。`check_agent.py` 的真實模型唯讀驗收同時檢查一般回覆與補問是否出現常見 Markdown／HTML 語法。工具本身仍使用 JSON schema，系統產生的 Telegram 身分標註保留必要的 Telegram 格式。

## Reaction 與 typing

依年度 bot 的互動方式，收到授權且未重複的業務訊息先加 👀，同時在原 topic 每 4 秒更新 typing。提示包在處理鎖與 agent 名額外，排隊時也看得到；結束或取消時取消並等待背景工作清理。`/reload` 也有進度提示。

完成回覆的 reaction 為 👍，模型可透過 `react_heart` 選擇 ❤。採 👍 是因 [Telegram 官方 reaction 清單](https://core.telegram.org/bots/api#reactiontypeemoji)不含參考程式的 ✅。補問預設保留 👀；程式錯誤或訊息尚未全部送達時，不標示完成。

完成 reaction 與觸發訊息 ID 存在 outbox 的 JSON 內。只有該事件的所有訊息都已確認送達才更新；延後送出或人工重送同樣遵守此規則。舊 outbox 沒有這些欄位也能正常送出，不需 schema migration。

提示是獨立的附加功能：API 有 3 秒時間限制，失敗記錄 DEBUG、不影響業務結果；typing 遵守 Retry-After，遇到沒有權限或不支援的請求時停止。未授權訊息沒有提示。按鈕會顯示 typing，但不對 bot 的選單訊息加 reaction。

## 通知與重新啟動

通知內容與原群／topic 先寫入 outbox，再送 Telegram。送出前狀態改為 `sending`，取得 message ID 才改 `sent`。

| 狀態 | 意義與處理 |
| --- | --- |
| pending | 尚未送出，背景工作每 15 秒嘗試；遇限流遵守 Retry-After。 |
| sending | 已開始送出；若此時當機，重啟改為 uncertain。 |
| sent | 有確認回應，不自動重送。 |
| failed | Telegram 明確拒絕，需處理原因。 |
| uncertain | 連線逾時或當機，可能已送達；避免盲目重送。 |

`/delivery` 列出本群未完成通知。管理員查核後使用 `/resend 編號`，重送可能重複，需人工判斷。當機前完成的 review 收據可恢復成通知；中斷中的其他操作列出 ID 與狀態，不自動重跑業務寫入。

## Agent 工具清單

| 工具 | 功能 |
| --- | --- |
| resolve_member | 精確查詢名冊身分、解析「我」。 |
| create_card／resume_operation | 開卡及接續部分完成的開卡／補建流程。 |
| add_card_document | 依既有卡號補建資料夾與範本文案，保留原卡並去重。 |
| gitlab_get_issue | 卡片、關聯卡及選讀留言。 |
| gitlab_search_issues | 標題、label、負責人、狀態、日期篩選，預設 opened 含 Review。 |
| gitlab_update_issue | 標題、description、到期日、負責人、既有 label、狀態。 |
| gitlab_list_labels | 讀取既有 label 定義。 |
| gitlab_comment_issue | 使用者要求時留言，使用操作識別查重。 |
| gitlab_link_issues／gitlab_unlink_issues | 同專案卡片的關聯與解除。 |
| gitlab_set_issue_state | 明確關閉／重新開啟卡片。 |
| review_cards | 批次送審與固定格式通知。 |
| mention_editors | 標註全部名冊成員，包含發指令者。 |
| search_wiki／read_wiki_page／read_wiki_document | 查 Wiki 與 Wiki 連結的 Google Docs。 |
| memory_list／memory_remember／memory_forget | 本群持久記憶，上限 30 筆、每筆 500 字。 |
| web_search | 獨立模型的公開網路搜尋與來源。 |
| react_heart | 選擇在本輪回覆送達後，對觸發訊息按 ❤；不能指定其他訊息。 |
| ask_user | 缺資訊時結束本輪並補問。 |

不提供 label 定義的新增、修改、刪除，不提供刪卡、任意 HTTP、Shell、任意 Drive 寫入工具。GitLab description／留言中的 quick action 會跳脫，避免繞過工具限制。

## 儲存與 migration

`migrations/001_initial.sql` 建立授權、群組記憶、operations、resources、events、outbox、audit_log。`002_event_delivery.sql` 增加中斷事件恢復所需的 topic、操作者與訊息 ID。`003_review_signatures.sql` 新增 PDF 快照、文件簽名、各次已讀名單與訊息更新表。`004_optional_card_resources.sql` 在同一交易中複製既有資源對照並重建表，讓 `folder_id` 可為空值；保留全部舊資料與主鍵，讓僅開卡也有明確資源紀錄，供改卡及送審判斷。啟動時以 `schema_migrations` 追蹤並依序套用，交易失敗會 rollback。

SQLite 使用 WAL。備份必須採 SQLite backup API 或停機後完整備份資料，不單獨複製運作中的主檔。不要刪除 events／operations 作為「清快取」，否則會失去去重與接續依據。

audit_log 保存群組、操作者、action、status 與工具名稱；operations 保存執行 payload、步驟及結果；日常非觸發聊天不保存。`data/` 包含內部資料，應限制主機權限與備份存取。

## API、部署與憑證

沒有公開 HTTP API 端點。CLI 有 `--check-services`（唯讀服務檢查）、`--check-ai`（主模型與公開搜尋檢查）。測試容器使用 Compose `test` profile，無網路也不掛載憑證。

部署與維護步驟見 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)，完整環境變數見 [.env.example](.env.example)。Google 憑證與獨立搜尋設定從指定年度 bot 複製到本專案；DeepSeek 主模型自 2026-09-25 改走使用者指定的 Cloudflare AI Gateway。GitLab 與 Telegram 使用本專案專用 token。沒有複製年度 bot 的群組記憶或業務資料。

主模型保留 `LLM_PROVIDER=openai_compat`，以 `LLM_BASE_URL=https://cf-ai.yuan-tw.net/compat`、`LLM_MODEL=dynamic/sitcon` 呼叫 `POST https://cf-ai.yuan-tw.net/compat/chat/completions`。`LLM_API_KEY` 保存 Cloudflare token，SDK 以 Bearer 認證；base URL 不包含 `/chat/completions`，避免 SDK 重複附加路徑。自 2026-09-26 起，請求中的 `model` 改為原樣傳送使用者指定的 `dynamic/sitcon`。

這次沿用 Chat Completions 的訊息、function calling 與工具結果往返，不新增 Responses adapter、依賴或資料表。選擇此方式是因為 gateway 與既有 adapter 相容，能以環境設定完成遷移。網路搜尋仍使用獨立 `WEB_SEARCH_*` 設定，不與主模型共用新憑證。Cloudflare token 與該 gateway 的 `dynamic/sitcon` 路由必須可用；錯誤沿用既有遮蔽憑證的 log 與操作錯誤處理。參考 [Cloudflare 相容端點](https://developers.cloudflare.com/ai-gateway/usage/chat-completion/)。

直接使用 service account 存取 Shared Drive，不使用網域委派。GitLab 帳號須在固定專案有 Developer 權限並能讀 Wiki；Google 帳號須能讀名冊、複製範本及在根目錄建立內容。

`.env` 權限為 600；JSON 由 root 與容器群組 10001 讀取，權限 640；憑證、資料庫、原始 n8n 檔不進映像。Log 對設定的 token／API key 遮蔽，httpx 不開 INFO URL 紀錄。

若已有 webhook，程式會拒絕啟動 long polling。切換工具需明確 `--confirm-switch`，先備份舊 webhook 資訊到 `data/`，再以 `drop_pending_updates=false` 移除 webhook。此工具本次尚未執行；不默默接管既有 n8n。

## 已知限制與待處理事項

- 已依使用者明確授權啟動正式服務；目前沒有人工建立測試卡、測試檔或發送測試群組通知。這些寫入路徑以隔離模擬測試覆蓋。
- Telegram API 沒有客戶端 idempotency key，無法承諾通知恰好一次。結果不明時由管理員查核。
- 只支援單一 bot instance／SQLite 資料目錄；不能用 Compose replicas 啟動多個 poller。
- 2026-09-24 唯讀檢查發現名冊 `@Zctong`、`@yorukot` 的 GitLab ID 查詢回 404；這兩筆指派會被拒絕，Telegram 標註不受影響。請在原表查核修正，程式不猜測替代 ID，也不自行修改原表。
- LLM 意圖理解可能有誤；程式層固定專案／路徑、驗證參數與標籤，外部文字在 prompt 中標示為資料。這些措施不代表模型不可能受提示注入影響。
- 不移動舊卡資源，不在改卡名／日期後同步改資料夾／文件名稱，不自動撰寫整份文案。
- 不編輯 Wiki／名冊，不提供年度 bot 的行事曆、HackMD 寫入、照片或年會名冊功能。
- 多人／外部系統同時修改卡片仍可能衝突。讀回會檢查實際結果，但 GitLab REST 與 Google 之間沒有跨服務交易。
- GitLab Issue API 的 labels 介面以名稱操作；每次送出前即時檢查存在，但外部管理員同時刪除標籤仍有競態窗口。
- 短期補問脈絡不跨重啟；需要使用者重新提供上下文。長期群組記憶與操作紀錄會保留。


## Cloudflare 切換驗證（2026-09-25）

- 指定的 `https://cf-ai.yuan-tw.net/compat/chat/completions` 實際回應 HTTP 200，`deepseek/deepseek-flash` 可取得文字回覆。
- 透過 Compose 執行 `--check-ai`，主模型與原有獨立搜尋均成功；搜尋回傳 1 筆來源，沒有工具錯誤。
- `scripts/check_agent.py` 透過新 gateway 完成查詢實際標籤、工具結果往返、相對日期補問、同意選項、純文字回覆與 MMDD 開卡參數檢查；遠端寫入為 0。
- 重新建置正式與測試映像，164 項隔離測試、Ruff check 與 format check 全部通過。這次僅調整環境設定及文件，沿用既有測試與 adapter，沒有新增程式邏輯。
- Compose 已重建正式 bot 容器；確認容器內的 gateway URL／模型設定正確，log 顯示 `Editorial bot ready`、`Application started`，restart count 為 0。
- `.env` 只有主模型的 API key、base URL、model 三個值變更；搜尋設定不變。憑證僅保存在權限 600 且不進 Git 的 `.env`，切換用暫存檔已移除。

## dynamic/sitcon 設定驗證（2026-09-26）

- `.env`、`.env.example` 與部署文件已改為 `LLM_MODEL=dynamic/sitcon`；既有 adapter 原樣傳送模型名稱，無須修改程式邏輯。
- 既有 Docker 測試服務的 16 項 LLM adapter 測試通過。
- 首次真實主模型請求回傳 HTTP 400：上游回報收到不受支援的 `deepseek-v4.1-flash`，當時未重新載入正式服務。依使用者要求重試後，`dynamic/sitcon` 已成功回覆 `OK`，`stop_reason=stop`。
- 已執行 `docker compose up -d --no-deps bot` 重建正式容器，確認容器內 `LLM_MODEL=dynamic/sitcon`。啟動 log 顯示 `Editorial bot ready`、`Application started`，容器為 running，restart count 為 0。

## 本次交付驗證

### 2026-10-05 開卡與補問修正

- 補問按鈕新增 6 個真實 Telegram Update 結構的回歸案例，修正前全部重現拒絕；修正後保留原群、原提問者及原問題訊息驗證，並沿用保存的話題接續。
- 一般開卡改為預設建立資料夾與範本文案，明確「只開卡」仍不呼叫 Drive／Docs；舊操作沿用當時保存的模式。
- 既有卡片補建覆蓋跨請求去重、同時請求、既有文件／資料夾、權限、失敗接續、未知寫入查找與恢復通知；未實際替 #682 建立遠端資源。
- 重建後的 Docker 測試映像通過 271 項測試，Ruff check 與 format check 通過；正式映像建置成功。真實模型驗收需另取得外部資料傳送授權，不列為已通過。
- 正式 bot 容器已重建並啟動，紀錄出現 `Editorial bot ready` 與 `Application started`。容器內文案預設為 `true`、補建工具可用，五個修改程式檔的 SHA-256 與工作目錄一致；沿用原資料卷及環境設定。

### 2026-10-05 相對日期、補問續接與待辦分類

- 今天／明天／後天依同一輪台灣時間直接換算，覆蓋 UTC 與台灣日期不同、跨月、跨年及閏年；保留明確日期與 MMDD 支援，模糊日期仍補問。
- 文字回覆脈絡遺失的 6 個案例及多輪補問答案遺失的 2 個案例，均先重現再修正；一般群組不以回覆串 ID 當 topic，論壇的原話題／原操作者限制仍有效。
- 預設未完成查詢在後端排除 Review；明確 Review、全部未關閉及所有狀態查詢仍可取得送審卡，未修改實際卡片狀態。
- 重建後的無網路 Docker 測試映像通過 310 項測試，Ruff check 及 format check 通過。真實模型驗收仍未執行，沒有建立測試卡片、文件或發送測試通知。
- 正式 bot 已重建，啟動紀錄顯示 `Editorial bot ready`、`Application started`；容器 running、restart count 0，六個修改程式檔的 SHA-256 與提交內容一致。保留既有資料卷及環境設定，沒有新增依賴、環境變數或 migration；重啟前的短期補問需重新提出需求。

### 先前交付紀錄

- 164 項測試於最終 Docker 測試映像通過；Ruff check 與 format check 通過。
- 原通知 callback 因回覆串 ID 不同被誤擋：新增真實 Telegram Update 結構的 6 個回歸案例，修正前均重現拒絕、修正後可簽到／分頁；另覆蓋撤銷授權及其他 review 混用訊息 ID。舊測試的跨 topic 案例改為不同 message_id，以符合轉傳會建立新訊息的實際契約。
- #678 文案成功唯讀匯出 76,429 bytes PDF，確認存在文件 revision 及唯一簽到欄位，未實際插入測試簽名或發送測試附件。
- Google、GitLab、Telegram 的 Compose 唯讀預檢通過；沒有執行真實建卡、建檔或群組通知測試。
- 主模型及獨立公開網路搜尋可回應；真實 agent 查 label、相對日期補問、同意按鈕候選、純文字回覆及 MMDD 開卡參數解析檢查通過，驗收腳本在工具邊界封鎖寫入。
- 最終正式映像 pip-audit 未發現已知套件弱點；交付檔案未包含實際 token 或 API key。
- 使用者已停用舊 n8n，唯讀確認 webhook 已解除，當時尚有 3 筆待處理 Telegram updates。
- 使用者已明確授權啟動並處理 pending updates；`docker compose up --build -d bot` 成功，log 顯示 `Editorial bot ready` 與 `Application started`。初次檢查容器 running、restart count 0、兩版 migration 已套用；授權群組初始為 0，待管理員在群組 `/authorize`。

API 行為參考：[GitLab Issues API](https://docs.gitlab.com/api/issues/)、[Google Docs tabs](https://developers.google.com/workspace/docs/api/how-tos/tabs)、[python-telegram-bot Application](https://docs.python-telegram-bot.org/en/stable/telegram.ext.application.html)。依賴修補參考：[cryptography changelog](https://github.com/pyca/cryptography/blob/main/CHANGELOG.rst)。
