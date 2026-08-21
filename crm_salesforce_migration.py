"""Migration complète et sécurisée des pistes Salesforce vers le CRM.

L'ancien import 2025 reste disponible dans ``crm_salesforce_import``. Ce module
ajoute un mode de migration intégrale sans modifier ce comportement historique :
aperçu obligatoire, rapprochement prudent et protection des données déjà saisies.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
import threading
import unicodedata
import uuid
from collections import Counter
from typing import Any, Iterable

import pytz

import crm_salesforce_import as legacy
from crm_salesforce_csv_compat import parse_salesforce_csv as parse_compatible_csv

IMPORT_MODE_COMPLETE = "complete"
IMPORT_MODE_LEGACY = "legacy_2025"
MERGE_POLICY_SAFE = "safe"
MERGE_POLICY_SALESFORCE = "salesforce"
_IMPORT_LOCK = threading.Lock()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _fold(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", _text(value))
    normalized = "".join(
        character for character in normalized
        if unicodedata.category(character) != "Mn"
    ).casefold()
    normalized = re.sub(r"[_:./\\()\[\]{}-]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _row_value(row: dict[str, Any], *names: str) -> str:
    for name in names:
        if _text(row.get(name)):
            return _text(row.get(name))
    folded = {_fold(key): value for key, value in row.items() if key is not None}
    for name in names:
        value = folded.get(_fold(name))
        if _text(value):
            return _text(value)
    return ""


def _truthy(value: Any) -> bool:
    return _fold(value) in {"oui", "yes", "true", "1", "y", "o", "vrai"}


def _email(value: Any) -> str:
    candidate = _text(value).casefold()
    return candidate if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", candidate) else ""


def _phone(value: Any) -> str:
    digits = re.sub(r"\D", "", _text(value))
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10 and digits.startswith("0"):
        digits = f"33{digits[1:]}"
    return digits if 8 <= len(digits) <= 15 else ""


def _parse_datetime(value: Any) -> dt.datetime | None:
    raw = _text(value)
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in (
            "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y",
            "%d/%m/%Y, %H:%M", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S",
            "%m/%d/%Y, %I:%M %p", "%m/%d/%Y %I:%M %p",
            "%m/%d/%Y %H:%M", "%m/%d/%Y",
        ):
            try:
                parsed = dt.datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = pytz.UTC.localize(parsed)
    return parsed


def _iso(value: Any) -> str:
    parsed = _parse_datetime(value)
    return parsed.isoformat() if parsed else _text(value)


def _date_bound(value: str, *, end: bool = False) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.date.fromisoformat(value[:10])
    except ValueError as exc:
        raise ValueError(f"Date de filtre invalide : {value}") from exc
    return pytz.UTC.localize(dt.datetime.combine(parsed, dt.time.max if end else dt.time.min))


def _normalized_formation(value: Any) -> str:
    raw, folded = _text(value), _fold(value)
    aliases = {
        "apr": "A3P", "a3p": "A3P", "aps": "APS",
        "dirigeant": "DESP", "desp": "DESP", "desp initial": "DESP",
        "desp vae": "DESP", "chauffeur vtc": "Chauffeur VTC", "vtc": "Chauffeur VTC",
        "ssiap": "SSIAP 1", "ssiap 1": "SSIAP 1", "poei": "POEI",
        "bts": "BTS", "bts mos": "BTS MOS", "bts mco": "BTS MCO",
        "bts ndrc": "BTS NDRC", "bts ci": "BTS CI", "bts pi": "BTS PI",
        "bts cg": "BTS CG",
    }
    if folded in aliases:
        return aliases[folded]
    for code in ("mos", "mco", "ndrc", "ci", "pi", "cg"):
        if folded.startswith(f"bts {code}"):
            return f"BTS {code.upper()}"
    return raw


def _normalized_origin(row: dict[str, Any]) -> str:
    raw = _row_value(row, "Origine__c", "LeadSource", "Origine", "Source de la piste")
    aliases = {
        "compte cpf": "CPF", "cpf": "CPF", "france travail": "FT", "ft": "FT",
        "website": "Site internet", "site internet": "Site internet", "site web": "Site internet",
        "calendly": "Calendly", "google": "Google Ads", "google ads": "Google Ads",
        "adwords": "Google Ads", "facebook": "Meta", "instagram": "Meta", "meta": "Meta",
        "poei": "POEI", "reseaux sociaux": "Réseaux sociaux",
        "bouche a oreille": "Bouche à oreille", "manual": "Ajout manuel",
        "saisie manuelle": "Ajout manuel",
    }
    return aliases.get(_fold(raw), raw or "Salesforce")


def _normalized_status(row: dict[str, Any]) -> str:
    if _truthy(_row_value(row, "IsConverted", "Converti", "Est converti")):
        return "Converti"
    value = _fold(_row_value(row, "Status", "Statut"))
    aliases = {
        "qualified": "Converti", "converted": "Converti", "converti": "Converti", "inscrit": "Converti",
        "unqualified": "Disqualifié", "closed not converted": "Disqualifié",
        "closed lost": "Disqualifié", "disqualifie": "Disqualifié",
        "open not contacted": "Nouveaux", "new": "Nouveaux", "nouveau": "Nouveaux",
        "nouveaux": "Nouveaux", "working contacted": "A relancer", "contacted": "A relancer",
        "nurturing": "A relancer", "a relancer": "A relancer", "blocage": "Blocage",
        "poei": "POEI", "session ft": "Marché FT", "marche ft": "Marché FT",
        "def mob": "Def MOB", "def mobilite": "Def MOB", "rdv programme": "RDV programmé",
        "prochain rdv inscription": "Prochain RDV inscription",
        "financement ft en cours": "Financement FT en cours",
        "financement ft refuse": "Financement FT refusé",
    }
    return aliases.get(value, "Nouveaux")


def _map_row(row: dict[str, Any]) -> dict[str, Any]:
    mapped = legacy.map_salesforce_row(row)
    source_formation = _row_value(row, "Type_de_formation__c", "Type de formation", "Formation")
    owner = _row_value(
        row, "OwnerName", "Lead Owner", "Lead Owner: Full Name",
        "Propriétaire de la piste", "Nom complet du propriétaire",
    )
    converted = _truthy(_row_value(row, "IsConverted", "Converti", "Est converti"))
    mapped.update({
        "formation": _normalized_formation(source_formation or mapped.get("formation")),
        "statut": _normalized_status(row),
        "statut_secondaire": _text(mapped.get("statut_secondaire")),
        "origine": _normalized_origin(row),
        "source": "salesforce_migration",
        "source_detail": _row_value(row, "LeadSource", "Source de la piste"),
        "commercial": owner,
        "cpf_montant": _row_value(row, "Montant_CPF__c", "Montant CPF", "CPF_Amount__c"),
        "statut_demande_financement_ft": _row_value(row, "Statut_financement_FT__c", "Statut financement FT"),
        "reste_a_charge_perso": _row_value(row, "Reste_a_charge__c", "Reste à charge"),
        "gclid": _row_value(row, "GCLID__c", "GCLID", "Google Click ID"),
        "utm_source": _row_value(row, "UTM_Source__c", "UTM Source"),
        "utm_medium": _row_value(row, "UTM_Medium__c", "UTM Medium"),
        "utm_campaign": _row_value(row, "UTM_Campaign__c", "UTM Campaign"),
        "converted_at": _iso(_row_value(row, "ConvertedDate", "Date de conversion")) if converted else "",
        "salesforce_is_converted": converted,
        "salesforce_owner": owner,
        "salesforce_owner_id": _row_value(row, "OwnerId", "ID du propriétaire"),
        "salesforce_company": _row_value(row, "Company", "Société"),
    })
    mapped.setdefault("received_at", mapped.get("created_at"))
    return mapped


def _name_key(contact: dict[str, Any]) -> str:
    return _fold(f"{contact.get('prenom', '')} {contact.get('nom', '')}")


def _compatible_names(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_name, right_name = _name_key(left), _name_key(right)
    return not left_name or not right_name or left_name == right_name


def _merge_source_rows(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if target.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
            target[key] = value
    source_comments, target_comments = _text(source.get("commentaires")), _text(target.get("commentaires"))
    if source_comments and source_comments not in target_comments:
        target["commentaires"] = f"{target_comments}\n\n{source_comments}" if target_comments else source_comments


def _deduplicate(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    if len(rows) < 2:
        for row in rows:
            row["salesforce_ids"] = [_text(row.get("salesforce_id"))] if row.get("salesforce_id") else []
        return rows, 0, 0
    parent = list(range(len(rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left

    seen: dict[tuple[str, str], int] = {}
    conflicts = 0
    for index, row in enumerate(rows):
        for kind, value in (
            ("salesforce", _text(row.get("salesforce_id"))),
            ("email", _email(row.get("mail"))),
            ("phone", _phone(row.get("telephone"))),
        ):
            if not value:
                continue
            previous = seen.get((kind, value))
            if previous is None:
                seen[(kind, value)] = index
            elif kind == "salesforce" or _compatible_names(rows[previous], row):
                union(previous, index)
            else:
                conflicts += 1

    groups: dict[int, list[dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        groups.setdefault(find(index), []).append(row)
    prepared: list[dict[str, Any]] = []
    for group in groups.values():
        ordered = sorted(
            group,
            key=lambda item: _parse_datetime(item.get("salesforce_last_modified_at"))
            or dt.datetime.min.replace(tzinfo=pytz.UTC),
            reverse=True,
        )
        winner = dict(ordered[0])
        ids: list[str] = []
        for item in ordered:
            sfid = _text(item.get("salesforce_id"))
            if sfid and sfid not in ids:
                ids.append(sfid)
            _merge_source_rows(winner, item)
        winner["salesforce_ids"] = ids
        prepared.append(winner)
    return prepared, len(rows) - len(prepared), conflicts


def _prepare_complete_rows(
    rows: Iterable[dict[str, Any]], *, include_converted: bool,
    deduplicate: bool, created_from: str = "", created_to: str = "",
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    start, end = _date_bound(created_from), _date_bound(created_to, end=True)
    if start and end and start > end:
        raise ValueError("La date de début doit précéder la date de fin.")
    prepared: list[dict[str, Any]] = []
    stats = Counter()
    for row in rows:
        if _truthy(_row_value(row, "IsDeleted", "Supprimé")):
            stats["skipped_deleted"] += 1
            continue
        created = _parse_datetime(_row_value(row, "CreatedDate", "Date de création"))
        if (start or end) and (
            not created or (start and created.astimezone(pytz.UTC) < start)
            or (end and created.astimezone(pytz.UTC) > end)
        ):
            stats["skipped_outside_date_range"] += 1
            continue
        mapped = _map_row(row)
        if not _text(mapped.get("nom")) and not _text(mapped.get("prenom")):
            stats["skipped_invalid"] += 1
            continue
        if not include_converted and mapped.get("statut") == "Converti":
            stats["skipped_converted"] += 1
            continue
        prepared.append(mapped)
    duplicates = conflicts = 0
    if deduplicate:
        prepared, duplicates, conflicts = _deduplicate(prepared)
    else:
        for row in prepared:
            row["salesforce_ids"] = [_text(row.get("salesforce_id"))] if row.get("salesforce_id") else []
    stats["duplicates_in_file"] = duplicates
    stats["duplicate_conflicts_in_file"] = conflicts
    return prepared, dict(stats)


def _append(index: dict[str, list[dict[str, Any]]], key: str, contact: dict[str, Any]) -> None:
    if key and all(candidate is not contact for candidate in index.setdefault(key, [])):
        index[key].append(contact)


def _indexes(contacts: list[dict[str, Any]]):
    by_sf: dict[str, list[dict[str, Any]]] = {}
    by_email: dict[str, list[dict[str, Any]]] = {}
    by_phone: dict[str, list[dict[str, Any]]] = {}
    for contact in contacts:
        ids = list(contact.get("salesforce_ids") or [])
        if contact.get("salesforce_id") and contact["salesforce_id"] not in ids:
            ids.append(contact["salesforce_id"])
        for sfid in ids:
            _append(by_sf, _text(sfid), contact)
        _append(by_email, _email(contact.get("mail")), contact)
        _append(by_phone, _phone(contact.get("telephone")), contact)
    return by_sf, by_email, by_phone


def _unique(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items:
        if all(candidate is not item for candidate in result):
            result.append(item)
    return result


def _match(row, by_sf, by_email, by_phone, *, deduplicate: bool):
    ids = list(row.get("salesforce_ids") or [])
    if row.get("salesforce_id") and row["salesforce_id"] not in ids:
        ids.insert(0, row["salesforce_id"])
    candidates = _unique(item for sfid in ids for item in by_sf.get(_text(sfid), []))
    if len(candidates) == 1:
        return candidates[0], "salesforce", ""
    if len(candidates) > 1:
        return None, "", "Plusieurs fiches CRM portent le même identifiant Salesforce."
    if not deduplicate:
        return None, "", ""
    email, phone = _email(row.get("mail")), _phone(row.get("telephone"))
    emails = _unique(by_email.get(email, [])) if email else []
    phones = _unique(by_phone.get(phone, [])) if phone else []
    if len(emails) > 1:
        return None, "", "Plusieurs fiches CRM utilisent la même adresse e-mail."
    if len(phones) > 1:
        return None, "", "Plusieurs fiches CRM utilisent le même téléphone."
    if emails and phones:
        return (emails[0], "email+phone", "") if emails[0] is phones[0] else (
            None, "", "L’e-mail et le téléphone correspondent à deux fiches CRM différentes."
        )
    if emails:
        return emails[0], "email", ""
    if phones:
        return phones[0], "phone", ""
    return None, "", ""


def _merge(target: dict[str, Any], incoming: dict[str, Any], *, authoritative: bool) -> bool:
    changed = False
    protected = {"id", "activities", "relances", "created_at", "received_at", "commentaires", "salesforce_ids"}
    for key, value in incoming.items():
        if key in protected or value in (None, "", [], {}):
            continue
        if key.startswith("salesforce_") or authoritative or target.get(key) in (None, "", [], {}):
            if target.get(key) != value:
                target[key] = value
                changed = True
    incoming_created, target_created = _parse_datetime(incoming.get("created_at")), _parse_datetime(target.get("created_at"))
    if incoming_created and (not target_created or incoming_created < target_created):
        target["created_at"] = incoming_created.isoformat()
        target.setdefault("received_at", target["created_at"])
        changed = True
    comments, existing = _text(incoming.get("commentaires")), _text(target.get("commentaires"))
    if comments and comments not in existing:
        target["commentaires"] = f"{existing}\n\n--- Import Salesforce ---\n{comments}" if existing else comments
        changed = True
    return changed


def _activity(contact: dict[str, Any], title: str, detail: str, now: str) -> None:
    contact.setdefault("activities", []).insert(0, {
        "id": str(uuid.uuid4()), "kind": "import", "title": title,
        "detail": detail, "preview": "", "date": now,
        "author": "Migration Salesforce",
    })


def import_complete_rows(
    contacts: list[dict[str, Any]], rows: list[dict[str, Any]], *,
    include_converted: bool = True, deduplicate: bool = True,
    dry_run: bool = False, created_from: str = "", created_to: str = "",
    merge_policy: str = MERGE_POLICY_SAFE,
) -> dict[str, Any]:
    if merge_policy not in {MERGE_POLICY_SAFE, MERGE_POLICY_SALESFORCE}:
        raise ValueError("Politique de fusion Salesforce invalide.")
    if dry_run:
        contacts = copy.deepcopy(contacts)
    now, batch_id = dt.datetime.now(pytz.timezone("Europe/Paris")).isoformat(), str(uuid.uuid4())
    prepared, stats = _prepare_complete_rows(
        rows, include_converted=include_converted, deduplicate=deduplicate,
        created_from=created_from, created_to=created_to,
    )
    by_sf, by_email, by_phone = _indexes(contacts)
    created_contacts: list[dict[str, Any]] = []
    counts = Counter()
    statuses, formations, sources, owners, years = Counter(), Counter(), Counter(), Counter(), Counter()
    ambiguous_samples: list[dict[str, str]] = []

    for incoming in prepared:
        status = _text(incoming.get("statut")) or "Nouveaux"
        statuses[status] += 1
        formations[_text(incoming.get("formation")) or "Non renseignée"] += 1
        sources[_text(incoming.get("origine")) or "Non renseignée"] += 1
        owners[_text(incoming.get("salesforce_owner")) or "Non renseigné"] += 1
        source_date = _parse_datetime(incoming.get("salesforce_created_at"))
        years[str(source_date.year) if source_date else "Date inconnue"] += 1
        email, phone = _email(incoming.get("mail")), _phone(incoming.get("telephone"))
        counts["missing_email"] += int(not email)
        counts["missing_phone"] += int(not phone)
        counts["missing_email_and_phone"] += int(not email and not phone)

        contact, method, reason = _match(
            incoming, by_sf, by_email, by_phone, deduplicate=deduplicate,
        )
        if reason:
            counts["ambiguous"] += 1
            if len(ambiguous_samples) < 20:
                ambiguous_samples.append({
                    "salesforce_id": _text(incoming.get("salesforce_id")),
                    "nom": " ".join(filter(None, (_text(incoming.get("prenom")), _text(incoming.get("nom"))))),
                    "raison": reason,
                })
            continue

        ids = list(incoming.get("salesforce_ids") or [])
        sfid = _text(incoming.get("salesforce_id"))
        if sfid and sfid not in ids:
            ids.insert(0, sfid)
        if contact:
            counts[f"matched_{method.replace('+', '_')}"] += 1
            changed = _merge(
                contact, incoming,
                authoritative=merge_policy == MERGE_POLICY_SALESFORCE,
            )
            imported_ids = contact.setdefault("salesforce_ids", [])
            for item in ids:
                if item and item not in imported_ids:
                    imported_ids.append(item)
                    _append(by_sf, item, contact)
                    changed = True
            if sfid and not contact.get("salesforce_id"):
                contact["salesforce_id"] = sfid
                changed = True
            if changed:
                counts["updated"] += 1
                if not dry_run:
                    contact.update({
                        "salesforce_import_batch_id": batch_id,
                        "salesforce_imported_at": now,
                        "updated_at": now,
                    })
                    _activity(
                        contact, "Piste rapprochée avec Salesforce",
                        f"Correspondance par {method}. Fusion : {'Salesforce prioritaire' if merge_policy == MERGE_POLICY_SALESFORCE else 'données CRM protégées'}.",
                        now,
                    )
            else:
                counts["unchanged"] += 1
            continue

        counts["created"] += 1
        new_contact = {
            "id": str(uuid.uuid4()), **incoming, "activities": [], "relances": [],
            "salesforce_ids": ids, "salesforce_import_batch_id": batch_id,
            "salesforce_imported_at": now, "updated_at": now,
        }
        _activity(new_contact, "Piste migrée depuis Salesforce", f"Statut Salesforce : {incoming.get('salesforce_status') or 'non renseigné'}.", now)
        created_contacts.append(new_contact)
        for item in ids:
            _append(by_sf, item, new_contact)
        _append(by_email, email, new_contact)
        _append(by_phone, phone, new_contact)

    if not dry_run and created_contacts:
        created_contacts.sort(key=lambda item: _text(item.get("created_at")), reverse=True)
        contacts[:0] = created_contacts

    result = {
        "ok": True, "dry_run": dry_run, "mode": IMPORT_MODE_COMPLETE,
        "merge_policy": merge_policy, "batch_id": batch_id,
        "csv_rows": len(rows), "prepared_rows": len(prepared),
        "created": counts["created"], "updated": counts["updated"],
        "unchanged": counts["unchanged"], "ambiguous": counts["ambiguous"],
        "ambiguous_samples": ambiguous_samples,
        "missing_email": counts["missing_email"], "missing_phone": counts["missing_phone"],
        "missing_email_and_phone": counts["missing_email_and_phone"],
        "matched_salesforce": counts["matched_salesforce"],
        "matched_email": counts["matched_email"], "matched_phone": counts["matched_phone"],
        "matched_email_phone": counts["matched_email_phone"],
        "status_counts": dict(statuses.most_common()),
        "formation_counts": dict(formations.most_common()),
        "source_counts": dict(sources.most_common()),
        "owner_counts": dict(owners.most_common()),
        "year_counts": dict(years.most_common()),
        "new_status_source_counts": {},
        "skipped_other_year": 0, "skipped_formation": 0,
        **stats,
    }
    for key in (
        "skipped_deleted", "skipped_converted", "skipped_outside_date_range",
        "skipped_invalid", "duplicates_in_file", "duplicate_conflicts_in_file",
    ):
        result.setdefault(key, 0)
    return result


def _contacts_signature(contacts: list[dict[str, Any]]) -> str:
    payload = [
        (_text(contact.get("id")), _text(contact.get("updated_at")), _text(contact.get("salesforce_id")))
        for contact in sorted(contacts, key=lambda item: _text(item.get("id")))
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def _preview_token(raw: bytes, options: dict[str, Any], contacts: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256(raw)
    digest.update(json.dumps(options, sort_keys=True, ensure_ascii=False).encode())
    digest.update(_contacts_signature(contacts).encode())
    return digest.hexdigest()


def register_salesforce_migration(
    app, *, current_user_fn, load_data_fn, login_required_fn, save_data_fn,
) -> None:
    if "crm_migrate_salesforce" in app.view_functions:
        return
    from flask import jsonify, request

    @app.post("/api/crm/migrate-salesforce", endpoint="crm_migrate_salesforce")
    @login_required_fn
    def crm_migrate_salesforce():
        if (current_user_fn() or {}).get("role") != "admin":
            return jsonify({"error": "Seul un administrateur peut migrer les pistes Salesforce."}), 403
        upload = request.files.get("file")
        if not upload or not upload.filename:
            return jsonify({"error": "Sélectionnez le fichier CSV exporté depuis Salesforce."}), 400
        mode = _text(request.form.get("mode")) or IMPORT_MODE_COMPLETE
        merge_policy = _text(request.form.get("merge_policy")) or MERGE_POLICY_SAFE
        include_converted = _text(request.form.get("include_converted", "1")) != "0"
        deduplicate = _text(request.form.get("deduplicate", "1")) != "0"
        created_from, created_to = _text(request.form.get("created_from")), _text(request.form.get("created_to"))
        dry_run = _text(request.form.get("dry_run", "0")) == "1"
        supplied_token = _text(request.form.get("preview_token"))
        options = {
            "mode": mode, "merge_policy": merge_policy,
            "include_converted": include_converted, "deduplicate": deduplicate,
            "created_from": created_from, "created_to": created_to,
        }
        try:
            raw = upload.read(legacy.MAX_CSV_BYTES + 1)
            rows = parse_compatible_csv(raw, max_csv_bytes=legacy.MAX_CSV_BYTES)
            lock = _IMPORT_LOCK if not dry_run else threading.Lock()
            with lock:
                data = load_data_fn()
                contacts = data.setdefault("crm_contacts", [])
                token = _preview_token(raw, options, contacts)
                if not dry_run and mode == IMPORT_MODE_COMPLETE:
                    if not supplied_token:
                        return jsonify({"error": "Un aperçu doit être validé avant la migration complète."}), 409
                    if supplied_token != token:
                        return jsonify({"error": "Le fichier, les options ou le CRM ont changé depuis l’aperçu. Relancez l’analyse."}), 409
                if mode == IMPORT_MODE_LEGACY:
                    result = legacy.import_salesforce_rows(
                        contacts, rows, include_converted=include_converted,
                        deduplicate=deduplicate, dry_run=dry_run,
                    )
                    result.update({"mode": mode, "merge_policy": "legacy"})
                elif mode == IMPORT_MODE_COMPLETE:
                    result = import_complete_rows(
                        contacts, rows, include_converted=include_converted,
                        deduplicate=deduplicate, dry_run=dry_run,
                        created_from=created_from, created_to=created_to,
                        merge_policy=merge_policy,
                    )
                else:
                    raise ValueError("Mode d’import Salesforce invalide.")
                result.update({
                    "preview_token": token, "filename": upload.filename,
                    "contacts_before": len(contacts) if dry_run else len(contacts) - result["created"],
                    "contacts_after": len(contacts) if not dry_run else len(contacts) + result["created"],
                })
                if not dry_run:
                    summary = {
                        "date": dt.datetime.now(pytz.timezone("Europe/Paris")).isoformat(),
                        "batch_id": result.get("batch_id"), "filename": upload.filename,
                        "mode": mode, "merge_policy": result.get("merge_policy"),
                        **{key: result.get(key, 0) for key in ("csv_rows", "prepared_rows", "created", "updated", "unchanged", "ambiguous")},
                    }
                    data["crm_salesforce_last_import"] = summary
                    history = data.setdefault("crm_salesforce_import_history", [])
                    history.insert(0, summary)
                    del history[20:]
                    save_data_fn(data)
                return jsonify(result)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # pragma: no cover
            app.logger.exception("Erreur migration Salesforce")
            return jsonify({"error": f"La migration Salesforce a échoué : {exc}"}), 500
