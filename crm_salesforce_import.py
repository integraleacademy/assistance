"""Import en masse des pistes Salesforce dans Intégrale Connect CRM.

Ce module est enregistré depuis ``crm_app.py`` afin de ne pas alourdir le
fichier monolithique ``app.py``. L'import est réservé aux administrateurs et
n'écrit le fichier de données qu'une seule fois par import.
"""

from __future__ import annotations

import copy
import csv
import datetime as dt
import io
import re
import unicodedata
import uuid
from collections import Counter
from typing import Any, Iterable

import pytz

MAX_CSV_BYTES = 20 * 1024 * 1024
SALESFORCE_IMPORT_YEAR = 2025
EXCLUDED_SALESFORCE_FORMATIONS = {
    "afc",
    "aps + ssiap",
    "bts",
    "bts ci",
    "bts mco",
    "bts mos",
    "bts mos 2025",
    "bts mos 2026",
    "bts ndrc",
    "bts pi",
    "bts pi a distance 2026",
    "cap aepe",
    "cap boulangerie",
    "cap coiffure",
    "cap cuisine",
    "cap patisserie",
}
REQUIRED_COLUMNS = {"Id", "FirstName", "LastName"}
HEADER_ALIASES = {
    "id": "Id",
    "lead id": "Id",
    "record id": "Id",
    "identifiant de la piste": "Id",
    "identifiant piste": "Id",
    "firstname": "FirstName",
    "first name": "FirstName",
    "prenom": "FirstName",
    "lastname": "LastName",
    "last name": "LastName",
    "nom": "LastName",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _fold(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", _text(value))
    return " ".join(normalized.encode("ascii", "ignore").decode().casefold().split())


def _email(value: Any) -> str:
    return _text(value).casefold()


def _phone(value: Any) -> str:
    digits = re.sub(r"\D", "", _text(value))
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10 and digits.startswith("0"):
        digits = f"33{digits[1:]}"
    return digits


def _yes_no(value: Any) -> str:
    folded = _fold(value)
    if folded in {"oui", "yes", "true", "1", "y", "o"}:
        return "OUI"
    if folded in {"non", "no", "false", "0", "n"}:
        return "NON"
    if "financement personnel ok" in folded:
        return "OUI"
    if "pas de possibilite" in folded or "financement personnel non" in folded:
        return "NON"
    return ""


def _iso_datetime(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    candidate = raw.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = pytz.UTC.localize(parsed)
    return parsed.isoformat()


def _status(row: dict[str, str]) -> str:
    if _yes_no(row.get("IsConverted")) == "OUI":
        return "Converti"
    value = _fold(row.get("Status"))
    aliases = {
        "qualified": "Converti",
        "unqualified": "Disqualifié",
        "open - not contacted": "Nouveaux",
        "open not contacted": "Nouveaux",
        "new": "Nouveaux",
        "nouveau": "Nouveaux",
        "nouveaux": "Nouveaux",
        "blocage": "Blocage",
        "poei": "POEI",
        "session ft": "Session FT",
        "def mob": "Def MOB",
        "def mobilite": "Def MOB",
        "rdv programme": "RDV programmé",
        "prochain rdv inscription": "Prochain RDV inscription",
        "financement ft en cours": "Financement FT en cours",
        "financement ft refuse": "Financement FT refusé",
        "a relancer": "A relancer",
        "disqualifie": "Disqualifié",
        "converti": "Converti",
    }
    return aliases.get(value, "Nouveaux")


def _formation(row: dict[str, str]) -> str:
    raw = _text(row.get("Type_de_formation__c")) or _text(row.get("Company"))
    folded = _fold(raw)
    if not folded or folded in {
        "company placeholder",
        "particulier",
        "integrale securite formations",
        "integrale academy",
    }:
        return ""
    aliases = {
        "apr": "A3P",
        "a3p": "A3P",
        "aps": "APS",
        "dirigeant": "DESP",
        "desp": "DESP",
        "desp initial": "DESP",
        "desp vae": "DESP",
        "chauffeur vtc": "Chauffeur VTC",
        "vtc": "Chauffeur VTC",
        "ssiap": "SSIAP 1",
        "ssiap 1": "SSIAP 1",
        "poei": "POEI",
        "bts": "BTS",
        "bts mos": "BTS MOS",
        "bts mco": "BTS MCO",
        "bts ndrc": "BTS NDRC",
        "bts ci": "BTS CI",
        "bts pi": "BTS PI",
    }
    return aliases.get(folded, raw)


def _salesforce_formation(row: dict[str, str]) -> str:
    """Retourne le libellé source utilisé pour appliquer les exclusions métier."""
    return _text(row.get("Type_de_formation__c")) or _text(row.get("Company"))


def _is_import_year(value: Any) -> bool:
    raw = _text(value)
    if not raw:
        return False
    candidate = raw.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(candidate).year == SALESFORCE_IMPORT_YEAR
    except ValueError:
        # Certains exports Excel présentent la date au format français.
        return bool(re.search(rf"(?:^|\D){SALESFORCE_IMPORT_YEAR}(?:\D|$)", raw))


def _desp_type(value: Any) -> str:
    folded = _fold(value)
    if "vae" in folded:
        return "VAE"
    if "initial" in folded:
        return "INITIAL"
    return ""


def _origin(row: dict[str, str]) -> str:
    raw = _text(row.get("Origine__c")) or _text(row.get("LeadSource"))
    folded = _fold(raw)
    aliases = {
        "compte cpf": "CPF",
        "cpf": "CPF",
        "france travail": "FT",
        "ft": "FT",
        "website": "Site internet",
        "calendly": "Calendly",
        "google": "Google",
        "poei": "POEI",
        "reseaux sociaux": "Réseaux sociaux",
        "bouche a oreille": "Bouche à oreille",
    }
    return aliases.get(folded, raw)


def _comments(row: dict[str, str]) -> str:
    parts: list[str] = []
    for label, column in (
        ("Informations complémentaires", "Infos_compl_mentaires__c"),
        ("Description Salesforce", "Description"),
    ):
        value = _text(row.get(column))
        if value:
            parts.append(f"{label} :\n{value}")
    return "\n\n".join(parts)


def map_salesforce_row(row: dict[str, str], now: str | None = None) -> dict[str, Any]:
    """Transforme une ligne Salesforce en contact CRM."""
    now = now or dt.datetime.now(pytz.timezone("Europe/Paris")).isoformat()
    phone = _text(row.get("MobilePhone")) or _text(row.get("Phone"))
    created_at = _iso_datetime(row.get("CreatedDate")) or now
    updated_at = _iso_datetime(row.get("LastModifiedDate")) or created_at
    contact = {
        "prenom": _text(row.get("FirstName")),
        "nom": _text(row.get("LastName")),
        "telephone": phone,
        "mail": _text(row.get("Email")),
        "formation": _formation(row),
        "lieu": _text(row.get("Lieu__c")),
        "statut": _status(row),
        "dates_formation": _text(row.get("Dates_souhait_es__c")),
        "cpf": _yes_no(row.get("Compte_CPF__c")),
        "carte_pro": _yes_no(row.get("Carte_prof__c")),
        "antecedents": _yes_no(row.get("Ant_c_dents__c")),
        "desp_type": _desp_type(row.get("CHOIX_DIRIGEANT_DESP__c")),
        "identite_creation": _yes_no(row.get("Cr_ation_identit_num_rique__c")),
        "identite_ok": _yes_no(row.get("Identit_num_rique_fonctionnelle__c")),
        "financement_ft": _yes_no(row.get("Souhaite_demande_financement_FT__c")),
        "refus_ft_perso": (
            _yes_no(row.get("Financement_personnel__c"))
            or _yes_no(row.get("Si_refus_France_Travail__c"))
        ),
        "origine": _origin(row),
        "inscrit_ft": _yes_no(row.get("Inscrit_France_Travail__c")),
        "commentaires": _comments(row),
        "relance_date": "",
        "created_at": created_at,
        "updated_at": updated_at,
        "salesforce_id": _text(row.get("Id")),
        "salesforce_status": _text(row.get("Status")),
        "salesforce_is_converted": _text(row.get("IsConverted")) == "1",
        "salesforce_created_at": _text(row.get("CreatedDate")),
        "salesforce_last_modified_at": _text(row.get("LastModifiedDate")),
        "salesforce_lead_source": _text(row.get("LeadSource")),
    }
    return contact


def _decode_csv(raw: bytes) -> str:
    errors: list[str] = []
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")
    raise ValueError("Encodage CSV illisible : " + " | ".join(errors[:2]))


def parse_salesforce_csv(raw: bytes) -> list[dict[str, str]]:
    if not raw:
        raise ValueError("Le fichier CSV est vide.")
    if len(raw) > MAX_CSV_BYTES:
        raise ValueError("Le fichier dépasse la limite de 20 Mo.")
    text = _decode_csv(raw)
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text, newline=""), dialect=dialect)
    original_columns = reader.fieldnames or []
    column_names = {
        column: HEADER_ALIASES.get(_fold(column), _text(column))
        for column in original_columns
        if column is not None
    }
    columns = set(column_names.values())
    missing = sorted(REQUIRED_COLUMNS - columns)
    if missing:
        raise ValueError(
            "Colonnes Salesforce manquantes : "
            + ", ".join(missing)
            + ". Vérifiez que le fichier est un export CSV de pistes Salesforce "
            "(séparateur virgule, point-virgule ou tabulation)."
        )
    return [
        {column_names.get(key, _text(key)): _text(value) for key, value in row.items()}
        for row in reader
        if isinstance(row, dict) and any(_text(value) for value in row.values())
    ]


def _merge_non_empty(target: dict[str, Any], incoming: dict[str, Any], *, authoritative: bool) -> bool:
    changed = False
    protected_when_matched = {"commentaires", "created_at", "activities"}
    for key, value in incoming.items():
        if key in protected_when_matched or value in (None, "", [], {}):
            continue
        if authoritative or not _text(target.get(key)):
            if target.get(key) != value:
                target[key] = value
                changed = True
    incoming_comments = _text(incoming.get("commentaires"))
    if incoming_comments:
        existing = _text(target.get("commentaires"))
        marker = "--- Import Salesforce ---"
        if incoming_comments not in existing:
            target["commentaires"] = (
                f"{existing}\n\n{marker}\n{incoming_comments}" if existing else incoming_comments
            )
            changed = True
    return changed


def _activity(contact: dict[str, Any], title: str, detail: str, now: str) -> None:
    contact.setdefault("activities", []).insert(
        0,
        {
            "id": str(uuid.uuid4()),
            "kind": "import",
            "title": title,
            "detail": detail,
            "preview": "",
            "date": now,
            "author": "Import Salesforce",
        },
    )


def _prepare_rows(
    rows: Iterable[dict[str, str]],
    *,
    include_converted: bool,
    deduplicate: bool,
) -> tuple[list[dict[str, Any]], int, int, int, int, int]:
    mapped_rows: list[dict[str, Any]] = []
    skipped_deleted = 0
    skipped_converted = 0
    skipped_other_year = 0
    skipped_formation = 0
    for row in rows:
        if _text(row.get("IsDeleted")) == "1":
            skipped_deleted += 1
            continue
        if not _is_import_year(row.get("CreatedDate")):
            skipped_other_year += 1
            continue
        if _fold(_salesforce_formation(row)) in EXCLUDED_SALESFORCE_FORMATIONS:
            skipped_formation += 1
            continue
        mapped = map_salesforce_row(row)
        if not include_converted and mapped.get("statut") == "Converti":
            skipped_converted += 1
            continue
        mapped_rows.append(mapped)

    if not deduplicate or len(mapped_rows) < 2:
        for mapped in mapped_rows:
            sfid = _text(mapped.get("salesforce_id"))
            mapped["salesforce_ids"] = [sfid] if sfid else []
        return (
            mapped_rows,
            skipped_deleted,
            skipped_converted,
            skipped_other_year,
            skipped_formation,
            0,
        )

    parent = list(range(len(mapped_rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    seen_email: dict[str, int] = {}
    seen_phone: dict[str, int] = {}
    for index, mapped in enumerate(mapped_rows):
        email = _email(mapped.get("mail"))
        phone = _phone(mapped.get("telephone"))
        if email:
            if email in seen_email:
                union(index, seen_email[email])
            else:
                seen_email[email] = index
        if phone:
            if phone in seen_phone:
                union(index, seen_phone[phone])
            else:
                seen_phone[phone] = index

    groups: dict[int, list[dict[str, Any]]] = {}
    for index, mapped in enumerate(mapped_rows):
        groups.setdefault(find(index), []).append(mapped)

    prepared: list[dict[str, Any]] = []
    for group in groups.values():
        ordered = sorted(
            group,
            key=lambda item: _text(item.get("salesforce_last_modified_at")),
            reverse=True,
        )
        winner = dict(ordered[0])
        salesforce_ids: list[str] = []
        for item in ordered:
            sfid = _text(item.get("salesforce_id"))
            if sfid and sfid not in salesforce_ids:
                salesforce_ids.append(sfid)
            for key, value in item.items():
                if winner.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
                    winner[key] = value
        winner["salesforce_ids"] = salesforce_ids
        prepared.append(winner)

    return (
        prepared,
        skipped_deleted,
        skipped_converted,
        skipped_other_year,
        skipped_formation,
        len(mapped_rows) - len(prepared),
    )


def _indexes(contacts: list[dict[str, Any]]) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    by_sf: dict[str, dict[str, Any]] = {}
    by_email: dict[str, dict[str, Any]] = {}
    by_phone: dict[str, dict[str, Any]] = {}
    for contact in contacts:
        salesforce_ids = list(contact.get("salesforce_ids") or [])
        primary = _text(contact.get("salesforce_id"))
        if primary and primary not in salesforce_ids:
            salesforce_ids.append(primary)
        email = _email(contact.get("mail"))
        phone = _phone(contact.get("telephone"))
        for sfid in salesforce_ids:
            if _text(sfid):
                by_sf[_text(sfid)] = contact
        if email:
            by_email.setdefault(email, contact)
        if phone:
            by_phone.setdefault(phone, contact)
    return by_sf, by_email, by_phone


def import_salesforce_rows(
    contacts: list[dict[str, Any]],
    rows: list[dict[str, str]],
    *,
    include_converted: bool = True,
    deduplicate: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    now = dt.datetime.now(pytz.timezone("Europe/Paris")).isoformat()
    if dry_run:
        contacts = copy.deepcopy(contacts)
    (
        prepared,
        skipped_deleted,
        skipped_converted,
        skipped_other_year,
        skipped_formation,
        duplicates_in_file,
    ) = _prepare_rows(rows, include_converted=include_converted, deduplicate=deduplicate)
    by_sf, by_email, by_phone = _indexes(contacts)
    created_contacts: list[dict[str, Any]] = []
    created = updated = unchanged = matched_email = matched_phone = matched_sf = 0
    statuses: Counter[str] = Counter()
    formations: Counter[str] = Counter()
    new_status_sources: Counter[str] = Counter()

    for incoming in prepared:
        status = _text(incoming.get("statut")) or "Nouveaux"
        statuses[status] += 1
        if status == "Nouveaux":
            # Le CRM classe par défaut les statuts Salesforce vides ou inconnus
            # dans « Nouveaux ». Exposer leur libellé source permet d'expliquer
            # le total de l'aperçu avant que l'import soit lancé.
            source_status = _text(incoming.get("salesforce_status")) or "Non renseigné"
            new_status_sources[source_status] += 1
        formations[_text(incoming.get("formation")) or "Non renseignée"] += 1
        incoming_ids = list(incoming.get("salesforce_ids") or [])
        sfid = _text(incoming.get("salesforce_id"))
        if sfid and sfid not in incoming_ids:
            incoming_ids.insert(0, sfid)
        email = _email(incoming.get("mail"))
        phone = _phone(incoming.get("telephone"))
        contact = next((by_sf.get(item) for item in incoming_ids if by_sf.get(item)), None)
        match_method = "salesforce" if contact else ""
        if not contact and deduplicate and email:
            contact = by_email.get(email)
            match_method = "email" if contact else ""
        if not contact and deduplicate and phone:
            contact = by_phone.get(phone)
            match_method = "phone" if contact else ""

        if contact:
            if match_method == "salesforce":
                matched_sf += 1
            elif match_method == "email":
                matched_email += 1
            elif match_method == "phone":
                matched_phone += 1
            changed = _merge_non_empty(
                contact,
                incoming,
                authoritative=match_method == "salesforce",
            )
            imported_ids = contact.setdefault("salesforce_ids", [])
            for incoming_id in incoming_ids:
                if incoming_id and incoming_id not in imported_ids:
                    imported_ids.append(incoming_id)
                    changed = True
                    by_sf[incoming_id] = contact
            if not contact.get("salesforce_id") and sfid:
                contact["salesforce_id"] = sfid
                changed = True
            if changed:
                updated += 1
                if not dry_run:
                    contact["salesforce_imported_at"] = now
                    contact["updated_at"] = now
                    _activity(
                        contact,
                        "Piste mise à jour depuis Salesforce",
                        f"Correspondance par {match_method}. Statut Salesforce : {incoming.get('salesforce_status') or 'non renseigné'}.",
                        now,
                    )
            else:
                unchanged += 1
            continue

        created += 1
        new_contact = {
            "id": str(uuid.uuid4()),
            **incoming,
            "activities": [],
            "salesforce_ids": incoming_ids,
            "salesforce_imported_at": now,
        }
        _activity(
            new_contact,
            "Piste importée depuis Salesforce",
            f"Statut Salesforce : {incoming.get('salesforce_status') or 'non renseigné'}.",
            now,
        )
        created_contacts.append(new_contact)
        for incoming_id in incoming_ids:
            if incoming_id:
                by_sf[incoming_id] = new_contact
        if email:
            by_email.setdefault(email, new_contact)
        if phone:
            by_phone.setdefault(phone, new_contact)

    if not dry_run and created_contacts:
        created_contacts.sort(key=lambda item: _text(item.get("created_at")), reverse=True)
        contacts[:0] = created_contacts

    return {
        "ok": True,
        "dry_run": dry_run,
        "csv_rows": len(rows),
        "prepared_rows": len(prepared),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "matched_salesforce": matched_sf,
        "matched_email": matched_email,
        "matched_phone": matched_phone,
        "duplicates_in_file": duplicates_in_file,
        "skipped_deleted": skipped_deleted,
        "skipped_converted": skipped_converted,
        "skipped_other_year": skipped_other_year,
        "skipped_formation": skipped_formation,
        "status_counts": dict(statuses.most_common()),
        "new_status_source_counts": dict(new_status_sources.most_common()),
        "formation_counts": dict(formations.most_common()),
    }


def register_salesforce_import(
    app,
    *,
    current_user_fn=None,
    load_data_fn=None,
    login_required_fn=None,
    save_data_fn=None,
) -> None:
    """Enregistre la route d'import sur l'application Flask existante."""
    if "crm_import_salesforce" in app.view_functions:
        return

    from flask import jsonify, request
    if any(
        dependency is None
        for dependency in (current_user_fn, load_data_fn, login_required_fn, save_data_fn)
    ):
        from app import current_user, load_data, login_required, save_data

        current_user_fn = current_user_fn or current_user
        load_data_fn = load_data_fn or load_data
        login_required_fn = login_required_fn or login_required
        save_data_fn = save_data_fn or save_data

    @app.route("/api/crm/import-salesforce", methods=["POST"], endpoint="crm_import_salesforce")
    @login_required_fn
    def crm_import_salesforce():
        user = current_user_fn() or {}
        if user.get("role") != "admin":
            return jsonify({"error": "Seul un administrateur peut importer des pistes Salesforce."}), 403

        upload = request.files.get("file")
        if not upload or not upload.filename:
            return jsonify({"error": "Sélectionnez le fichier CSV exporté depuis Salesforce."}), 400

        include_converted = _text(request.form.get("include_converted", "1")) != "0"
        deduplicate = _text(request.form.get("deduplicate", "1")) != "0"
        dry_run = _text(request.form.get("dry_run", "0")) == "1"

        try:
            rows = parse_salesforce_csv(upload.read(MAX_CSV_BYTES + 1))
            data = load_data_fn()
            contacts = data.setdefault("crm_contacts", [])
            result = import_salesforce_rows(
                contacts,
                rows,
                include_converted=include_converted,
                deduplicate=deduplicate,
                dry_run=dry_run,
            )
            if not dry_run:
                data["crm_salesforce_last_import"] = {
                    "date": dt.datetime.now(pytz.timezone("Europe/Paris")).isoformat(),
                    "filename": upload.filename,
                    **{
                        key: result[key]
                        for key in ("csv_rows", "prepared_rows", "created", "updated", "unchanged")
                    },
                }
                save_data_fn(data)
            return jsonify(result)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # pragma: no cover - journal de production
            app.logger.exception("Erreur import Salesforce")
            return jsonify({"error": f"L'import Salesforce a échoué : {exc}"}), 500
