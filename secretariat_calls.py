"""Small projections used by the call desk and the CRM callback queue."""

import datetime
import re
import unicodedata

import pytz
from candidate_scoring import normalize_cnaps_tracking_status
from crm_cnaps_tracking import _cnaps_activity_code


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

CNAPS_REQUEST_WORDING = {
    "registered": "La demande est enregistrée. Son envoi au CNAPS reste à confirmer.",
    "transmitted": "La demande a été envoyée au CNAPS. Nous attendons sa décision.",
    "in_review": "Le CNAPS étudie votre demande. Nous attendons sa décision.",
    "accepted": "Le suivi indique que le CNAPS a accepté votre demande.",
    "refused": "Le suivi indique un refus du CNAPS. Je peux demander à un conseiller de faire le point avec vous.",
    "no_result": "Je ne retrouve pas de demande à votre nom dans notre suivi. Je peux demander à l’école de vérifier.",
}

CNAPS_DOCUMENTS = {
    "ap sh": "Autorisation d’entrer en formation de surveillance et de gardiennage",
    "cp sh": "Carte professionnelle pour travailler dans la surveillance et le gardiennage",
    "ap a3p": "Autorisation d’entrer en formation de protection rapprochée",
    "cp a3p": "Carte professionnelle pour travailler dans la protection rapprochée",
}

DESK_COURSES = {
    "APS": ("Agent de prévention et de sécurité (APS)", "Surveiller des sites, contrôler les accès et prévenir les incidents."),
    "A3P": ("Agent de protection physique des personnes (A3P)", "Assurer la protection rapprochée d’une personne, comme garde du corps."),
    "SSIAP": ("Agent de sécurité incendie (SSIAP 1)", "Prévenir les incendies, donner l’alerte et aider à évacuer un bâtiment."),
    "DESP_INIT": ("Dirigeant d’entreprise de sécurité privée (DESP)", "Se former à la création et à la gestion d’une entreprise de sécurité."),
    "DESP_VAE": ("Dirigeant d’entreprise de sécurité privée par validation des acquis de l’expérience (DESP par VAE)", "Faire reconnaître son expérience de direction pour obtenir le titre."),
    "VTC": ("Voiture de transport avec chauffeur (VTC)", "Se préparer à l’examen pour transporter des clients sur réservation."),
}

# CRM stages describe the school's follow-up. Only a booked time establishes an appointment.
DESK_STAGES = {
    "nouveaux": ("Nouvelle demande", "Votre demande est enregistrée auprès de l’école."),
    "blocage": ("Un point bloque le dossier", "Un point doit être vérifié pour que votre dossier avance. Je peux transmettre votre question à votre conseiller."),
    "en cours": ("Demande en cours de suivi", "L’équipe suit actuellement votre demande."),
    "a relancer": ("Reprise de contact prévue", "Le dossier indique que l’équipe doit reprendre contact avec vous."),
    "rdv programme": ("Rendez-vous à vérifier", "Le dossier mentionne un rendez-vous. Je peux vérifier le créneau avec vous dans l’agenda."),
    "rdv programme sans rendez vous": ("Créneau à confirmer", "Je peux vérifier avec l’équipe si une date a été fixée."),
    "converti": ("Suivi de l’inscription", "Votre demande est passée au suivi des inscriptions. Si vous attendez votre convocation, je peux demander à votre conseiller de vérifier."),
    "disqualifie": ("Demande clôturée", "L’école a arrêté le suivi de cette demande. Si votre projet a changé, je peux le signaler à votre conseiller."),
}

DESK_SECONDARY_STAGES = {
    "prochain rdv inscription": "Prochain échange consacré à l’inscription",
    "financement ft en cours": "France Travail étudie la demande de financement",
    "financement ft refuse": "France Travail a refusé la demande de financement",
    "def mob": "Accompagnement à la reconversion par Défense Mobilité",
    "poei": "Formation avant une embauche, avec France Travail (préparation opérationnelle à l’emploi individuelle)",
    "c2p en cours": "Demande en cours au titre du compte professionnel de prévention",
    "transition pro": "Projet de reconversion avec Transitions Pro",
    "marche ft": "Formation relevant d’un marché de France Travail",
    "session ft": "Session de formation organisée dans le cadre de France Travail",
}


def folded(value):
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return " ".join(re.findall(r"[^\W_]+", "".join(c for c in text if not unicodedata.combining(c))))


def desk_course_code(code, catalogue):
    if code in catalogue:
        return code
    key = folded(code)
    if key == "apr":
        return "A3P"
    return next((k for k, config in catalogue.items()
                 if key in {folded(k), folded(config["short"]), folded(config["label"])}), code)


def desk_course(code, catalogue, original):
    config = catalogue.get(code)
    if not config:
        name = original if original and not str(original).isupper() else "Formation à préciser avec l’école"
        return name, ""
    if code in DESK_COURSES:
        return DESK_COURSES[code]
    if code.startswith("BTS_"):
        subject = config["label"].removeprefix("BTS ").split(" (")[0]
        return f"Brevet de technicien supérieur – {subject} ({config['short']})", config["purpose"]
    return config["label"], config["purpose"]


def desk_stage(contact, appointment):
    status = folded(contact.get("statut"))
    if contact.get("archived_at"):
        return "Dossier archivé", "Votre dossier est conservé dans les archives de l’école. Je peux transmettre votre question à l’équipe."
    if appointment and status in {"rdv programme", "rdv programme sans rendez vous"}:
        return "Rendez-vous prévu", "Un rendez-vous figure dans l’agenda de l’école."
    return DESK_STAGES.get(status, ("Situation à préciser", "Je peux demander au conseiller de vérifier où en est votre dossier."))


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


def _expiry_day(value):
    try:
        stamp = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (stamp.astimezone(PARIS) if stamp.tzinfo else stamp).date()
    except ValueError:
        try:
            return datetime.datetime.strptime(str(value), "%d/%m/%Y").date()
        except ValueError:
            return None


def _desk_document(item, today):
    title = next((item[key] for key in ("code", "type", "label", "title", "titre", "name") if item.get(key)), None)
    if not title:
        return None
    state = item.get("state") or item.get("status") or item.get("etat") or ""
    # Some older entries include the state in the title itself, e.g. "AP SH ACTIF".
    embedded = re.fullmatch(r"(ap sh|ap a3p|cp sh|cp a3p) (actif|active|inactif|inactive|expire|expiree)", folded(title))
    code = embedded[1] if embedded else folded(_cnaps_activity_code(title))
    state = state or (embedded[2] if embedded else "")
    state_key = folded(state)
    expiry = next((item[k] for k in ("expires_at", "date_fin_validite", "date_expiration", "valid_until", "expiration_date") if item.get(k)), "")
    end = _expiry_day(expiry) if expiry else None
    until = end.strftime("%d/%m/%Y") if end else ""
    name = CNAPS_DOCUMENTS.get(code)
    if not name:
        wording = "L’école doit préciser le type de document et ce qu’il permet."
    elif state_key in {"actif", "active", "valide", "valid"} and end and end < today:
        wording = f"La date de validité indiquée est dépassée depuis le {until}."
    elif state_key in {"actif", "active", "valide", "valid"} and (end or not expiry):
        wording = "Document indiqué comme valide lors du dernier contrôle."
        wording += f" Fin de validité le {until}." if until else " Date de fin à confirmer."
    elif state_key in {"expire", "expiree", "expired", "perime", "perimee"}:
        wording = f"Document expiré le {until}." if end and end <= today else "Document indiqué comme expiré."
    elif state_key in {"inactif", "inactive", "suspendu", "suspendue", "retire", "retiree", "annule", "annulee", "revoque", "revoquee"}:
        wording = "Ce document n’est plus indiqué comme actif. Un conseiller peut vérifier la suite avec vous."
    else:
        wording = "La validité de ce document reste à vérifier avec l’école."
    reference = " · ".join(str(v) for v in (title, state, "Échéance " + (until or str(expiry)) if expiry else "") if v)
    return {"name": name or "Document à identifier", "wording": wording, "reference": reference}


def _desk_documents(remote, card, today):
    cnaps = remote.get("cnaps") or {}
    sources = [s for s in (remote.get("card_pro"), cnaps)
               if isinstance(s, dict) and ("titles" in s or s.get("cnaps_unavailable") or s.get("nub_missing") or s.get("error"))]
    if not sources and card:
        sources = [card]
    documents = {}
    problems = []
    consulted = False
    checked = ""
    for source in sources:
        if source.get("nub_missing"):
            problems.append("Le numéro de dossier CNAPS manque. L’école doit le vérifier pour consulter les documents.")
            continue
        if source.get("cnaps_unavailable") or source.get("error") or source.get("check_status") in {"error", "failed", "unavailable"}:
            problems.append("La vérification des documents est indisponible. Réessayez ou transmettez la question à un conseiller.")
            continue
        if not isinstance(source.get("titles"), list):
            continue
        checked = date_label(source.get("checked_at") or source.get("last_checked_at")) or checked
        consulted = True
        for item in source["titles"]:
            doc = _desk_document(item, today) if isinstance(item, dict) else None
            if doc:
                documents[doc["reference"]] = doc
    note = " ".join(dict.fromkeys(problems))
    if not documents and not note:
        note = "Aucun document retrouvé lors de cette consultation." if consulted else "Aucune vérification de document n’est renseignée."
    return list(documents.values()), note, checked


def cnaps_overview(contact, course, snapshot=None, remote=None):
    if not isinstance(snapshot, dict):
        snapshot = {"raw_status": snapshot} if snapshot else {}
    card = contact.get("cnaps_card_validity") or {}
    card = card if isinstance(card, dict) else {}
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
    wording = CNAPS_REQUEST_WORDING.get(status, "L’avancement de votre demande reste à confirmer avec l’école.")
    match_count = str(remote.get("match_count") or "")
    if match_count.isdecimal() and int(match_count) > 1:
        label = "Plusieurs dossiers possibles"
        wording = "Plusieurs dossiers portent ce nom. Un conseiller doit vérifier lequel vous concerne."
        documents, document_note, card_checked = [], "", ""
    else:
        documents, document_note, card_checked = _desk_documents(remote, card, datetime.datetime.now(PARIS).date())
    return {
        "status": label or "Suivi non renseigné", "checked": date_label(last_check),
        "account": {"OUI": "Créé", "NON": "Non créé"}.get(str(contact.get("compte_cnaps") or "").upper(), ""),
        "card": {"OUI": "Oui", "NON": "Non"}.get(str(contact.get("carte_pro") or "").upper(), ""),
        "request_wording": wording, "documents": documents, "document_note": document_note,
        "titles": [doc["reference"] for doc in documents], "card_checked": card_checked,
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
        financing.append("Compte personnel de formation (CPF) consulté")
    if contact.get("financement_ft") == "OUI":
        financing.append("Demande de financement France Travail souhaitée")
    if contact.get("reste_a_charge_perso") == "OUI":
        financing.append("Financement personnel envisagé")
    stage_label, phone_wording = desk_stage(contact, appointment)
    secondary = str(contact.get("statut_secondaire") or "").strip()
    return {
        "formation": contact.get("formation") or "",
        "conseiller": contact.get("commercial") or contact.get("salesforce_owner") or "",
        "statut": contact.get("statut") or "", "statut_secondaire": contact.get("statut_secondaire") or "",
        "stage_label": stage_label, "phone_wording": phone_wording,
        "secondary_label": DESK_SECONDARY_STAGES.get(folded(secondary), "Étape à préciser avec le conseiller" if secondary else ""),
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
