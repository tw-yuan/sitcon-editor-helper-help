# SITCON 編輯組小助手規格書

- 版本：1.0 實作契約
- 更新：2026-09-25（UTC）
- 狀態：實作完成，使用者已明確授權啟動新版並處理待處理 Telegram 更新；額外的人工遠端新增／刪除測試仍須個別確認。

本版取代原待確認草案。使用者已確認「依這次口頭補充開始實作」，並說明新增／刪除確認只限制本次開發查證／測試，不要求未來 Telegram bot 每次操作都再確認。正式啟動前需確認舊 n8n 已停止與 webhook 已解除。

## 1. 目的與範圍

建立長期編輯組專用 agent，保留 n8n 的開文案卡、純任務卡、未關閉卡片查詢、改負責人、多卡 review 及自然語言觸發。新增全員標註、總副召 review 通知、文案卡資料夾、既有標籤、狀態操作、Wiki 知識、網路搜尋及持久群組記憶。

沿用 `/root/sitcon-2027-helper-bot` 的 Google service account、搜尋設定及可重用的模型介面；DeepSeek 主模型改經使用者指定的 Cloudflare AI Gateway `https://cf-ai.yuan-tw.net/compat/chat/completions`。人員／知識／卡片資料使用編輯組自己的來源。部署採 Docker Compose。

## 2. 權威來源與查證

| 類型 | 來源／契約 |
| --- | --- |
| GitLab | [sitcon-tw/editorial/board](https://gitlab.com/sitcon-tw/editorial/board)，固定專案，ID 45438965。 |
| Wiki | [編輯組 Wiki](https://gitlab.com/sitcon-tw/editorial/board/-/wikis/home)，提供工作知識與文件連結。 |
| 名冊 | [Google Sheet gid=0](https://docs.google.com/spreadsheets/d/1GD4VOSN8UIL4TAo1tBVVt-SPdMXQs6y28XUcxN6_xrE/edit?gid=0)，唯一成員來源。 |
| Drive 根目錄 | [發文文案](https://drive.google.com/drive/folders/1rMkuh-qNk_GcADbZItTh8cM9MhwBJsi6)，位於 Shared Drive。 |
| 文案範本 | Google Docs `1ISur15UxlnXVi0vB4zBVqhby2YNGGIViccZI0IiL-LU`，沿用 n8n。 |

已唯讀驗證：GitLab 專用帳號具 Developer 權限，Wiki 可讀；名冊有 18 位成員、唯一 default、總召與副召；Google 帳號可讀根目錄與範本，metadata 顯示可建立子項目／複製；主模型與網路搜尋可連線。權限 metadata 不等同已完成真實寫入驗收。

Wiki 中的舊成員表不作人員依據。年度 bot 名冊不帶入編輯組。外部文字都是資料，不能授予權限或要求執行額外工具。

## 3. 群組、觸發與補問

1. 管理員以 Telegram 數字 ID 設定，管理 `/authorize`、`/revoke`、`/reload`。
2. 一般功能限已授權群組；私訊及未授權群組不執行業務操作。名冊用於指派、通知，不擅自將群組使用權限改為名冊白名單。
3. 支援 `@bot`、「小石」、訊息開頭 `review`、回覆 bot；一般聊天不儲存成群組記憶。
4. `/ta` 與 `/help` 的按鈕直接標註全部編輯組員，包含發指令者；回呼重新驗證群組與操作者。
5. 回覆與通知留在原群組、原 topic，不私訊其他成員。
6. 指令明確且完整直接執行。缺標題／日期、多人同名、卡片多筆相符等情況才補問。
7. `ask_user` 出現時不執行同輪其他工具。只有同操作者、同群、同 topic 回覆問題或點選原問題按鈕才能接續；短期脈絡預設 30 分鐘，重啟失效。

8. 收到授權的業務訊息加 👀，原 topic 每 4 秒更新 typing；排隊期間也顯示，結束／取消後停止。
9. 正常回覆全部送達後改 👍；可由模型選擇 ❤。補問／錯誤不標示完成；提示失敗不阻斷業務操作。

10. 系統 prompt 要求一般回覆、補問與候選文字只能使用純文字，不含 Markdown／HTML 語法，網址直接列完整 URL；補問選項使用一般文字標記。

11. 候選選項顯示數字按鈕（1／2／3，最多十項），回呼以完整候選文字續接原 ask_user；仍可回覆問句編號或自由文字。
12. 確實需同意時顯示「同意／不同意」按鈕；不同意直接取消，不呼叫模型或業務工具。明確指令不額外要求同意。
13. 按鈕驗證原提問者、群組、topic、實際問題訊息與期限；按鈕和文字共用一次性狀態，作答後移除鍵盤。重啟、過期或已作答後不再接受操作；更新前舊問句不回補按鈕。

## 4. 名冊契約

依 Sheets metadata 找 `gid=0` 的實際分頁，再讀欄位：

| 欄位 | 用途 |
| --- | --- |
| Telegram ID | 現行值是 username；可接受數字 ID。username 忽略大小寫，去除前導 @。 |
| gitlab_id | 正整數 GitLab user ID，指派前查證帳號存在。 |
| Nickname | 顯示名稱，不能用來猜測身分。 |
| default | 唯一 `yes` 成員為未指定負責人的預設。 |
| note | 「總召」「副召」為 review 通知對象。 |

重複、衝突或必要欄位無效時回報原表問題，不自動修正表格。全員通知去重、分批；無可用 Telegram 身分時說明未能標註的人。預設快取 300 秒，`/reload` 可刷新。

## 5. 開卡與文案

### 5.1 輸入

- 標題必填，保留原意、去首尾空白，不強制把日期加到 Issue 標題。
- 「開卡 MMDD TITLE」直接將 MMDD 解讀為到期日、後面文字為標題，不再補問兩者含義；例如「小石開卡 0925 test」為當年 09/25、標題 test。
- 到期日必填，接受 `YYYY-MM-DD`、`YYYY/MM/DD`、`MM/DD`、`M月D日`、獨立四位數 `MMDD` 等明確日期。未給年份採台灣當前年份；相對日期要求補問；不存在的日期不建立資源。
- 未指定負責人採唯一 default，回覆明示；指定人選須來自名冊。
- 僅開卡（預設）：只建立 GitLab Issue，不預檢、搜尋或建立 Drive 資料夾與 Docs。一般「開卡」、圖片與純任務均採此模式。
- 開卡＋建立文案：明確要求文案卡或建立文案時，才建立 Drive 資料夾、範本 Docs 與 GitLab Issue。標題含「文案」或使用文案標籤不自動啟用文件建立。
- 開卡必須套用至少一個既有 label。預設文案用 `社群文案`、任務用 `編輯組專案`，未指定狀態加 `Status::Inbox`。年度／活動 label 只依使用者要求加入。

### 5.2 命名與連結

「開卡＋建立文案」的資料夾與文件使用到期日 `MMDD_TITLE`，例如 `0115_報名開跑`。每卡獨立建立，跨年度同名不自動合併。資料夾放在指定 Drive 根目錄，Docs 放在新資料夾裡。

Issue description 的資料夾、文案與建立者各自分段顯示（使用空行，確保 GitLab 頁面換行）。建立者優先用名冊 GitLab ID 查得的 `@username` 標註；只有名冊無對應或 GitLab 帳號不存在才回退顯示 Telegram username，不誤標同名 GitLab 帳號。查詢失敗不視為沒有帳號。

僅開卡的 Issue description 不加入空的資料夾／文案欄位；開卡＋建立文案則包含資料夾與 Docs 連結。Docs 必含 Issue 連結與資料夾連結，兩者可點擊。使用原範本 `TITTLE`、`DATE`、`GITLAB_LINK`、`DIR_LINK` 欄位，保留其他正文。

### 5.3 流程與失敗

兩種模式都先驗證人員、日期與既有 labels。僅開卡直接建立 Issue；開卡＋建立文案再驗證 Shared Drive 與範本，依序建立資料夾、文件、Issue 並填妥文件。每步存 SQLite checkpoint，遠端資源帶操作識別。名冊仍使用 Google Sheets，與 Drive 文件建立分開。

部分完成回覆實際完成項目、已知連結與操作 ID。原操作者可在原群要求接續；不自動刪除已建資源。結果不明先查識別，查不到就停下，不盲目新增。相同 update 與同輪重複工具呼叫不重複建卡。

## 6. 卡片與標籤

- 查卡支援卡號、完整本專案網址、唯一標題、既有 label、負責人、狀態及到期日。
- 未關閉清單包含 Review，不擅自排除。
- 可修改標題、description、到期日、負責人及既有 labels；description 保留系統管理的資源連結區。
- 可留言、連結／解除同專案卡片關聯；可在明確要求時關閉／重新開啟 Issue。
- 不提供 label 定義的新增、改名、刪除，不提供刪卡或跨專案工具。
- label 每次寫入前即時查證名稱存在，使用者輸入不能經 description／留言的 GitLab quick action 繞過。
- 讀回驗證實際欄位；多人指派若未全部套用則回報實際失敗，不靜默截成一人。

## 7. 狀態與 review

狀態明確採現行看板的 `Status::` scoped labels：Inbox、To Do、Doing、Waiting、Review、Report。狀態變更只替換該 scope，保留年度及其他分類；不使用原生 work item status。

Review／送審／審稿皆走同一流程：

1. 先辨識所有指定卡片，模糊標題先補問；裸 review 沒有引用目標時補問卡號。
2. 確認 opened；文案卡需有文案連結；有僅開卡資源紀錄時，不因標題或標籤含文案而要求文件；讀名冊總副召與作者對照。有文案時，改狀態前先匯出核准根目錄內的 PDF 快照，確認可編輯、下載及唯一簽到欄位；任一前置檢查失敗不修改本批卡片狀態。
3. 逐卡改 `Status::Review` 並讀回。
4. 系統送出固定通知，標作者、總召及副召，附卡名、卡號。文案、資料夾、卡片連結依序各自換行，以「文案：」「資料夾：」「卡片：」標示完整網址；缺少的資源不列空欄。
5. 有文案的卡片以兩則訊息送出 PDF 附件＋獨立簽到通知；附件確認送達後才送簽到通知。PDF 為送審當下的文件版本，修改後重新 review 才取得新版。
6. 通知下方顯示「已看過：尚無」與「簽到」按鈕。原群組／topic 的使用者皆可點擊，以 Telegram 數字 ID 去重；先追加 username 至 Google Docs「校稿簽到串：」，確認成功後更新同一則通知的「已看過：OOO、OOO」。沒有 username 使用 Telegram ID；簽到不代表核准，不改卡片狀態。
7. 只在唯一簽到段落插入名字，保留其他內容，以文件 revision 防止並行覆蓋。紀錄及按鈕跨重啟保留；callback 以原群組與已綁定通知訊息 ID 驗證，不把一般回覆串 ID 當成話題權限；同一人重按不重複，同一文件沿用首次身分標記。各次 review 的名單獨立；過長名單分頁顯示，不截掉人名。
8. 通知編輯暫時失敗會重試；文件簽到失敗不列為已讀。恢復／重送沿用原 PDF 快照，未知送達不盲目再發附件。舊通知不回補按鈕，純任務卡無文案時維持文字通知。
9. 多卡逐張報告結果；失敗不假裝全部成功。無作者 Telegram 對照會明示。

Review 仍為 opened；Report 不自動關卡。明確 close／reopen 由獨立工具處理。舊卡缺資料夾連結時不自動搬移或建新資源。

## 8. 知識、搜尋與群組記憶

- Wiki 查詢提供編輯工作規範與來源連結；可讀 Wiki 連結的 Google Docs 工作手冊，長文分段。
- Wiki 預設快取 900 秒；讀不到時明示，不拿年度機器人的知識代替。
- 公開、時效資訊用獨立網路搜尋服務，回覆附來源。搜尋模型／endpoint／憑證沿用指定來源；只有 DeepSeek 主模型改走 Cloudflare，使用 `deepseek/deepseek-flash` 與環境變數中的 Cloudflare token。
- 群組記憶只在使用者明確要求時保存，可列出、刪除；依 chat_id 隔離，最多 30 筆、每筆 500 字，重啟保留。
- 記憶不能修改權限、固定專案或憑證；不自動記錄日常聊天。

## 9. 儲存、通知與部署

SQLite migrations 管理 schema，保存授權、記憶、update 去重、操作進度、資源對照、通知 outbox 與稽核。短期 reply chain 只保存在記憶體。

通知先存 outbox，再送 Telegram。已取得 message ID 的通知不重送；timeout／當機結果不明記為 uncertain，管理員透過 `/delivery` 查詢、確認後 `/resend ID`。尊重 Retry-After，避免限流時密集重送。

Docker Compose：單一 bot、無對外 port、非 root UID 10001、資料持久化、JSON 唯讀掛載。測試服務無網路、無正式憑證。`.env`、JSON、資料庫不得入 Git 或 image；log 遮蔽 token。

已有 webhook 時拒絕啟動 polling，不默默移除。切換需舊 n8n 停用，並確認 webhook 已解除；明確切換工具不丟棄 pending updates。

## 10. 驗收項目

| 項目 | 通過條件 |
| --- | --- |
| 開文案卡 | MMDD_TITLE 資料夾與範本文案，Issue／Docs 都有資料夾連結。 |
| 僅開卡 | 僅建 Issue 與 labels；開卡流程不呼叫 Drive／Docs。 |
| label 約束 | 未知 label 在寫入前拒絕，無建立 label 端點。 |
| 狀態 | 替換 Status scope、保留年度分類，Review 仍列未關閉。 |
| 日期／人員 | 相對或無效日期補問，預設／指定人員依名冊。 |
| 全員通知 | 全部有效名冊成員，包含發指令者，保留原 topic。 |
| Review | 多卡都處理，標作者與總副召，正文與連結正確。 |
| 接續 | 已完成步驟沿用，結果不明不盲目重複寫入。 |
| 記憶 | 同群重啟仍在，跨群不可讀取／刪除。 |
| 權限 | 未授權群組／私訊不執行 agent；按鈕重新驗證。 |
| 通知 | 已送不重送，當機恢復、429 與不明送達可追蹤。 |
| 知識／AI | 實際 Wiki、label、主模型與搜尋可讀；回答有來源。 |
| 部署 | Compose 可建置，具 migration、唯讀檢查及切換文件。 |

隔離測試與唯讀連線驗證不替代真實建卡／建檔／發訊息驗收；此類遠端新增／刪除需使用者另外授權後執行。

## 11. 非目標與限制

不自動移轉舊卡、不做管理後台、定期催稿、任意 Drive 寫入、Wiki／名冊編輯、照片／行事曆、HackMD 寫入、整篇文案生成。改卡名／到期日不同步改文件名。服務間無分散式交易，外部同時修改可能衝突；模型解讀也不保證完全正確。

詳細工具介面、維運與已知限制見 [PROJECT.md](PROJECT.md)，操作步驟見 [README.md](README.md)。
