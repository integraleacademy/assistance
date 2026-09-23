"""Fill missing secretary answers without replaying deliveries or changing a lead."""

import copy
from collections import defaultdict

VERSION = "2026-09-23-v1"
REPAIR_KEY = "crm_secretariat_answers_repair"
FIELDS = (
    "cpf", "cpf_montant", "financement_ft", "financement_perso_possible",
    "refus_ft_perso", "reste_a_charge_perso", "identite_creation",
    "carte_pro", "garde_vue", "antecedents", "titre_sejour",
    "dates_formation", "desp_type",
)
ALIASES = (
    ("financement_perso_possible", "refus_ft_perso"),
    ("garde_vue", "antecedents"),
)


def apply_answers(contact, values, *, is_empty, normalize_amount):
    """Only explicitly submitted values may fill empty, compatible fields."""
    changes, conflicts = {}, []
    for field in ("formation", "desp_type"):
        current, incoming = contact.get(field), values.get(field)
        if (not is_empty(current) and not is_empty(incoming)
                and str(current).strip().casefold() != str(incoming).strip().casefold()):
            return {}, [field]

    def equivalent(field, first, second):
        if field == "cpf_montant":
            try:
                return normalize_amount(first) == normalize_amount(second)
            except ValueError:
                return False
        return str(first).strip().casefold() == str(second).strip().casefold()

    def effective(field):
        current = contact.get(field)
        return str(values.get(field) if is_empty(current) else current).strip().upper()

    for field in FIELDS:
        incoming = values.get(field)
        if is_empty(incoming):
            continue
        aliases = next((group for group in ALIASES if field in group), (field,))
        existing = [contact.get(key) for key in aliases if not is_empty(contact.get(key))]
        if any(not equivalent(field, value, incoming) for value in existing):
            conflicts.append(field)
            continue
        if not is_empty(contact.get(field)):
            continue
        # Do not attach a balance to an unconsulted CPF, or dependent answers
        # to a conflicting qualification already entered by a counsellor.
        if field == "cpf_montant":
            if (effective("cpf") != "OUI" or
                    (not is_empty(values.get("cpf")) and str(values["cpf"]).strip().upper() != "OUI")):
                continue
        if field in ("financement_perso_possible", "refus_ft_perso"):
            if (effective("financement_ft") != "OUI" or
                    (not is_empty(values.get("financement_ft")) and str(values["financement_ft"]).strip().upper() != "OUI")):
                continue
        if field in ("garde_vue", "antecedents", "titre_sejour"):
            if effective("carte_pro") == "OUI" or str(values.get("carte_pro") or "").strip().upper() == "OUI":
                continue
        contact[field] = incoming
        changes[field] = incoming
    return changes, conflicts


def recover_answers(data, *, map_answers, is_empty, normalize_amount,
                    matches_contact, now, dry_run=True):
    """Recover only formation requests with one durable, corroborated CRM link.

    No name-only or coordinate-only search, quote defaults, or callback request
    can authorize a recovery. The default is an in-memory dry run.
    """
    contacts = {str(c.get("id")): c for c in data.get("crm_contacts", [])
                if isinstance(c, dict) and c.get("id")}
    if dry_run:
        contacts = copy.deepcopy(contacts)
    links, submissions = defaultdict(set), {}
    blocked = set()

    def link(request_id, contact_id):
        if request_id and str(contact_id) in contacts:
            links[str(request_id)].add(str(contact_id))

    for row in data.get("crm_inbound_requests", []):
        if not isinstance(row, dict) or row.get("source") != "assistant-secretariat":
            continue
        request_id = str(row.get("external_id") or "")
        raw = row.get("raw_payload")
        if not request_id or not isinstance(raw, dict):
            continue
        submissions.setdefault(request_id, {
            "created_at": row.get("created_at", ""), **raw, "id": request_id,
        })
        if row.get("status") == "pending_review":
            blocked.add(request_id)
        else:
            link(request_id, row.get("contact_id"))
    for row in data.get("secretariat_demandes", []):
        if not isinstance(row, dict) or not row.get("id"):
            continue
        request_id = str(row["id"])
        # The last saved secretary entry takes precedence, including its blanks.
        submissions[request_id] = {**submissions.get(request_id, {}), **row}
        link(request_id, row.get("crm_contact_id"))
    for contact_id, contact in contacts.items():
        link(contact.get("source_secretariat_id"), contact_id)
        for publication in contact.get("publications", []):
            if isinstance(publication, dict) and publication.get("source") == "assistant-secretariat":
                link(publication.get("source_secretariat_id"), contact_id)

    report = {"version": VERSION, "contacts": 0, "fields": 0,
              "recovered": [], "conflicts": [], "skipped": []}
    touched = set()
    entries = sorted(submissions.values(), key=lambda r: str(r.get("created_at") or ""), reverse=True)
    for entry in entries:
        if entry.get("type") != "formation":
            continue
        request_id = str(entry["id"])
        candidates = links[request_id]
        if request_id in blocked or len(candidates) != 1:
            report["skipped"].append({"request_id": request_id, "reason": "ambiguous_or_missing_link"})
            continue
        contact = contacts[next(iter(candidates))]
        if not matches_contact(contact, entry):
            report["skipped"].append({"request_id": request_id, "reason": "identity_or_coordinates_changed"})
            continue
        try:
            values = map_answers(entry)
        except (ValueError, TypeError):
            report["skipped"].append({"request_id": request_id, "reason": "invalid_answers"})
            continue
        changes, conflicts = apply_answers(contact, values, is_empty=is_empty, normalize_amount=normalize_amount)
        reference = {"request_id": request_id, "contact_id": str(contact["id"])}
        if conflicts:
            report["conflicts"].append({**reference, "fields": conflicts})
        if not changes:
            continue
        contact["updated_at"] = now
        contact.setdefault("activities", []).insert(0, {
            "id": "secretariat-answers-repair-" + request_id,
            "date": now, "kind": "note", "author": "Automatisation CRM",
            "title": "Réponses du secrétariat récupérées",
            "detail": "Champs complétés : " + ", ".join(changes),
            "source_secretariat_id": request_id,
        })
        touched.add(str(contact["id"]))
        report["fields"] += len(changes)
        report["recovered"].append({**reference, "fields": list(changes)})
    report["contacts"] = len(touched)
    return report
