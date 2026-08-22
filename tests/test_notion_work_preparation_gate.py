from __future__ import annotations

from pathlib import Path
from typing import Any

from notion_crm_lib.clients import NotionClient
from notion_crm_lib.core import (
    DEFAULT_DATA_SOURCE_ID,
    TRIGGER_STATUS,
    WORK_PREPARED_AT_PROPERTY,
    WORK_PREPARED_PROPERTY,
    is_eligible_page,
)

PAGE_ID = "3c26e0d1-a86e-8192-9950-cdf229ada797"


def rich_text(value: str) -> list[dict[str, Any]]:
    return [{"type": "text", "plain_text": value, "text": {"content": value}}]


def select(value: str) -> dict[str, Any]:
    return {"type": "select", "select": {"name": value}}


def ready_page() -> dict[str, Any]:
    return {
        "id": PAGE_ID,
        "properties": {
            "Pensée": {"type": "title", "title": rich_text("Corriger la fiche CRM")},
            "Domaine": select("Développement web"),
            "Plateforme": select("CRM"),
            "Statut": select("Prêt à coder"),
            "ID automatisation": {"type": "rich_text", "rich_text": []},
            WORK_PREPARED_PROPERTY: {"type": "checkbox", "checkbox": True},
            WORK_PREPARED_AT_PROPERTY: {
                "type": "date",
                "date": {"start": "2026-08-21T08:00:00.000+02:00", "end": None},
            },
        },
    }


def test_new_trigger_is_ready_to_code() -> None:
    assert TRIGGER_STATUS == "Prêt à coder"


def test_work_gate_requires_ready_status_checkbox_and_date() -> None:
    page = ready_page()
    assert is_eligible_page(page)

    page["properties"]["Statut"] = select("À faire")
    assert not is_eligible_page(page)

    page["properties"]["Statut"] = select("Prêt à coder")
    page["properties"][WORK_PREPARED_PROPERTY] = {
        "type": "checkbox",
        "checkbox": False,
    }
    assert not is_eligible_page(page)

    page["properties"][WORK_PREPARED_PROPERTY] = {
        "type": "checkbox",
        "checkbox": True,
    }
    page["properties"][WORK_PREPARED_AT_PROPERTY] = {
        "type": "date",
        "date": None,
    }
    assert not is_eligible_page(page)


def test_work_gate_still_rejects_locked_pages() -> None:
    page = ready_page()
    page["properties"]["ID automatisation"] = {
        "type": "rich_text",
        "rich_text": rich_text("issue:123"),
    }
    assert not is_eligible_page(page)


def test_notion_query_requires_complete_work_preparation() -> None:
    class FakeApi:
        def __init__(self) -> None:
            self.payload: dict[str, Any] = {}

        def json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
            self.payload = {"method": method, "url": url, **kwargs}
            return {"results": []}

    client = NotionClient("notion-test-token")
    fake_api = FakeApi()
    client.api = fake_api  # type: ignore[assignment]

    assert client.query_ready_pages(DEFAULT_DATA_SOURCE_ID, page_size=3) == []
    filters = fake_api.payload["json"]["filter"]["and"]

    assert {
        "property": "Statut",
        "select": {"equals": "Prêt à coder"},
    } in filters
    assert {
        "property": WORK_PREPARED_PROPERTY,
        "checkbox": {"equals": True},
    } in filters
    assert {
        "property": WORK_PREPARED_AT_PROPERTY,
        "date": {"is_not_empty": True},
    } in filters
    assert {
        "property": "ID automatisation",
        "rich_text": {"is_empty": True},
    } in filters


def test_night_batch_blocks_only_an_active_implementation_run() -> None:
    workflow = Path(".github/workflows/notion-crm-queue.yml").read_text(encoding="utf-8")

    assert 'actions/workflows/notion-crm-implement.yml/runs?per_page=50' in workflow
    assert 'pulls?state=open' not in workflow
    assert 'Les pull requests déjà ouvertes ne bloquent plus la file' in workflow
    assert 'MAX_TASKS: "1"' in workflow


def test_queue_restarts_immediately_after_an_implementation_finishes() -> None:
    workflow = Path(".github/workflows/notion-crm-queue.yml").read_text(encoding="utf-8")

    assert 'workflow_run:' in workflow
    assert 'workflows: ["Notion CRM - préparer la PR avec Codex"]' in workflow
    assert 'types: [completed]' in workflow
    assert 'cron: "*/5 * * * *"' in workflow
