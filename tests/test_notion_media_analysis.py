from __future__ import annotations

from typing import Any

import pytest

from notion_crm_lib.clients import OpenAIMediaClient
from notion_crm_lib.core import MediaAttachment, PageSnapshot
from notion_crm_lib.service import (
    build_issue_body,
    enrich_snapshot_with_media_analysis,
    extract_media_from_comment,
    extract_media_from_markdown,
    extract_media_from_properties,
)


def test_extract_media_from_notion_markdown() -> None:
    markdown = """
Voici la zone à revoir :

![Capture fiche CRM](https://prod-files-secure.s3.us-west-2.amazonaws.com/workspace/capture.png?X-Amz-Signature=test)

<pdf src="https://prod-files-secure.s3.us-west-2.amazonaws.com/workspace/spec.pdf?X-Amz-Signature=test">Maquette PDF</pdf>

<file src="https://prod-files-secure.s3.us-west-2.amazonaws.com/workspace/brief.docx?X-Amz-Signature=test">Brief</file>
"""

    media = extract_media_from_markdown(markdown)

    assert [(item.kind, item.caption) for item in media] == [
        ("image", "Capture fiche CRM"),
        ("pdf", "Maquette PDF"),
        ("file", "Brief"),
    ]


def test_extract_media_rejects_local_urls() -> None:
    markdown = "![Secret](https://127.0.0.1/internal.png)"
    assert extract_media_from_markdown(markdown) == []


def test_extract_media_from_files_property_and_comment_attachment() -> None:
    properties = {
        "Pièces jointes": {
            "type": "files",
            "files": [
                {
                    "name": "maquette.xlsx",
                    "type": "file",
                    "file": {"url": "https://files.notion.so/maquette.xlsx?sig=1"},
                }
            ],
        }
    }
    comment = {
        "attachments": [
            {
                "name": "capture.webp",
                "type": "file",
                "file": {"url": "https://files.notion.so/capture.webp?sig=2"},
            }
        ]
    }

    prop_media = extract_media_from_properties(properties)
    comment_media = extract_media_from_comment(comment, index=2)

    assert len(prop_media) == 1
    assert prop_media[0].kind == "file"
    assert prop_media[0].filename == "maquette.xlsx"
    assert len(comment_media) == 1
    assert comment_media[0].kind == "image"
    assert comment_media[0].source == "commentaire Notion 2"


def test_openai_media_client_builds_multimodal_responses_payload() -> None:
    class FakeApi:
        def __init__(self) -> None:
            self.call: dict[str, Any] = {}

        def json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
            self.call = {"method": method, "url": url, **kwargs}
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "### Ce que montrent les pièces jointes\nUne fiche CRM.",
                            }
                        ],
                    }
                ]
            }

    client = OpenAIMediaClient("test-key", model="gpt-5.6-luna")
    fake_api = FakeApi()
    client.api = fake_api  # type: ignore[assignment]

    result = client.analyze(
        title="Revoir la fiche",
        context="Je trouve cette zone moche.",
        attachments=[
            MediaAttachment(
                kind="image",
                url="https://files.notion.so/capture.png",
                caption="Capture",
            ),
            MediaAttachment(
                kind="pdf",
                url="https://files.notion.so/maquette.pdf",
                caption="Maquette",
            ),
        ],
    )

    assert "Une fiche CRM" in result
    payload = fake_api.call["json"]
    assert payload["model"] == "gpt-5.6-luna"
    content = payload["input"][0]["content"]
    assert any(item.get("type") == "input_image" for item in content)
    assert any(item.get("type") == "input_file" for item in content)
    image = next(item for item in content if item.get("type") == "input_image")
    pdf = next(item for item in content if item.get("type") == "input_file")
    assert image["detail"] == "high"
    assert pdf["detail"] == "high"


def test_issue_contains_media_inventory_and_analysis() -> None:
    snapshot = PageSnapshot(
        page_id="3c26e0d1-a86e-8192-9950-cdf229ada797",
        url="https://www.notion.so/example",
        title="Revoir la fiche",
        properties={},
        content="Je trouve cette zone trop chargée.",
        comments=[],
        attachments=(
            MediaAttachment(
                kind="image",
                url="https://files.notion.so/capture.png",
                caption="Capture écran",
            ),
        ),
        media_analysis="### Ce que montrent les pièces jointes\nLa zone est très dense.",
    )

    body = build_issue_body(snapshot)

    assert "## Pièces jointes détectées" in body
    assert "Capture écran" in body
    assert "## Analyse visuelle et documentaire des pièces jointes" in body
    assert "La zone est très dense" in body
    assert "https://files.notion.so/capture.png" not in body


def test_media_is_not_silently_ignored_without_analyzer() -> None:
    snapshot = PageSnapshot(
        page_id="3c26e0d1-a86e-8192-9950-cdf229ada797",
        url="https://www.notion.so/example",
        title="Revoir la fiche",
        properties={},
        content="Capture ci-dessous",
        comments=[],
        attachments=(
            MediaAttachment(kind="image", url="https://files.notion.so/capture.png"),
        ),
    )

    with pytest.raises(Exception, match="analyse multimodale"):
        enrich_snapshot_with_media_analysis(snapshot, None)
