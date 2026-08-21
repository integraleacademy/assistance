"""Clients HTTP bornés pour Notion et GitHub."""

from __future__ import annotations

import re
import sys
import time
from typing import Any, Iterable, Iterator, Mapping, MutableMapping

import requests

from .core import (
    AutomationError,
    GITHUB_API_BASE,
    MAX_COMMENT_CONTENT_CHARS,
    NOTION_API_BASE,
    NOTION_VERSION,
    TRIGGER_DOMAIN,
    TRIGGER_PLATFORM,
    TRIGGER_STATUS,
    _safe_issue_title,
    compact_page_id,
    split_text,
)

class JsonApiClient:
    """Client HTTP JSON avec reprise sur les erreurs temporaires."""

    def __init__(self, *, headers: Mapping[str, str], timeout: tuple[int, int] = (10, 45)) -> None:
        self.session = requests.Session()
        self.session.headers.update(dict(headers))
        self.timeout = timeout

    def request(
        self,
        method: str,
        url: str,
        *,
        expected: Iterable[int] = (200,),
        **kwargs: Any,
    ) -> requests.Response:
        expected_codes = set(expected)
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                if attempt == 3:
                    break
                time.sleep(2**attempt)
                continue

            if response.status_code in expected_codes:
                return response

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < 3:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else float(2**attempt)
                    except ValueError:
                        delay = float(2**attempt)
                    time.sleep(max(0.5, min(delay, 15.0)))
                    continue

            detail = response.text[:2_000]
            raise AutomationError(
                f"API {method.upper()} {url} : HTTP {response.status_code} — {detail}"
            )

        raise AutomationError(f"API {method.upper()} {url} inaccessible : {last_error}")

    def json(
        self,
        method: str,
        url: str,
        *,
        expected: Iterable[int] = (200,),
        **kwargs: Any,
    ) -> MutableMapping[str, Any]:
        response = self.request(method, url, expected=expected, **kwargs)
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise AutomationError(f"Réponse JSON invalide pour {url}") from exc
        if not isinstance(payload, MutableMapping):
            raise AutomationError(f"Réponse JSON inattendue pour {url}")
        return payload


class NotionClient:
    """Sous-ensemble minimal et typé de l'API Notion."""

    def __init__(self, token: str, *, version: str = NOTION_VERSION) -> None:
        if not token:
            raise AutomationError("Le secret NOTION_API_TOKEN est absent.")
        self.api = JsonApiClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": version,
                "Content-Type": "application/json",
                "User-Agent": "IntegraleAcademy-NotionCRM/1.0",
            }
        )

    def query_ready_pages(self, data_source_id: str, *, page_size: int = 3) -> list[dict[str, Any]]:
        source_id = compact_page_id(data_source_id)
        payload = {
            "filter": {
                "and": [
                    {"property": "Domaine", "select": {"equals": TRIGGER_DOMAIN}},
                    {"property": "Plateforme", "select": {"equals": TRIGGER_PLATFORM}},
                    {"property": "Statut", "select": {"equals": TRIGGER_STATUS}},
                    {"property": "ID automatisation", "rich_text": {"is_empty": True}},
                ]
            },
            "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
            "page_size": max(1, min(int(page_size), 100)),
        }
        data = self.api.json(
            "POST",
            f"{NOTION_API_BASE}/data_sources/{source_id}/query",
            json=payload,
        )
        results = data.get("results")
        return [dict(item) for item in results if isinstance(item, Mapping)] if isinstance(results, list) else []

    def get_page(self, page_id: str) -> dict[str, Any]:
        return dict(
            self.api.json("GET", f"{NOTION_API_BASE}/pages/{compact_page_id(page_id)}")
        )

    def get_page_markdown(self, page_id: str) -> dict[str, Any]:
        """Récupère le rendu Markdown natif fourni par l'API Notion."""

        return dict(
            self.api.json(
                "GET",
                f"{NOTION_API_BASE}/pages/{compact_page_id(page_id)}/markdown",
            )
        )

    def iter_comments(self, block_id: str) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "block_id": compact_page_id(block_id),
                "page_size": 100,
            }
            if cursor:
                params["start_cursor"] = cursor
            data = self.api.json("GET", f"{NOTION_API_BASE}/comments", params=params)
            results = data.get("results")
            if isinstance(results, list):
                for item in results:
                    if isinstance(item, Mapping):
                        yield dict(item)
            if not data.get("has_more"):
                break
            cursor = str(data.get("next_cursor") or "") or None
            if not cursor:
                break

    def update_page(self, page_id: str, properties: Mapping[str, Any]) -> dict[str, Any]:
        return dict(
            self.api.json(
                "PATCH",
                f"{NOTION_API_BASE}/pages/{compact_page_id(page_id)}",
                json={"properties": dict(properties)},
            )
        )

    def add_comment(self, page_id: str, text: str) -> None:
        content = str(text or "").strip()
        if not content:
            return
        payload = {
            "parent": {"page_id": compact_page_id(page_id)},
            "rich_text": [
                {"type": "text", "text": {"content": chunk}}
                for chunk in split_text(content[:MAX_COMMENT_CONTENT_CHARS], 2_000)
            ],
        }
        self.api.json("POST", f"{NOTION_API_BASE}/comments", expected=(200,), json=payload)

    def safe_add_comment(self, page_id: str, text: str) -> None:
        try:
            self.add_comment(page_id, text)
        except AutomationError as exc:
            print(f"Avertissement : commentaire Notion non ajouté : {exc}", file=sys.stderr)


class GitHubClient:
    """Sous-ensemble minimal de l'API GitHub utilisé par les workflows."""

    def __init__(self, token: str, repository: str) -> None:
        if not token:
            raise AutomationError("Le jeton GitHub est absent.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or ""):
            raise AutomationError(f"Dépôt GitHub invalide : {repository!r}")
        self.repository = repository
        self.api = JsonApiClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "IntegraleAcademy-NotionCRM/1.0",
            }
        )

    def create_issue(self, title: str, body: str) -> dict[str, Any]:
        return dict(
            self.api.json(
                "POST",
                f"{GITHUB_API_BASE}/repos/{self.repository}/issues",
                expected=(201,),
                json={"title": _safe_issue_title(title), "body": body},
            )
        )

    def get_issue(self, number: int) -> dict[str, Any]:
        return dict(
            self.api.json(
                "GET",
                f"{GITHUB_API_BASE}/repos/{self.repository}/issues/{int(number)}",
            )
        )

    def comment_issue(self, number: int, body: str) -> None:
        self.api.json(
            "POST",
            f"{GITHUB_API_BASE}/repos/{self.repository}/issues/{int(number)}/comments",
            expected=(201,),
            json={"body": str(body or "")[:65_000]},
        )

    def dispatch(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self.api.request(
            "POST",
            f"{GITHUB_API_BASE}/repos/{self.repository}/dispatches",
            expected=(204,),
            json={"event_type": event_type, "client_payload": dict(payload)},
        )
