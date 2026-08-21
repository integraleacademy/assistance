"""Correction des fiches CRM existantes depuis un export Salesforce.

Ce mode est distinct de la migration 2026 : il accepte les pistes de toutes les
années mais ne crée jamais de contact. Il sert à remettre à niveau des fiches
déjà présentes dans le CRM — lieu, session, formation, origine et statuts — en
conservant les relances, activités et commentaires internes.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import threading
import uuid
from collections import Counter
from typing import Any, Iterable

import pytz

from crm_salesforce_scope_guardrails import (
    _is_disqualified,
    _is_excluded_formation,
    _is_excluded_internal_record,
    _is_excluded_test_record,
    _is_open_without_formation,
    _source_formation,
)


MAX_CSV_BYTES = 20 * 1024 * 1024
_PARIS_TZ = pytz.timezone("Europe/Paris")
_IMPORT_LOCK = threading.Lock()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _contact_name(contact: dict[str, Any]) -> str:
    return " ".join(
        part
        for part in (
            _text(contact.get("prenom")),
            _text(contact.get("nom")),
        )
        if part
    )


def _prepare_existing_rows(
    migration_module,
    rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    mapped_rows: list[dict[str, Any]] = []
    stats: Counter[str] = Counter()

    for row in rows:
        if migration_module._truthy(migration_module._row_value(
            row,
            "IsDeleted",
            "Supprimé",
        )):
            stats["skipped_deleted"] += 1
            continue
        if _is_disqualified(migration_module, row):
            stats["skipped_disqualified"] += 1
            continue
        if _is_excluded_test_record(migration_module, row):
            stats["skipped_test"] += 1
            continue
        if _is_excluded_internal_record(migration_module, row):
            stats["skipped_internal"] += 1
            continue
        if _is_excluded_formation(
            migration_module,
            _source_formation(migration_module, row),
        ):
            stats["skipped_formation"] += 1
            continue
        if _is_open_without_formation(migration_module, row):
            stats["skipped_open_without_formation"] += 1
            continue

        mapped = migration_module._map_row(row)
        has_name = bool(
            _text(mapped.get("prenom"))
            or _text(mapped.get("nom"))
        )
        has_stable_key = bool(
            _text(mapped.get("salesforce_id"))
            or migration_module._email(mapped.get("mail"))
            or migration_module._phone(mapped.get("telephone"))
        )
        if not has_name or not has_stable_key:
            stats["skipped_invalid"] += 1
            continue
        mapped_rows.append(mapped)

    prepared, duplicates, conflicts = migration_module._deduplicate(mapped_rows)
    stats["duplicates_in_file"] += duplicates
    stats["duplicate_conflicts_in_file"] += conflicts
    return prepared, dict(stats)


def _add_activity(
    contact: dict[str, Any],
    *,
    now: str,
    match_method: str,
    before: dict[str, str],
    after: dict[str, str],
) -> None:
    changes = []
    labels = {
        "formation": "Formation",
        "lieu": "Lieu",
        "dates_formation": "Dates souhaitées",
        "statut": "Statut principal",
        "statut_secondaire": "Deuxième statut",
        "origine": "Origine",
    }
    for key, label in labels.items():
        if before.get(key) != after.get(key):
            changes.append(
                f"{label} : {before.get(key) or 'non renseigné'} → "
                f"{after.get(key) or 'non renseigné'}"
            )
    detail = (
        f"Correspondance par {match_method}. "
        + (" · ".join(changes) if changes else "Aucune différence métier.")
    )
    contact.setdefault("activities", []).insert(0, {
        "id": str(uuid.uuid4()),
        "kind": "import",
        "title": "Fiche corrigée depuis Salesforce",
        "detail": detail,
        "preview": "",
        "date": now,
        "author": "Correction Salesforce",
    })


def repair_existing_salesforce_rows(
    migration_module,
    contacts: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    if dry_run:
        contacts = copy.deepcopy(contacts)

    now = dt.datetime.now(_PARIS_TZ).isoformat()
    batch_id = str(uuid.uuid4())
    prepared, stats = _prepare_existing_rows(migration_module, rows)
    by_sf, by_email, by_phone = migration_module._indexes(contacts)

    counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    secondary_counts: Counter[str] = Counter()
    formation_counts: Counter[str] = Counter()
    year_counts: Counter[str] = Counter()
    ready_rows: list[dict[str, Any]] = []
    blocked_rows: list[dict[str, Any]] = []

    observed_fields = (
        "formation",
        "lieu",
        "dates_formation",
        "statut",
        "statut_secondaire",
        "origine",
    )

    for incoming in prepared:
        status_counts[_text(incoming.get("statut")) or "Non renseigné"] += 1
        secondary_counts[
            _text(incoming.get("statut_secondaire")) or "Aucun"
        ] += 1
        formation_counts[
            _text(incoming.get("formation")) or "Non renseignée"
        ] += 1
        source_date = migration_module._parse_datetime(
            incoming.get("salesforce_created_at")
        )
        year_counts[
            str(source_date.year) if source_date else "Date inconnue"
        ] += 1

        contact, method, reason = migration_module._match(
            incoming,
            by_sf,
            by_email,
            by_phone,
            deduplicate=True,
        )
        if reason:
            counts["ambiguous"] += 1
            blocked_rows.append({
                "person": " ".join(filter(None, (
                    _text(incoming.get("prenom")),
                    _text(incoming.get("nom")),
                ))),
                "salesforce_id": _text(incoming.get("salesforce_id")),
                "reason": reason,
            })
            continue
        if contact is None:
            counts["not_found"] += 1
            blocked_rows.append({
                "person": " ".join(filter(None, (
                    _text(incoming.get("prenom")),
                    _text(incoming.get("nom")),
                ))),
                "salesforce_id": _text(incoming.get("salesforce_id")),
                "reason": (
                    "Aucune fiche CRM existante ne correspond. "
                    "Aucune création n’est autorisée dans ce mode."
                ),
            })
            continue
        if not _text(contact.get("formation")):
            counts["without_crm_formation"] += 1
            blocked_rows.append({
                "person": _contact_name(contact),
                "contact_id": _text(contact.get("id")),
                "reason": "La fiche CRM ne possède aucune formation.",
            })
            continue
        if _text(contact.get("statut")) == "Disqualifié":
            counts["preserved_disqualified"] += 1
            blocked_rows.append({
                "person": _contact_name(contact),
                "contact_id": _text(contact.get("id")),
                "reason": "La fiche CRM est disqualifiée et ne sera pas réactivée automatiquement.",
            })
            continue

        before = {key: _text(contact.get(key)) for key in observed_fields}
        merge_payload = dict(incoming)

        # Une inscription confirmée ne doit jamais être rabaissée par un ancien
        # statut Salesforce encore actif. Les autres champs restent corrigibles.
        if (
            _text(contact.get("statut")) == "Converti"
            and _text(incoming.get("statut")) != "Converti"
        ):
            merge_payload.pop("statut", None)
            merge_payload.pop("statut_secondaire", None)
            merge_payload.pop("statut_secondaire_source", None)
            counts["preserved_converted_status"] += 1

        changed = migration_module._merge(
            contact,
            merge_payload,
            authoritative=True,
        )
        incoming_ids = list(incoming.get("salesforce_ids") or [])
        salesforce_id = _text(incoming.get("salesforce_id"))
        if salesforce_id and salesforce_id not in incoming_ids:
            incoming_ids.insert(0, salesforce_id)
        contact_ids = contact.setdefault("salesforce_ids", [])
        for item in incoming_ids:
            if item and item not in contact_ids:
                contact_ids.append(item)
                migration_module._append(by_sf, item, contact)
                changed = True
        if salesforce_id and not contact.get("salesforce_id"):
            contact["salesforce_id"] = salesforce_id
            changed = True

        after = {key: _text(contact.get(key)) for key in observed_fields}
        if changed:
            counts["updated"] += 1
            if not dry_run:
                contact.update({
                    "salesforce_existing_repair_batch_id": batch_id,
                    "salesforce_existing_repaired_at": now,
                    "updated_at": now,
                })
                _add_activity(
                    contact,
                    now=now,
                    match_method=method,
                    before=before,
                    after=after,
                )
        else:
            counts["unchanged"] += 1

        counts["matched"] += 1
        ready_rows.append({
            "contact_id": _text(contact.get("id")),
            "person": _contact_name(contact),
            "match_method": method,
            "salesforce_id": salesforce_id,
            "before": before,
            "after": after,
            "changed": bool(changed),
        })

    result = {
        "ok": True,
        "dry_run": dry_run,
        "mode": "existing_all_years",
        "batch_id": batch_id,
        "csv_rows": len(rows),
        "prepared_rows": len(prepared),
        "created": 0,
        "matched": counts["matched"],
        "updated": counts["updated"],
        "unchanged": counts["unchanged"],
        "not_found": counts["not_found"],
        "ambiguous": counts["ambiguous"],
        "without_crm_formation": counts["without_crm_formation"],
        "preserved_disqualified": counts["preserved_disqualified"],
        "preserved_converted_status": counts["preserved_converted_status"],
        "status_counts": dict(status_counts.most_common()),
        "secondary_status_counts": dict(secondary_counts.most_common()),
        "formation_counts": dict(formation_counts.most_common()),
        "year_counts": dict(year_counts.most_common()),
        "ready_rows": ready_rows,
        "blocked_rows": blocked_rows,
        **stats,
    }
    for key in (
        "skipped_deleted",
        "skipped_disqualified",
        "skipped_test",
        "skipped_internal",
        "skipped_formation",
        "skipped_open_without_formation",
        "skipped_invalid",
        "duplicates_in_file",
        "duplicate_conflicts_in_file",
    ):
        result.setdefault(key, 0)
    return result


def _contacts_signature(
    contacts: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    migration_module,
) -> str:
    prepared, _ = _prepare_existing_rows(migration_module, rows)
    identifiers = {
        _text(item.get("salesforce_id"))
        for item in prepared
        if _text(item.get("salesforce_id"))
    }
    emails = {
        migration_module._email(item.get("mail"))
        for item in prepared
        if migration_module._email(item.get("mail"))
    }
    phones = {
        migration_module._phone(item.get("telephone"))
        for item in prepared
        if migration_module._phone(item.get("telephone"))
    }
    payload = []
    for contact in contacts:
        contact_ids = set(contact.get("salesforce_ids") or [])
        if contact.get("salesforce_id"):
            contact_ids.add(_text(contact.get("salesforce_id")))
        relevant = bool(contact_ids & identifiers)
        relevant = relevant or (
            migration_module._email(contact.get("mail")) in emails
        )
        relevant = relevant or (
            migration_module._phone(contact.get("telephone")) in phones
        )
        if not relevant:
            continue
        payload.append((
            _text(contact.get("id")),
            _text(contact.get("updated_at")),
            _text(contact.get("statut")),
            _text(contact.get("statut_secondaire")),
            _text(contact.get("formation")),
            _text(contact.get("lieu")),
            _text(contact.get("dates_formation")),
            sorted(contact_ids),
        ))
    payload.sort(key=lambda item: item[0])
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _preview_token(
    raw: bytes,
    contacts: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    migration_module,
) -> str:
    digest = hashlib.sha256(raw)
    digest.update(_contacts_signature(
        contacts,
        rows,
        migration_module,
    ).encode())
    return digest.hexdigest()


def register_salesforce_existing_repair(
    app,
    *,
    migration_module,
    current_user_fn,
    load_data_fn,
    login_required_fn,
    save_data_fn,
    transaction_lock=None,
) -> None:
    """Enregistre l’aperçu et la correction des fiches existantes."""
    endpoint = "crm_repair_existing_salesforce"
    if endpoint in app.view_functions:
        return
    from flask import jsonify, request

    shared_lock = transaction_lock or _IMPORT_LOCK

    @app.post(
        "/api/crm/repair-existing-salesforce",
        endpoint=endpoint,
    )
    @login_required_fn
    def crm_repair_existing_salesforce():
        if (current_user_fn() or {}).get("role") != "admin":
            return jsonify({
                "error": "Seul un administrateur peut corriger les fiches Salesforce."
            }), 403
        upload = request.files.get("file")
        if not upload or not upload.filename:
            return jsonify({
                "error": "Sélectionnez un export CSV de pistes Salesforce."
            }), 400
        dry_run = _text(request.form.get("dry_run", "0")) == "1"
        supplied_token = _text(request.form.get("preview_token"))

        try:
            raw = upload.read(MAX_CSV_BYTES + 1)
            if len(raw) > MAX_CSV_BYTES:
                raise ValueError("Le fichier dépasse la limite de 20 Mo.")
            rows = migration_module.parse_compatible_csv(
                raw,
                max_csv_bytes=MAX_CSV_BYTES,
            )

            if dry_run:
                data = load_data_fn()
                contacts = data.setdefault("crm_contacts", [])
                token = _preview_token(
                    raw,
                    contacts,
                    rows,
                    migration_module,
                )
                result = repair_existing_salesforce_rows(
                    migration_module,
                    contacts,
                    rows,
                    dry_run=True,
                )
            else:
                with shared_lock:
                    data = load_data_fn()
                    contacts = data.setdefault("crm_contacts", [])
                    token = _preview_token(
                        raw,
                        contacts,
                        rows,
                        migration_module,
                    )
                    if not supplied_token:
                        return jsonify({
                            "error": "Un aperçu doit être validé avant la correction."
                        }), 409
                    if supplied_token != token:
                        return jsonify({
                            "error": (
                                "Le fichier ou les fiches CRM ont changé depuis "
                                "l’aperçu. Relancez l’analyse."
                            )
                        }), 409
                    result = repair_existing_salesforce_rows(
                        migration_module,
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
                                "prepared_rows",
                                "matched",
                                "updated",
                                "unchanged",
                                "not_found",
                                "ambiguous",
                            )
                        },
                    }
                    data["crm_salesforce_existing_repair_last"] = summary
                    history = data.setdefault(
                        "crm_salesforce_existing_repair_history",
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
            app.logger.exception("Erreur correction des fiches Salesforce")
            return jsonify({
                "error": f"La correction Salesforce a échoué : {exc}"
            }), 500
