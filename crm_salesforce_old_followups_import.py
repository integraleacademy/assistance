"""Import ciblé des anciennes pistes Salesforce ayant une relance ouverte.

Ce traitement utilise simultanément :

- un export complet des pistes Salesforce, toutes dates confondues ;
- un export des tâches et événements Salesforce.

Il ne reprend que les pistes créées avant 2026 qui possèdent au moins une tâche
ouverte dans le second fichier. Les pistes disqualifiées, converties, BTS/CAP,
internes ou de test restent exclues. Un aperçu est obligatoire avant toute
écriture et les identifiants Salesforce assurent l'idempotence.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import threading
import uuid
from collections import Counter, defaultdict
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
CUTOFF_YEAR = 2026
TARGET_STATUS = "A relancer"
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


def _name_tokens(value: Any, fold_fn) -> list[str]:
    return [token for token in fold_fn(value).split() if token]


def _names_compatible(left: Any, right: Any, fold_fn) -> bool:
    left_folded = fold_fn(left)
    right_folded = fold_fn(right)
    if not left_folded or not right_folded:
        return True
    if left_folded == right_folded:
        return True
    left_tokens = _name_tokens(left, fold_fn)
    right_tokens = _name_tokens(right, fold_fn)
    if sorted(left_tokens) == sorted(right_tokens):
        return True
    # Salesforce ajoute parfois « M. », inverse prénom/nom ou omet le prénom.
    left_surname = left_tokens[-1] if left_tokens else ""
    right_surname = right_tokens[-1] if right_tokens else ""
    return bool(
        left_surname
        and right_surname
        and (
            left_surname == right_surname
            or left_surname in right_tokens
            or right_surname in left_tokens
        )
    )


def _created_year(migration_module, row: dict[str, Any]) -> int | None:
    raw = migration_module._row_value(
        row,
        "CreatedDate",
        "Date de création",
    )
    parsed = migration_module._parse_datetime(raw)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = pytz.UTC.localize(parsed)
    return parsed.astimezone(_PARIS_TZ).year


def _prepare_old_leads(
    migration_module,
    rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Mappe uniquement les pistes antérieures à 2026 encore exploitables."""
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

        year = _created_year(migration_module, row)
        if year is None:
            stats["skipped_missing_created_date"] += 1
            continue
        if year >= CUTOFF_YEAR:
            stats["skipped_current_year_or_newer"] += 1
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
        if _text(mapped.get("statut")) == "Converti":
            stats["skipped_converted"] += 1
            continue
        if not (_text(mapped.get("prenom")) or _text(mapped.get("nom"))):
            stats["skipped_invalid_identity"] += 1
            continue
        if not (
            _text(mapped.get("salesforce_id"))
            or migration_module._email(mapped.get("mail"))
            or migration_module._phone(mapped.get("telephone"))
        ):
            stats["skipped_missing_coordinates"] += 1
            continue

        mapped["salesforce_source_year"] = year
        # Une tâche ouverte signifie que la fiche doit être dans la file des
        # relances, quel que soit son ancien statut principal Salesforce.
        mapped["statut"] = TARGET_STATUS
        mapped_rows.append(mapped)

    prepared, duplicates, conflicts = migration_module._deduplicate(mapped_rows)
    stats["old_leads_before_deduplication"] = len(mapped_rows)
    stats["old_leads_prepared"] = len(prepared)
    stats["duplicates_in_leads_file"] = duplicates
    stats["duplicate_conflicts_in_leads_file"] = conflicts
    return prepared, dict(stats)


def _append_unique(index: dict[str, list[int]], key: str, value: int) -> None:
    if key and value not in index.setdefault(key, []):
        index[key].append(value)


def _lead_indexes(migration_module, leads: list[dict[str, Any]]):
    by_email: dict[str, list[int]] = {}
    by_phone: dict[str, list[int]] = {}
    for index, lead in enumerate(leads):
        _append_unique(by_email, migration_module._email(lead.get("mail")), index)
        _append_unique(by_phone, migration_module._phone(lead.get("telephone")), index)
    return by_email, by_phone


def _unique_indexes(values: Iterable[int]) -> list[int]:
    result: list[int] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _match_task_to_old_lead(
    migration_module,
    task: dict[str, Any],
    leads: list[dict[str, Any]],
    by_email: dict[str, list[int]],
    by_phone: dict[str, list[int]],
) -> tuple[int | None, str, str]:
    email_matches = _unique_indexes(
        by_email.get(task.get("email") or "", [])
    ) if task.get("email") else []
    phone_matches = _unique_indexes(
        lead_index
        for phone in (task.get("phones") or [])
        for lead_index in by_phone.get(phone, [])
    )

    if len(email_matches) > 1:
        return None, "", "Plusieurs anciennes pistes utilisent cet e-mail."
    if len(phone_matches) > 1:
        return None, "", "Plusieurs anciennes pistes utilisent ce téléphone."

    candidate: int | None = None
    method = ""
    if email_matches and phone_matches:
        if email_matches[0] != phone_matches[0]:
            return None, "", (
                "L’e-mail et le téléphone désignent deux anciennes pistes différentes."
            )
        candidate = email_matches[0]
        method = "email+phone"
    elif email_matches:
        candidate = email_matches[0]
        method = "email"
    elif phone_matches:
        candidate = phone_matches[0]
        method = "phone"

    if candidate is None:
        return None, "", ""

    lead = leads[candidate]
    if not _names_compatible(
        _contact_name(lead),
        task.get("relation_name") or "",
        migration_module._fold,
    ):
        return None, "", (
            "Les coordonnées correspondent, mais le nom de la tâche et celui "
            "de la piste sont différents."
        )
    return candidate, method, ""


def _group_open_tasks_by_old_lead(
    migration_module,
    tasks_module,
    leads: list[dict[str, Any]],
    task_rows: list[dict[str, Any]],
) -> tuple[dict[int, list[dict[str, Any]]], dict[str, Any]]:
    prepared_tasks, task_stats = tasks_module._prepare_task_rows(task_rows)
    by_email, by_phone = _lead_indexes(migration_module, leads)
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    counts: Counter[str] = Counter()
    blocked_rows: list[dict[str, Any]] = []
    match_methods: Counter[str] = Counter()

    for task in prepared_tasks:
        if task.get("source_conflict"):
            counts["task_source_conflicts"] += 1
            blocked_rows.append({
                "person": task.get("relation_name") or "Sans nom",
                "scheduled_date": task.get("scheduled_date") or "",
                "salesforce_task_id": task.get("salesforce_task_id") or "",
                "reason": task["source_conflict"],
            })
            continue

        lead_index, method, reason = _match_task_to_old_lead(
            migration_module,
            task,
            leads,
            by_email,
            by_phone,
        )
        if reason:
            counts["task_lead_ambiguities"] += 1
            blocked_rows.append({
                "person": task.get("relation_name") or "Sans nom",
                "scheduled_date": task.get("scheduled_date") or "",
                "salesforce_task_id": task.get("salesforce_task_id") or "",
                "reason": reason,
            })
            continue
        if lead_index is None:
            counts["tasks_outside_old_lead_scope"] += 1
            continue

        grouped[lead_index].append(task)
        match_methods[method] += 1
        counts["matched_open_tasks"] += 1

    return grouped, {
        **task_stats,
        **counts,
        "task_match_method_counts": dict(match_methods.most_common()),
        "task_blocked_rows": blocked_rows,
        "prepared_open_tasks": len(prepared_tasks),
    }


def _crm_task_index(contacts: list[dict[str, Any]]) -> dict[str, list[tuple[dict[str, Any], dict[str, Any]]]]:
    result: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for contact in contacts:
        for relance in contact.get("relances") or []:
            if not isinstance(relance, dict):
                continue
            task_id = _text(relance.get("salesforce_task_id"))
            if task_id:
                result[task_id].append((contact, relance))
    return result


def _add_import_activity(
    contact: dict[str, Any],
    *,
    now: str,
    action: str,
    tasks: list[dict[str, Any]],
    old_status: str,
) -> None:
    dates = sorted({task["scheduled_date"] for task in tasks})
    detail = (
        f"Ancienne piste Salesforce {action}. Statut principal : "
        f"{old_status or 'non renseigné'} → {TARGET_STATUS}. "
        f"Relance(s) ouverte(s) : {', '.join(dates)}."
    )
    contact.setdefault("activities", []).insert(0, {
        "id": str(uuid.uuid4()),
        "kind": "import",
        "title": "Ancienne piste avec relance ouverte importée",
        "detail": detail,
        "preview": "",
        "date": now,
        "author": "Import Salesforce",
    })


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


def import_old_salesforce_followups(
    migration_module,
    tasks_module,
    contacts: list[dict[str, Any]],
    lead_rows: list[dict[str, Any]],
    task_rows: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Crée ou rapproche seulement les anciennes pistes avec tâche ouverte."""
    if dry_run:
        contacts = copy.deepcopy(contacts)

    now = dt.datetime.now(_PARIS_TZ).isoformat()
    batch_id = str(uuid.uuid4())
    leads, lead_stats = _prepare_old_leads(migration_module, lead_rows)
    grouped_tasks, task_stats = _group_open_tasks_by_old_lead(
        migration_module,
        tasks_module,
        leads,
        task_rows,
    )

    by_sf, by_email, by_phone = migration_module._indexes(contacts)
    task_index = _crm_task_index(contacts)
    counts: Counter[str] = Counter()
    years: Counter[str] = Counter()
    formations: Counter[str] = Counter()
    secondary_statuses: Counter[str] = Counter()
    due_dates: Counter[str] = Counter()
    ready_rows: list[dict[str, Any]] = []
    blocked_rows: list[dict[str, Any]] = list(
        task_stats.get("task_blocked_rows") or []
    )
    created_contacts: list[dict[str, Any]] = []

    for lead_index, tasks in grouped_tasks.items():
        incoming = dict(leads[lead_index])
        person = _contact_name(incoming)
        years[str(incoming.get("salesforce_source_year") or "Date inconnue")] += 1
        formations[_text(incoming.get("formation")) or "Non renseignée"] += 1
        secondary_statuses[
            _text(incoming.get("statut_secondaire")) or "Aucun"
        ] += 1
        for task in tasks:
            due_dates[task["scheduled_date"]] += 1

        contact, match_method, match_reason = migration_module._match(
            incoming,
            by_sf,
            by_email,
            by_phone,
            deduplicate=True,
        )
        if match_reason:
            counts["blocked_contact_match"] += 1
            blocked_rows.append({
                "person": person,
                "salesforce_id": _text(incoming.get("salesforce_id")),
                "reason": match_reason,
            })
            continue

        if contact is not None and match_method != "salesforce" and not _names_compatible(
            person,
            _contact_name(contact),
            migration_module._fold,
        ):
            counts["blocked_identity_mismatch"] += 1
            blocked_rows.append({
                "person": person,
                "crm_name": _contact_name(contact),
                "salesforce_id": _text(incoming.get("salesforce_id")),
                "reason": (
                    "Les coordonnées correspondent à une fiche CRM portant un autre nom."
                ),
            })
            continue

        if contact is not None and _text(contact.get("archived_at")):
            counts["blocked_archived_contact"] += 1
            blocked_rows.append({
                "person": person,
                "crm_name": _contact_name(contact),
                "reason": "La fiche CRM correspondante est archivée.",
            })
            continue
        if contact is not None and _text(contact.get("statut")) == "Disqualifié":
            counts["blocked_disqualified_contact"] += 1
            blocked_rows.append({
                "person": person,
                "crm_name": _contact_name(contact),
                "reason": "La fiche CRM correspondante est disqualifiée.",
            })
            continue
        if contact is not None and _text(contact.get("statut")) == "Converti":
            counts["blocked_converted_contact"] += 1
            blocked_rows.append({
                "person": person,
                "crm_name": _contact_name(contact),
                "reason": (
                    "La fiche CRM est déjà convertie ; une ancienne tâche ne doit "
                    "pas la replacer dans les relances."
                ),
            })
            continue

        usable_tasks: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        for task in tasks:
            existing_locations = task_index.get(task["salesforce_task_id"], [])
            if len(existing_locations) > 1:
                counts["blocked_duplicate_task_in_crm"] += 1
                blocked_rows.append({
                    "person": person,
                    "scheduled_date": task["scheduled_date"],
                    "salesforce_task_id": task["salesforce_task_id"],
                    "reason": (
                        "L’identifiant de tâche existe sur plusieurs fiches CRM."
                    ),
                })
                continue
            if existing_locations:
                owner_contact, existing = existing_locations[0]
                if contact is None or owner_contact is not contact:
                    counts["blocked_task_owned_by_other_contact"] += 1
                    blocked_rows.append({
                        "person": person,
                        "scheduled_date": task["scheduled_date"],
                        "salesforce_task_id": task["salesforce_task_id"],
                        "crm_name": _contact_name(owner_contact),
                        "reason": (
                            "Cette tâche Salesforce est déjà rattachée à une autre "
                            "fiche CRM."
                        ),
                    })
                    continue
                usable_tasks.append((task, existing))
            else:
                usable_tasks.append((task, None))

        if not usable_tasks:
            counts["blocked_without_usable_task"] += 1
            continue

        old_status = _text(contact.get("statut")) if contact else ""
        action = "créée"
        contact_changed = False
        if contact is None:
            salesforce_ids = list(incoming.get("salesforce_ids") or [])
            primary_id = _text(incoming.get("salesforce_id"))
            if primary_id and primary_id not in salesforce_ids:
                salesforce_ids.insert(0, primary_id)
            contact = {
                "id": str(uuid.uuid4()),
                **incoming,
                "statut": TARGET_STATUS,
                "activities": [],
                "relances": [],
                "salesforce_ids": salesforce_ids,
                "salesforce_old_followup_batch_id": batch_id,
                "salesforce_old_followup_imported_at": now,
                "updated_at": now,
            }
            created_contacts.append(contact)
            counts["created"] += 1
            contact_changed = True
            for sfid in salesforce_ids:
                migration_module._append(by_sf, _text(sfid), contact)
            migration_module._append(
                by_email,
                migration_module._email(contact.get("mail")),
                contact,
            )
            migration_module._append(
                by_phone,
                migration_module._phone(contact.get("telephone")),
                contact,
            )
        else:
            action = "mise à jour"
            if migration_module._merge(contact, incoming, authoritative=True):
                contact_changed = True

            salesforce_ids = contact.setdefault("salesforce_ids", [])
            for sfid in incoming.get("salesforce_ids") or []:
                sfid = _text(sfid)
                if sfid and sfid not in salesforce_ids:
                    salesforce_ids.append(sfid)
                    migration_module._append(by_sf, sfid, contact)
                    contact_changed = True
            primary_id = _text(incoming.get("salesforce_id"))
            if primary_id and not contact.get("salesforce_id"):
                contact["salesforce_id"] = primary_id
                migration_module._append(by_sf, primary_id, contact)
                contact_changed = True

        applied_tasks: list[dict[str, Any]] = []
        for task, existing_relance in usable_tasks:
            if existing_relance is None:
                relance = tasks_module._relance_payload(task, now=now)
                relance["source"] = "salesforce_old_open_followup_import"
                contact.setdefault("relances", []).append(relance)
                task_index[task["salesforce_task_id"]].append((contact, relance))
                counts["relances_created"] += 1
                contact_changed = True
                applied_tasks.append(task)
                continue

            changed, preserved = tasks_module._update_existing_relance(
                existing_relance,
                task,
                now=now,
            )
            if preserved:
                counts["preserved_completed_relances"] += 1
                continue
            if changed:
                counts["relances_updated"] += 1
                contact_changed = True
            else:
                counts["relances_unchanged"] += 1
            applied_tasks.append(task)

        if not applied_tasks:
            if action == "créée":
                created_contacts.remove(contact)
                counts["created"] -= 1
            counts["blocked_only_completed_tasks"] += 1
            blocked_rows.append({
                "person": person,
                "reason": (
                    "Toutes les relances correspondantes ont déjà été traitées "
                    "dans le CRM et ne doivent pas être rouvertes."
                ),
            })
            continue

        if _text(contact.get("statut")) != TARGET_STATUS:
            contact["statut"] = TARGET_STATUS
            contact["status_changed_at"] = now
            counts["statuses_changed"] += 1
            contact_changed = True
        if tasks_module._refresh_relance_date(contact):
            contact_changed = True

        if action == "mise à jour":
            if contact_changed:
                counts["updated"] += 1
            else:
                counts["unchanged"] += 1

        if contact_changed:
            contact["salesforce_old_followup_batch_id"] = batch_id
            contact["salesforce_old_followup_imported_at"] = now
            contact["updated_at"] = now
            if not dry_run:
                _add_import_activity(
                    contact,
                    now=now,
                    action=action,
                    tasks=applied_tasks,
                    old_status=old_status,
                )

        counts["ready"] += 1
        ready_rows.append({
            "contact_id": _text(contact.get("id")),
            "person": person,
            "salesforce_id": _text(incoming.get("salesforce_id")),
            "source_year": incoming.get("salesforce_source_year"),
            "formation": _text(incoming.get("formation")),
            "source_status": _text(incoming.get("salesforce_status")),
            "secondary_status": _text(incoming.get("statut_secondaire")),
            "action": action,
            "match_method": match_method or "nouvelle fiche",
            "task_count": len(applied_tasks),
            "due_dates": sorted({task["scheduled_date"] for task in applied_tasks}),
        })

    if not dry_run and created_contacts:
        created_contacts.sort(
            key=lambda item: _text(item.get("created_at")),
            reverse=True,
        )
        contacts[:0] = created_contacts

    result = {
        "ok": True,
        "dry_run": dry_run,
        "batch_id": batch_id,
        "lead_csv_rows": len(lead_rows),
        "task_csv_rows": len(task_rows),
        "old_leads_prepared": lead_stats.get("old_leads_prepared", 0),
        "old_leads_with_open_task": len(grouped_tasks),
        "ready": counts["ready"],
        "created": counts["created"],
        "updated": counts["updated"],
        "unchanged": counts["unchanged"],
        "blocked": len(blocked_rows),
        "relances_created": counts["relances_created"],
        "relances_updated": counts["relances_updated"],
        "relances_unchanged": counts["relances_unchanged"],
        "preserved_completed_relances": counts["preserved_completed_relances"],
        "statuses_changed": counts["statuses_changed"],
        "years": dict(years.most_common()),
        "formation_counts": dict(formations.most_common()),
        "secondary_status_counts": dict(secondary_statuses.most_common()),
        "due_date_counts": dict(sorted(due_dates.items())),
        "ready_rows": ready_rows,
        "blocked_rows": blocked_rows,
        **lead_stats,
        **{
            key: value
            for key, value in task_stats.items()
            if key != "task_blocked_rows"
        },
        **counts,
    }
    return result


def _contacts_signature(contacts: list[dict[str, Any]]) -> str:
    payload = []
    for contact in sorted(contacts, key=lambda item: _text(item.get("id"))):
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
            _text(contact.get("statut_secondaire")),
            _text(contact.get("mail")),
            _text(contact.get("telephone")),
            _text(contact.get("salesforce_id")),
            sorted(_text(value) for value in (contact.get("salesforce_ids") or [])),
            relances,
        ))
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _preview_token(
    leads_raw: bytes,
    tasks_raw: bytes,
    contacts: list[dict[str, Any]],
) -> str:
    digest = hashlib.sha256(leads_raw)
    digest.update(tasks_raw)
    digest.update(_contacts_signature(contacts).encode())
    return digest.hexdigest()


def register_salesforce_old_followups_import(
    app,
    *,
    migration_module,
    tasks_module,
    current_user_fn,
    load_data_fn,
    login_required_fn,
    save_data_fn,
    transaction_lock=None,
) -> None:
    """Enregistre l'API d'aperçu et d'import ciblé des anciennes pistes."""
    endpoint = "crm_import_salesforce_old_followups"
    if endpoint in app.view_functions:
        return
    from flask import jsonify, request

    shared_lock = transaction_lock or _IMPORT_LOCK

    @app.post(
        "/api/crm/import-salesforce-old-followups",
        endpoint=endpoint,
    )
    @login_required_fn
    def crm_import_salesforce_old_followups():
        if (current_user_fn() or {}).get("role") != "admin":
            return jsonify({
                "error": "Seul un administrateur peut importer ces anciennes pistes."
            }), 403

        leads_upload = request.files.get("leads_file")
        tasks_upload = request.files.get("tasks_file")
        if not leads_upload or not leads_upload.filename:
            return jsonify({
                "error": "Sélectionnez l’export complet des pistes Salesforce."
            }), 400
        if not tasks_upload or not tasks_upload.filename:
            return jsonify({
                "error": "Sélectionnez l’export des relances Salesforce."
            }), 400

        dry_run = _text(request.form.get("dry_run", "0")) == "1"
        supplied_token = _text(request.form.get("preview_token"))

        try:
            leads_raw = leads_upload.read(MAX_CSV_BYTES + 1)
            tasks_raw = tasks_upload.read(MAX_CSV_BYTES + 1)
            if len(leads_raw) > MAX_CSV_BYTES or len(tasks_raw) > MAX_CSV_BYTES:
                raise ValueError("Chaque fichier doit peser au maximum 20 Mo.")

            lead_rows = migration_module.parse_compatible_csv(
                leads_raw,
                max_csv_bytes=MAX_CSV_BYTES,
            )
            task_rows = tasks_module.parse_salesforce_tasks_csv(tasks_raw)

            if dry_run:
                data = load_data_fn()
                contacts = data.setdefault("crm_contacts", [])
                token = _preview_token(leads_raw, tasks_raw, contacts)
                result = import_old_salesforce_followups(
                    migration_module,
                    tasks_module,
                    contacts,
                    lead_rows,
                    task_rows,
                    dry_run=True,
                )
            else:
                with shared_lock:
                    data = load_data_fn()
                    contacts = data.setdefault("crm_contacts", [])
                    token = _preview_token(leads_raw, tasks_raw, contacts)
                    if not supplied_token:
                        return jsonify({
                            "error": "Un aperçu doit être validé avant l’import."
                        }), 409
                    if supplied_token != token:
                        return jsonify({
                            "error": (
                                "Les fichiers ou le CRM ont changé depuis l’aperçu. "
                                "Relancez l’analyse."
                            )
                        }), 409

                    result = import_old_salesforce_followups(
                        migration_module,
                        tasks_module,
                        contacts,
                        lead_rows,
                        task_rows,
                        dry_run=False,
                    )
                    summary = {
                        "date": dt.datetime.now(_PARIS_TZ).isoformat(),
                        "leads_filename": leads_upload.filename,
                        "tasks_filename": tasks_upload.filename,
                        "batch_id": result.get("batch_id"),
                        **{
                            key: result.get(key, 0)
                            for key in (
                                "lead_csv_rows",
                                "task_csv_rows",
                                "old_leads_with_open_task",
                                "ready",
                                "created",
                                "updated",
                                "blocked",
                                "relances_created",
                                "relances_updated",
                            )
                        },
                    }
                    data["crm_salesforce_old_followups_last_import"] = summary
                    history = data.setdefault(
                        "crm_salesforce_old_followups_import_history",
                        [],
                    )
                    history.insert(0, summary)
                    del history[20:]
                    save_data_fn(data)

            result.update({
                "preview_token": token,
                "leads_filename": leads_upload.filename,
                "tasks_filename": tasks_upload.filename,
                "cutoff_year": CUTOFF_YEAR,
            })
            return jsonify(result)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # pragma: no cover
            app.logger.exception(
                "Erreur import anciennes pistes Salesforce avec relance"
            )
            return jsonify({
                "error": f"L’import ciblé a échoué : {exc}"
            }), 500
