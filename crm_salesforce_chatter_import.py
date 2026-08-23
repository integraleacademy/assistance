"""Import sécurisé des publications et commentaires Chatter Salesforce.

Le traitement accepte trois exports Data Loader :

- ``FeedItem`` pour les publications ;
- ``FeedComment`` pour les commentaires ;
- ``User`` pour résoudre les auteurs.

Seuls les éléments rattachés directement à une piste Salesforce (ParentId
commençant par ``00Q``) et à une fiche déjà présente dans le CRM sont repris.
Aucune personne n'est créée. Les identifiants Salesforce assurent
l'idempotence et un aperçu signé est obligatoire avant toute écriture.
"""

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

_PUBLICATION_REQUIRED_HEADERS = {
    "Id",
    "ParentId",
    "CreatedById",
    "CreatedDate",
    "Type",
}
_COMMENT_REQUIRED_HEADERS = {
    "Id",
    "FeedItemId",
    "ParentId",
    "CreatedById",
    "CreatedDate",
    "CommentBody",
}
_USER_REQUIRED_HEADERS = {"Id", "Name"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _fold(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", _text(value))
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Mn"
    ).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", normalized)).strip()


def _truthy(value: Any) -> bool:
    return _fold(value) in {"1", "true", "yes", "oui", "y", "o", "vrai"}


def _integer(value: Any) -> int:
    try:
        return max(0, int(float(_text(value) or "0")))
    except (TypeError, ValueError):
        return 0


def _salesforce_key(value: Any) -> str:
    """Compare indifféremment les identifiants Salesforce 15 et 18 caractères."""
    raw = _text(value)
    return raw[:15] if len(raw) >= 15 else raw


def _is_lead_id(value: Any) -> bool:
    return _salesforce_key(value).startswith("00Q")


def _decode_csv(raw: bytes) -> str:
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return raw.decode("utf-32")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    if raw.count(b"\x00") > max(4, len(raw) // 20):
        odd_nulls = raw[1::2].count(0)
        even_nulls = raw[0::2].count(0)
        encodings = (
            ("utf-16-le", "utf-16-be")
            if odd_nulls >= even_nulls
            else ("utf-16-be", "utf-16-le")
        )
        for encoding in encodings:
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
    errors: list[str] = []
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")
    raise ValueError("Encodage CSV illisible : " + " | ".join(errors[:2]))


def _canonical_header(value: Any) -> str:
    return _text(value).lstrip("\ufeff")


def _find_header(
    text: str,
    *,
    required: set[str],
    label: str,
) -> tuple[list[str], int, str]:
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
            headers = {_canonical_header(cell) for cell in cells}
            score = len(headers & required)
            candidate = (score, index, delimiter)
            if best is None or candidate[0] > best[0]:
                best = candidate

    if best is None or best[0] < len(required):
        preview = " | ".join(line[:180] for line in lines[:3] if line.strip())
        raise ValueError(
            f"Impossible d’identifier les colonnes du fichier {label}. "
            f"Début du fichier : {preview or 'aucun contenu lisible'}"
        )
    return lines, best[1], best[2]


def _parse_csv(
    raw: bytes,
    *,
    required: set[str],
    max_bytes: int,
    label: str,
) -> list[dict[str, str]]:
    if not raw:
        raise ValueError(f"Le fichier {label} est vide.")
    if len(raw) > max_bytes:
        raise ValueError(
            f"Le fichier {label} dépasse la limite de {max_bytes // (1024 * 1024)} Mo."
        )

    decoded = _decode_csv(raw).replace("\x00", "")
    lines, header_index, delimiter = _find_header(
        decoded,
        required=required,
        label=label,
    )
    reader = csv.DictReader(
        io.StringIO("\n".join(lines[header_index:]), newline=""),
        delimiter=delimiter,
    )
    headers = [_canonical_header(header) for header in (reader.fieldnames or [])]
    missing = sorted(required - set(headers))
    if missing:
        raise ValueError(
            f"Colonnes indispensables manquantes dans {label} : "
            + ", ".join(missing)
            + "."
        )

    original_headers = reader.fieldnames or []
    header_map = {
        original: _canonical_header(original)
        for original in original_headers
        if original is not None
    }
    rows: list[dict[str, str]] = []
    for source in reader:
        if not isinstance(source, dict):
            continue
        row: dict[str, str] = {}
        for key, value in source.items():
            if key is None:
                continue
            canonical = header_map.get(key, _canonical_header(key))
            clean = _text(value)
            if clean or canonical not in row:
                row[canonical] = clean
        if any(_text(value) for value in row.values()):
            rows.append(row)
    return rows


def parse_salesforce_publications_csv(raw: bytes) -> list[dict[str, str]]:
    return _parse_csv(
        raw,
        required=_PUBLICATION_REQUIRED_HEADERS,
        max_bytes=MAX_PUBLICATIONS_BYTES,
        label="des publications Salesforce",
    )


def parse_salesforce_comments_csv(raw: bytes) -> list[dict[str, str]]:
    return _parse_csv(
        raw,
        required=_COMMENT_REQUIRED_HEADERS,
        max_bytes=MAX_COMMENTS_BYTES,
        label="des commentaires Salesforce",
    )


def parse_salesforce_users_csv(raw: bytes) -> list[dict[str, str]]:
    return _parse_csv(
        raw,
        required=_USER_REQUIRED_HEADERS,
        max_bytes=MAX_USERS_BYTES,
        label="des utilisateurs Salesforce",
    )


class _RichTextToPlain(HTMLParser):
    BLOCK_START = {
        "address", "article", "aside", "blockquote", "div", "dl",
        "fieldset", "figcaption", "figure", "footer", "form", "h1",
        "h2", "h3", "h4", "h5", "h6", "header", "hr", "main",
        "nav", "ol", "p", "pre", "section", "table", "tbody", "td",
        "tfoot", "th", "thead", "tr", "ul",
    }
    BLOCK_END = BLOCK_START | {"li"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def _newline(self) -> None:
        if not self.parts or not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs) -> None:
        lowered = tag.casefold()
        if lowered in self.BLOCK_START:
            self._newline()
        if lowered == "br":
            self._newline()
        elif lowered == "li":
            self._newline()
            self.parts.append("• ")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self.BLOCK_END:
            self._newline()

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def _plain_text(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    parser = _RichTextToPlain()
    try:
        parser.feed(raw)
        parser.close()
        rendered = parser.text()
    except Exception:
        rendered = re.sub(r"<[^>]+>", " ", raw)
    rendered = html.unescape(rendered).replace("\xa0", " ")
    rendered = rendered.replace("\r\n", "\n").replace("\r", "\n")
    cleaned_lines = [
        re.sub(r"[ \t]+", " ", line).strip()
        for line in rendered.split("\n")
    ]
    result: list[str] = []
    blank = False
    for line in cleaned_lines:
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
        authors[identifier] = {
            "name": _text(row.get("Name")) or _text(row.get("Email")) or identifier,
            "email": _text(row.get("Email")),
            "active": _text(row.get("IsActive")),
        }
    return authors


def _author(
    authors: dict[str, dict[str, str]],
    identifier: Any,
) -> tuple[str, str]:
    author_id = _text(identifier)
    data = authors.get(author_id) or {}
    name = _text(data.get("name")) or (
        f"Utilisateur Salesforce {author_id}" if author_id else "Salesforce"
    )
    return name, _text(data.get("email"))


def _comment_payload(
    row: dict[str, Any],
    authors: dict[str, dict[str, str]],
) -> dict[str, Any] | None:
    if _truthy(row.get("IsDeleted")):
        return None
    status = _fold(row.get("Status"))
    if status and status != "published":
        return None
    identifier = _text(row.get("Id"))
    feed_item_id = _text(row.get("FeedItemId"))
    parent_id = _text(row.get("ParentId"))
    text = _plain_text(row.get("CommentBody"))
    if not identifier or not feed_item_id or not _is_lead_id(parent_id) or not text:
        return None
    author, author_email = _author(authors, row.get("CreatedById"))
    return {
        "id": f"sf-comment-{identifier}",
        "date": _text(row.get("CreatedDate")) or _text(row.get("SystemModstamp")),
        "texte": text,
        "author": author,
        "author_email": f"salesforce://{_text(row.get('CreatedById'))}",
        "source": _SOURCE,
        "salesforce_feed_comment_id": identifier,
        "salesforce_feed_item_id": feed_item_id,
        "salesforce_parent_id": parent_id,
        "salesforce_created_by_id": _text(row.get("CreatedById")),
        "salesforce_author_email": author_email,
        "salesforce_comment_type": _text(row.get("CommentType")),
        "salesforce_last_edit_date": _text(row.get("LastEditDate")),
    }


def _prepare_comments(
    rows: Iterable[dict[str, Any]],
    authors: dict[str, dict[str, str]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[str] = set()
    stats: Counter[str] = Counter()

    for row in rows:
        stats["comment_rows"] += 1
        if not _is_lead_id(row.get("ParentId")):
            stats["comments_ignored_non_lead"] += 1
            continue
        if _truthy(row.get("IsDeleted")):
            stats["comments_ignored_deleted"] += 1
            continue
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
        stats["lead_comments"] += 1

    for values in grouped.values():
        values.sort(key=lambda item: (_text(item.get("date")), _text(item.get("id"))))
    return grouped, dict(stats)


def _publication_text(
    row: dict[str, Any],
    *,
    has_comments: bool,
) -> str:
    body = _plain_text(row.get("Body"))
    title = _plain_text(row.get("Title"))
    link_url = _text(row.get("LinkUrl"))
    publication_type = _text(row.get("Type"))
    parts: list[str] = []

    if body:
        parts.append(body)
    if title:
        label = (
            f"Pièce jointe Salesforce : {title}"
            if publication_type == "ContentPost"
            else title
        )
        if label not in parts:
            parts.append(label)
    if link_url and link_url not in "\n".join(parts):
        parts.append(f"Lien : {link_url}")
    if not parts and has_comments:
        parts.append(
            "Modification Salesforce"
            if publication_type == "TrackedChange"
            else "Publication Salesforce sans texte"
        )
    return "\n\n".join(parts).strip()


def _publication_payload(
    row: dict[str, Any],
    authors: dict[str, dict[str, str]],
    comments: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if _truthy(row.get("IsDeleted")):
        return None
    status = _fold(row.get("Status"))
    if status and status != "published":
        return None
    identifier = _text(row.get("Id"))
    parent_id = _text(row.get("ParentId"))
    if not identifier or not _is_lead_id(parent_id):
        return None
    text = _publication_text(row, has_comments=bool(comments))
    if not text:
        return None
    author, author_email = _author(authors, row.get("CreatedById"))
    return {
        "id": f"sf-feed-{identifier}",
        "date": _text(row.get("CreatedDate")) or _text(row.get("LastModifiedDate")),
        "texte": text,
        "author": author,
        "author_email": f"salesforce://{_text(row.get('CreatedById'))}",
        "comments": comments,
        "source": _SOURCE,
        "salesforce_feed_item_id": identifier,
        "salesforce_parent_id": parent_id,
        "salesforce_created_by_id": _text(row.get("CreatedById")),
        "salesforce_author_email": author_email,
        "salesforce_type": _text(row.get("Type")),
        "salesforce_title": _plain_text(row.get("Title")),
        "salesforce_link_url": _text(row.get("LinkUrl")),
        "salesforce_related_record_id": _text(row.get("RelatedRecordId")),
        "salesforce_comment_count": _integer(row.get("CommentCount")),
        "salesforce_like_count": _integer(row.get("LikeCount")),
        "salesforce_has_content": _truthy(row.get("HasContent")),
        "salesforce_last_modified_date": _text(row.get("LastModifiedDate")),
    }


def _prepare_publications(
    rows: Iterable[dict[str, Any]],
    authors: dict[str, dict[str, str]],
    comments_by_feed_item: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    prepared: list[dict[str, Any]] = []
    seen: set[str] = set()
    stats: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()

    for row in rows:
        stats["publication_rows"] += 1
        if not _is_lead_id(row.get("ParentId")):
            stats["publications_ignored_non_lead"] += 1
            continue
        stats["lead_publications"] += 1
        if _truthy(row.get("IsDeleted")):
            stats["publications_ignored_deleted"] += 1
            continue
        identifier = _text(row.get("Id"))
        if identifier in seen:
            stats["publication_duplicates"] += 1
            continue
        comments = copy.deepcopy(comments_by_feed_item.get(identifier) or [])
        payload = _publication_payload(row, authors, comments)
        if payload is None:
            stats["publications_ignored_empty"] += 1
            continue
        seen.add(identifier)
        prepared.append(payload)
        type_counts[payload.get("salesforce_type") or "Non renseigné"] += 1

    prepared_ids = {item["salesforce_feed_item_id"] for item in prepared}
    for feed_item_id, values in comments_by_feed_item.items():
        if feed_item_id in prepared_ids or not values:
            continue
        parent_id = _text(values[0].get("salesforce_parent_id"))
        if not _is_lead_id(parent_id):
            continue
        prepared.append({
            "id": f"sf-feed-{feed_item_id}",
            "date": _text(values[0].get("date")),
            "texte": "Publication Salesforce non présente dans l’export",
            "author": "Salesforce",
            "author_email": "salesforce://system",
            "comments": copy.deepcopy(values),
            "source": _SOURCE,
            "salesforce_feed_item_id": feed_item_id,
            "salesforce_parent_id": parent_id,
            "salesforce_type": "MissingFeedItem",
            "salesforce_comment_count": len(values),
        })
        stats["publication_placeholders"] += 1
        type_counts["MissingFeedItem"] += 1

    prepared.sort(
        key=lambda item: (_text(item.get("date")), _text(item.get("id"))),
        reverse=True,
    )
    stats["prepared_publications"] = len(prepared)
    stats["publication_type_counts"] = dict(type_counts.most_common())
    return prepared, dict(stats)


def _contact_name(contact: dict[str, Any]) -> str:
    return " ".join(
        part
        for part in (
            _text(contact.get("prenom")),
            _text(contact.get("nom")),
        )
        if part
    )


def _contact_salesforce_keys(contact: dict[str, Any]) -> set[str]:
    values: list[Any] = [
        contact.get("salesforce_id"),
        contact.get("salesforce_lead_id"),
    ]
    salesforce_ids = contact.get("salesforce_ids")
    if isinstance(salesforce_ids, (list, tuple, set)):
        values.extend(salesforce_ids)
    elif salesforce_ids:
        values.append(salesforce_ids)
    return {
        _salesforce_key(value)
        for value in values
        if _salesforce_key(value)
    }


def _contact_index(
    contacts: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for contact in contacts:
        for key in _contact_salesforce_keys(contact):
            if all(existing is not contact for existing in index[key]):
                index[key].append(contact)
    return index


def _existing_publication_index(
    contact: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for publication in contact.get("salesforce_chatter") or []:
        if not isinstance(publication, dict):
            continue
        identifier = _text(publication.get("salesforce_feed_item_id"))
        if identifier:
            result[identifier] = publication
    return result


def _comment_signature(comment: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _text(comment.get("date")),
        _text(comment.get("texte")),
        _text(comment.get("author")),
        _text(comment.get("salesforce_created_by_id")),
        _text(comment.get("salesforce_last_edit_date")),
    )


def _publication_signature(publication: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _text(publication.get("date")),
        _text(publication.get("texte")),
        _text(publication.get("author")),
        _text(publication.get("salesforce_type")),
        _text(publication.get("salesforce_title")),
        _text(publication.get("salesforce_link_url")),
        _text(publication.get("salesforce_related_record_id")),
        _integer(publication.get("salesforce_comment_count")),
        _integer(publication.get("salesforce_like_count")),
        tuple(
            (
                _text(comment.get("salesforce_feed_comment_id")),
                _comment_signature(comment),
            )
            for comment in (publication.get("comments") or [])
            if isinstance(comment, dict)
            and _text(comment.get("salesforce_feed_comment_id"))
        ),
    )


def _merge_comments(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> tuple[int, int, int]:
    existing_comments = [
        comment
        for comment in (existing.get("comments") or [])
        if isinstance(comment, dict)
    ]
    imported_by_id = {
        _text(comment.get("salesforce_feed_comment_id")): comment
        for comment in existing_comments
        if _text(comment.get("salesforce_feed_comment_id"))
    }
    created = updated = unchanged = 0

    for incoming_comment in incoming.get("comments") or []:
        identifier = _text(incoming_comment.get("salesforce_feed_comment_id"))
        current = imported_by_id.get(identifier)
        if current is None:
            existing_comments.append(copy.deepcopy(incoming_comment))
            imported_by_id[identifier] = existing_comments[-1]
            created += 1
            continue
        if _comment_signature(current) == _comment_signature(incoming_comment):
            unchanged += 1
            continue
        preserved = {
            key: value
            for key, value in current.items()
            if key not in incoming_comment
        }
        current.clear()
        current.update(preserved)
        current.update(copy.deepcopy(incoming_comment))
        updated += 1

    existing_comments.sort(
        key=lambda item: (_text(item.get("date")), _text(item.get("id"))),
    )
    existing["comments"] = existing_comments
    return created, updated, unchanged


def _merge_publication(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> tuple[bool, int, int, int]:
    before = _publication_signature(existing)
    comment_created, comment_updated, comment_unchanged = _merge_comments(
        existing,
        incoming,
    )
    comments = existing.get("comments") or []
    preserved = {
        key: value
        for key, value in existing.items()
        if key not in incoming and key != "comments"
    }
    existing.clear()
    existing.update(preserved)
    existing.update({
        key: copy.deepcopy(value)
        for key, value in incoming.items()
        if key != "comments"
    })
    existing["comments"] = comments
    changed = before != _publication_signature(existing)
    return changed, comment_created, comment_updated, comment_unchanged


def import_salesforce_chatter_rows(
    contacts: list[dict[str, Any]],
    publication_rows: list[dict[str, Any]],
    comment_rows: list[dict[str, Any]],
    user_rows: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    if dry_run:
        contacts = copy.deepcopy(contacts)

    now = dt.datetime.now(_PARIS_TZ).isoformat()
    batch_id = hashlib.sha256(now.encode()).hexdigest()[:16]
    authors = _author_map(user_rows)
    comments_by_feed_item, comment_stats = _prepare_comments(
        comment_rows,
        authors,
    )
    publications, publication_stats = _prepare_publications(
        publication_rows,
        authors,
        comments_by_feed_item,
    )
    contacts_by_salesforce = _contact_index(contacts)

    counts: Counter[str] = Counter()
    contact_counts: dict[str, Counter[str]] = defaultdict(Counter)
    ready_rows: dict[str, dict[str, Any]] = {}
    unmatched: dict[str, dict[str, Any]] = {}
    ambiguous: dict[str, dict[str, Any]] = {}

    for incoming in publications:
        parent_key = _salesforce_key(incoming.get("salesforce_parent_id"))
        candidates = contacts_by_salesforce.get(parent_key, [])
        if not candidates:
            counts["unmatched_publications"] += 1
            sample = unmatched.setdefault(parent_key, {
                "salesforce_parent_id": _text(incoming.get("salesforce_parent_id")),
                "publication_count": 0,
                "comment_count": 0,
            })
            sample["publication_count"] += 1
            sample["comment_count"] += len(incoming.get("comments") or [])
            continue
        if len(candidates) > 1:
            counts["ambiguous_publications"] += 1
            sample = ambiguous.setdefault(parent_key, {
                "salesforce_parent_id": _text(incoming.get("salesforce_parent_id")),
                "crm_contacts": [_contact_name(contact) for contact in candidates],
                "publication_count": 0,
                "comment_count": 0,
            })
            sample["publication_count"] += 1
            sample["comment_count"] += len(incoming.get("comments") or [])
            continue

        contact = candidates[0]
        contact_id = _text(contact.get("id"))
        existing_index = _existing_publication_index(contact)
        current = existing_index.get(incoming["salesforce_feed_item_id"])
        publication_changed = False
        comments_created = comments_updated = comments_unchanged = 0

        if current is None:
            contact.setdefault("salesforce_chatter", []).append(copy.deepcopy(incoming))
            counts["publications_created"] += 1
            comments_created = len(incoming.get("comments") or [])
            counts["comments_created"] += comments_created
            publication_changed = True
        else:
            (
                publication_changed,
                comments_created,
                comments_updated,
                comments_unchanged,
            ) = _merge_publication(current, incoming)
            counts["comments_created"] += comments_created
            counts["comments_updated"] += comments_updated
            counts["comments_unchanged"] += comments_unchanged
            if publication_changed:
                counts["publications_updated"] += 1
            else:
                counts["publications_unchanged"] += 1

        contact_counts[contact_id]["publications"] += 1
        contact_counts[contact_id]["comments"] += len(incoming.get("comments") or [])
        if publication_changed or comments_created or comments_updated:
            contact_counts[contact_id]["changed"] += 1
            contact["salesforce_chatter_import_batch_id"] = batch_id
            contact["salesforce_chatter_imported_at"] = now
            contact["updated_at"] = now

        ready_rows.setdefault(contact_id, {
            "contact_id": contact_id,
            "person": _contact_name(contact) or "Fiche sans nom",
            "formation": _text(contact.get("formation")),
            "salesforce_ids": sorted(_contact_salesforce_keys(contact)),
            "publications": 0,
            "comments": 0,
        })
        ready_rows[contact_id]["publications"] += 1
        ready_rows[contact_id]["comments"] += len(incoming.get("comments") or [])

    changed_contact_ids = {
        contact_id
        for contact_id, values in contact_counts.items()
        if values["changed"]
    }
    for contact in contacts:
        contact_id = _text(contact.get("id"))
        if contact_id not in contact_counts:
            continue
        chatter = [
            item
            for item in (contact.get("salesforce_chatter") or [])
            if isinstance(item, dict)
        ]
        chatter.sort(
            key=lambda item: (_text(item.get("date")), _text(item.get("id"))),
            reverse=True,
        )
        contact["salesforce_chatter"] = chatter
        contact["salesforce_chatter_count"] = len(chatter)
        contact["salesforce_chatter_comment_count"] = sum(
            len(item.get("comments") or [])
            for item in chatter
        )

    counts["matched_publications"] = sum(
        values["publications"]
        for values in contact_counts.values()
    )
    counts["matched_comments"] = sum(
        values["comments"]
        for values in contact_counts.values()
    )
    counts["matched_contacts"] = len(contact_counts)
    counts["contacts_updated"] = len(changed_contact_ids)

    return {
        "ok": True,
        "dry_run": dry_run,
        "batch_id": batch_id,
        "user_rows": len(user_rows),
        "authors_resolved": len(authors),
        "publication_csv_rows": len(publication_rows),
        "comment_csv_rows": len(comment_rows),
        "matched_contacts": counts["matched_contacts"],
        "contacts_updated": counts["contacts_updated"],
        "matched_publications": counts["matched_publications"],
        "matched_comments": counts["matched_comments"],
        "publications_created": counts["publications_created"],
        "publications_updated": counts["publications_updated"],
        "publications_unchanged": counts["publications_unchanged"],
        "comments_created": counts["comments_created"],
        "comments_updated": counts["comments_updated"],
        "comments_unchanged": counts["comments_unchanged"],
        "unmatched_publications": counts["unmatched_publications"],
        "ambiguous_publications": counts["ambiguous_publications"],
        "unmatched_parent_count": len(unmatched),
        "ambiguous_parent_count": len(ambiguous),
        "ready_rows": sorted(
            ready_rows.values(),
            key=lambda item: (-int(item.get("publications") or 0), item.get("person") or ""),
        ),
        "unmatched_rows": sorted(
            unmatched.values(),
            key=lambda item: -int(item.get("publication_count") or 0),
        ),
        "ambiguous_rows": sorted(
            ambiguous.values(),
            key=lambda item: -int(item.get("publication_count") or 0),
        ),
        **comment_stats,
        **publication_stats,
    }


def _contacts_signature(contacts: list[dict[str, Any]]) -> str:
    payload: list[Any] = []
    for contact in sorted(contacts, key=lambda item: _text(item.get("id"))):
        salesforce_keys = sorted(_contact_salesforce_keys(contact))
        if not salesforce_keys:
            continue
        chatter = []
        for item in contact.get("salesforce_chatter") or []:
            if not isinstance(item, dict):
                continue
            chatter.append((
                _text(item.get("salesforce_feed_item_id")),
                _text(item.get("date")),
                _text(item.get("texte")),
                tuple(sorted(
                    _text(comment.get("salesforce_feed_comment_id"))
                    for comment in (item.get("comments") or [])
                    if isinstance(comment, dict)
                    and _text(comment.get("salesforce_feed_comment_id"))
                )),
            ))
        payload.append((
            _text(contact.get("id")),
            _text(contact.get("updated_at")),
            salesforce_keys,
            sorted(chatter),
        ))
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _preview_token(
    publication_raw: bytes,
    comment_raw: bytes,
    user_raw: bytes,
    contacts: list[dict[str, Any]],
) -> str:
    digest = hashlib.sha256()
    for raw in (publication_raw, comment_raw, user_raw):
        digest.update(hashlib.sha256(raw).digest())
    digest.update(_contacts_signature(contacts).encode())
    return digest.hexdigest()


def register_salesforce_chatter_import(
    app,
    *,
    current_user_fn,
    load_data_fn,
    login_required_fn,
    save_data_fn,
    transaction_lock=None,
) -> None:
    """Enregistre l'API d'aperçu et d'import de l'historique Chatter."""
    endpoint = "crm_import_salesforce_chatter"
    if endpoint in app.view_functions:
        return
    from flask import jsonify, request

    shared_lock = transaction_lock or _IMPORT_LOCK

    @app.post("/api/crm/import-salesforce-chatter", endpoint=endpoint)
    @login_required_fn
    def crm_import_salesforce_chatter():
        if (current_user_fn() or {}).get("role") != "admin":
            return jsonify({
                "error": "Seul un administrateur peut importer l’historique Salesforce."
            }), 403

        publication_upload = request.files.get("publications_file")
        comment_upload = request.files.get("comments_file")
        user_upload = request.files.get("users_file")
        if not publication_upload or not publication_upload.filename:
            return jsonify({"error": "Sélectionnez le fichier FeedItem des publications."}), 400
        if not comment_upload or not comment_upload.filename:
            return jsonify({"error": "Sélectionnez le fichier FeedComment des commentaires."}), 400
        if not user_upload or not user_upload.filename:
            return jsonify({"error": "Sélectionnez le fichier User des utilisateurs."}), 400

        dry_run = _text(request.form.get("dry_run", "0")) == "1"
        supplied_token = _text(request.form.get("preview_token"))

        try:
            publication_raw = publication_upload.read(MAX_PUBLICATIONS_BYTES + 1)
            comment_raw = comment_upload.read(MAX_COMMENTS_BYTES + 1)
            user_raw = user_upload.read(MAX_USERS_BYTES + 1)
            publication_rows = parse_salesforce_publications_csv(publication_raw)
            comment_rows = parse_salesforce_comments_csv(comment_raw)
            user_rows = parse_salesforce_users_csv(user_raw)

            if dry_run:
                data = load_data_fn()
                contacts = data.setdefault("crm_contacts", [])
                token = _preview_token(publication_raw, comment_raw, user_raw, contacts)
                result = import_salesforce_chatter_rows(
                    contacts,
                    publication_rows,
                    comment_rows,
                    user_rows,
                    dry_run=True,
                )
            else:
                with shared_lock:
                    data = load_data_fn()
                    contacts = data.setdefault("crm_contacts", [])
                    token = _preview_token(publication_raw, comment_raw, user_raw, contacts)
                    if not supplied_token:
                        return jsonify({
                            "error": "Un aperçu doit être validé avant l’import de l’historique."
                        }), 409
                    if supplied_token != token:
                        return jsonify({
                            "error": (
                                "Les fichiers ou les fiches CRM ont changé depuis "
                                "l’aperçu. Relancez l’analyse."
                            )
                        }), 409

                    result = import_salesforce_chatter_rows(
                        contacts,
                        publication_rows,
                        comment_rows,
                        user_rows,
                        dry_run=False,
                    )
                    summary = {
                        "date": dt.datetime.now(_PARIS_TZ).isoformat(),
                        "batch_id": result.get("batch_id"),
                        "publications_filename": publication_upload.filename,
                        "comments_filename": comment_upload.filename,
                        "users_filename": user_upload.filename,
                        **{
                            key: result.get(key, 0)
                            for key in (
                                "publication_csv_rows", "comment_csv_rows", "user_rows",
                                "matched_contacts", "contacts_updated",
                                "publications_created", "publications_updated",
                                "comments_created", "comments_updated",
                                "unmatched_parent_count", "ambiguous_parent_count",
                            )
                        },
                    }
                    data["crm_salesforce_chatter_last_import"] = summary
                    history = data.setdefault("crm_salesforce_chatter_import_history", [])
                    history.insert(0, summary)
                    del history[20:]
                    save_data_fn(data)

            result.update({
                "preview_token": token,
                "publications_filename": publication_upload.filename,
                "comments_filename": comment_upload.filename,
                "users_filename": user_upload.filename,
            })
            return jsonify(result)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # pragma: no cover - journal de production
            app.logger.exception("Erreur import historique Chatter Salesforce")
            return jsonify({
                "error": f"L’import de l’historique Salesforce a échoué : {exc}"
            }), 500
