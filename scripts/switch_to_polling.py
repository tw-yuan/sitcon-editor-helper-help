"""Explicit operator action: remove the existing webhook without dropping queued updates."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from telegram import Bot

from editorial_bot.logging_setup import configure
from editorial_bot.settings import Settings


async def switch():
    settings = Settings()
    configure(settings)
    async with Bot(settings.telegram_bot_token.get_secret_value()) as bot:
        old = await bot.get_webhook_info()
        if old.url:
            backup = Path(settings.db_path).parent / "webhook-before-switch.json"
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as file:
                json.dump(old.to_dict(), file, ensure_ascii=False, default=str)
            await bot.delete_webhook(drop_pending_updates=False)
            print("已保存原 webhook 設定並移除 webhook；保留待處理更新。可啟動 bot。")
        else:
            print("目前沒有 webhook，可啟動 bot。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--confirm-switch",
        action="store_true",
        required=True,
        help="已取得切換部署授權，並確認舊 n8n 不會重新註冊 webhook",
    )
    parser.parse_args()
    asyncio.run(switch())
