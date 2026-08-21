"""Constantes, modèles et conversions Notion de l’automatisation CRM."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

NOTION_API_BASE = "https://api.notion.com/v1"
GITHUB_API_BASE = "https://api.github.com"
NOTION_VERSION = "2026-03-11"
DEFAULT_DATA_SOURCE_ID = "7f12fe92-dbc4-40c8-af4e-77578b5dbfc0"
AUTOMATION_VERSION = "notion-crm-v1"
MAX_GITHUB_BODY_CHARS = 60_000
MAX_NOTION_PROPERTY_CHARS = 1_900
MAX_PAGE_CONTENT_CHARS = 45_000
MAX_COMMENT_CONTENT_CHARS = 10_000

TRIGGER_DOMAIN = "Développement web"
TRIGGER_PLATFORM = "CRM"
TRIGGER_STATUS = "À faire"

NOTION_PAGE_ID_RE = re.compile(r"<!--\s*notion-page-id:\s*([0-9a-fA-F-]{32,36})\s*-->")
NOTION_PAGE_URL_RE = re.compile(r"<!--\s*notion-page-url:\s*(https?://[^\s]+)\s*-->")


class AutomationError(RuntimeError):
    """Erreur attendue et compréhensible de l'automatisation."""


@dataclass(frozen=True)
class PageSnapshot:
    """Version figée d'une demande Notion au moment de sa prise en charge."""

    page_id: str
    url: str
    title: str
    properties: Mapping[str, Any]
    content: str
    comments: Sequence[str]


def utc_now_iso() -> str:
    """Retourne un horodatage ISO 8601 en UTC sans microsecondes."""

    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def compact_page_id(page_id: str) -> str:
    """Normalise un identifiant Notion pour les marqueurs et noms de branche."""

    compact = re.sub(r"[^0-9a-fA-F]", "", str(page_id or ""))
    if len(compact) != 32:
        raise AutomationError(f"Identifiant de page Notion invalide : {page_id!r}")
    return compact.lower()


def dashed_page_id(page_id: str) -> str:
    """Retourne l'identifiant Notion au format UUID avec tirets."""

    compact = compact_page_id(page_id)
    return f"{compact[:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:]}"


def branch_name_for_page(page_id: str) -> str:
    """Construit le nom de branche déterministe d'une demande Notion."""

    return f"agent/notion-crm-{compact_page_id(page_id)[:12]}"


def split_text(value: str, limit: int = 2_000) -> list[str]:
    """Découpe un texte selon les limites des objets rich_text Notion."""

    text = str(value or "")
    if not text:
        return []
    return [text[index : index + limit] for index in range(0, len(text), limit)]


def notion_rich_text(value: str, *, limit: int = MAX_NOTION_PROPERTY_CHARS) -> list[dict[str, Any]]:
    """Construit une valeur rich_text Notion en évitant les dépassements."""

    text = str(value or "").strip()
    if not text:
        return []
    text = text[:limit]
    return [
        {"type": "text", "text": {"content": chunk}}
        for chunk in split_text(text, 2_000)
    ]


def plain_rich_text(items: Any) -> str:
    """Extrait le texte visible d'une collection rich_text Notion."""

    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        return ""
    parts: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        plain = item.get("plain_text")
        if plain is None:
            text = item.get("text")
            plain = text.get("content") if isinstance(text, Mapping) else ""
        parts.append(str(plain or ""))
    return "".join(parts).strip()


def property_value_text(prop: Any) -> str:
    """Convertit une propriété de page Notion en texte lisible."""

    if not isinstance(prop, Mapping):
        return ""
    prop_type = str(prop.get("type") or "")
    value = prop.get(prop_type)

    if prop_type in {"title", "rich_text"}:
        return plain_rich_text(value)
    if prop_type in {"select", "status"}:
        return str(value.get("name") or "") if isinstance(value, Mapping) else ""
    if prop_type == "multi_select" and isinstance(value, Sequence):
        return ", ".join(
            str(item.get("name") or "")
            for item in value
            if isinstance(item, Mapping) and item.get("name")
        )
    if prop_type == "date" and isinstance(value, Mapping):
        start = str(value.get("start") or "")
        end = str(value.get("end") or "")
        return f"{start} → {end}" if start and end else start
    if prop_type in {"url", "email", "phone_number", "number", "checkbox"}:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "Oui" if value else "Non"
        return str(value)
    if prop_type in {"created_time", "last_edited_time"}:
        return str(value or "")
    if prop_type in {"created_by", "last_edited_by"} and isinstance(value, Mapping):
        return str(value.get("name") or value.get("id") or "")
    if prop_type == "people" and isinstance(value, Sequence):
        return ", ".join(
            str(item.get("name") or item.get("id") or "")
            for item in value
            if isinstance(item, Mapping)
        )
    if prop_type == "relation" and isinstance(value, Sequence):
        return ", ".join(
            str(item.get("id") or "")
            for item in value
            if isinstance(item, Mapping) and item.get("id")
        )
    if prop_type == "files" and isinstance(value, Sequence):
        names: list[str] = []
        for item in value:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "")
            file_type = item.get("type")
            file_payload = item.get(file_type) if isinstance(file_type, str) else None
            url = file_payload.get("url") if isinstance(file_payload, Mapping) else ""
            names.append(f"{name} ({url})" if name and url else name or str(url or ""))
        return ", ".join(part for part in names if part)
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return ""


def page_property(page: Mapping[str, Any], name: str) -> str:
    properties = page.get("properties")
    if not isinstance(properties, Mapping):
        return ""
    return property_value_text(properties.get(name))


def page_title(page: Mapping[str, Any]) -> str:
    """Trouve la propriété titre sans dépendre de son nom français."""

    properties = page.get("properties")
    if isinstance(properties, Mapping):
        for prop in properties.values():
            if isinstance(prop, Mapping) and prop.get("type") == "title":
                title = property_value_text(prop)
                if title:
                    return title
    return "Demande CRM sans titre"


def is_eligible_page(page: Mapping[str, Any]) -> bool:
    """Vérifie le déclencheur métier exact et l'absence de prise en charge."""

    automation_id = page_property(page, "ID automatisation")
    return (
        page_property(page, "Domaine") == TRIGGER_DOMAIN
        and page_property(page, "Plateforme") == TRIGGER_PLATFORM
        and page_property(page, "Statut") == TRIGGER_STATUS
        and not automation_id.strip()
    )


def _safe_issue_title(title: str) -> str:
    clean = " ".join(str(title or "").split()) or "Demande CRM sans titre"
    prefix = "[Notion CRM] "
    return (prefix + clean)[:256]


def _truncate(text: str, limit: int, note: str) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    suffix = f"\n\n> {note}\n"
    return value[: max(0, limit - len(suffix))].rstrip() + suffix
