from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import notion_crm_automation as automation

PAGE_ID = "3c26e0d1-a86e-8192-9950-cdf229ada797"
PAGE_URL = "https://www.notion.so/3c26e0d1a86e81929950cdf229ada797"


def rich_text(value: str) -> list[dict[str, Any]]:
    return [{"type": "text", "plain_text": value, "text": {"content": value}}]


def select(value: str) -> dict[str, Any]:
    return {"type": "select", "select": {"name": value}}


def eligible_page() -> dict[str, Any]:
    return {
        "id": PAGE_ID,
        "url": PAGE_URL,
        "properties": {
            "Pensée": {"type": "title", "title": rich_text("Ajouter un bouton de relance")},
            "Domaine": select("Développement web"),
            "Plateforme": select("CRM"),
            "Statut": select("À faire"),
            "Type": select("Idée"),
            "ID automatisation": {"type": "rich_text", "rich_text": []},
        },
    }


def test_page_id_and_branch_are_deterministic() -> None:
    assert automation.compact_page_id(PAGE_ID) == "3c26e0d1a86e81929950cdf229ada797"
    assert automation.dashed_page_id(PAGE_ID) == PAGE_ID
    assert automation.branch_name_for_page(PAGE_ID) == "agent/notion-crm-3c26e0d1a86e"


def test_eligibility_requires_the_exact_trigger_and_no_lock() -> None:
    page = eligible_page()
    assert automation.is_eligible_page(page)

    page["properties"]["Statut"] = select("En cours")
    assert not automation.is_eligible_page(page)

    page["properties"]["Statut"] = select("À faire")
    page["properties"]["ID automatisation"] = {
        "type": "rich_text",
        "rich_text": rich_text("issue:42"),
    }
    assert not automation.is_eligible_page(page)


def test_issue_body_contains_markers_content_comments_and_rules() -> None:
    snapshot = automation.PageSnapshot(
        page_id=PAGE_ID,
        url=PAGE_URL,
        title="Ajouter un bouton de relance",
        properties=eligible_page()["properties"],
        content="## Détails\n\nLe bouton doit rester sur la fiche prospect.",
        comments=["Clément — 2026-08-21\nNe pas relancer les contacts convertis."],
    )
    body = automation.build_issue_body(snapshot, run_url="https://github.com/run/1")

    assert f"<!-- notion-page-id: {PAGE_ID} -->" in body
    assert PAGE_URL in body
    assert "Le bouton doit rester" in body
    assert "Ne pas relancer les contacts convertis" in body
    assert "pull request **brouillon**" in body
    assert len(body) <= automation.MAX_GITHUB_BODY_CHARS



def test_workspace_agent_input_contains_the_frozen_request() -> None:
    snapshot = automation.PageSnapshot(
        page_id=PAGE_ID,
        url=PAGE_URL,
        title="Ajouter un bouton de relance",
        properties=eligible_page()["properties"],
        content="Détail",
        comments=[],
    )
    prompt = automation.build_workspace_agent_input(
        snapshot,
        issue_url="https://github.com/integraleacademy/assistance/issues/77",
        issue_body="Corps figé de la demande",
    )
    assert "journal de travail lisible" in prompt
    assert "Corps figé de la demande" in prompt
    assert PAGE_URL in prompt


def test_render_prompt_rejects_an_issue_without_notion_marker() -> None:
    with pytest.raises(automation.AutomationError):
        automation.render_codex_prompt({"title": "Sans marqueur", "body": "Texte"})


def test_render_prompt_requires_a_complete_json_patch() -> None:
    issue = {
        "number": 123,
        "html_url": "https://github.com/integraleacademy/assistance/issues/123",
        "title": "[Notion CRM] Ajouter un bouton de relance",
        "body": (
            f"<!-- notion-page-id: {PAGE_ID} -->\n"
            f"<!-- notion-page-url: {PAGE_URL} -->\n"
            "## Contenu\nAjouter le bouton."
        ),
    }
    prompt, metadata = automation.render_codex_prompt(issue)

    assert "<NOTION_SPEC>" in prompt
    assert "Ajouter le bouton." in prompt
    assert "scripts/apply_notion_patch.py" in prompt
    assert "diff Git unifié complet" in prompt
    assert "Réponds uniquement avec l'objet JSON" in prompt
    assert "ne crée pas de pull request" in prompt.casefold()
    assert metadata["page_id"] == PAGE_ID
    assert metadata["branch"] == "agent/notion-crm-3c26e0d1a86e"



def test_workspace_agent_client_uses_the_trigger_api_and_beta_run_header() -> None:
    class FakeApi:
        def __init__(self) -> None:
            self.call: dict[str, Any] = {}

        def json(self, method: str, url: str, **kwargs: Any) -> dict[str, str]:
            self.call = {"method": method, "url": url, **kwargs}
            return {
                "conversation_url": "https://chatgpt.com/c/123",
                "agent_trigger_run_id": "apirun_123",
            }

    client = automation.WorkspaceAgentClient("secret", "agtch_crm_123")
    fake_api = FakeApi()
    client.api = fake_api  # type: ignore[assignment]
    result = client.trigger(
        input_text="Analyse cette demande",
        conversation_key="notion-crm-page",
        idempotency_key="notion-crm-page",
    )

    assert result == {
        "conversation_url": "https://chatgpt.com/c/123",
        "run_id": "apirun_123",
    }
    assert fake_api.call["method"] == "POST"
    assert fake_api.call["url"].endswith("/workspace_agents/agtch_crm_123/trigger")
    assert fake_api.call["expected"] == (202,)
    assert fake_api.call["headers"]["OpenAI-Beta"] == "workspace_agent_runs=v1"
    assert fake_api.call["headers"]["Idempotency-Key"] == "notion-crm-page"

    with pytest.raises(automation.AutomationError):
        automation.WorkspaceAgentClient("secret", "agent-invalide")


def test_tracking_properties_supports_report_and_clearing_error() -> None:
    properties = automation.tracking_properties(
        status="En cours",
        report="Trois tests réussis",
        pr_url="https://github.com/pr/1",
        chatgpt_url="https://chatgpt.com/c/123",
        chatgpt_run_id="apirun_123",
        clear_error=True,
    )

    assert properties["Statut"] == {"select": {"name": "En cours"}}
    assert properties["PR GitHub"] == {"url": "https://github.com/pr/1"}
    assert properties["Compte rendu IA"]["rich_text"][0]["text"]["content"] == "Trois tests réussis"
    assert properties["Conversation ChatGPT"] == {"url": "https://chatgpt.com/c/123"}
    assert properties["Run Agent ChatGPT"]["rich_text"][0]["text"]["content"] == "apirun_123"
    assert properties["Erreur automatisation"] == {"rich_text": []}


class FakeNotion:
    def __init__(self) -> None:
        self.page = eligible_page()
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.comments: list[tuple[str, str]] = []

    def query_ready_pages(self, data_source_id: str, *, page_size: int) -> list[dict[str, Any]]:
        assert data_source_id == automation.DEFAULT_DATA_SOURCE_ID
        assert page_size == 3
        return [self.page]

    def update_page(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        self.updates.append((page_id, properties))
        return {}

    def get_page(self, page_id: str) -> dict[str, Any]:
        return self.page

    def get_page_markdown(self, page_id: str) -> dict[str, Any]:
        return {
            "object": "page_markdown",
            "id": page_id,
            "markdown": "Détail fonctionnel complet",
            "truncated": False,
            "unknown_block_ids": [],
        }

    def iter_comments(self, block_id: str):
        yield {
            "rich_text": rich_text("Commentaire important"),
            "created_time": "2026-08-21T10:00:00.000Z",
            "created_by": {"id": "user-1"},
        }

    def safe_add_comment(self, page_id: str, text: str) -> None:
        self.comments.append((page_id, text))


class FakeGitHub:
    def __init__(self) -> None:
        self.issue_body = ""
        self.issue_comments: list[tuple[int, str]] = []
        self.dispatches: list[tuple[str, dict[str, Any]]] = []

    def create_issue(self, title: str, body: str) -> dict[str, Any]:
        assert title == "Ajouter un bouton de relance"
        self.issue_body = body
        return {
            "number": 77,
            "html_url": "https://github.com/integraleacademy/assistance/issues/77",
        }

    def comment_issue(self, number: int, body: str) -> None:
        self.issue_comments.append((number, body))

    def dispatch(self, event_type: str, payload: dict[str, Any]) -> None:
        self.dispatches.append((event_type, payload))



class FakeWorkspaceAgent:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def trigger(
        self,
        *,
        input_text: str,
        conversation_key: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        self.calls.append(
            {
                "input_text": input_text,
                "conversation_key": conversation_key,
                "idempotency_key": idempotency_key,
            }
        )
        return {
            "conversation_url": "https://chatgpt.com/c/123",
            "run_id": "apirun_123",
        }

def test_process_queue_reserves_creates_issue_dispatches_and_updates_notion() -> None:
    notion = FakeNotion()
    github = FakeGitHub()
    workspace_agent = FakeWorkspaceAgent()

    result = automation.process_queue(
        notion,  # type: ignore[arg-type]
        github,  # type: ignore[arg-type]
        data_source_id=automation.DEFAULT_DATA_SOURCE_ID,
        run_url="https://github.com/run/123",
        max_tasks=3,
        workspace_agent=workspace_agent,  # type: ignore[arg-type]
    )

    assert result["failures"] == []
    assert result["processed"][0]["issue_number"] == 77
    assert "Détail fonctionnel complet" in github.issue_body
    assert "Commentaire important" in github.issue_body
    assert github.dispatches == [
        (
            "notion_crm_task",
            {
                "notion_page_id": PAGE_ID,
                "issue_number": 77,
                "title": "Ajouter un bouton de relance",
                "notion_url": PAGE_URL,
            },
        )
    ]
    assert len(notion.updates) == 2
    final_properties = notion.updates[-1][1]
    assert final_properties["Tâche GitHub"]["url"].endswith("/issues/77")
    assert final_properties["ID automatisation"]["rich_text"][0]["text"]["content"] == "issue:77"
    assert final_properties["Conversation ChatGPT"] == {"url": "https://chatgpt.com/c/123"}
    assert final_properties["Run Agent ChatGPT"]["rich_text"][0]["text"]["content"] == "apirun_123"
    assert workspace_agent.calls[0]["conversation_key"] == "notion-crm-3c26e0d1a86e81929950cdf229ada797"
    assert "Détail fonctionnel complet" in workspace_agent.calls[0]["input_text"]
    assert any("Conversation ChatGPT Work" in comment for _, comment in notion.comments)


def test_patch_parser_accepts_a_textual_diff_and_report() -> None:
    from scripts import apply_notion_patch as applier

    payload = json.dumps(
        {
            "blocked": False,
            "blocker": "",
            "patch": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
            "report": "Test ciblé réussi",
        }
    )
    patch, report = applier.parse_codex_result(payload)
    assert patch.startswith("diff --git ")
    assert report == "Test ciblé réussi"


def test_patch_parser_rejects_blocked_binary_and_protected_changes(tmp_path: Path) -> None:
    from scripts import apply_notion_patch as applier

    with pytest.raises(applier.PatchError):
        applier.parse_codex_result(
            json.dumps({"blocked": True, "blocker": "Trop vaste", "patch": "", "report": ""})
        )

    binary_patch = tmp_path / "binary.patch"
    binary_patch.write_text(
        "diff --git a/logo.png b/logo.png\nGIT binary patch\n",
        encoding="utf-8",
    )
    with pytest.raises(applier.PatchError):
        applier.inspect_patch(binary_patch)

    with pytest.raises(applier.PatchError):
        applier.validate_paths([".git/hooks/pre-push"])
    with pytest.raises(applier.PatchError):
        applier.validate_paths(["folder/AGENTS.md"])


def test_patch_applier_applies_a_regular_text_patch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import apply_notion_patch as applier

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "app.py").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)

    patch_file = tmp_path / "change.patch"
    patch_file.write_text(
        "diff --git a/app.py b/app.py\n"
        "index 3367afd..3e75765 100644\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    paths = applier.inspect_patch(patch_file)
    applier.apply_patch(patch_file, paths)
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "new\n"


def test_validator_blocks_automation_and_sensitive_paths() -> None:
    from scripts import validate_notion_change as validator

    for path in (
        ".github/workflows/evil.yml",
        ".gitmodules",
        ".codex/config.toml",
        "folder/AGENTS.override.md",
        "requirements.txt",
        "scripts/apply_notion_patch.py",
        "notion_crm_lib/service.py",
        "../outside.txt",
    ):
        with pytest.raises(validator.ValidationError):
            validator.validate_paths([path])


def test_validator_accepts_a_scoped_crm_change_with_a_test() -> None:
    from scripts import validate_notion_change as validator

    assert validator.validate_paths(["app.py", "tests/test_crm_feature.py"]) == [
        "app.py",
        "tests/test_crm_feature.py",
    ]


def test_stager_manifest_rejects_traversal_and_deduplicates(tmp_path: Path) -> None:
    from scripts import stage_notion_changes as stager

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"version": 1, "paths": ["app.py", "app.py", "tests/test_crm.py"]}),
        encoding="utf-8",
    )
    assert stager.load_manifest(str(manifest)) == ["app.py", "tests/test_crm.py"]

    manifest.write_text(
        json.dumps({"version": 1, "paths": ["../secret"]}),
        encoding="utf-8",
    )
    with pytest.raises(stager.StagingError):
        stager.load_manifest(str(manifest))
