"""Small projections used by the call desk and the CRM callback queue."""

import datetime
import re
import unicodedata

import pytz
from candidate_scoring import normalize_cnaps_tracking_status


PARIS = pytz.timezone("Europe/Paris")
FINANCING_FIELDS = {
    "cpf_consulte": "consultation du compte CPF",
    "cpf_montant": "solde CPF",
    "france_travail": "financement France Travail",
    "ft_refus_ok": "financement personnel si France Travail refuse",
    "financement_perso": "financement personnel du reste à charge",
    "identite_numerique": "Identité Numérique La Poste",
}

CNAPS_LABELS = {
    "registered": "Enregistré", "transmitted": "Transmis au CNAPS", "in_review": "En instruction",
    "accepted": "Accepté", "refused": "Refusé", "no_result": "Aucun dossier retrouvé",
}


def folded(value):
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return " ".join(re.findall(r"[^\W_]+", "".join(c for c in text if not unicodedata.combining(c))))


def is_phone_appointment(appointment):
    location = appointment.get("location") or {}
    kind = folded(location.get("kind") or location.get("type")) if isinstance(location, dict) else folded(location)
    phone = r"\b(?:phone|call|appel|rappel|telephone\w*|telephoni\w*|tel)\b"
    if re.search(phone, kind):
        return True
    if kind in {"zoom", "google conference", "google meet", "microsoft teams conference", "physical", "in person"}:
        return False
    if isinstance(location, dict) and re.search(phone, folded(location.get("location") or location.get("additional_info"))):
        return True
    return bool(re.search(phone, folded(appointment.get("name"))))


def day_appointments(appointments, day, now):
    """Keep only the reception fields of the same active slots used by /crm/calendrier."""
    rows = []
    current = now.timestamp()
    for meeting in appointments:
        slot = meeting.get("calendar_slot")
        if not slot or slot["day"] != day:
            continue
        response = str(meeting.get("response_status") or "").lower()
        result = {"answered": "repondu", "no_answer": "sans_reponse"}.get(
            response, "a_venir" if slot["start"] > current else "sans_resultat")
        rows.append((slot["start"], {
            "time": slot["time"], "person": slot["person"],
            "host": meeting.get("host_name") or "", "subject": meeting.get("name") or "Rendez-vous Calendly",
            "state": result,
        }))
    return [row for _, row in sorted(rows, key=lambda pair: pair[0])]


def cnaps_overview(contact, course, snapshot=None, remote=None):
    snapshot = snapshot or {}
    card = contact.get("cnaps_card_validity") or {}
    relevant = (course in {"APS", "A3P", "PARCOURS_SECURITE_ALTERNANCE"} or bool(snapshot or card)
                or contact.get("carte_pro") == "OUI" or contact.get("compte_cnaps") == "OUI")
    if not relevant:
        return None
    remote = remote or {}
    cnaps = remote.get("cnaps") or {}
    status = normalize_cnaps_tracking_status(snapshot)
    label = CNAPS_LABELS.get(status) or str(snapshot.get("raw_status") or "").strip()
    last_check = (cnaps.get("last_checked_at") or cnaps.get("checked_at") or cnaps.get("date_derniere_verification")
                  or snapshot.get("last_checked_at") or snapshot.get("synced_at"))
    titles = (remote.get("card_pro") or {}).get("titles") or cnaps.get("titles") or card.get("titles") or []
    title_rows = []
    for item in titles:
        if not isinstance(item, dict):
            continue
        title = item.get("code") or item.get("type") or item.get("label") or item.get("title") or item.get("titre") or item.get("name")
        if title:
            expiry = date_label(item.get("expires_at") or item.get("date_expiration") or item.get("valid_until"))
            title_rows.append(" · ".join(str(s) for s in (title, item.get("state") or item.get("status") or item.get("etat"),
                              "Échéance " + expiry if expiry else "") if s))
    return {
        "status": label or "Suivi non renseigné", "checked": date_label(last_check),
        "account": {"OUI": "Créé", "NON": "Non créé"}.get(str(contact.get("compte_cnaps") or "").upper(), ""),
        "card": {"OUI": "Oui", "NON": "Non"}.get(str(contact.get("carte_pro") or "").upper(), ""),
        "titles": list(dict.fromkeys(title_rows)), "card_checked": date_label(card.get("checked_at")),
        "can_check": course in {"APS", "A3P", "PARCOURS_SECURITE_ALTERNANCE"},
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


def desk_summary(contact, callbacks, appointment, *, staff=True):
    entries = [c for c in callbacks if isinstance(c, dict) and contact.get("id")
               and c.get("crm_contact_id") == contact["id"]]
    exchanges = [a for a in contact.get("activities") or [] if isinstance(a, dict)
                 and a.get("kind") in {"appel", "email", "sms", "demande_rappel", "aircall", "email_recu"}
                 and a.get("date") and a.get("title")]
    exchanges.extend({"date": c["created_at"], "title": "Appel au secrétariat" + (" · " + c["notes"][:180] if staff else "")}
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
                              first.get("callback_recipient") or "Équipe", first.get("notes", "")[:160] if staff else ""]))
    reminders = [r.get("scheduled_date") for r in contact.get("relances") or []
                 if isinstance(r, dict) and r.get("status") == "scheduled" and r.get("scheduled_date")]
    if not reminders and contact.get("relance_date") and contact.get("statut") == "A relancer":
        reminders.append(contact["relance_date"])
    financing = []
    if contact.get("cpf") == "OUI":
        financing.append("CPF consulté")
    if contact.get("financement_ft") == "OUI":
        financing.append("Demande de financement France Travail souhaitée")
    if contact.get("reste_a_charge_perso") == "OUI":
        financing.append("Financement personnel envisagé")
    return {
        "formation": contact.get("formation") or "",
        "conseiller": contact.get("commercial") or contact.get("salesforce_owner") or "",
        "statut": contact.get("statut") or "", "statut_secondaire": contact.get("statut_secondaire") or "",
        "archive": bool(contact.get("archived_at")),
        "statut_depuis": date_label(contact.get("status_changed_at") or (contact.get("converted_at") if contact.get("statut") == "Converti" else "")),
        "motif_disqualification": contact.get("disqualification_reason") if contact.get("statut") == "Disqualifié" else "",
        "reactivation": date_label(contact.get("reactivation_date")) if contact.get("statut") == "Disqualifié" else "",
        "centre": contact.get("lieu") or "", "session": contact.get("dates_formation") or "",
        "financement": " · ".join(financing),
        "prochaine_action": contact.get("prochaine_action_manuelle") if staff else "",
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
