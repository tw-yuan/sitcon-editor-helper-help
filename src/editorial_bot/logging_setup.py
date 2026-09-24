"""Redact configured credentials from logs and user-facing diagnostics."""

import logging
import re

_secrets: list[str] = []


def redact(value: object) -> str:
    text = str(value)
    for secret in _secrets:
        text = text.replace(secret, "[REDACTED]")
    return re.sub(r"(?:glpat-[\w.\-]+|\d{7,}:[A-Za-z0-9_-]{25,})", "[REDACTED]", text)


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def configure(settings):
    global _secrets
    _secrets = [
        v.get_secret_value()
        for v in (settings.telegram_bot_token, settings.gitlab_token, settings.llm_api_key, settings.web_search_api_key)
        if v.get_secret_value()
    ]
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=settings.log_level, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
