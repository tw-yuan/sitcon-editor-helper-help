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

部署時須提供 Google service account、Cloudflare AI Gateway 與獨立搜尋服務的憑證。不要將 `.env` 或 JSON commit。

```bash
sudo chown root:10001 secrets/google-sa.json
sudo chmod 640 secrets/google-sa.json
sudo chown 10001:10001 data
sudo chmod 750 data
```

Google 帳號須能讀指定名冊、複製範本、在指定 Shared Drive 根資料夾新增內容。本版本直接使用 service account，無網域委派。GitLab 帳號須加入 `sitcon-tw/editorial/board`，具 Developer 權限並可讀 Wiki。

名冊 `gid=0` 必須包含 `Telegram ID`、`gitlab_id`、`Nickname`、`default`、`note`。`Telegram ID` 填 username；`default=yes` 要唯一；總副召在 `note` 標記「總召／副召」。範本文字需要 `TITTLE`、`DATE`、`GITLAB_LINK`、`DIR_LINK`。

## DeepSeek 與 Cloudflare AI Gateway

主模型使用以下 `.env` 設定，`LLM_API_KEY` 填入 Cloudflare token：

```dotenv
LLM_PROVIDER=openai_compat
LLM_MODEL=deepseek/deepseek-flash
LLM_BASE_URL=https://cf-ai.yuan-tw.net/compat
LLM_THINKING=off
LLM_SERVICE_TIER=
```

SDK 會自動附加 `/chat/completions`，實際請求送至 `https://cf-ai.yuan-tw.net/compat/chat/completions`；不要把完整 endpoint 填進 `LLM_BASE_URL`，也不要額外加 `/v1`。模型名稱包含 `deepseek/` 路由前綴，認證由 SDK 以 `Authorization: Bearer` 傳送；`LLM_AUTH_BEARER` 只影響 Anthropic adapter。DeepSeek 使用 Chat Completions；`WEB_SEARCH_*` 則設定獨立的 Anthropic 搜尋服務。

沿用現有 adapter 是因為 [Cloudflare 相容端點](https://developers.cloudflare.com/ai-gateway/usage/chat-completion/)支援相同的訊息與工具格式。修改 `.env` 後，先執行下方 AI 檢查，再用 `docker compose up -d bot` 重建容器載入新環境；單純 `docker compose restart` 不會載入新的環境變數。

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

這個檢查會在執行邊界封鎖所有寫入工具，驗證讀取真實狀態標籤、相對日期補問、同意按鈕候選、純文字回覆，並檢查「0925 test」的模型開卡參數；只解析，不實際開卡。

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

BotFather 的 privacy mode 必須允許讀取一般群組訊息，才能收到「小石」及裸 `review`。若使用群組 topics，回覆會留在原 topic。

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
