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
| OpenAI／Anthropic SDK | 沿用可替換 provider 的主模型介面；搜尋另有自己的服務與憑證。 |
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
| 文案與附件 | 指定 Drive 根目錄與 Docs 範本 | 新卡各自擁有 `MMDD_TITLE` 子資料夾。 |
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
- 觸發方式：`@bot`、文字含「小石」、訊息開頭 `review`、回覆 bot。一般提到 review 的聊天不觸發。
- 通知回原群組與 topic，不另外私訊被標註者。
- 補問與 transcript 只由同群、同 topic、同操作者的 reply chain 續接。TTL 預設 30 分鐘，重啟後失效；長期記憶不失效。
- 補問工具與其他工具同輪出現時，其他工具一律不執行，避免資訊尚未完整就先寫入。

## 開卡資料流

1. 使用者提供標題與明確到期日。支援獨立四位數 MMDD；「小石開卡 0925 test」直接解析為當年 09/25 到期、標題 test，無需補問。MMDD 使用真實日曆驗證，卡號及較長識別碼不當作日期。缺年份用台灣當前年份；相對日期補問。日期必須真實存在，也必須出現在使用者的指令／續接脈絡中。
2. 將指定 Telegram username 對應名冊 GitLab ID；未指定時採唯一 `default=yes`，回覆明示。
3. 驗證所有 label 已存在。未指定 label 時，文案卡套 `社群文案`、任務卡套 `編輯組專案`；沒有指定狀態時加 `Status::Inbox`。不自動加年度 label。
4. 預檢固定根目錄的 Shared Drive 屬性與建立權限；文案卡再檢查範本可複製、必要欄位存在。
5. 在固定根目錄建立到期日 `MMDD_TITLE` 資料夾；文案卡複製範本到其中，名稱也使用 `MMDD_TITLE`。
6. 建立 GitLab Issue。description 包含資料夾連結、可選文案連結、建立者及操作識別。
7. 讀回 Issue，驗證 labels 與所有負責人。GitLab 若不接受多人，不把部分成功說成全部成功。
8. 在文案填入 `TITTLE`、`DATE`、`GITLAB_LINK`、`DIR_LINK`，並設定兩個 URL 為可點擊連結。保留其他範本正文。
9. 保存 Issue／資料夾／文件對照，回覆實際網址與操作 ID。

資料夾每張卡獨立，不以名稱查找合併：相同 `MMDD_TITLE` 仍可能是不同年度或不同工作。重試以操作識別及 Drive `appProperties` 對應資源。非文案卡同樣有資料夾，但不建立 Docs。

## 失敗與重試

開卡是跨 Google／GitLab 的分步流程，不是原子交易。每次新增前先記錄 pending，成功後保存資源 ID。若連線中斷，先用操作識別查找遠端資源；找到則接續，找不到且結果仍不明則停下，避免盲目重建。

失敗回覆會列出操作 ID、完成步驟與已知連結。使用者可要求 `接續操作 ID`，只允許原操作者在原群接續。不會自動刪除已建立資源。相同 Telegram update 不再執行；同輪相同工具參數有持久收據，開卡未完成時也不能以不同參數另開一份。

已成功的文案替換在接續時先檢查連結，避免重複替換使用者標題中的文字。只允許在核准根目錄內、且屬於本次資料夾的文件上填入範本。

## 狀態與 review

現行看板使用 `Status::Inbox`、`Status::To Do`、`Status::Doing`、`Status::Waiting`、`Status::Review`、`Status::Report`。系統使用這些 label，不以原生 work item status 驅動看板。

改狀態只加入選定狀態並移除同 scope 舊狀態，保留年度及其他分類。Review 仍是 opened。關閉／重新開啟由獨立工具處理，需使用者明確要求。

批次 review 會先辨識全部目標，若某張標題模糊、已關閉、文案卡缺連結，則在寫入前回報。接著逐卡切換狀態、讀回、保存通知內容，再由 Telegram gateway 發送。通知列卡號、卡名、作者、總副召、文案／資料夾／Issue 連結。單張 API 失敗不會把全部卡片說成成功。舊卡可從 description 取連結，不會自動幫舊卡建資料夾。

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
| create_card／resume_operation | 開卡及接續部分完成流程。 |
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

`migrations/001_initial.sql` 建立授權、群組記憶、operations、resources、events、outbox、audit_log。`002_event_delivery.sql` 增加中斷事件恢復所需的 topic、操作者與訊息 ID。啟動時以 `schema_migrations` 追蹤並依序套用，交易失敗會 rollback。

SQLite 使用 WAL。備份必須採 SQLite backup API 或停機後完整備份資料，不單獨複製運作中的主檔。不要刪除 events／operations 作為「清快取」，否則會失去去重與接續依據。

audit_log 保存群組、操作者、action、status 與工具名稱；operations 保存執行 payload、步驟及結果；日常非觸發聊天不保存。`data/` 包含內部資料，應限制主機權限與備份存取。

## API、部署與憑證

沒有公開 HTTP API 端點。CLI 有 `--check-services`（唯讀服務檢查）、`--check-ai`（主模型與公開搜尋檢查）。測試容器使用 Compose `test` profile，無網路也不掛載憑證。

部署步驟見 [README.md](README.md)，完整環境變數見 [.env.example](.env.example)。Google 憑證從指定年度 bot 複製到本專案；AI／搜尋設定同樣沿用。GitLab 與 Telegram 使用本專案專用 token。沒有複製年度 bot 的群組記憶或業務資料。

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


## 本次交付驗證

- 76 項測試於最終 Docker 測試映像通過；Ruff check 與 format check 通過。
- Google、GitLab、Telegram 的 Compose 唯讀預檢通過；沒有執行真實建卡、建檔或群組通知測試。
- 主模型及獨立公開網路搜尋可回應；真實 agent 查 label、相對日期補問及純文字回覆檢查通過，驗收腳本在工具邊界封鎖寫入。
- 最終正式映像 pip-audit 未發現已知套件弱點；交付檔案未包含實際 token 或 API key。
- 使用者已停用舊 n8n，唯讀確認 webhook 已解除，當時尚有 3 筆待處理 Telegram updates。
- 使用者已明確授權啟動並處理 pending updates；`docker compose up --build -d bot` 成功，log 顯示 `Editorial bot ready` 與 `Application started`。初次檢查容器 running、restart count 0、兩版 migration 已套用；授權群組初始為 0，待管理員在群組 `/authorize`。

API 行為參考：[GitLab Issues API](https://docs.gitlab.com/api/issues/)、[Google Docs tabs](https://developers.google.com/workspace/docs/api/how-tos/tabs)、[python-telegram-bot Application](https://docs.python-telegram-bot.org/en/stable/telegram.ext.application.html)。依賴修補參考：[cryptography changelog](https://github.com/pyca/cryptography/blob/main/CHANGELOG.rst)。
