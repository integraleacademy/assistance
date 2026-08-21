"""Import contrôlé des anomalies de relances Salesforce avec formation CRM.

Le fichier est celui produit par l'aperçu complet des relances. Il ne crée
jamais de contact : seules les lignes portant une formation CRM et un
identifiant de fiche CRM sont susceptibles d'être appliquées. Chaque ligne
programme la relance Salesforce correspondante et place la fiche en
« A relancer » après un aperçu obligatoire.
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
from difflib import SequenceMatcher
from typing import Any, Iterable

import pytz


MAX_CSV_BYTES = 20 * 1024 * 1024
_PARIS_TZ = pytz.timezone("Europe/Paris")
_IMPORT_LOCK = threading.Lock()
_TARGET_STATUS = "A relancer"

_HEADER_ALIASES = {
    "categorie": "category",
    "action recommandee": "recommended_action",
    "importable apres verification": "importable_after_review",
    "personne": "person",
    "e mail": "email",
    "email": "email",
    "telephone": "phone",
    "date de relance": "scheduled_date",
    "objet": "subject",
    "attribue a": "owner",
    "priorite": "priority",
    "statut salesforce": "salesforce_status",
    "commentaires": "comments",
    "id activite salesforce": "salesforce_task_id",
    "type de relation": "relation_type",
    "societe compte": "company",
    "motif": "reason",
    "methode de rapprochement": "match_method",
    "id fiche crm": "contact_id",
    "nom fiche crm": "crm_name",
    "statut fiche crm": "crm_status",
    "formation fiche crm": "crm_formation",
}

_REQUIRED_HEADERS = {
    "person",
    "scheduled_date",
    "salesforce_task_id",
    "contact_id",
    "crm_formation",
}
_FINAL_RELANCE_STATUSES = {
    "answered",
    "no_answer",
    "reprogrammed",
    "cancelled",
    "completed",
}


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
    return _HEADER_ALIASES.get(_fold(raw), raw)


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
            canonical = {_canonical_header(cell) for cell in cells}
            score = len(canonical & set(_HEADER_ALIASES.values()))
            score += 30 if "contact_id" in canonical else 0
            score += 25 if "crm_formation" in canonical else 0
            score += 20 if "salesforce_task_id" in canonical else 0
            candidate = (score, index, delimiter)
            if best is None or candidate[0] > best[0]:
                best = candidate
    if best is None or best[0] < 60:
        preview = " | ".join(line[:180] for line in lines[:3] if line.strip())
        raise ValueError(
            "Impossible d’identifier les colonnes de la liste d’anomalies. "
            f"Début du fichier : {preview or 'aucun contenu lisible'}"
        )
    return lines, best[1], best[2]


def parse_anomaly_followups_csv(raw: bytes) -> list[dict[str, str]]:
    if not raw:
        raise ValueError("Le fichier des anomalies est vide.")
    if len(raw) > MAX_CSV_BYTES:
        raise ValueError("Le fichier dépasse la limite de 20 Mo.")

    decoded = _decode_csv(raw).replace("\x00", "")
    lines, header_index, delimiter = _find_header(decoded)
    reader = csv.DictReader(
        io.StringIO("\n".join(lines[header_index:]), newline=""),
        delimiter=delimiter,
    )
    original_headers = reader.fieldnames or []
    header_names = {
        header: _canonical_header(header)
        for header in original_headers
        if header is not None
    }
    canonical_headers = set(header_names.values())
    missing = sorted(_REQUIRED_HEADERS - canonical_headers)
    if missing:
        raise ValueError(
            "Colonnes indispensables manquantes : " + ", ".join(missing) + "."
        )

    rows: list[dict[str, str]] = []
    for source in reader:
        if not isinstance(source, dict):
            continue
        row: dict[str, str] = {}
        for key, value in source.items():
            if key is None:
                continue
            canonical = header_names.get(key, _canonical_header(key))
            clean = _text(value)
            if clean or canonical not in row:
                row[canonical] = clean
        if any(_text(value) for value in row.values()):
            rows.append(row)
    if not rows:
        raise ValueError("Le fichier ne contient aucune ligne exploitable.")
    return rows


def _parse_date(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    for date_format in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(raw[:10], date_format).date().isoformat()
        except ValueError:
            continue
    return ""


def _name_tokens(value: Any) -> list[str]:
    return [token for token in _fold(value).split() if token]


def _names_compatible(left: Any, right: Any) -> bool:
    left_folded = _fold(left)
    right_folded = _fold(right)
    if not left_folded or not right_folded:
        return True
    if left_folded == right_folded:
        return True
    left_tokens = _name_tokens(left)
    right_tokens = _name_tokens(right)
    if len(left_tokens) > 1 and sorted(left_tokens) == sorted(right_tokens):
        return True
    return SequenceMatcher(None, left_folded, right_folded).ratio() >= 0.78


def _contact_name(contact: dict[str, Any]) -> str:
    return " ".join(
        part
        for part in (
            _text(contact.get("prenom")),
            _text(contact.get("nom")),
        )
        if part
    )


def _contact_phone(contact: dict[str, Any]) -> str:
    for field in (
        "telephone",
        "phone",
        "mobile",
        "mobile_phone",
        "telephone_mobile",
    ):
        normalized = _phone(contact.get(field))
        if normalized:
            return normalized
    return ""


def _contact_email(contact: dict[str, Any]) -> str:
    return _email(contact.get("mail") or contact.get("email"))


def _row_signature(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("contact_id"),
        row.get("crm_formation"),
        row.get("scheduled_date"),
        row.get("person"),
        row.get("email"),
        row.get("phone"),
        row.get("subject"),
        row.get("owner"),
        row.get("comments"),
    )


def _prepare_rows(
    rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    stats: Counter[str] = Counter()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for source in rows:
        formation = _text(source.get("crm_formation"))
        if not formation:
            stats["skipped_without_formation"] += 1
            continue

        task_id = _text(source.get("salesforce_task_id"))
        contact_id = _text(source.get("contact_id"))
        scheduled_date = _parse_date(source.get("scheduled_date"))
        if not task_id:
            stats["skipped_missing_activity_id"] += 1
            continue
        if not contact_id:
            stats["skipped_missing_contact_id"] += 1
            continue
        if not scheduled_date:
            stats["skipped_missing_date"] += 1
            continue

        row = {
            "category": _text(source.get("category")),
            "recommended_action": _text(source.get("recommended_action")),
            "importable_after_review": _text(source.get("importable_after_review")),
            "person": _text(source.get("person")),
            "email": _email(source.get("email")),
            "phone": _phone(source.get("phone")),
            "scheduled_date": scheduled_date,
            "subject": _text(source.get("subject")) or "Relance Salesforce",
            "owner": _text(source.get("owner")) or "Salesforce",
            "priority": _text(source.get("priority")),
            "salesforce_status": _text(source.get("salesforce_status")),
            "comments": _text(source.get("comments")),
            "salesforce_task_id": task_id,
            "relation_type": _text(source.get("relation_type")),
            "company": _text(source.get("company")),
            "reason": _text(source.get("reason")),
            "match_method": _text(source.get("match_method")),
            "contact_id": contact_id,
            "crm_name": _text(source.get("crm_name")),
            "crm_status": _text(source.get("crm_status")),
            "crm_formation": formation,
        }
        grouped[task_id].append(row)

    prepared: list[dict[str, Any]] = []
    for task_id, group in grouped.items():
        if len(group) == 1:
            prepared.append(group[0])
            continue
        signatures = {_row_signature(row) for row in group}
        if len(signatures) == 1:
            prepared.append(group[0])
            stats["duplicates_in_file"] += len(group) - 1
        else:
            conflicted = dict(group[0])
            conflicted["source_conflict"] = (
                "Le même ID d’activité Salesforce apparaît avec des données différentes."
            )
            prepared.append(conflicted)
            stats["duplicate_conflicts_in_file"] += len(group) - 1

    stats["selected_with_formation"] = len(prepared)
    return prepared, dict(stats)


def _existing_relance(
    contact: dict[str, Any],
    task_id: str,
) -> dict[str, Any] | None:
    for relance in contact.get("relances") or []:
        if (
            isinstance(relance, dict)
            and _text(relance.get("salesforce_task_id")) == task_id
        ):
            return relance
    return None


def _relance_payload(row: dict[str, Any], *, now: str) -> dict[str, Any]:
    detail_parts = [
        row.get("subject") or "Relance Salesforce",
        f"Attribuée à {row.get('owner')}" if row.get("owner") else "",
        f"Priorité {row.get('priority')}" if row.get("priority") else "",
    ]
    return {
        "id": str(uuid.uuid4()),
        "scheduled_date": row["scheduled_date"],
        "status": "scheduled",
        "created_at": now,
        "created_by": row.get("owner") or "Salesforce",
        "source": "salesforce_anomaly_followup_import",
        "salesforce_task_id": row["salesforce_task_id"],
        "salesforce_subject": row.get("subject") or "",
        "salesforce_owner": row.get("owner") or "",
        "salesforce_priority": row.get("priority") or "",
        "salesforce_task_status": row.get("salesforce_status") or "",
        "salesforce_comments": row.get("comments") or "",
        "salesforce_relation_name": row.get("person") or "",
        "salesforce_relation_type": row.get("relation_type") or "",
        "salesforce_company": row.get("company") or "",
        "salesforce_imported_at": now,
        "title": row.get("subject") or "Relance Salesforce",
        "detail": " · ".join(part for part in detail_parts if part),
    }


def _update_existing_relance(
    relance: dict[str, Any],
    row: dict[str, Any],
    *,
    now: str,
) -> tuple[bool, bool]:
    status = _text(relance.get("status") or "scheduled")
    if status in _FINAL_RELANCE_STATUSES:
        return False, True

    incoming = _relance_payload(row, now=now)
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
    relance.setdefault("source", "salesforce_anomaly_followup_import")
    relance.setdefault("salesforce_task_id", row["salesforce_task_id"])
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
    row: dict[str, Any],
    *,
    now: str,
    old_status: str,
    relance_updated: bool,
) -> None:
    action = "mise à jour" if relance_updated else "importée"
    detail = (
        f"Relance Salesforce {action} au {row['scheduled_date']} · "
        f"{row.get('subject') or 'Relance'} · statut principal : "
        f"{old_status or 'non renseigné'} → {_TARGET_STATUS}."
    )
    if row.get("comments"):
        detail += f" Commentaire : {row['comments']}"
    contact.setdefault("activities", []).insert(0, {
        "id": str(uuid.uuid4()),
        "kind": "import",
        "title": "Anomalie de relance Salesforce régularisée",
        "detail": detail,
        "preview": "",
        "date": now,
        "author": "Import Salesforce",
    })


def import_anomaly_followup_rows(
    contacts: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    if dry_run:
        contacts = copy.deepcopy(contacts)

    now = dt.datetime.now(_PARIS_TZ).isoformat()
    batch_id = str(uuid.uuid4())
    prepared, stats = _prepare_rows(rows)
    by_id = {
        _text(contact.get("id")): contact
        for contact in contacts
        if _text(contact.get("id"))
    }

    counts: Counter[str] = Counter()
    formations: Counter[str] = Counter()
    old_statuses: Counter[str] = Counter()
    due_dates: Counter[str] = Counter()
    ready_rows: list[dict[str, Any]] = []
    blocked_rows: list[dict[str, Any]] = []

    for row in prepared:
        formations[row["crm_formation"]] += 1
        old_statuses[row.get("crm_status") or "Non renseigné"] += 1
        due_dates[row["scheduled_date"]] += 1

        if row.get("source_conflict"):
            counts["blocked"] += 1
            blocked_rows.append({
                **row,
                "block_reason": row["source_conflict"],
            })
            continue

        contact = by_id.get(row["contact_id"])
        if contact is None:
            counts["missing_contact"] += 1
            counts["blocked"] += 1
            blocked_rows.append({
                **row,
                "block_reason": "La fiche CRM indiquée dans le fichier n’existe plus.",
            })
            continue

        current_formation = _text(contact.get("formation"))
        if not current_formation:
            counts["current_formation_empty"] += 1
            counts["blocked"] += 1
            blocked_rows.append({
                **row,
                "block_reason": (
                    "La colonne Formation fiche CRM est renseignée, mais la fiche "
                    "CRM actuelle ne possède plus de formation."
                ),
            })
            continue

        current_name = _contact_name(contact)
        if row.get("crm_name") and not _names_compatible(
            row["crm_name"],
            current_name,
        ):
            counts["stale_contact_name"] += 1
            counts["blocked"] += 1
            blocked_rows.append({
                **row,
                "crm_current_name": current_name,
                "block_reason": (
                    "Le nom de la fiche CRM a changé depuis la création du fichier."
                ),
            })
            continue

        if row.get("person") and not _names_compatible(
            row["person"],
            current_name,
        ):
            counts["identity_mismatch"] += 1
            counts["blocked"] += 1
            blocked_rows.append({
                **row,
                "crm_current_name": current_name,
                "block_reason": (
                    "Le nom de la tâche Salesforce et le nom de la fiche CRM "
                    "désignent deux personnes différentes."
                ),
            })
            continue

        file_email = row.get("email") or ""
        file_phone = row.get("phone") or ""
        contact_email = _contact_email(contact)
        contact_phone = _contact_phone(contact)
        email_matches = bool(file_email and contact_email and file_email == contact_email)
        phone_matches = bool(file_phone and contact_phone and file_phone == contact_phone)
        if (file_email or file_phone) and not (email_matches or phone_matches):
            counts["coordinate_mismatch"] += 1
            counts["blocked"] += 1
            blocked_rows.append({
                **row,
                "crm_current_name": current_name,
                "block_reason": (
                    "Ni l’e-mail ni le téléphone du fichier ne correspondent "
                    "plus à la fiche CRM."
                ),
            })
            continue

        old_status = _text(contact.get("statut"))
        relance = _existing_relance(contact, row["salesforce_task_id"])
        relance_updated = False
        contact_changed = False
        if relance is None:
            relance = _relance_payload(row, now=now)
            contact.setdefault("relances", []).append(relance)
            counts["relances_created"] += 1
            contact_changed = True
        else:
            changed, preserved = _update_existing_relance(
                relance,
                row,
                now=now,
            )
            if preserved:
                counts["preserved_completed"] += 1
                counts["blocked"] += 1
                blocked_rows.append({
                    **row,
                    "crm_current_name": current_name,
                    "block_reason": (
                        "Cette relance Salesforce a déjà été traitée dans le CRM "
                        "et ne doit pas être rouverte."
                    ),
                })
                continue
            if changed:
                counts["relances_updated"] += 1
                relance_updated = True
                contact_changed = True
            else:
                counts["relances_unchanged"] += 1

        if old_status != _TARGET_STATUS:
            contact["statut"] = _TARGET_STATUS
            contact["status_changed_at"] = now
            counts["statuses_changed"] += 1
            if old_status == "Disqualifié":
                counts["reactivated_disqualified"] += 1
                contact["disqualification_reason"] = ""
                contact["disqualification_detail"] = ""
                contact["archived_at"] = ""
                contact["reactivation_date"] = now[:10]
            contact_changed = True
        else:
            counts["already_followup"] += 1

        if _refresh_relance_date(contact):
            contact_changed = True

        if contact_changed:
            contact["salesforce_anomaly_import_batch_id"] = batch_id
            contact["salesforce_anomaly_imported_at"] = now
            contact["updated_at"] = now
            if not dry_run:
                _add_activity(
                    contact,
                    row,
                    now=now,
                    old_status=old_status,
                    relance_updated=relance_updated,
                )

        counts["ready"] += 1
        ready_rows.append({
            "contact_id": row["contact_id"],
            "person": row.get("person") or current_name,
            "crm_name": current_name,
            "formation": current_formation,
            "old_status": old_status,
            "new_status": _TARGET_STATUS,
            "scheduled_date": row["scheduled_date"],
            "salesforce_task_id": row["salesforce_task_id"],
            "subject": row.get("subject") or "",
            "reactivated": old_status == "Disqualifié",
        })

    result = {
        "ok": True,
        "dry_run": dry_run,
        "batch_id": batch_id,
        "csv_rows": len(rows),
        "selected_with_formation": stats.get("selected_with_formation", 0),
        "skipped_without_formation": stats.get("skipped_without_formation", 0),
        "ready": counts["ready"],
        "blocked": counts["blocked"],
        "relances_created": counts["relances_created"],
        "relances_updated": counts["relances_updated"],
        "relances_unchanged": counts["relances_unchanged"],
        "preserved_completed": counts["preserved_completed"],
        "statuses_changed": counts["statuses_changed"],
        "already_followup": counts["already_followup"],
        "reactivated_disqualified": counts["reactivated_disqualified"],
        "missing_contact": counts["missing_contact"],
        "current_formation_empty": counts["current_formation_empty"],
        "stale_contact_name": counts["stale_contact_name"],
        "identity_mismatch": counts["identity_mismatch"],
        "coordinate_mismatch": counts["coordinate_mismatch"],
        "formation_counts": dict(formations.most_common()),
        "old_status_counts": dict(old_statuses.most_common()),
        "due_date_counts": dict(sorted(due_dates.items())),
        "ready_rows": ready_rows,
        "blocked_rows": blocked_rows,
        **stats,
    }
    for key in (
        "skipped_missing_activity_id",
        "skipped_missing_contact_id",
        "skipped_missing_date",
        "duplicates_in_file",
        "duplicate_conflicts_in_file",
    ):
        result.setdefault(key, 0)
    return result


def _contacts_signature(
    contacts: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> str:
    ids = {
        _text(row.get("contact_id"))
        for row in rows
        if _text(row.get("contact_id"))
    }
    payload = []
    for contact in sorted(
        (contact for contact in contacts if _text(contact.get("id")) in ids),
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
            _text(contact.get("statut")),
            _text(contact.get("formation")),
            _contact_name(contact),
            _contact_email(contact),
            _contact_phone(contact),
            relances,
        ))
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _preview_token(
    raw: bytes,
    contacts: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> str:
    digest = hashlib.sha256(raw)
    digest.update(_contacts_signature(contacts, rows).encode())
    return digest.hexdigest()


def register_salesforce_anomaly_followups_import(
    app,
    *,
    current_user_fn,
    load_data_fn,
    login_required_fn,
    save_data_fn,
    transaction_lock=None,
) -> None:
    """Enregistre l'API d'aperçu et d'import de la liste d'anomalies."""
    endpoint = "crm_import_salesforce_anomaly_followups"
    if endpoint in app.view_functions:
        return
    from flask import jsonify, request

    shared_lock = transaction_lock or _IMPORT_LOCK

    @app.post(
        "/api/crm/import-salesforce-anomaly-followups",
        endpoint=endpoint,
    )
    @login_required_fn
    def crm_import_salesforce_anomaly_followups():
        if (current_user_fn() or {}).get("role") != "admin":
            return jsonify({
                "error": "Seul un administrateur peut régulariser ces relances."
            }), 403
        upload = request.files.get("file")
        if not upload or not upload.filename:
            return jsonify({
                "error": "Sélectionnez la liste CSV complète des anomalies."
            }), 400
        dry_run = _text(request.form.get("dry_run", "0")) == "1"
        supplied_token = _text(request.form.get("preview_token"))

        try:
            raw = upload.read(MAX_CSV_BYTES + 1)
            rows = parse_anomaly_followups_csv(raw)
            if dry_run:
                data = load_data_fn()
                contacts = data.setdefault("crm_contacts", [])
                token = _preview_token(raw, contacts, rows)
                result = import_anomaly_followup_rows(
                    contacts,
                    rows,
                    dry_run=True,
                )
            else:
                with shared_lock:
                    data = load_data_fn()
                    contacts = data.setdefault("crm_contacts", [])
                    token = _preview_token(raw, contacts, rows)
                    if not supplied_token:
                        return jsonify({
                            "error": (
                                "Un aperçu doit être validé avant la régularisation."
                            )
                        }), 409
                    if supplied_token != token:
                        return jsonify({
                            "error": (
                                "Le fichier ou les fiches CRM ont changé depuis "
                                "l’aperçu. Relancez l’analyse."
                            )
                        }), 409
                    result = import_anomaly_followup_rows(
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
                                "selected_with_formation",
                                "ready",
                                "blocked",
                                "relances_created",
                                "relances_updated",
                                "statuses_changed",
                                "reactivated_disqualified",
                            )
                        },
                    }
                    data["crm_salesforce_anomaly_followups_last_import"] = summary
                    history = data.setdefault(
                        "crm_salesforce_anomaly_followups_import_history",
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
        except Exception as exc:  # pragma: no cover
            app.logger.exception(
                "Erreur import des anomalies de relances Salesforce"
            )
            return jsonify({
                "error": f"La régularisation a échoué : {exc}"
            }), 500
