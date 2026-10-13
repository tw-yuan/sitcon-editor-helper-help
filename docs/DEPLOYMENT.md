# 部署與維護

本文整理環境設定、服務啟動、測試與備份方式。以下指令皆在專案根目錄執行，環境變數清單見 [.env.example](../.env.example)。

## 環境準備

需要 Docker Engine 與 Docker Compose v2+、可對外連線的主機。不需要開 inbound port。只執行一個 bot instance。

```bash
cp .env.example .env
mkdir -p secrets data
chmod 700 secrets
chmod 600 .env
```

在 `.env` 填入本專案 Telegram token、管理員數字 ID、GitLab token、主模型與獨立搜尋的憑證及 endpoint。將 Google service account JSON 放到 `secrets/google-sa.json`。

部署時須提供 Google service account 與 Anthropic API key（主模型與搜尋可共用同一把）。不要將 `.env` 或 JSON commit。

```bash
sudo chown root:10001 secrets/google-sa.json
sudo chmod 640 secrets/google-sa.json
sudo chown 10001:10001 data
sudo chmod 750 data
```

Google 帳號須能讀指定名冊、複製範本、在指定 Shared Drive 根資料夾新增內容。本版本直接使用 service account，無網域委派。GitLab 帳號須加入 `sitcon-tw/editorial/board`，具 Developer 權限並可讀 Wiki。

名冊 `gid=0` 必須包含 `Telegram ID`、`gitlab_id`、`Nickname`、`default`、`note`。`Telegram ID` 填 username；`default=yes` 要唯一；總副召在 `note` 標記「總召／副召」。範本文字需要 `TITTLE`、`DATE`、`GITLAB_LINK`、`DIR_LINK`。

欄名大小寫不拘，`nickname` 可直接查詢負責人，唯一相符不再要求 Telegram username。查人、指派及通知會強制重讀名冊，修正原表後不必等 `CACHE_TTL_ROSTER` 或先 `/reload`；同名才補問候選。重新讀取失敗會回報錯誤並清除舊快取，避免繼續使用舊 ID。

## 主模型（Anthropic 官方 API）

主模型使用以下 `.env` 設定，`LLM_API_KEY` 填入 Anthropic API key：

```dotenv
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-5-5
LLM_BASE_URL=
LLM_AUTH_BEARER=false
LLM_THINKING=medium
LLM_REFUSAL_FALLBACK=true
```

`LLM_BASE_URL` 留空即送往 `https://api.anthropic.com/v1/messages`，以 `x-api-key` 認證。`LLM_THINKING` 對映 adaptive thinking 的 effort（目前 `medium`）；Sonnet 5.5 不接受 `thinking.type=disabled`，設 `off` 時 adapter 改送 `between_tools`。`LLM_REFUSAL_FALLBACK=true`（搜尋為 `WEB_SEARCH_REFUSAL_FALLBACK`）會在請求帶 `fallbacks: "default"`：安全分類器拒答時，`cyber`／`frontier_llm` 類別由伺服器改用 Sonnet 5 重跑；其他類別仍拒答時，bot 回固定說明。此 beta 僅官方 API 支援，改走 gateway 時要設 `false`。`WEB_SEARCH_*` 設定獨立的搜尋子呼叫，目前同樣走官方端點（`WEB_SEARCH_BASE_URL` 留空、`WEB_SEARCH_AUTH_BEARER=false`、`WEB_SEARCH_MODEL=claude-sonnet-5-5`）。`openai_compat` adapter 保留，要切回 OpenAI 相容 gateway 時只需改 `.env`。

修改 `.env` 後，先執行下方 AI 檢查，再用 `docker compose up -d bot` 重建容器載入新環境；單純 `docker compose restart` 不會載入新的環境變數。

## 建置與檢查

```bash
docker compose build bot
docker compose run --rm --no-deps bot --check-services
docker compose run --rm --no-deps bot --check-ai
```

服務檢查僅讀取 Google、GitLab、Telegram。AI 檢查會使用模型額度並做一次公開網路搜尋，不建卡、不建檔、不發 Telegram 訊息。

實際模型的唯讀 agent 驗收：

```bash
docker compose run --rm --no-deps \
  -v "$PWD/scripts:/checks:ro" \
  --entrypoint /app/.venv/bin/python bot /checks/check_agent.py
```

這個檢查會在執行邊界封鎖所有寫入工具，驗證讀取真實狀態標籤、模糊日期補問、同意按鈕候選、純文字回覆，並檢查 MMDD、今天／明天／後天的開卡參數及既有卡片補建工具選擇；只解析，不實際開卡。相對日換算沿用 `TZ=Asia/Taipei`，不使用主機 UTC 日期。

開卡解析案例包含 DDL／deadline 的大小寫、空格與冒號，以及保留 SQL DDL 與引用標題。完整 `@bot 開卡 開源築夢計畫 DDL 今天 @wolfdayo` 案例會先唯讀查名冊，再檢查模型提出的標題、日期、負責人 ID 與文案模式，不執行開卡工具。這些模型行為需實際 LLM 驗收才能確認；離線測試不能取代。

僅驗證暱稱、改表與使用者更正 ID 的模型行為，可執行：

```bash
docker compose run --rm --no-deps \
  -v "$PWD/scripts:/checks:ro" \
  --entrypoint /app/.venv/bin/python bot /checks/check_member_agent.py
```

此腳本使用合成名冊與記憶體資料庫，只連既有模型端點，不存取真實 Google／GitLab／Telegram 資料。開卡工具只擷取參數並中止，檢查 nickname 唯一／同名、舊拒絕後重查、資料表已更新、一般回覆及補問中的明確 ID 更正；所有案例保留原標題、期限與不要文件的要求。

## 啟動與舊 n8n 切換

先停用舊 n8n 工作流。`--check-services` 中的 `existing_webhook` 應為 `false`；若仍為 `true`，bot 會拒絕啟動，保留既有部署。

只有已取得切換授權、確定舊 n8n 不會重新註冊 webhook 時，才執行以下指令：

```bash
docker compose run --rm --no-deps \
  -v "$PWD/scripts:/checks:ro" \
  --entrypoint /app/.venv/bin/python bot /checks/switch_to_polling.py --confirm-switch
```

它會將原 webhook 資訊存到 `data/webhook-before-switch.json`，再移除 webhook，保留尚未處理的更新。若備份已存在會停止，避免覆蓋回復資訊；不要未查核就刪除備份。若舊工作流已自行解除 webhook，就不必執行切換工具。

```bash
docker compose up --build -d bot
docker compose logs --tail=100 bot
docker compose ps
```

首次啟動自動套用 `migrations/`，不需要 seed。授權群組初始為空；在目標編輯組群組中，由 `.env` 的 `TELEGRAM_ADMIN_ID` 使用者送出 `/authorize`，再使用 `/help`。

BotFather 的 privacy mode 必須允許讀取一般群組訊息，才能收到以「小石」或獨立單字 `review` 開頭的訊息。若使用群組 topics，回覆會留在原 topic。

## 管理與失敗接續

| 指令 | 用途 |
| --- | --- |
| `/authorize` | 管理員授權本群。 |
| `/revoke` | 管理員撤銷本群。 |
| `/reload` | 管理員刷新名冊與 Wiki。 |
| `/help` | 操作說明及全員標註按鈕。 |
| `/delivery` | 列出本群尚未確認送達的通知 ID。 |
| `/resend ID` | 管理員人工確認未送達後重送，可能造成重複訊息。 |

開卡中途失敗時，保留回覆中的操作 ID 與已建資源。要求「小石，接續操作 ID」，系統會沿用已建立的資料夾／文件／卡片，不自動刪除它們。若遠端寫入結果不明又查不到資源，會停止並要求人工查核。

一般開卡預設包含資料夾與範本文案，僅需卡片時請明確說「只開卡」。歷史卡片可要求「小石，幫 #682 補建資料夾與文案」；不會重新開卡，補建失敗也可用操作 ID 接續。升級不會主動修改歷史卡片、建立文件或發送測試群組訊息。短期補問在重啟後失效，舊按鈕需重新提出需求。

## 測試與格式

```bash
docker compose --profile test build test
docker compose --profile test run --rm --no-deps test -q
docker compose --profile test run --rm --no-deps \
  --entrypoint /app/.venv/bin/ruff test check src tests
docker compose --profile test run --rm --no-deps \
  --entrypoint /app/.venv/bin/ruff test format --check src tests
```

測試服務沒有網路、沒有正式 `.env` 或 Google JSON。涵蓋 label 白名單、狀態保留其他分類、日期驗證、部分完成接續、未知寫入不重送、review 多卡及總副召、跨群記憶、授權、補問、topic、通知當機恢復與模型／搜尋介面。

依賴稽核可在沒有掛載憑證的容器執行：

```bash
docker run --rm --entrypoint /bin/uv sitcon-editorial-bot:local \
  tool run pip-audit --path /app/.venv/lib/python3.12/site-packages --skip-editable
```

## 資料與備份

`data/editorial.sqlite3` 保存群組授權、長期記憶、操作進度、資源連結、通知狀態、PDF 快照與簽到紀錄。短期對話脈絡只存在記憶體。`secrets/`、`.env`、`data/` 不進 Git 或映像。

啟動時會依序套用尚未執行的 migration。`003_review_signatures.sql` 增加 PDF／簽到相關表；`004_optional_card_resources.sql` 保留既有資源對照，將資料夾欄位改為可空值，以支援僅開 GitLab 卡片。升級前先備份；舊資料夾與文件不會刪除。PDF 快照存於 SQLite，備份會包含附件；不自動清除歷史快照。

停機備份範例（資料包含內部資訊，備份檔請限制權限）：

```bash
docker compose stop bot
sudo tar -czf /path/to/private-backup/editorial-data.tar.gz data
sudo chmod 600 /path/to/private-backup/editorial-data.tar.gz
docker compose start bot
```

請自行將 `/path/to/private-backup` 換成已存在的安全備份目錄。運作中的 SQLite 使用 WAL，不要只複製主檔。不要為了重試而刪除 events／operations；這些表負責避免重複操作。Log 會遮蔽憑證，排錯時仍避免公開完整內部訊息或資料庫。
