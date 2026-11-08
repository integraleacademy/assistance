"""Call-desk messages shared by the verified dossier, agenda and CRM queue."""

import datetime
import hashlib
import json
import uuid

from secretariat_calls import PARIS, date_label, folded, is_phone_appointment


EVENT_LABELS = {
    "claimed": "Pris en charge", "reached": "Personne rappelée et jointe",
    "no_answer": "Appel tenté, sans réponse", "processed": "Traitement terminé",
    "reopened": "Demande rouverte", "supplement": "Complément transmis",
}
SUBJECTS = {"formation": "Question sur une formation", "suivi": "Suivi du dossier",
            "waiting": "La personne attend son appel"}


def status(entry):
    raw = folded(entry.get("callback_status"))
    if raw in {"processed", "traite"}:
        return "processed"
    return "in_progress" if raw == "in progress" else "pending"


def events(entry):
    history = list(entry.get("callback_events") or [])
    # A historical "processed" flag is evidence of handling, not a successful call.
    if status(entry) == "processed" and entry.get("callback_processed_at") and not any(
            e.get("kind") in {"processed", "reached"} for e in history):
        history.append({"id": "legacy-processed", "kind": "processed",
                        "at": entry["callback_processed_at"], "by": entry.get("callback_processed_by") or ""})
    return history


def add_event(entry, kind, when, actor, *, text="", event_id=None):
    event = {"id": event_id or uuid.uuid4().hex, "kind": kind, "at": when, "by": actor}
    if text:
        event["text"] = text
    entry.setdefault("callback_events", []).append(event)


def message_view(entry, *, staff=False):
    history = []
    for event in events(entry):
        kind = event.get("kind")
        if kind not in EVENT_LABELS:
            continue
        history.append({"kind": kind, "label": EVENT_LABELS[kind], "when": date_label(event.get("at")),
                        "by": event.get("by") or "", "text": event.get("text", "") if kind == "supplement" else ""})
    subject = SUBJECTS.get(entry.get("callback_motif"), "Message pour l’équipe")
    if entry.get("callback_motif") == "waiting" and entry.get("rdv"):
        subject += " · " + entry["rdv"]
    state = status(entry)
    return {"id": str(entry.get("id") or ""), "subject": subject, "status": state,
            "status_label": {"pending": "Transmis", "in_progress": "En cours de prise en charge",
                             "processed": "Traité"}[state],
            "sent": date_label(entry.get("created_at") or entry.get("date")),
            "recipient": entry.get("callback_recipient") or "Équipe", "due": date_label(entry.get("callback_due_at")),
            "note": str(entry.get("notes") or "") if staff or entry.get("desk_shared_note") is True else "", "events": history}


def time_order(raw):
    try:
        moment = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        try:
            moment = datetime.datetime.strptime(str(raw), "%d/%m/%Y %H:%M")
        except ValueError:
            return 0
    return (moment if moment.tzinfo else PARIS.localize(moment)).timestamp()


def message_history(rows, *, staff=False):
    ordered = sorted(reversed(rows), key=lambda row: time_order(row.get("created_at") or row.get("date")), reverse=True)
    return [message_view(row, staff=staff) for row in ordered]


def waiting_key(meeting):
    slot = meeting["calendar_slot"]
    identity = meeting.get("id") or meeting.get("invitee_uri") or [
        meeting.get("event_uri"), meeting.get("invitee_email"), meeting.get("invitee_phone"),
        meeting.get("contact_id"), meeting.get("invitee_name"), meeting.get("host_name"), meeting.get("name"),
    ]
    # The slot is part of the reference so a reschedule invalidates a stale button.
    value = json.dumps([identity, slot["start"]], ensure_ascii=False)
    return hashlib.sha256(value.encode()).hexdigest()


def meeting_host(meeting, users):
    email = str(meeting.get("host_email") or "").strip().casefold()
    if email in users:
        return users[email]
    name = folded(meeting.get("host_name"))
    matches = [user for user in users.values() if name and name in {folded(user["name"]), folded(user["first_name"])}]
    return matches[0] if len(matches) == 1 else None


def waiting_slots(meetings, day):
    result = {}
    for meeting in meetings:
        slot = meeting.get("calendar_slot")
        if slot and slot["day"] == day and is_phone_appointment(meeting):
            key = waiting_key(meeting)
            result[key] = None if key in result else meeting
    return result


def waiting_action(meeting, slots, reports, users, now):
    key = waiting_key(meeting)
    if slots.get(key) is not meeting:
        return None
    entry = reports.get(key)
    if entry:
        state = status(entry)
        progress = {"in_progress": "Pris en charge", "processed": "Signalement traité"}.get(state, "")
        progress = EVENT_LABELS.get(entry.get("callback_outcome"), progress)
        return {"state": "sent", "recipient": entry["callback_recipient"], "when": date_label(entry["created_at"]),
                "progress": progress}
    if meeting["calendar_slot"]["start"] > now.timestamp() or meeting.get("response_status") == "answered":
        return None
    host = meeting_host(meeting, users)
    return {"state": "ready", "key": key, "recipient": host["name"] if host else ""}


def waiting_notification(entry):
    return {"id": "waiting-" + entry["id"], "kind": "waiting_call", "author": "Le secrétariat",
            "callback_request_id": entry["id"], "contact_id": entry.get("crm_contact_id") or "",
            "contact_name": entry["nom"], "date": entry["created_at"], "text": entry["notes"],
            "recipient_email": entry["waiting_recipient_email"], "read": bool(entry.get("waiting_read")),
            "awaiting_pickup": status(entry) == "pending"}
