"""Editorial GitLab API. Label definitions cannot be created, renamed or deleted."""

import asyncio
import difflib
import re
from urllib.parse import quote

import httpx


class RemoteError(RuntimeError):
    def __init__(self, service: str, status: int | None, *, uncertain: bool = False):
        self.status = status
        self.uncertain = uncertain
        text = "連線失敗或逾時" if status is None else f"HTTP {status}"
        super().__init__(f"{service}：{text}，請確認服務與帳號權限。")


def neutralize_quick_actions(text: str) -> str:
    # Descriptions/comments must not provide an indirect label-management endpoint.
    return re.sub(r"(?m)^(\s*)(/[A-Za-z][^\n]*)$", lambda m: m[1] + "\\" + m[2], text)


def scope(name: str) -> str | None:
    return name.split("::", 1)[0].strip().casefold() if "::" in name else None


class GitLab:
    def __init__(self, url: str, project: str, token: str, client: httpx.AsyncClient | None = None):
        self.url = url.rstrip("/")
        self.project = project
        self.path = "/projects/" + quote(project, safe="")
        self.client = client or httpx.AsyncClient(
            base_url=self.url + "/api/v4", headers={"PRIVATE-TOKEN": token}, timeout=30
        )
        self.locks: dict[int, asyncio.Lock] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def request(self, method: str, suffix: str, **kwargs) -> httpx.Response:
        for attempt in range(3 if method == "GET" else 1):
            try:
                response = await self.client.request(method, self.path + suffix, **kwargs)
            except httpx.HTTPError:
                if method == "GET" and attempt < 2:
                    await asyncio.sleep(0.5 * 2**attempt)
                    continue
                raise RemoteError("GitLab", None, uncertain=method != "GET") from None
            if method == "GET" and response.status_code in (429, 502, 503, 504) and attempt < 2:
                retry = response.headers.get("Retry-After", "1")
                await asyncio.sleep(min(float(retry) if retry.isdigit() else 1, 10))
                continue
            if response.is_error:
                raise RemoteError("GitLab", response.status_code, uncertain=response.status_code >= 500)
            return response
        raise RemoteError("GitLab", None)

    async def pages(self, suffix: str, params: dict | None = None) -> list[dict]:
        rows = []
        page = 1
        while True:
            resp = await self.request("GET", suffix, params={**(params or {}), "per_page": 100, "page": page})
            chunk = resp.json()
            if not isinstance(chunk, list):
                raise ValueError("GitLab 清單回應格式不正確")
            rows.extend(chunk)
            if not resp.headers.get("X-Next-Page"):
                break
            page = int(resp.headers["X-Next-Page"])
            if page > 100:
                raise ValueError("查詢超過 100 頁，請縮小範圍。")
        return rows

    async def labels(self) -> list[dict]:
        return await self.pages("/labels", {"include_ancestor_groups": "true"})

    async def validate_labels(self, names: list[str], *, required: bool = False) -> list[str]:
        if required and not names:
            raise ValueError("開卡至少需要一個編輯組既有 label。")
        known = {row["name"] for row in await self.labels()}
        selected = list(dict.fromkeys(names))
        for name in selected:
            if name not in known:
                candidates = difflib.get_close_matches(name, sorted(known), n=5, cutoff=0.2)
                raise ValueError(f"不存在既有 label「{name}」，不會建立新標籤。候選：{'、'.join(candidates)}")
        scopes = [scope(name) for name in selected if scope(name)]
        if len(scopes) != len(set(scopes)):
            raise ValueError("不能同時指定相同 scope 的多個 label。")
        return selected

    async def user_username(self, user_id: int) -> str | None:
        """Resolve a roster ID to a real GitLab mention; only 404 means absent."""
        if user_id <= 0:
            raise ValueError("GitLab ID 必須是正整數")
        try:
            response = await self.client.get(f"/users/{user_id}")
        except httpx.HTTPError:
            raise RemoteError("GitLab", None) from None
        if response.status_code == 404:
            return None
        if response.is_error:
            raise RemoteError("GitLab", response.status_code)
        user = response.json()
        username = user.get("username") if isinstance(user, dict) else None
        if (
            not isinstance(username, str)
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", username)
            or user.get("id") != user_id
        ):
            raise ValueError("GitLab 使用者回應的 ID 或 username 不正確")
        return username

    async def get(self, iid: int) -> dict:
        return (await self.request("GET", f"/issues/{iid}")).json()

    async def search(self, **params) -> list[dict]:
        params.setdefault("state", "opened")
        return await self.pages("/issues", params)

    async def resolve(self, target: str) -> dict:
        target = target.strip()
        if target.startswith("http"):
            prefix = f"{self.url}/{self.project}/-/issues/"
            if not target.startswith(prefix) or not re.fullmatch(r"\d+", target[len(prefix) :]):
                raise ValueError("只接受編輯組專案的完整 Issue 網址。")
            target = target[len(prefix) :]
        if re.fullmatch(r"#?\d+", target):
            return await self.get(int(target.lstrip("#")))
        items = await self.search(search=target, **{"in": "title"})
        exact = [i for i in items if i["title"] == target]
        matches = exact or [i for i in items if target.casefold() in i["title"].casefold()]
        if len(matches) != 1:
            candidates = "；".join(f"#{i['iid']} {i['title']}" for i in matches[:20])
            raise ValueError(f"卡片無法唯一辨識：{target}。" + (f"請選擇：{candidates}" if matches else "請提供卡號。"))
        return matches[0]

    async def create(self, payload: dict) -> dict:
        labels = await self.validate_labels(payload.get("labels", []), required=True)
        body = {**payload, "labels": ",".join(labels)}
        body["description"] = neutralize_quick_actions(body.get("description", ""))
        return (await self.request("POST", "/issues", json=body)).json()

    async def update(
        self, iid: int, *, add_labels: list[str] | None = None, remove_labels: list[str] | None = None, **fields
    ) -> dict:
        allowed = {"title", "description", "due_date", "assignee_ids", "state_event"}
        if not set(fields).issubset(allowed):
            raise ValueError("不支援的卡片更新欄位")
        if fields.get("state_event") not in (None, "close", "reopen"):
            raise ValueError("state_event 只能為 close 或 reopen")
        async with self.locks.setdefault(iid, asyncio.Lock()):
            current = await self.get(iid)
            added = await self.validate_labels(add_labels or [])
            removed = await self.validate_labels(remove_labels or [])
            replace_scopes = {scope(x) for x in added if scope(x)}
            removed += [x for x in current.get("labels", []) if scope(x) in replace_scopes and x not in added]
            if set(added) & set(removed):
                raise ValueError("不能同時新增與移除同一個 label")
            payload = {**fields}
            if added:
                payload["add_labels"] = ",".join(added)
            if removed:
                payload["remove_labels"] = ",".join(dict.fromkeys(removed))
            if "description" in payload:
                payload["description"] = neutralize_quick_actions(payload["description"])
            if not payload:
                return current
            await self.request("PUT", f"/issues/{iid}", json=payload)
            actual = await self.get(iid)
            if set(added) - set(actual.get("labels", [])) or set(removed) & set(actual.get("labels", [])):
                raise ValueError("GitLab 回讀的標籤未符合更新要求，請檢查權限與實際卡片。")
            if "assignee_ids" in fields:
                expected = set(fields["assignee_ids"])
                actual_ids = {a["id"] for a in actual.get("assignees", [])}
                if expected != actual_ids:
                    raise ValueError("GitLab 未套用全部指定負責人，請檢查帳號及專案限制。")
            if "state_event" in fields:
                expected_state = "closed" if fields["state_event"] == "close" else "opened"
                if actual.get("state") != expected_state:
                    raise ValueError("GitLab 卡片開關狀態與要求不符，請查核實際結果。")
            for field in ("title", "description", "due_date"):
                if field in payload and (actual.get(field) or "") != (payload[field] or ""):
                    raise ValueError(f"GitLab 回讀 {field} 與要求不符，可能有其他人同時修改，請查核卡片。")
            return actual

    async def notes(self, iid: int) -> list[dict]:
        return await self.pages(f"/issues/{iid}/notes", {"sort": "asc", "order_by": "created_at"})

    async def comment(self, iid: int, body: str) -> dict:
        return (
            await self.request("POST", f"/issues/{iid}/notes", json={"body": neutralize_quick_actions(body)})
        ).json()

    async def links(self, iid: int) -> list[dict]:
        return await self.pages(f"/issues/{iid}/links")

    async def link(self, iid: int, target: int, link_type: str) -> dict:
        if iid == target:
            raise ValueError("不能把卡片連結到自己")
        existing = await self.links(iid)
        for item in existing:
            if item["iid"] == target and item.get("references", {}).get("full", "").startswith(self.project + "#"):
                return item
            if item["iid"] == target and item.get("project_id") == (await self.get(iid)).get("project_id"):
                return item
        return (
            await self.request(
                "POST",
                f"/issues/{iid}/links",
                json={"target_project_id": self.project, "target_issue_iid": target, "link_type": link_type},
            )
        ).json()

    async def unlink(self, iid: int, target: int) -> bool:
        project_id = (await self.get(iid))["project_id"]
        for item in await self.links(iid):
            if item["iid"] == target and item.get("project_id") == project_id:
                await self.request("DELETE", f"/issues/{iid}/links/{item['issue_link_id']}")
                return True
        return False

    async def wiki_pages(self) -> list[dict]:
        return await self.pages("/wikis", {"with_content": "true"})

    async def wiki_page(self, slug: str) -> dict:
        return (await self.request("GET", "/wikis/" + quote(slug, safe=""))).json()
