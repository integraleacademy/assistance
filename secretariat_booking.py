"""Réservation Calendly depuis le secrétariat, avant la création éventuelle d'une fiche CRM."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from pathlib import Path
import sqlite3
import time

from flask import jsonify, request
from secretariat_calls import PARIS


def _matches_agenda(event, url, directory):
    public_url = (event.get("scheduling_url") or "").split("?")[0].rstrip("/")
    return event.get("active") and (public_url == url or (
        url == directory and not event.get("secret") and public_url.startswith(directory + "/")))


def _questions(event):
    return [q for q in event.get("custom_questions") or [] if q.get("enabled")]


def _supported(event):
    return (not event.get("is_paid") and event.get("booking_method") != "poll"
            and all(q.get("type") in {"text", "string", "phone_number"} for q in _questions(event))
            and len(event.get("locations") or []) <= 1
            and all(loc and loc.get("kind") != "ask_invitee" for loc in event.get("locations") or []))


def _create_invitee(legacy, booking, event, phone):
    try:
        return legacy._calendly_request("POST", "/invitees", json_body=booking)
    except legacy.CalendlyAPIError as exc:
        # Some existing round-robin events still require their configured call location.
        # Only a definite validation rejection is safe to retry.
        if (exc.status_code not in (400, 422) or event.get("pooling_type") != "round_robin"
                or len(event.get("locations") or []) != 1):
            raise
        details = exc.payload.get("details") or []
        missing_location = any(str(d.get("parameter", "")).startswith("location") for d in details if isinstance(d, dict))
        generic = (not details and exc.payload.get("title") == "Invalid Argument"
                   and exc.payload.get("message") == "The supplied parameters are invalid.")
        if not missing_location and not generic:
            raise
        location = legacy._calendly_booking_location(event, None, {"telephone": phone})
        if not location:
            raise
        return legacy._calendly_request("POST", "/invitees", json_body={**booking, "location": location})


def _operator_key(legacy, call_key):
    return sha256((legacy.current_user()["email"] + call_key).encode()).hexdigest()


def _caller_key(legacy, payload):
    identity = [str(payload.get("formation") or ""), legacy._crm_normalize_email(payload.get("email")),
                legacy._calendly_phone_number(payload.get("telephone"))]
    return sha256(json.dumps(identity).encode()).hexdigest()


def confirmed_receipt(legacy, call_key, payload):
    path = Path(str(legacy.DATA_FILE) + ".desk-bookings.sqlite3")
    if not call_key or not legacy._secretariat_role() or not path.exists():
        return None
    with closing(sqlite3.connect(path)) as db:
        row = db.execute("SELECT result FROM desk_bookings WHERE call_key=?", (_operator_key(legacy, call_key),)).fetchone()
    if row and row[0]:
        receipt = json.loads(row[0])
        if receipt["caller"] == _caller_key(legacy, payload):
            return receipt["appointment"]
    return None


def _claim(db, key, fingerprint):
    # Commit the claim before the external POST. A lost reply or a second worker
    # must never send a second invitation for the same call.
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS desk_bookings "
                   "(call_key TEXT PRIMARY KEY, fingerprint TEXT, created REAL, result TEXT)")
        db.execute("DELETE FROM desk_bookings WHERE created < ?", (time.time() - 172800,))
        created = db.execute("INSERT OR IGNORE INTO desk_bookings VALUES (?, ?, ?, NULL)",
                             (key, fingerprint, time.time())).rowcount
        if created:
            return None
        saved, receipt = db.execute("SELECT fingerprint, result FROM desk_bookings WHERE call_key=?", (key,)).fetchone()
        if saved == fingerprint and receipt:
            return jsonify(appointment=json.loads(receipt)["appointment"])
        return jsonify(error="Une réservation existe ou est en cours pour cet appel. Vérifiez l’agenda avant de réserver à nouveau.",
                       reason="booking_unconfirmed"), 409


def register_secretariat_booking(legacy):
    def agenda_url(code):
        return (legacy.SECRETARIAT_FORMATIONS[code]["calendly"].split("?")[0].rstrip("/")
                if code in legacy.SECRETARIAT_FORMATIONS else "")

    @legacy.app.get("/api/secretariat/calendly/creneaux", endpoint="secretariat_booking_slots")
    def slots():
        if not legacy._secretariat_role():
            return legacy._secretariat_private(jsonify(error="Connectez-vous au secrétariat."), 401)
        url = agenda_url(request.args.get("formation"))
        if not url:
            return jsonify(error="Formation inconnue."), 400
        directory = legacy.SECRETARIAT_GENERIC_CALENDLY_URL.rstrip("/")
        now = datetime.now(timezone.utc)
        try:
            week = int(request.args.get("week", 0))
            if week < 0:
                raise ValueError
            day = now.astimezone(PARIS).date() + timedelta(weeks=week)
            midnight = PARIS.localize(datetime.combine(day, datetime.min.time()))
            end = PARIS.localize(datetime.combine(day + timedelta(days=7), datetime.min.time()))
            start = max(midnight, now + timedelta(minutes=1))
            end = min(end, start + timedelta(days=7))
        except (ValueError, OverflowError):
            return jsonify(error="Période invalide."), 400
        try:
            events = [e for e in legacy._calendly_event_types_for_context(legacy._load_data_snapshot())
                      if _matches_agenda(e, url, directory)]
            options = [{"uri": e["uri"], "name": e["name"]} for e in events]
            selected = request.args.get("event_type", "")
            event = next((e for e in events if e["uri"] == selected), None) if selected else (
                events[0] if len(events) == 1 else None)
            if selected and not event:
                return jsonify(error="Agenda indisponible pour cette formation."), 400
            base = {"agendas": options, "day": day.isoformat(), "end_day": (day + timedelta(days=6)).isoformat()}
            if not event:
                return jsonify(**base, hosted_only=not events)
            if not _supported(event):
                return jsonify(**base, hosted_only=True)
            values = legacy._calendly_request("GET", "/event_type_available_times", params={
                "event_type": event["uri"], "start_time": start.astimezone(timezone.utc).isoformat(),
                "end_time": end.astimezone(timezone.utc).isoformat(),
            })
            questions = [{k: q.get(k) for k in ("name", "position", "required", "type")} for q in _questions(event)]
            return jsonify(**base, event_type=event["uri"], questions=questions,
                           slots=[row["start_time"] for row in values["collection"]
                                  if row.get("start_time") and row.get("status") in (None, "available")])
        except (legacy.CalendlyAPIError, RuntimeError) as exc:
            return legacy._calendly_route_error(exc, action="la lecture des créneaux")

    @legacy.app.post("/api/secretariat/calendly/reserver", endpoint="secretariat_book_slot")
    def reserve():
        if not legacy._secretariat_role():
            return legacy._secretariat_private(jsonify(error="Connectez-vous au secrétariat."), 401)
        legacy.crm_external_access._require_csrf()
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Réservation invalide."), 400
        key = legacy._secretariat_call_key(body)
        code = str(body.get("formation") or "")
        url = agenda_url(code)
        uuid = legacy._calendly_resource_uuid(body.get("event_type"), "event_types")
        phone = legacy._calendly_phone_number(body.get("telephone"))
        email = str(body.get("email") or "").strip()
        first, last = (str(body.get(k) or "").strip()[:120] for k in ("prenom", "nom"))
        if not all((url, key, uuid, phone, first, last)) or len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            return jsonify(error="Vérifiez le nom, le prénom, l’e-mail, le téléphone et le créneau."), 400
        sms = legacy._calendly_phone_number(body.get("sms"))
        if body.get("sms") and not sms:
            return jsonify(error="Le numéro pour le rappel SMS est invalide."), 400
        try:
            start = datetime.fromisoformat(str(body.get("start_time", "")).replace("Z", "+00:00"))
            if start.tzinfo is None or start <= datetime.now(timezone.utc):
                raise ValueError
        except ValueError:
            return jsonify(error="Choisissez un créneau à venir."), 400
        try:
            event = legacy._calendly_request("GET", f"/event_types/{uuid}")["resource"]
            if not _matches_agenda(event, url, legacy.SECRETARIAT_GENERIC_CALENDLY_URL.rstrip("/")) or not _supported(event):
                return jsonify(error="Cet agenda a changé. Rechargez les créneaux."), 409
            entered = body.get("answers", {})
            if not isinstance(entered, dict):
                return jsonify(error="Réponses de réservation invalides."), 400
            answers = {str(k): str(v or "").strip()[:1500] for k, v in entered.items()}
            for field in _questions(event):
                if field["type"] == "phone_number":
                    answers[str(field["position"])] = phone
            responses, _ = legacy._calendly_question_answers(event, answers)
            invitee = {"name": f"{first} {last}", "first_name": first, "last_name": last, "email": email,
                       "timezone": "Europe/Paris"}
            if sms:
                invitee["text_reminder_number"] = sms
            booking = {"event_type": event["uri"], "start_time": start.astimezone(timezone.utc).isoformat(),
                       "invitee": invitee, "questions_and_answers": responses}
            if event.get("pooling_type") != "round_robin":
                location = legacy._calendly_booking_location(event, None, {"telephone": phone})
                if location:
                    booking["location"] = location
            if code == "SSIAP3":
                booking["tracking"] = {"utm_campaign": "ssiap3"}
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except (legacy.CalendlyAPIError, RuntimeError) as exc:
            return legacy._calendly_route_error(exc, action="la lecture de l’agenda")

        fingerprint = sha256(json.dumps(booking, sort_keys=True).encode()).hexdigest()
        user_key = _operator_key(legacy, key)
        with closing(sqlite3.connect(str(legacy.DATA_FILE) + ".desk-bookings.sqlite3")) as db:
            duplicate = _claim(db, user_key, fingerprint)
            if duplicate is not None:
                return duplicate
            try:
                result = _create_invitee(legacy, booking, event, phone).get("resource") or {}
                if not result.get("uri") or not result.get("event"):
                    raise legacy.CalendlyTransportError(booking_uncertain=True)
            except (legacy.CalendlyAPIError, RuntimeError) as exc:
                if not getattr(exc, "booking_uncertain", False):
                    with db:
                        db.execute("DELETE FROM desk_bookings WHERE call_key=?", (user_key,))
                return legacy._calendly_route_error(exc, action="la réservation")
            local = start.astimezone(PARIS)
            appointment = {"date": local.strftime("%d/%m/%Y"), "time": local.strftime("%H:%M"),
                           "mode": "Appel téléphonique", "label": legacy._crm_calendly_datetime_label(start.isoformat())}
            appointment.update(event_uri=result["event"], invitee_uri=result["uri"], name=event.get("name") or "")
            receipt = {"appointment": appointment, "caller": _caller_key(legacy, body)}
            with db:
                db.execute("UPDATE desk_bookings SET result=? WHERE call_key=?", (json.dumps(receipt), user_key))
            return jsonify(appointment=appointment), 201
