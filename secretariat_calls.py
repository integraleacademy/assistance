"""Small projections used by the call desk and the CRM callback queue."""

import datetime

import pytz


PARIS = pytz.timezone("Europe/Paris")
FINANCING_FIELDS = {
    "cpf_consulte": "consultation du compte CPF",
    "cpf_montant": "solde CPF",
    "france_travail": "financement France Travail",
    "ft_refus_ok": "financement personnel si France Travail refuse",
    "financement_perso": "financement personnel du reste à charge",
    "identite_numerique": "Identité Numérique La Poste",
}


def collect_pending_financing(entry, payload):
    pending = []
    if entry.get("cpf_consulte") == "A_CONFIRMER":
        entry["cpf_montant"] = ""
    if entry.get("france_travail") == "A_CONFIRMER":
        entry["ft_refus_ok"] = ""
    for key, label in FINANCING_FIELDS.items():
        if key == "cpf_montant":
            unknown = entry["cpf_consulte"] == "OUI" and payload.get("cpf_montant_statut") == "a_confirmer"
        else:
            unknown = entry.get(key) == "A_CONFIRMER"
        if unknown:
            entry[key] = ""
            pending.append(label)
    entry["financement_a_confirmer"] = pending


def funding_note(entry):
    pending = entry.get("financement_a_confirmer") or []
    return "À confirmer avec le conseiller : " + ", ".join(pending) if pending else ""


def callback_deadline(value):
    """A datetime-local entry on the desk denotes French civil time."""
    if not value:
        return ""
    try:
        parsed = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M")
        return PARIS.localize(parsed, is_dst=None).isoformat(timespec="minutes")
    except (TypeError, ValueError, pytz.InvalidTimeError):
        raise ValueError("Choisissez une date et une heure de rappel valides (heure de Paris).") from None


def date_label(value):
    if not value:
        return ""
    try:
        if len(str(value)) == 10:
            return datetime.date.fromisoformat(value).strftime("%d/%m/%Y")
        when = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = PARIS.localize(when)
        return when.astimezone(PARIS).strftime("%d/%m/%Y à %H:%M")
    except (TypeError, ValueError, OverflowError):
        return str(value)


def desk_summary(contact, callbacks, appointment):
    entries = [c for c in callbacks if isinstance(c, dict) and contact.get("id")
               and c.get("crm_contact_id") == contact["id"]]
    exchanges = [a for a in contact.get("activities") or [] if isinstance(a, dict)
                 and a.get("kind") in {"appel", "email", "sms", "demande_rappel", "aircall", "email_recu"}
                 and a.get("date") and a.get("title")]
    exchanges.extend({"date": c["created_at"], "title": "Appel au secrétariat · " + c["notes"][:180]}
                     for c in entries if c.get("notes") and c.get("created_at"))
    def exchange_time(item):
        try:
            return datetime.datetime.fromisoformat(str(item["date"]).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OSError, OverflowError):
            return 0

    last = max(exchanges, key=exchange_time, default={})
    pending = [c for c in entries if c.get("type") == "autre" and
               str(c.get("callback_status") or "").casefold() not in {"processed", "traite", "traité"}]
    first = min(pending, key=lambda c: (not c.get("callback_due_at"), c.get("callback_due_at") or c.get("created_at") or ""), default={})
    callback = ""
    if first:
        callback = " · ".join(filter(None, [date_label(first.get("callback_due_at")) or "Échéance à préciser",
                              first.get("callback_recipient") or "Équipe", first.get("notes", "")[:160]]))
    reminders = [r.get("scheduled_date") for r in contact.get("relances") or []
                 if isinstance(r, dict) and r.get("status") == "scheduled" and r.get("scheduled_date")]
    if not reminders and contact.get("relance_date") and contact.get("statut") == "A relancer":
        reminders.append(contact["relance_date"])
    return {
        "formation": contact.get("formation") or "",
        "conseiller": contact.get("salesforce_owner") or "",
        "dernier_echange": " · ".join(filter(None, [date_label(last.get("date")), last.get("title")])),
        "rdv": " · ".join(filter(None, [date_label(appointment.get("start_time")), appointment.get("host_name")])) if appointment else "",
        "rappel": callback,
        "relance": date_label(min(reminders)) if reminders else "",
    }


def delivery_state(entry, channel):
    if (entry.get(f"{channel}_summary_sent_at") or entry.get(f"information_{channel}_sent_at")
            or entry.get(f"information_{channel}_template_id")):
        return "sent"
    return entry.get(f"{channel}_summary_status") or "not_attempted"
