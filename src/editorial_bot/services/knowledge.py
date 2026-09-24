"""Wiki-backed knowledge with explicit provenance and bounded caching."""

import asyncio
import re
import time
from urllib.parse import quote, urlparse

from .google import document_text


def editorial_content(text: str) -> str:
    # The Wiki has an older directory. It must not compete with the authoritative sheet.
    return re.sub(
        r"(?ms)^#\s+現役編輯組成員[^\n]*\n.*?(?=^#{1,2}\s|\Z)",
        "成員身分與通知對象請查詢指定的編輯組成員表。\n\n",
        text.replace("\r\n", "\n"),
    )


class Knowledge:
    def __init__(self, gitlab, google, ttl: int = 900):
        self.gitlab = gitlab
        self.google = google
        self.ttl = ttl
        self.pages: list[dict] = []
        self.at = 0.0
        self.lock = asyncio.Lock()

    async def refresh(self, force: bool = False) -> list[dict]:
        async with self.lock:
            if not force and self.pages and time.monotonic() - self.at < self.ttl:
                return self.pages
            pages = await self.gitlab.wiki_pages()
            self.pages = [{**p, "content": editorial_content(p.get("content", ""))} for p in pages]
            self.at = time.monotonic()
            return self.pages

    def url(self, slug: str) -> str:
        return f"{self.gitlab.url}/{self.gitlab.project}/-/wikis/{quote(slug, safe='/')}"

    async def search(self, query: str) -> list[dict]:
        pages = await self.refresh()
        terms = query.casefold().split()
        ranked = sorted(
            pages, key=lambda p: sum(t in (p["title"] + p.get("content", "")).casefold() for t in terms), reverse=True
        )
        # A small Wiki often has just a home directory; return it as navigation even without a term match.
        return [
            {
                "title": p["title"],
                "slug": p["slug"],
                "url": self.url(p["slug"]),
                "content": p.get("content", "")[:16000],
            }
            for p in ranked[:5]
        ]

    async def page(self, slug: str, offset: int = 0) -> dict:
        data = await self.gitlab.wiki_page(slug)
        text = editorial_content(data.get("content", ""))
        return {
            "title": data["title"],
            "url": self.url(slug),
            "content": text[offset : offset + 16000],
            "next_offset": offset + 16000 if len(text) > offset + 16000 else None,
        }

    async def reference(self, url: str, offset: int = 0) -> dict:
        parsed = urlparse(url)
        match = re.fullmatch(r"/document/d/([A-Za-z0-9_-]+)(?:/.*)?", parsed.path)
        if parsed.hostname != "docs.google.com" or parsed.scheme != "https" or not match:
            raise ValueError("此工具只讀 Wiki 連結的 Google Docs；公開網站請用 web_search。")
        doc_id = match[1]
        pages = await self.refresh()
        allowed = set(
            re.findall(
                r"https://docs\.google\.com/document/d/([A-Za-z0-9_-]+)", "\n".join(p.get("content", "") for p in pages)
            )
        )
        if doc_id not in allowed:
            raise ValueError("這份文件不在編輯組 Wiki 的參考連結內。")
        document = await self.google.execute(
            self.google.docs.documents().get(documentId=doc_id, includeTabsContent=True)
        )
        text = document_text(document)
        return {
            "title": document.get("title"),
            "url": f"https://docs.google.com/document/d/{doc_id}/edit",
            "content": text[offset : offset + 16000],
            "next_offset": offset + 16000 if len(text) > offset + 16000 else None,
        }
