"""Compose entry point; diagnostics never modify remote project data."""

import argparse
import asyncio
import contextlib
import json
import logging

from telegram import Bot, Update
from telegram.ext import AIORateLimiter, Application, CallbackQueryHandler, MessageHandler, filters

from .agent.core import Agent
from .agent.prompts import PromptBuilder
from .agent.tools.base import ToolRegistry
from .agent.tools.editorial import EditorialTools
from .agent.tools.reaction_tools import build_reaction_tools
from .agent.tools.search_tools import build_search_tools
from .gateway import Gateway
from .logging_setup import configure, redact
from .services.gitlab import GitLab
from .services.google import Google
from .services.knowledge import Knowledge
from .services.llm.base import Message, TextBlock, build_llm_client
from .services.sheets_roster import RosterService
from .services.web_search import build_web_search_service
from .services.workflow import CardWorkflow
from .settings import Settings
from .storage.db import Store

log = logging.getLogger(__name__)


async def configured_labels(gl, settings):
    expected = [
        settings.default_document_label,
        settings.default_task_label,
        settings.initial_status,
        settings.review_status,
    ]
    known = {label["name"] for label in await gl.labels()}
    if missing := set(expected) - known:
        raise ValueError("設定的 label 不存在：" + "、".join(sorted(missing)))
    return expected


async def service_check(settings):
    gl = GitLab(settings.gitlab_url, settings.gitlab_project, settings.gitlab_token.get_secret_value())
    try:
        google = Google(settings)
        root = await google.preflight()
        roster = await RosterService(google).get()
        project = (await gl.request("GET", "")).json()
        labels = await configured_labels(gl, settings)
        wiki = await gl.wiki_pages()
        async with Bot(settings.telegram_bot_token.get_secret_value()) as bot:
            me = await bot.get_me()
            webhook = await bot.get_webhook_info()
        return {
            "gitlab_project": project["path_with_namespace"],
            "wiki_pages": len(wiki),
            "labels": labels,
            "shared_drive": bool(root.get("driveId")),
            "roster_members": len(roster.members),
            "review_roles": [m.position for m in roster.chiefs()],
            "default_assignee_unique": bool(roster.default_member()),
            "bot": me.username,
            "existing_webhook": bool(webhook.url),
        }
    finally:
        await gl.close()


async def ai_check(settings):
    llm = build_llm_client(settings)
    response = await llm.chat(
        system="請僅回覆 OK。", messages=[Message("user", [TextBlock("連線測試")])], tools=[], thinking="off"
    )
    search = build_web_search_service(settings)
    if search is None:
        raise ValueError("未設定網路搜尋憑證")
    result = await search.search("請查詢 SITCON 官方網站網址，提供官方來源連結。")
    return {
        "llm_model": settings.llm_model,
        "llm_response_received": bool(response.text),
        "search_model": settings.web_search_model,
        "search_response_received": bool(result.text),
        "search_citations": len(result.citations),
        "search_errors": result.tool_errors,
    }


async def webhook_present(settings):
    async with Bot(settings.telegram_bot_token.get_secret_value()) as bot:
        return bool((await bot.get_webhook_info()).url)


def run(settings):
    # python-telegram-bot polling clears the webhook. Never let it implicitly take over an existing deployment.
    if asyncio.run(webhook_present(settings)):
        raise RuntimeError("已有 Telegram webhook，未啟動 polling。請確認切換部署後，先移除舊 webhook，再啟動。")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def initialize(app):
        store = await Store.open(settings.db_path, settings.migrations_path)
        gl = GitLab(settings.gitlab_url, settings.gitlab_project, settings.gitlab_token.get_secret_value())
        google = Google(settings)
        roster = RosterService(google, settings.cache_ttl_roster)
        knowledge = Knowledge(gl, google, settings.cache_ttl_wiki)
        workflow = CardWorkflow(gl, google, store, settings)
        editorial = EditorialTools(gl, google, roster, knowledge, workflow, store, settings)
        search = build_web_search_service(settings)
        if search is None:
            raise ValueError("此部署需要 WEB_SEARCH_API_KEY 與網路搜尋設定")
        agent = Agent(
            build_llm_client(settings),
            ToolRegistry(editorial.build() + build_search_tools(search) + build_reaction_tools()),
            PromptBuilder(settings, store),
            roster,
            thinking=settings.llm_thinking,
            max_iterations=settings.llm_max_tool_iterations,
        )
        gateway = Gateway(settings, store, agent, editorial, roster, knowledge, reviews=editorial.reviews)
        app.bot_data.update(gateway=gateway, store=store, gitlab=gl)
        await google.preflight()
        await roster.get()
        await configured_labels(gl, settings)
        await knowledge.refresh()
        await gateway.recover(app.bot)
        app.bot_data["delivery_worker"] = asyncio.create_task(gateway.delivery_worker(app.bot))
        log.info(
            "Editorial bot ready; authorized_groups=%d", len(await store.all("SELECT chat_id FROM authorized_groups"))
        )

    async def shutdown(app):
        if worker := app.bot_data.get("delivery_worker"):
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        if store := app.bot_data.get("store"):
            await store.close()
        if gl := app.bot_data.get("gitlab"):
            await gl.close()

    async def receive(update, context):
        await context.application.bot_data["gateway"].handle(update, context)

    async def error(update, context):
        log.error(
            "Telegram handler error update=%s type=%s", getattr(update, "update_id", None), type(context.error).__name__
        )
        if (
            update
            and update.effective_message
            and await context.application.bot_data["gateway"].allowed(update.effective_chat)
        ):
            await update.effective_message.reply_text("操作未完成，請稍後再試；若持續發生請通知管理員。")

    app = (
        Application.builder()
        .token(settings.telegram_bot_token.get_secret_value())
        .rate_limiter(AIORateLimiter(max_retries=1))
        .concurrent_updates(settings.max_concurrent_agent_turns)
        .post_init(initialize)
        .post_shutdown(shutdown)
        .build()
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.UpdateType.EDITED_MESSAGE, receive))
    app.add_handler(CallbackQueryHandler(receive, pattern="^(?:mention_editors$|choose:|review_sign:|review_page:)"))
    app.add_error_handler(error)
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY], drop_pending_updates=False)


def main():
    parser = argparse.ArgumentParser(description="SITCON 編輯組小石")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check-services", action="store_true", help="唯讀查證 GitLab、Google 與 Telegram")
    group.add_argument("--check-ai", action="store_true", help="驗證模型及公開網路搜尋連線")
    args = parser.parse_args()
    try:
        settings = Settings()
        configure(settings)
        if args.check_services:
            print(json.dumps(asyncio.run(service_check(settings)), ensure_ascii=False, indent=2))
        elif args.check_ai:
            print(json.dumps(asyncio.run(ai_check(settings)), ensure_ascii=False, indent=2))
        else:
            run(settings)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        log.error("Startup/check failed: %s", redact(exc))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
