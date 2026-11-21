"""Lecture sécurisée des rendez-vous CRM pour les dossiers Intégrale EDOF.

La réponse est limitée aux dossiers demandés et aux créneaux téléphoniques
Calendly encore à venir. Aucun contact n'est rapproché par le seul nom.
Aucun appel Calendly, envoi de mail ou changement du CRM depuis cette route.
"""
from datetime import datetime, timezone
import hmac
import json
import re
from urllib.parse import quote

from flask import jsonify, request


SIGNING_CONTEXT = b"integrale-edof-appointments-v1\0"
SIGNATURE_HEADER = "X-Integrale-EDOF-Appointment-Signature"
MAX_FOLDERS = 30
FOLDER_PATTERN = re.compile(r"[0-9]{6,20}\Z")
CRM_ORIGIN = "https://assistance-alw9.onrender.com"


def _match_contact(legacy, folder, contact):
    """Preuve forte : nom complet ET au moins un moyen de contact identique."""
    if not contact or not legacy._wedof_contact_name_matches(folder, contact):
        return False
    emails, phones, _ = legacy._wedof_attendee_values(folder)
    email = legacy._crm_normalize_email(contact.get("mail"))
    phone = legacy._crm_normalize_phone(contact.get("telephone"))
    return bool((email and email in emails) or (phone and phone in phones))


def appointment_for(legacy, data, folder, *, now=None):
    """Indique prévu / aucun / rapprochement impossible, sans inventer de RDV."""
    contacts = [c for c in data.get("crm_contacts", []) if _match_contact(legacy, folder, c)]
    if not contacts:
        return {"status": "unmatched"}
    meetings = []
    for contact in contacts:
        # Réutilise les mêmes règles que le CRM : téléphone uniquement,
        # annulations/sans réponse exclus, identité Calendly vérifiée.
        meeting = legacy._crm_next_phone_appointment(
            data, {}, contact=contact, now=now, include_current=False
        )
        if meeting:
            starts_at, appointment = meeting
            meetings.append((starts_at, str(contact.get("id") or ""), appointment))
    if not meetings:
        contact_id = str(contacts[0].get("id") or "")
        return {"status": "none", "crm_url": _contact_url(contact_id) if contact_id else ""}
    start, contact_id, appointment = min(meetings, key=lambda item: item[0])
    return {
        "status": "scheduled",
        "start_at": start.isoformat(),
        "host": str(appointment.get("host_name") or "")[:80],
        "name": str(appointment.get("name") or "Rendez-vous téléphonique")[:120],
        "crm_url": _contact_url(contact_id),
    }


def _contact_url(contact_id):
    return CRM_ORIGIN + "/crm/contacts?fiche=" + quote(contact_id, safe="") + "&retour=pistes"


def register_edof_appointment_lookup(legacy):
    app = legacy.app

    @app.post("/api/integrations/edof-appointments")
    def integrale_edof_appointment_lookup():
        secret = legacy._edof_token()
        if not secret:
            return jsonify(error="Connexion Intégrale EDOF indisponible"), 503
        if request.content_length is not None and request.content_length > 4000:
            return jsonify(error="Requête trop volumineuse"), 413
        raw = request.get_data()
        if len(raw) > 4000:
            return jsonify(error="Requête trop volumineuse"), 413
        expected = hmac.digest(secret.encode(), SIGNING_CONTEXT + raw, "sha256").hex()
        supplied = request.headers.get(SIGNATURE_HEADER, "")
        if not hmac.compare_digest(expected, supplied):
            return jsonify(error="Authentification invalide"), 401
        payload = request.get_json(silent=True)
        ids = payload.get("folder_ids") if isinstance(payload, dict) else None
        if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_FOLDERS or
                any(not isinstance(item, str) or not FOLDER_PATTERN.fullmatch(item) for item in ids) or
                len(set(ids)) != len(ids)):
            return jsonify(error="Numéros de dossier EDOF invalides"), 400

        # Le cache EDOF du CRM n'est pas interrogé par le navigateur.
        data = legacy._load_data_snapshot()
        placeholders = ",".join("?" for _ in ids)
        with legacy._wedof_connect() as db:
            records = db.execute(
                """SELECT stable_id,payload_json FROM wedof_resources
                   WHERE resource_type='registrationFolder'
                     AND stable_id IN (""" + placeholders + ")",
                ids,
            ).fetchall()
        folders = {}
        for record in records:
            try:
                item = json.loads(record["payload_json"])
                if isinstance(item, dict) and str(item.get("externalId") or "") == record["stable_id"]:
                    folders[record["stable_id"]] = item
            except (ValueError, TypeError):
                continue

        now = datetime.now(timezone.utc)
        result = {
            folder_id: appointment_for(legacy, data, folders[folder_id], now=now)
            if folder_id in folders else {"status": "unmatched"}
            for folder_id in ids
        }
        response = jsonify(ok=True, appointments=result, checked_at=now.isoformat())
        response.headers["Cache-Control"] = "private, no-store"
        return response
