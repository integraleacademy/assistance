from __future__ import annotations
import codecs
import copy
import csv
import datetime as dt
import hashlib
import html
import io
import json
import re
import threading
import unicodedata
from collections import Counter, defaultdict
from html.parser import HTMLParser
from typing import Any, Iterable
import pytz
MAX_PUBLICATIONS_BYTES = 30 * 1024 * 1024
MAX_COMMENTS_BYTES = 15 * 1024 * 1024
MAX_USERS_BYTES = 5 * 1024 * 1024
_PARIS_TZ = pytz.timezone("Europe/Paris")
_IMPORT_LOCK = threading.Lock()
_SOURCE = "salesforce_chatter_import"
_PUBLICATION_REQUIRED = {"Id", "ParentId", "CreatedById", "CreatedDate", "Type"}
_COMMENT_REQUIRED = {"Id", "FeedItemId", "ParentId", "CreatedById", "CreatedDate", "CommentBody"}
_USER_REQUIRED = {"Id", "Name"}

def _text(value: Any) -> str:
    return str(value or "").strip()

def _fold(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", _text(value))
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Mn").casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", normalized)).strip()

def _truthy(value: Any) -> bool:
    return _fold(value) in {"1", "true", "yes", "oui", "y", "o", "vrai"}

def _integer(value: Any) -> int:
    try:
        return max(0, int(float(_text(value) or "0")))
    except (TypeError, ValueError):
        return 0

def _sf_key(value: Any) -> str:
    raw = _text(value)
    return raw[:15] if len(raw) >= 15 else raw

def _is_lead(value: Any) -> bool:
    return _sf_key(value).startswith("00Q")

def _is_task(value: Any) -> bool:
    return _sf_key(value).startswith("00T")

def _is_supported_parent(value: Any) -> bool:
    return _is_lead(value) or _is_task(value)

def _decode_csv(raw: bytes) -> str:
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return raw.decode("utf-32")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    if raw.count(b"\x00") > max(4, len(raw) // 20):
        odd_nulls = raw[1::2].count(0)
        even_nulls = raw[0::2].count(0)
        encodings = (("utf-16-le", "utf-16-be") if odd_nulls >= even_nulls else ("utf-16-be", "utf-16-le"))
        for encoding in encodings:
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                pass
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError("Encodage CSV illisible.")

def _find_header(text: str, required: set[str], label: str) -> tuple[list[str], int, str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and re.fullmatch(r"\s*sep\s*=\s*[,;\t|]\s*", lines[0], flags=re.I):
        lines = lines[1:]
    best: tuple[int, int, str] | None = None
    for delimiter in (",", ";", "\t", "|"):
        for index, line in enumerate(lines[:25]):
            try:
                cells = next(csv.reader([line], delimiter=delimiter))
            except csv.Error:
                continue
            headers = {_text(cell).lstrip("\ufeff") for cell in cells}
            candidate = (len(headers & required), index, delimiter)
            if best is None or candidate[0] > best[0]:
                best = candidate
    if best is None or best[0] < len(required):
        raise ValueError(f"Colonnes indispensables introuvables dans le fichier {label}.")
    return lines, best[1], best[2]

def _parse_csv(raw: bytes, *, required: set[str], max_bytes: int, label: str) -> list[dict[str, str]]:
    if not raw:
        raise ValueError(f"Le fichier {label} est vide.")
    if len(raw) > max_bytes:
        raise ValueError(f"Le fichier {label} dépasse {max_bytes // (1024 * 1024)} Mo.")
    decoded = _decode_csv(raw).replace("\x00", "")
    lines, header_index, delimiter = _find_header(decoded, required, label)
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:]), newline=""), delimiter=delimiter)
    original_headers = reader.fieldnames or []
    header_map = {header: _text(header).lstrip("\ufeff") for header in original_headers if header is not None}
    missing = sorted(required - set(header_map.values()))
    if missing:
        raise ValueError(f"Colonnes manquantes dans le fichier {label} : {', '.join(missing)}.")
    rows: list[dict[str, str]] = []
    for source in reader:
        if not isinstance(source, dict):
            continue
        row: dict[str, str] = {}
        for key, value in source.items():
            if key is None:
                continue
            canonical = header_map.get(key, _text(key).lstrip("\ufeff"))
            clean = _text(value)
            if clean or canonical not in row:
                row[canonical] = clean
        if any(_text(value) for value in row.values()):
            rows.append(row)
    return rows

def parse_salesforce_publications_csv(raw: bytes) -> list[dict[str, str]]:
    return _parse_csv(raw, required=_PUBLICATION_REQUIRED, max_bytes=MAX_PUBLICATIONS_BYTES, label="FeedItem")

def parse_salesforce_comments_csv(raw: bytes) -> list[dict[str, str]]:
    return _parse_csv(raw, required=_COMMENT_REQUIRED, max_bytes=MAX_COMMENTS_BYTES, label="FeedComment")

def parse_salesforce_users_csv(raw: bytes) -> list[dict[str, str]]:
    return _parse_csv(raw, required=_USER_REQUIRED, max_bytes=MAX_USERS_BYTES, label="User")

class _HtmlToText(HTMLParser):
    BLOCKS = {"address", "article", "aside", "blockquote", "div", "dl", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul"}
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
    def _newline(self) -> None:
        if not self.parts or not self.parts[-1].endswith("\n"):
            self.parts.append("\n")
    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.casefold()
        if tag in self.BLOCKS or tag == "br":
            self._newline()
        if tag == "li":
            self._newline()
            self.parts.append("• ")
    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self.BLOCKS | {"li"}:
            self._newline()
    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)

def _plain(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    parser = _HtmlToText()
    try:
        parser.feed(raw)
        parser.close()
        rendered = "".join(parser.parts)
    except Exception:
        rendered = re.sub(r"<[^>]+>", " ", raw)
    rendered = html.unescape(rendered).replace("\xa0", " ")
    rendered = rendered.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in rendered.split("\n")]
    result: list[str] = []
    blank = False
    for line in lines:
        if line:
            result.append(line)
            blank = False
        elif result and not blank:
            result.append("")
            blank = True
    return "\n".join(result).strip()

def _author_map(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, str]]:
    authors: dict[str, dict[str, str]] = {}
    for row in rows:
        identifier = _text(row.get("Id"))
        if not identifier:
            continue
        authors[identifier] = {"name": _text(row.get("Name")) or _text(row.get("Email")) or identifier, "email": _text(row.get("Email"))}
    return authors

def _author(authors: dict[str, dict[str, str]], identifier: Any) -> tuple[str, str]:
    author_id = _text(identifier)
    data = authors.get(author_id) or {}
    name = _text(data.get("name")) or (f"Utilisateur Salesforce {author_id}" if author_id else "Salesforce")
    return name, _text(data.get("email"))

def _comment_payload(row: dict[str, Any], authors: dict[str, dict[str, str]]) -> dict[str, Any] | None:
    if _truthy(row.get("IsDeleted")):
        return None
    status = _fold(row.get("Status"))
    if status and status != "published":
        return None
    identifier = _text(row.get("Id"))
    feed_id = _text(row.get("FeedItemId"))
    parent_id = _text(row.get("ParentId"))
    body = _plain(row.get("CommentBody"))
    if not identifier or not feed_id or not _is_supported_parent(parent_id) or not body:
        return None
    author, email = _author(authors, row.get("CreatedById"))
    created_by = _text(row.get("CreatedById"))
    return {"id": f"sf-comment-{identifier}", "date": _text(row.get("CreatedDate")) or _text(row.get("SystemModstamp")), "texte": body, "author": author, "author_email": f"salesforce://{created_by or 'system'}", "source": _SOURCE, "salesforce_feed_comment_id": identifier, "salesforce_feed_item_id": feed_id, "salesforce_parent_id": parent_id, "salesforce_created_by_id": created_by, "salesforce_author_email": email, "salesforce_comment_type": _text(row.get("CommentType")), "salesforce_last_edit_date": _text(row.get("LastEditDate"))}

def _prepare_comments(rows: Iterable[dict[str, Any]], authors: dict[str, dict[str, str]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[str] = set()
    stats: Counter[str] = Counter()
    for row in rows:
        stats["comment_rows"] += 1
        if not _is_supported_parent(row.get("ParentId")):
            stats["comments_ignored_unsupported_parent"] += 1
            continue
        stats["lead_comments" if _is_lead(row.get("ParentId")) else "task_comments"] += 1
        identifier = _text(row.get("Id"))
        if identifier in seen:
            stats["comment_duplicates"] += 1
            continue
        payload = _comment_payload(row, authors)
        if payload is None:
            stats["comments_ignored_invalid"] += 1
            continue
        seen.add(identifier)
        grouped[payload["salesforce_feed_item_id"]].append(payload)
    for comments in grouped.values():
        comments.sort(key=lambda item: (_text(item.get("date")), _text(item.get("id"))))
    return grouped, dict(stats)

def _publication_text(row: dict[str, Any], has_comments: bool) -> str:
    body = _plain(row.get("Body"))
    title = _plain(row.get("Title"))
    link = _text(row.get("LinkUrl"))
    kind = _text(row.get("Type"))
    parts: list[str] = []
    if body:
        parts.append(body)
    if title:
        value = f"Pièce jointe Salesforce : {title}" if kind == "ContentPost" else title
        if value not in parts:
            parts.append(value)
    if link and link not in "\n".join(parts):
        parts.append(f"Lien : {link}")
    if not parts and has_comments:
        parts.append("Modification Salesforce" if kind == "TrackedChange" else "Publication Salesforce sans texte")
    return "\n\n".join(parts).strip()

def _publication_payload(row: dict[str, Any], authors: dict[str, dict[str, str]], comments: list[dict[str, Any]]) -> dict[str, Any] | None:
    if _truthy(row.get("IsDeleted")):
        return None
    status = _fold(row.get("Status"))
    if status and status != "published":
        return None
    identifier = _text(row.get("Id"))
    parent_id = _text(row.get("ParentId"))
    if not identifier or not _is_supported_parent(parent_id):
        return None
    body = _publication_text(row, bool(comments))
    if not body:
        return None
    author, email = _author(authors, row.get("CreatedById"))
    created_by = _text(row.get("CreatedById"))
    return {"id": f"sf-feed-{identifier}", "date": _text(row.get("CreatedDate")) or _text(row.get("LastModifiedDate")), "texte": body, "author": author, "author_email": f"salesforce://{created_by or 'system'}", "comments": comments, "source": _SOURCE, "salesforce_feed_item_id": identifier, "salesforce_parent_id": parent_id, "salesforce_created_by_id": created_by, "salesforce_author_email": email, "salesforce_type": _text(row.get("Type")), "salesforce_title": _plain(row.get("Title")), "salesforce_link_url": _text(row.get("LinkUrl")), "salesforce_related_record_id": _text(row.get("RelatedRecordId")), "salesforce_comment_count": _integer(row.get("CommentCount")), "salesforce_like_count": _integer(row.get("LikeCount")), "salesforce_has_content": _truthy(row.get("HasContent")), "salesforce_last_modified_date": _text(row.get("LastModifiedDate"))}

def _prepare_publications(rows: Iterable[dict[str, Any]], authors: dict[str, dict[str, str]], comments_by_feed: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    seen: set[str] = set()
    stats: Counter[str] = Counter()
    types: Counter[str] = Counter()
    for row in rows:
        stats["publication_rows"] += 1
        if not _is_supported_parent(row.get("ParentId")):
            stats["publications_ignored_unsupported_parent"] += 1
            continue
        stats["lead_publications" if _is_lead(row.get("ParentId")) else "task_publications"] += 1
        identifier = _text(row.get("Id"))
        if identifier in seen:
            stats["publication_duplicates"] += 1
            continue
        payload = _publication_payload(row, authors, copy.deepcopy(comments_by_feed.get(identifier) or []))
        if payload is None:
            stats["publications_ignored_empty"] += 1
            continue
        seen.add(identifier)
        prepared.append(payload)
        types[payload.get("salesforce_type") or "Non renseigné"] += 1
    prepared_ids = {item["salesforce_feed_item_id"] for item in prepared}
    for feed_id, comments in comments_by_feed.items():
        if feed_id in prepared_ids or not comments:
            continue
        parent_id = _text(comments[0].get("salesforce_parent_id"))
        if not _is_supported_parent(parent_id):
            continue
        prepared.append({"id": f"sf-feed-{feed_id}", "date": _text(comments[0].get("date")), "texte": "Publication Salesforce non présente dans l'export", "author": "Salesforce", "author_email": "salesforce://system", "comments": copy.deepcopy(comments), "source": _SOURCE, "salesforce_feed_item_id": feed_id, "salesforce_parent_id": parent_id, "salesforce_type": "MissingFeedItem", "salesforce_comment_count": len(comments)})
        stats["publication_placeholders"] += 1
        types["MissingFeedItem"] += 1
    prepared.sort(key=lambda item: (_text(item.get("date")), _text(item.get("id"))), reverse=True)
    result: dict[str, Any] = dict(stats)
    result["prepared_publications"] = len(prepared)
    result["publication_type_counts"] = dict(types.most_common())
    return prepared, result

def _contact_name(contact: dict[str, Any]) -> str:
    return " ".join(part for part in (_text(contact.get("prenom")), _text(contact.get("nom"))) if part)

def _contact_sf_keys(contact: dict[str, Any]) -> set[str]:
    values: list[Any] = [contact.get("salesforce_id"), contact.get("salesforce_lead_id")]
    extra = contact.get("salesforce_ids")
    if isinstance(extra, (list, tuple, set)):
        values.extend(extra)
    elif extra:
        values.append(extra)
    return {_sf_key(value) for value in values if _sf_key(value)}

def _contact_index(contacts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for contact in contacts:
        for key in _contact_sf_keys(contact):
            if all(item is not contact for item in result[key]):
                result[key].append(contact)
    return result

def _task_contact_index(contacts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for contact in contacts:
        for relance in contact.get("relances") or []:
            if not isinstance(relance, dict):
                continue
            key = _sf_key(relance.get("salesforce_task_id"))
            if key and all(item is not contact for item in result[key]):
                result[key].append(contact)
    return result

def _publication_index(contact: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {_text(item.get("salesforce_feed_item_id")): item for item in (contact.get("salesforce_chatter") or []) if isinstance(item, dict) and _text(item.get("salesforce_feed_item_id"))}

def _comment_signature(item: dict[str, Any]) -> tuple[Any, ...]:
    return (_text(item.get("date")), _text(item.get("texte")), _text(item.get("author")), _text(item.get("salesforce_created_by_id")), _text(item.get("salesforce_last_edit_date")))

def _publication_signature(item: dict[str, Any]) -> tuple[Any, ...]:
    comments = tuple((_text(comment.get("salesforce_feed_comment_id")), _comment_signature(comment)) for comment in (item.get("comments") or []) if isinstance(comment, dict) and _text(comment.get("salesforce_feed_comment_id")))
    return (_text(item.get("date")), _text(item.get("texte")), _text(item.get("author")), _text(item.get("salesforce_type")), _text(item.get("salesforce_title")), _text(item.get("salesforce_link_url")), _text(item.get("salesforce_related_record_id")), _integer(item.get("salesforce_comment_count")), _integer(item.get("salesforce_like_count")), comments)

def _merge_comments(existing: dict[str, Any], incoming: dict[str, Any]) -> tuple[int, int, int]:
    current_rows = [row for row in (existing.get("comments") or []) if isinstance(row, dict)]
    by_id = {_text(row.get("salesforce_feed_comment_id")): row for row in current_rows if _text(row.get("salesforce_feed_comment_id"))}
    created = updated = unchanged = 0
    for source in incoming.get("comments") or []:
        identifier = _text(source.get("salesforce_feed_comment_id"))
        current = by_id.get(identifier)
        if current is None:
            current_rows.append(copy.deepcopy(source))
            by_id[identifier] = current_rows[-1]
            created += 1
        elif _comment_signature(current) == _comment_signature(source):
            unchanged += 1
        else:
            preserved = {key: value for key, value in current.items() if key not in source}
            current.clear()
            current.update(preserved)
            current.update(copy.deepcopy(source))
            updated += 1
    current_rows.sort(key=lambda row: (_text(row.get("date")), _text(row.get("id"))))
    existing["comments"] = current_rows
    return created, updated, unchanged

def _merge_publication(existing: dict[str, Any], incoming: dict[str, Any]) -> tuple[bool, int, int, int]:
    before = _publication_signature(existing)
    created, updated, unchanged = _merge_comments(existing, incoming)
    comments = existing.get("comments") or []
    preserved = {key: value for key, value in existing.items() if key not in incoming and key != "comments"}
    existing.clear()
    existing.update(preserved)
    existing.update({key: copy.deepcopy(value) for key, value in incoming.items() if key != "comments"})
    existing["comments"] = comments
    return before != _publication_signature(existing), created, updated, unchanged

__all__ = [name for name in globals() if not name.startswith("__")]
