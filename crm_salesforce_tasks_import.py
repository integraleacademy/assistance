"""Import sécurisé des tâches Salesforce comme relances du CRM.

L'import est volontairement séparé de la migration des pistes : il ne crée
jamais de contact. Il rattache uniquement les tâches ouvertes à des fiches CRM
déjà reliées à Salesforce, avec un aperçu obligatoire et un contrôle
idempotent fondé sur l'identifiant de l'activité Salesforce.
"""

from __future__ import annotations

import codecs
import copy
import csv
import datetime as dt
import hashlib
import io
import json
import re
import threading
import unicodedata
import uuid
from collections import Counter, defaultdict
from typing import Any, Iterable

import pytz


MAX_CSV_BYTES = 20 * 1024 * 1024
_PARIS_TZ = pytz.timezone("Europe/Paris")
_IMPORT_LOCK = threading.Lock()

_HEADER_ALIASES = {
    # Rapport Lightning français transmis.
    "date": "DueDate",
    "societe compte": "Company",
    "opportunite": "OpportunityName",
    "contact": "ContactName",
    "piste": "LeadName",
    "objet": "Subject",
    "attribue": "OwnerName",
    "priorite": "Priority",
    "statut": "Status",
    "tache": "IsTask",
    "debut": "StartDateTime",
    "echeance heures": "DueDateTime",
    "date heure de realisation": "CompletedAt",
    "fin": "EndDateTime",
    "type d activite": "ActivityType",
    "type d appel": "CallType",
    "id du compte": "AccountId",
    "id du compte principal": "ParentAccountId",
    "commentaires": "Comments",
    "telephone": "Phone",
    "telephone mobile": "MobilePhone",
    "adresse e mail": "Email",
    "id de l activite": "ActivityId",
    "date de creation": "CreatedDate",
    "origine de la piste de l opportunite": "OpportunityLeadSource",
    # Variantes anglaises et noms API courants.
    "activity id": "ActivityId",
    "task id": "ActivityId",
    "id": "ActivityId",
    "due date": "DueDate",
    "activity date": "DueDate",
    "due date time": "DueDateTime",
    "start": "StartDateTime",
    "start date": "StartDateTime",
    "end": "EndDateTime",
    "subject": "Subject",
    "assigned to": "OwnerName",
    "owner": "OwnerName",
    "priority": "Priority",
    "status": "Status",
    "is task": "IsTask",
    "task": "IsTask",
    "completed date": "CompletedAt",
    "completed at": "CompletedAt",
    "lead": "LeadName",
    "lead name": "LeadName",
    "contact name": "ContactName",
    "opportunity": "OpportunityName",
    "account": "Company",
    "comments": "Comments",
    "description": "Comments",
    "phone": "Phone",
    "mobile phone": "MobilePhone",
    "email": "Email",
    "created date": "CreatedDate",
}
_KNOWN_HEADERS = set(_HEADER_ALIASES.values())
_CLOSED_STATUSES = {
    "closed",
    "completed",
    "complete",
    "done",
    "termine",
    "ferme",
    "cloture",
}
_BTS_SHORT_LABELS = ("mos", "mco", "ndrc", "ci", "pi", "cg")
_CAP_SHORT_LABELS = ("aepe", "boulangerie", "coiffure", "cuisine", "patisserie")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _fold(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", _text(value))
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Mn"
    ).casefold()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _truthy(value: Any) -> bool:
    return _fold(value) in {"1", "true", "yes", "oui", "y", "o", "vrai"}


def _email(value: Any) -> str:
    candidate = _text(value).casefold()
    return candidate if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", candidate) else ""


def _phone(value: Any) -> str:
    digits = re.sub(r"\D", "", _text(value))
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("330") and len(digits) == 12:
        digits = f"33{digits[3:]}"
    if len(digits) == 10 and digits.startswith("0"):
        digits = f"33{digits[1:]}"
    return digits if 8 <= len(digits) <= 15 else ""


def _decode_csv(raw: bytes) -> str:
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return raw.decode("utf-32")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    if raw.count(b"\x00") > max(4, len(raw) // 20):
        odd_nulls = raw[1::2].count(0)
        even_nulls = raw[0::2].count(0)
        candidates = (
            ("utf-16-le", "utf-16-be")
            if odd_nulls >= even_nulls
            else ("utf-16-be", "utf-16-le")
        )
        for encoding in candidates:
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
    raw = _text(value).lstrip("\ufeff")
    folded = _fold(raw)
    if folded in _HEADER_ALIASES:
        return _HEADER_ALIASES[folded]
    for prefix in ("tache ", "task ", "activite ", "activity "):
        if folded.startswith(prefix):
            reduced = folded[len(prefix):].strip()
            if reduced in _HEADER_ALIASES:
                return _HEADER_ALIASES[reduced]
    return raw


def _find_header(text: str) -> tuple[list[str], int, str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and re.fullmatch(r"\s*sep\s*=\s*[,;\t|]\s*", lines[0], flags=re.I):
        lines = lines[1:]
    best: tuple[int, int, str] | None = None
    for delimiter in (";", ",", "\t", "|"):
        for index, line in enumerate(lines[:25]):
            try:
                cells = next(csv.reader([line], delimiter=delimiter))
            except csv.Error:
                continue
            canonical = [_canonical_header(cell) for cell in cells]
            known = sum(item in _KNOWN_HEADERS for item in canonical)
            score = known
            score += 30 if "ActivityId" in canonical else 0
            score += 20 if "IsTask" in canonical else 0
            score += 15 if any(item in canonical for item in ("DueDate", "DueDateTime")) else 0
            score += 10 if "Subject" in canonical else 0
            candidate = (score, index, delimiter)
            if best is None or candidate[0] > best[0]:
                best = candidate
    if best is None or best[0] < 40:
        preview = " | ".join(line[:180] for line in lines[:3] if line.strip())
        raise ValueError(
            "Impossible d’identifier les colonnes du rapport de tâches Salesforce. "
            f"Début du fichier : {preview or 'aucun contenu lisible'}"
        )
    return lines, best[1], best[2]


def parse_salesforce_tasks_csv(raw: bytes) -> list[dict[str, str]]:
    if not raw:
        raise ValueError("Le fichier CSV des relances est vide.")
    if len(raw) > MAX_CSV_BYTES:
        raise ValueError("Le fichier dépasse la limite de 20 Mo.")
    decoded = _decode_csv(raw).replace("\x00", "")
    lines, header_index, delimiter = _find_header(decoded)
    reader = csv.DictReader(
        io.StringIO("\n".join(lines[header_index:]), newline=""),
        delimiter=delimiter,
    )
    original_headers = reader.fieldnames or []
    names = {
        header: _canonical_header(header)
        for header in original_headers
        if header is not None
    }
    canonical_headers = set(names.values())
    missing = [
        label
        for label in ("ActivityId", "IsTask")
        if label not in canonical_headers
    ]
    if not any(label in canonical_headers for label in ("DueDate", "DueDateTime", "StartDateTime")):
        missing.append("DueDate")
    if missing:
        raise ValueError(
            "Colonnes indispensables manquantes dans le rapport Salesforce : "
            + ", ".join(missing)
            + "."
        )
    rows: list[dict[str, str]] = []
    for source in reader:
        if not isinstance(source, dict):
            continue
        row: dict[str, str] = {}
        for key, value in source.items():
            if key is None:
                continue
            canonical = names.get(key, _canonical_header(key))
            clean = _text(value)
            if clean or canonical not in row:
                row[canonical] = clean
        if any(_text(value) for value in row.values()):
            rows.append(row)
    if not rows:
        raise ValueError("Le rapport Salesforce ne contient aucune activité exploitable.")
    return rows


def _parse_datetime(value: Any) -> dt.datetime | None:
    raw = _text(value)
    if not raw:
        return None
    candidate = raw.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError:
        parsed = None
    if parsed is None:
        for date_format in (
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y %H:%M",
            "%d/%m/%Y",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%m/%d/%Y %I:%M %p",
            "%m/%d/%Y",
        ):
            try:
                parsed = dt.datetime.strptime(raw, date_format)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = _PARIS_TZ.localize(parsed)
    return parsed.astimezone(_PARIS_TZ)


def _iso_datetime(value: Any) -> str:
    parsed = _parse_datetime(value)
    return parsed.isoformat() if parsed else ""


def _scheduled_date(row: dict[str, Any]) -> str:
    for field in ("DueDateTime", "DueDate", "StartDateTime"):
        parsed = _parse_datetime(row.get(field))
        if parsed is not None:
            return parsed.date().isoformat()
    return ""


def _relation_name(row: dict[str, Any]) -> str:
    return (
        _text(row.get("LeadName"))
        or _text(row.get("ContactName"))
        or _text(row.get("OpportunityName"))
    )


def _relation_type(row: dict[str, Any]) -> str:
    if _text(row.get("LeadName")):
        return "lead"
    if _text(row.get("ContactName")):
        return "contact"
    if _text(row.get("OpportunityName")):
        return "opportunity"
    return "unknown"


def _is_task(row: dict[str, Any]) -> bool:
    activity_id = _text(row.get("ActivityId"))
    return _truthy(row.get("IsTask")) or activity_id.startswith("00T")


def _is_closed_task(row: dict[str, Any]) -> bool:
    return bool(_text(row.get("CompletedAt"))) or _fold(row.get("Status")) in _CLOSED_STATUSES


def _map_task(row: dict[str, Any]) -> dict[str, Any]:
    phones = []
    for source in (row.get("MobilePhone"), row.get("Phone")):
        normalized = _phone(source)
        if normalized and normalized not in phones:
            phones.append(normalized)
    return {
        "salesforce_task_id": _text(row.get("ActivityId")),
        "scheduled_date": _scheduled_date(row),
        "subject": _text(row.get("Subject")) or "Relance Salesforce",
        "owner": _text(row.get("OwnerName")) or "Salesforce",
        "priority": _text(row.get("Priority")),
        "salesforce_status": _text(row.get("Status")),
        "comments": _text(row.get("Comments")),
        "email": _email(row.get("Email")),
        "phones": phones,
        "relation_name": _relation_name(row),
        "relation_type": _relation_type(row),
        "company": _text(row.get("Company")),
        "salesforce_created_at": _iso_datetime(row.get("CreatedDate")),
        "source_row": row,
    }


def _task_payload_signature(task: dict[str, Any]) -> tuple[Any, ...]:
    return (
        task.get("scheduled_date"),
        task.get("subject"),
        task.get("owner"),
        task.get("priority"),
        task.get("salesforce_status"),
        task.get("comments"),
        task.get("email"),
        tuple(task.get("phones") or []),
        _fold(task.get("relation_name")),
    )


def _prepare_task_rows(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    stats: Counter[str] = Counter()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not _is_task(row):
            stats["skipped_events"] += 1
            continue
        stats["task_rows"] += 1
        if _is_closed_task(row):
            stats["skipped_closed"] += 1
            continue
        task = _map_task(row)
        if not task["salesforce_task_id"]:
            stats["skipped_missing_activity_id"] += 1
            continue
        if not task["scheduled_date"]:
            stats["skipped_missing_date"] += 1
            continue
        grouped[task["salesforce_task_id"]].append(task)

    prepared: list[dict[str, Any]] = []
    for task_group in grouped.values():
        if len(task_group) == 1:
            prepared.append(task_group[0])
            continue
        signatures = {_task_payload_signature(task) for task in task_group}
        if len(signatures) == 1:
            prepared.append(task_group[0])
            stats["duplicates_in_file"] += len(task_group) - 1
            continue
        conflicted = dict(task_group[0])
        conflicted["source_conflict"] = (
            "Le même identifiant d’activité Salesforce apparaît avec des données différentes."
        )
        prepared.append(conflicted)
        stats["duplicate_conflicts_in_file"] += len(task_group) - 1
    return prepared, dict(stats)


def _append(index: dict[str, list[Any]], key: str, value: Any) -> None:
    if not key:
        return
    bucket = index.setdefault(key, [])
    if all(candidate is not value for candidate in bucket):
        bucket.append(value)


def _contact_phones(contact: dict[str, Any]) -> set[str]:
    values = {
        _phone(contact.get(field))
        for field in (
            "telephone",
            "phone",
            "mobile",
            "mobile_phone",
            "telephone_mobile",
        )
    }
    values.discard("")
    return values


def _contact_is_salesforce_linked(contact: dict[str, Any]) -> bool:
    return bool(
        _text(contact.get("salesforce_id"))
        or any(_text(value) for value in (contact.get("salesforce_ids") or []))
    )


def _contact_is_excluded(contact: dict[str, Any]) -> str:
    if _fold(contact.get("statut")) == "disqualifie":
        return "La fiche CRM est disqualifiée."
    if _text(contact.get("archived_at")):
        return "La fiche CRM est archivée."
    values = (
        contact.get("formation"),
        contact.get("salesforce_company"),
        contact.get("company"),
    )
    folded_values = [_fold(value) for value in values if _text(value)]
    if "test aps" in folded_values:
        return "La fiche TEST APS est exclue."
    for folded in folded_values:
        if re.search(r"(?:^|\s)(?:bts|cap)(?:\s|$)", folded):
            return "La formation BTS/CAP est exclue."
        without_year = re.sub(r"(?:^|\s)20\d{2}(?:\s|$)", " ", folded)
        without_year = re.sub(r"\s+", " ", without_year).strip()
        if any(
            without_year == label or without_year.startswith(f"{label} ")
            for label in (*_BTS_SHORT_LABELS, *_CAP_SHORT_LABELS)
        ):
            return "La formation BTS/CAP est exclue."
    return ""


def _contact_name(contact: dict[str, Any]) -> str:
    return _fold(f"{contact.get('prenom', '')} {contact.get('nom', '')}")


def _names_compatible(contact: dict[str, Any], relation_name: str) -> bool:
    task_name = _fold(relation_name)
    if not task_name:
        return True
    full_name = _contact_name(contact)
    if not full_name or full_name == task_name:
        return True
    surname = _fold(contact.get("nom"))
    if surname and (task_name == surname or task_name.endswith(f" {surname}")):
        return True
    return False


def _unique_contacts(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items:
        if all(candidate is not item for candidate in result):
            result.append(item)
    return result


def _indexes(contacts: list[dict[str, Any]]):
    by_email: dict[str, list[dict[str, Any]]] = {}
    by_phone: dict[str, list[dict[str, Any]]] = {}
    by_task_id: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for contact in contacts:
        _append(by_email, _email(contact.get("mail") or contact.get("email")), contact)
        for phone in _contact_phones(contact):
            _append(by_phone, phone, contact)
        for relance in contact.get("relances") or []:
            if not isinstance(relance, dict):
                continue
            task_id = _text(relance.get("salesforce_task_id"))
            if task_id:
                by_task_id.setdefault(task_id, []).append((contact, relance))
    return by_email, by_phone, by_task_id


def _match_task(
    task: dict[str, Any],
    by_email: dict[str, list[dict[str, Any]]],
    by_phone: dict[str, list[dict[str, Any]]],
    by_task_id: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str, str, bool]:
    """Retourne contact, relance existante, méthode, raison, avertissement nom."""
    existing = by_task_id.get(task["salesforce_task_id"], [])
    if len(existing) > 1:
        return None, None, "", (
            "L’identifiant de tâche Salesforce existe sur plusieurs fiches CRM."
        ), False
    existing_contact = existing[0][0] if existing else None
    existing_relance = existing[0][1] if existing else None

    email_candidates = _unique_contacts(
        by_email.get(task.get("email") or "", [])
    ) if task.get("email") else []
    phone_candidates = _unique_contacts(
        candidate
        for phone in task.get("phones") or []
        for candidate in by_phone.get(phone, [])
    )
    if len(email_candidates) > 1:
        return None, None, "", "Plusieurs fiches CRM utilisent cette adresse e-mail.", False
    if len(phone_candidates) > 1:
        return None, None, "", "Plusieurs fiches CRM utilisent ce téléphone.", False

    coordinate_contact = None
    method = ""
    if email_candidates and phone_candidates:
        if email_candidates[0] is not phone_candidates[0]:
            return None, None, "", (
                "L’e-mail et le téléphone correspondent à deux fiches CRM différentes."
            ), False
        coordinate_contact = email_candidates[0]
        method = "email+phone"
    elif email_candidates:
        coordinate_contact = email_candidates[0]
        method = "email"
    elif phone_candidates:
        coordinate_contact = phone_candidates[0]
        method = "phone"

    if existing_contact is not None:
        if coordinate_contact is not None and coordinate_contact is not existing_contact:
            return None, None, "", (
                "La tâche existe déjà, mais ses coordonnées désignent une autre fiche CRM."
            ), False
        warning = not _names_compatible(existing_contact, task.get("relation_name") or "")
        return existing_contact, existing_relance, "salesforce_task_id", "", warning

    if coordinate_contact is None:
        return None, None, "", "Aucune fiche CRM ne correspond aux coordonnées.", False

    names_compatible = _names_compatible(
        coordinate_contact,
        task.get("relation_name") or "",
    )
    # Deux coordonnées concordantes sont suffisamment fortes ; un écart de nom
    # reste visible dans l’aperçu. Avec une seule coordonnée, on bloque.
    if not names_compatible and method != "email+phone":
        return None, None, "", (
            "La coordonnée correspond, mais le nom de la personne est différent."
        ), False
    return coordinate_contact, None, method, "", not names_compatible


def _relance_payload(task: dict[str, Any], *, now: str) -> dict[str, Any]:
    detail_parts = [
        task.get("subject") or "Relance Salesforce",
        f"Attribuée à {task.get('owner')}" if task.get("owner") else "",
        f"Priorité {task.get('priority')}" if task.get("priority") else "",
    ]
    detail = " · ".join(part for part in detail_parts if part)
    return {
        "id": str(uuid.uuid4()),
        "scheduled_date": task["scheduled_date"],
        "status": "scheduled",
        "created_at": task.get("salesforce_created_at") or now,
        "created_by": task.get("owner") or "Salesforce",
        "source": "salesforce_task_import",
        "salesforce_task_id": task["salesforce_task_id"],
        "salesforce_subject": task.get("subject") or "",
        "salesforce_owner": task.get("owner") or "",
        "salesforce_priority": task.get("priority") or "",
        "salesforce_task_status": task.get("salesforce_status") or "",
        "salesforce_comments": task.get("comments") or "",
        "salesforce_relation_name": task.get("relation_name") or "",
        "salesforce_relation_type": task.get("relation_type") or "",
        "salesforce_company": task.get("company") or "",
        "salesforce_imported_at": now,
        "title": task.get("subject") or "Relance Salesforce",
        "detail": detail,
    }


def _update_existing_relance(
    relance: dict[str, Any],
    task: dict[str, Any],
    *,
    now: str,
) -> tuple[bool, bool]:
    """Retourne (modifiée, statut CRM terminé préservé)."""
    if _text(relance.get("status")) not in {"", "scheduled"}:
        # Une action déjà répondue/annulée dans le CRM ne doit jamais être
        # rouverte par un ancien export Salesforce.
        metadata = {
            "salesforce_subject": task.get("subject") or "",
            "salesforce_owner": task.get("owner") or "",
            "salesforce_priority": task.get("priority") or "",
            "salesforce_task_status": task.get("salesforce_status") or "",
            "salesforce_comments": task.get("comments") or "",
        }
        changed = False
        for key, value in metadata.items():
            if value and relance.get(key) != value:
                relance[key] = value
                changed = True
        if changed:
            relance["salesforce_imported_at"] = now
        return changed, True

    incoming = _relance_payload(task, now=now)
    editable = (
        "scheduled_date",
        "salesforce_subject",
        "salesforce_owner",
        "salesforce_priority",
        "salesforce_task_status",
        "salesforce_comments",
        "salesforce_relation_name",
        "salesforce_relation_type",
        "salesforce_company",
        "title",
        "detail",
    )
    changed = False
    for key in editable:
        value = incoming.get(key)
        if relance.get(key) != value:
            relance[key] = value
            changed = True
    relance.setdefault("source", "salesforce_task_import")
    relance.setdefault("salesforce_task_id", task["salesforce_task_id"])
    relance.setdefault("status", "scheduled")
    if changed:
        relance["salesforce_imported_at"] = now
    return changed, False


def _refresh_relance_date(contact: dict[str, Any]) -> bool:
    dates = [
        _text(relance.get("scheduled_date"))
        for relance in contact.get("relances") or []
        if isinstance(relance, dict)
        and _text(relance.get("status") or "scheduled") == "scheduled"
        and _text(relance.get("scheduled_date"))
    ]
    next_date = min(dates) if dates else ""
    changed = _text(contact.get("relance_date")) != next_date
    contact["relance_date"] = next_date
    return changed


def _add_activity(
    contact: dict[str, Any],
    task: dict[str, Any],
    *,
    now: str,
    updated: bool,
) -> None:
    title = (
        "Relance Salesforce mise à jour"
        if updated
        else "Relance importée depuis Salesforce"
    )
    detail = (
        f"{task.get('subject') or 'Relance'} · échéance le "
        f"{task.get('scheduled_date')} · attribuée à "
        f"{task.get('owner') or 'Salesforce'}."
    )
    if task.get("comments"):
        detail += f" Commentaire : {task['comments']}"
    contact.setdefault("activities", []).insert(0, {
        "id": str(uuid.uuid4()),
        "kind": "import",
        "title": title,
        "detail": detail,
        "preview": "",
        "date": now,
        "author": "Import Salesforce",
    })


def import_salesforce_task_rows(
    contacts: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    if dry_run:
        contacts = copy.deepcopy(contacts)
    now = dt.datetime.now(_PARIS_TZ).isoformat()
    batch_id = str(uuid.uuid4())
    prepared, stats = _prepare_task_rows(rows)
    by_email, by_phone, by_task_id = _indexes(contacts)
    counts: Counter[str] = Counter()
    due_dates: Counter[str] = Counter()
    owners: Counter[str] = Counter()
    subjects: Counter[str] = Counter()
    match_methods: Counter[str] = Counter()
    matched_contact_ids: set[str] = set()
    promoted_contact_ids: set[str] = set()
    unmatched_samples: list[dict[str, Any]] = []
    ambiguous_samples: list[dict[str, Any]] = []
    warning_samples: list[dict[str, Any]] = []

    for task in prepared:
        due_dates[task["scheduled_date"]] += 1
        owners[task.get("owner") or "Non renseigné"] += 1
        subjects[task.get("subject") or "Non renseigné"] += 1
        if task.get("source_conflict"):
            counts["ambiguous"] += 1
            if len(ambiguous_samples) < 30:
                ambiguous_samples.append({
                    "activity_id": task["salesforce_task_id"],
                    "person": task.get("relation_name") or "Sans nom",
                    "date": task["scheduled_date"],
                    "reason": task["source_conflict"],
                })
            continue

        contact, existing_relance, method, reason, name_warning = _match_task(
            task,
            by_email,
            by_phone,
            by_task_id,
        )
        if reason:
            key = "unmatched" if reason.startswith("Aucune fiche") else "ambiguous"
            counts[key] += 1
            sample = {
                "activity_id": task["salesforce_task_id"],
                "person": task.get("relation_name") or "Sans nom",
                "email": task.get("email") or "",
                "phone": (task.get("phones") or [""])[0],
                "date": task["scheduled_date"],
                "reason": reason,
            }
            target = unmatched_samples if key == "unmatched" else ambiguous_samples
            if len(target) < 30:
                target.append(sample)
            continue
        assert contact is not None

        if not _contact_is_salesforce_linked(contact) and existing_relance is None:
            counts["skipped_not_salesforce_linked"] += 1
            if len(unmatched_samples) < 30:
                unmatched_samples.append({
                    "activity_id": task["salesforce_task_id"],
                    "person": task.get("relation_name") or "Sans nom",
                    "email": task.get("email") or "",
                    "phone": (task.get("phones") or [""])[0],
                    "date": task["scheduled_date"],
                    "reason": (
                        "La fiche existe dans le CRM, mais n’est pas encore reliée à une piste Salesforce. "
                        "Importez d’abord le fichier des pistes."
                    ),
                })
            continue

        excluded_reason = _contact_is_excluded(contact)
        if excluded_reason:
            counts["skipped_excluded_contact"] += 1
            if len(unmatched_samples) < 30:
                unmatched_samples.append({
                    "activity_id": task["salesforce_task_id"],
                    "person": task.get("relation_name") or "Sans nom",
                    "date": task["scheduled_date"],
                    "reason": excluded_reason,
                })
            continue

        contact_id = _text(contact.get("id"))
        matched_contact_ids.add(contact_id)
        match_methods[method] += 1
        if name_warning:
            counts["name_warnings"] += 1
            if len(warning_samples) < 20:
                warning_samples.append({
                    "activity_id": task["salesforce_task_id"],
                    "task_name": task.get("relation_name") or "",
                    "crm_name": " ".join(
                        part for part in (
                            _text(contact.get("prenom")),
                            _text(contact.get("nom")),
                        ) if part
                    ),
                    "date": task["scheduled_date"],
                    "match_method": method,
                })

        task_changed = False
        activity_updated = False
        if existing_relance is not None:
            changed, preserved = _update_existing_relance(
                existing_relance,
                task,
                now=now,
            )
            if preserved:
                counts["preserved_completed"] += 1
            if changed:
                counts["updated"] += 1
                task_changed = True
                activity_updated = True
            else:
                counts["unchanged"] += 1
        else:
            relance = _relance_payload(task, now=now)
            contact.setdefault("relances", []).append(relance)
            by_task_id.setdefault(task["salesforce_task_id"], []).append(
                (contact, relance)
            )
            counts["created"] += 1
            task_changed = True

        if _text(contact.get("statut")) in {"", "Nouveaux"}:
            contact["statut"] = "A relancer"
            promoted_contact_ids.add(contact_id)
            task_changed = True
        if _refresh_relance_date(contact):
            task_changed = True

        if task_changed:
            if not dry_run and (existing_relance is None or activity_updated):
                _add_activity(
                    contact,
                    task,
                    now=now,
                    updated=activity_updated,
                )
            contact["salesforce_tasks_import_batch_id"] = batch_id
            contact["salesforce_tasks_imported_at"] = now
            contact["updated_at"] = now

    result = {
        "ok": True,
        "dry_run": dry_run,
        "batch_id": batch_id,
        "csv_rows": len(rows),
        "prepared_tasks": len(prepared),
        "created": counts["created"],
        "updated": counts["updated"],
        "unchanged": counts["unchanged"],
        "preserved_completed": counts["preserved_completed"],
        "matched_contacts": len(matched_contact_ids),
        "promoted_to_followup": len(promoted_contact_ids),
        "unmatched": counts["unmatched"],
        "ambiguous": counts["ambiguous"],
        "name_warnings": counts["name_warnings"],
        "skipped_not_salesforce_linked": counts["skipped_not_salesforce_linked"],
        "skipped_excluded_contact": counts["skipped_excluded_contact"],
        "unmatched_samples": unmatched_samples,
        "ambiguous_samples": ambiguous_samples,
        "warning_samples": warning_samples,
        "due_date_counts": dict(sorted(due_dates.items())),
        "owner_counts": dict(owners.most_common()),
        "subject_counts": dict(subjects.most_common()),
        "match_method_counts": dict(match_methods.most_common()),
        **stats,
    }
    for key in (
        "task_rows",
        "skipped_events",
        "skipped_closed",
        "skipped_missing_activity_id",
        "skipped_missing_date",
        "duplicates_in_file",
        "duplicate_conflicts_in_file",
    ):
        result.setdefault(key, 0)
    return result


def _contacts_signature(contacts: list[dict[str, Any]]) -> str:
    payload = []
    for contact in sorted(
        contacts,
        key=lambda item: _text(item.get("id")),
    ):
        relances = sorted(
            (
                _text(relance.get("salesforce_task_id")),
                _text(relance.get("scheduled_date")),
                _text(relance.get("status")),
            )
            for relance in (contact.get("relances") or [])
            if isinstance(relance, dict)
            and _text(relance.get("salesforce_task_id"))
        )
        payload.append((
            _text(contact.get("id")),
            _text(contact.get("updated_at")),
            _email(contact.get("mail") or contact.get("email")),
            sorted(_contact_phones(contact)),
            _text(contact.get("salesforce_id")),
            sorted(_text(value) for value in (contact.get("salesforce_ids") or [])),
            _text(contact.get("statut")),
            relances,
        ))
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _preview_token(raw: bytes, contacts: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256(raw)
    digest.update(_contacts_signature(contacts).encode())
    return digest.hexdigest()


def register_salesforce_tasks_import(
    app,
    *,
    current_user_fn,
    load_data_fn,
    login_required_fn,
    save_data_fn,
    transaction_lock=None,
) -> None:
    """Enregistre l'API d'aperçu et d'import des relances Salesforce."""
    if "crm_import_salesforce_tasks" in app.view_functions:
        return
    from flask import jsonify, request

    shared_lock = transaction_lock or _IMPORT_LOCK

    @app.post(
        "/api/crm/import-salesforce-relances",
        endpoint="crm_import_salesforce_tasks",
    )
    @login_required_fn
    def crm_import_salesforce_tasks():
        if (current_user_fn() or {}).get("role") != "admin":
            return jsonify({
                "error": "Seul un administrateur peut importer les relances Salesforce."
            }), 403
        upload = request.files.get("file")
        if not upload or not upload.filename:
            return jsonify({
                "error": "Sélectionnez le fichier CSV des tâches Salesforce."
            }), 400
        dry_run = _text(request.form.get("dry_run", "0")) == "1"
        supplied_token = _text(request.form.get("preview_token"))
        try:
            raw = upload.read(MAX_CSV_BYTES + 1)
            rows = parse_salesforce_tasks_csv(raw)
            if dry_run:
                data = load_data_fn()
                contacts = data.setdefault("crm_contacts", [])
                token = _preview_token(raw, contacts)
                result = import_salesforce_task_rows(
                    contacts,
                    rows,
                    dry_run=True,
                )
            else:
                with shared_lock:
                    data = load_data_fn()
                    contacts = data.setdefault("crm_contacts", [])
                    token = _preview_token(raw, contacts)
                    if not supplied_token:
                        return jsonify({
                            "error": "Un aperçu doit être validé avant l’import des relances."
                        }), 409
                    if supplied_token != token:
                        return jsonify({
                            "error": (
                                "Le fichier ou le CRM a changé depuis l’aperçu. "
                                "Relancez l’analyse avant d’importer."
                            )
                        }), 409
                    result = import_salesforce_task_rows(
                        contacts,
                        rows,
                        dry_run=False,
                    )
                    summary = {
                        "date": dt.datetime.now(_PARIS_TZ).isoformat(),
                        "filename": upload.filename,
                        "batch_id": result.get("batch_id"),
                        **{
                            key: result.get(key, 0)
                            for key in (
                                "csv_rows",
                                "task_rows",
                                "prepared_tasks",
                                "created",
                                "updated",
                                "unchanged",
                                "matched_contacts",
                                "unmatched",
                                "ambiguous",
                                "skipped_events",
                            )
                        },
                    }
                    data["crm_salesforce_tasks_last_import"] = summary
                    history = data.setdefault(
                        "crm_salesforce_tasks_import_history",
                        [],
                    )
                    history.insert(0, summary)
                    del history[20:]
                    save_data_fn(data)
            result.update({
                "preview_token": token,
                "filename": upload.filename,
            })
            return jsonify(result)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # pragma: no cover - journal de production
            app.logger.exception("Erreur import des relances Salesforce")
            return jsonify({
                "error": f"L’import des relances Salesforce a échoué : {exc}"
            }), 500
