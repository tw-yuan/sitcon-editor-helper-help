# SITCON 編輯組小石

已完成實作與容器驗證，並於 2026-09-24（UTC）取得授權後以 Docker Compose 啟動。首次使用須由管理員在目標群組執行 `/authorize`。

長期編輯組專用的 Telegram agent，取代 n8n 工作流。支援開卡與文案資料夾、查卡、改負責人／狀態、批次 review、自動標註總副召、全員通知、Wiki 查詢、網路搜尋與群組記憶。

- 使用契約：[SPEC.md](SPEC.md)
- 架構、工具、資料流與限制：[PROJECT.md](PROJECT.md)
- 環境變數：[.env.example](.env.example)

## 環境準備

需要 Docker Engine 與 Docker Compose v2+、可對外連線的主機。不需要開 inbound port。只執行一個 bot instance。

```bash
cp .env.example .env
mkdir -p secrets data
chmod 700 secrets
chmod 600 .env
```

在 `.env` 填入本專案 Telegram token、管理員數字 ID、GitLab token、主模型與獨立搜尋的憑證及 endpoint。將 Google service account JSON 放到 `secrets/google-sa.json`。

本機已依使用者授權從 `/root/sitcon-2027-helper-bot` 複製 Google JSON 與 AI／搜尋設定；新環境需要自行提供這些值。不要將 `.env` 或 JSON commit。

```bash
sudo chown root:10001 secrets/google-sa.json
sudo chmod 640 secrets/google-sa.json
sudo chown 10001:10001 data
sudo chmod 750 data
```

Google 帳號須能讀指定名冊、複製範本、在指定 Shared Drive 根資料夾新增內容。本版本直接使用 service account，無網域委派。GitLab 帳號須加入 `sitcon-tw/editorial/board`，具 Developer 權限並可讀 Wiki。

名冊 `gid=0` 必須包含 `Telegram ID`、`gitlab_id`、`Nickname`、`default`、`note`。`Telegram ID` 填 username；`default=yes` 要唯一；總副召在 `note` 標記「總召／副召」。範本文字需要 `TITTLE`、`DATE`、`GITLAB_LINK`、`DIR_LINK`。

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

這個檢查會在執行邊界封鎖所有寫入工具，驗證讀取真實狀態標籤與相對日期補問。

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

## 使用例子

```text
/ta
小石，開文案卡「報名開跑」，到期 2027/01/15
小石，開圖片任務「主視覺」，到期 10/20，交給 @username
小石，列出未關閉卡片
小石，把 #123 指派給 @username
review 123 124
小石，把 #123 改成 Doing
小石，把 #123 加上 SITCON::2027
小石，記住我們這群文案先放參考來源
小石，列出本群記憶
小石，忘記第 3 筆記憶
```

- 一張卡建立一個 `MMDD_TITLE` 資料夾，日期取到期日；文案卡複製範本，純任務不建立 Docs。
- 文案卡預設 `社群文案`、任務卡預設 `編輯組專案`，狀態預設 `Status::Inbox`。所有 label 必須既有，不會自建。
- 未指定負責人時採名冊唯一 `default=yes`。只有相對日期時會補問。
- Review 自動標作者與名冊總副召。未關閉清單仍包含 Review；Report 不會自動關卡。
- 改標題／到期日只改卡片，既有資料夾及 Docs 不會自動改名。
- 使用者指令完整時直接執行；缺資訊才補問。回覆 bot 的問句續接，短期脈絡有效 30 分鐘。

AI 的一般回覆、補問與候選文字都要求使用純文字，不含 Markdown 或 HTML 語法；網址直接顯示完整 URL。補問選項使用「選項 1：」等一般文字。

## 處理狀態提示

收到已授權的業務訊息時，會在原訊息加上 👀，並在原群組／topic 顯示 typing，處理期間每 4 秒更新。排隊時也會顯示；完成、錯誤或取消後停止更新。

正常回覆全部送達後改成 👍；模型可在道謝或鼓勵等互動選擇 ❤。需要補問或程式發生錯誤時不標示完成。`/ta` 與全員標註按鈕也有 typing；按鈕不會對 bot 自己的選單加 reaction。群組若禁用表情或暫時連線失敗，主要操作仍照常處理。

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

`data/editorial.sqlite3` 保存群組授權、長期記憶、操作進度、資源連結與通知狀態。短期對話脈絡只存在記憶體。`secrets/`、`.env`、`data/` 不進 Git 或映像。

停機備份範例（資料包含內部資訊，備份檔請限制權限）：

```bash
docker compose stop bot
sudo tar -czf /path/to/private-backup/editorial-data.tar.gz data
sudo chmod 600 /path/to/private-backup/editorial-data.tar.gz
docker compose start bot
```

請自行將 `/path/to/private-backup` 換成已存在的安全備份目錄。運作中的 SQLite 使用 WAL，不要只複製主檔。不要為了重試而刪除 events／operations；這些表負責避免重複操作。Log 會遮蔽憑證，排錯時仍避免公開完整內部訊息或資料庫。
