"""Parcours SMS Aircall : informations formation, rappel et création de piste."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import hmac
import os
import re
import secrets
import threading
import unicodedata
import uuid
from collections.abc import Mapping
from typing import Any, Callable
from zoneinfo import ZoneInfo


AIRCALL_TRAINING_INFORMATION_PATH = "/api/integrations/aircall/formations/information"
AIRCALL_LEAD_SMS_PATH = "/api/integrations/aircall/lead-capture/sms"
AIRCALL_LEAD_FORM_PATH = "/rappel-formation/<token>"
AIRCALL_ACTIONS_SECRET_ENV = "AIRCALL_ACTIONS_API_KEY"
AIRCALL_ACTIONS_KEY_HEADER = "X-Aircall-Actions-Key"
AIRCALL_SMS_SOURCE = "aircall_sms_form"
AIRCALL_SMS_ORIGIN = "Aircall – Formulaire SMS"
AIRCALL_REQUESTS_KEY = "crm_aircall_lead_requests"

_PARIS = ZoneInfo("Europe/Paris")
_REQUEST_LOCK = threading.RLock()
_TOKEN_TTL_DAYS = 7
_MAX_SMS_PER_HOUR = 3
_IDEMPOTENCY_WINDOW_SECONDS = 5 * 60

_TRAINING_ORDER = (
    "A3P", "APS", "SSIAP", "DESP_INIT", "DESP_VAE", "VTC",
    "BTS_MOS", "BTS_MCO", "BTS_NDRC", "BTS_CI", "BTS_PI", "BTS_CG",
)
_DIRECT_TRAINING_KEYS = {
    "A3P": "A3P", "APS": "APS", "SSIAP": "SSIAP",
    "SSIAP1": "SSIAP", "SSIAP 1": "SSIAP", "VTC": "VTC",
    "DESP": "DESP_INIT", "DESP INITIAL": "DESP_INIT",
    "DESP_INIT": "DESP_INIT", "DESP VAE": "DESP_VAE",
    "DESP_VAE": "DESP_VAE",
    "BTS MOS": "BTS_MOS", "BTS_MOS": "BTS_MOS",
    "BTS MCO": "BTS_MCO", "BTS_MCO": "BTS_MCO",
    "BTS NDRC": "BTS_NDRC", "BTS_NDRC": "BTS_NDRC",
    "BTS CI": "BTS_CI", "BTS_CI": "BTS_CI",
    "BTS PI": "BTS_PI", "BTS_PI": "BTS_PI",
    "BTS CG": "BTS_CG", "BTS_CG": "BTS_CG",
}
_CRM_TRAINING_LABELS = {
    "A3P": "A3P", "APS": "APS", "SSIAP": "SSIAP 1",
    "DESP_INIT": "DESP", "DESP_VAE": "DESP", "VTC": "Chauffeur VTC",
    "BTS_MOS": "BTS MOS", "BTS_MCO": "BTS MCO", "BTS_NDRC": "BTS NDRC",
    "BTS_CI": "BTS CI", "BTS_PI": "BTS PI", "BTS_CG": "BTS CG",
}
_CENTRE_LABELS = {
    "cote_azur": "Côte d’Azur – Puget-sur-Argens",
    "auvergne": "Auvergne – Aurillac",
    "paris": "Paris",
}
_AIRCALL_TRAINING_OVERRIDES = {
    "A3P": {"duration": "327 h · 9 semaines", "price": "4 200 € TTC"},
    "SSIAP": {
        "duration": "70 h · 2 semaines (84 h avec SST)",
        "price": "1 230 € TTC",
    },
    "DESP_VAE": {"duration": "3 semaines d’accompagnement", "price": "3 800 € TTC"},
    "VTC": {
        "duration": "105 h", "price": "1 500 € TTC",
        "format": "Théorie à distance + pratique en présentiel",
    },
}


def _now() -> dt.datetime:
    return dt.datetime.now(_PARIS)


def _iso(value: dt.datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="microseconds")


def _compact(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text)


def _normalize_phone(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("0033"):
        digits = digits[2:]
    if len(digits) == 10 and digits.startswith("0"):
        digits = f"33{digits[1:]}"
    return digits if 8 <= len(digits) <= 15 else ""


def _provided_api_key(request_obj: Any) -> str:
    header_value = str(request_obj.headers.get(AIRCALL_ACTIONS_KEY_HEADER) or "").strip()
    if header_value:
        return header_value
    authorization = str(request_obj.headers.get("Authorization") or "").strip()
    if authorization.casefold().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def _authenticate(request_obj: Any, jsonify_fn: Callable[[dict], Any]):
    expected = str(os.getenv(AIRCALL_ACTIONS_SECRET_ENV) or "").strip()
    if not expected:
        return jsonify_fn({
            "ok": False,
            "error": "La connexion des actions Aircall n'est pas configurée.",
        }), 503
    provided = _provided_api_key(request_obj)
    if not provided or not hmac.compare_digest(provided, expected):
        return jsonify_fn({"ok": False, "error": "Authentification Aircall invalide."}), 401
    return None


def _training_key(value: Any) -> str:
    raw = " ".join(str(value or "").strip().upper().replace("-", " ").split())
    if raw in _DIRECT_TRAINING_KEYS:
        return _DIRECT_TRAINING_KEYS[raw]
    compact = _compact(raw)
    for alias, key in _DIRECT_TRAINING_KEYS.items():
        if compact == _compact(alias):
            return key

    # Le parseur vocal existant connaît notamment « A trois P » et « Siappe un ».
    from crm_aircall_ai import normalize_training

    formation, desp_type = normalize_training(value)
    if formation == "A3P":
        return "A3P"
    if formation == "APS":
        return "APS"
    if formation == "SSIAP 1":
        return "SSIAP"
    if formation == "Chauffeur VTC":
        return "VTC"
    if formation == "DESP":
        return "DESP_VAE" if desp_type == "VAE" else "DESP_INIT"
    if formation.startswith("BTS "):
        return formation.replace(" ", "_", 1)
    return ""


def _training_options(legacy_app: Any) -> list[dict[str, str]]:
    catalogue = getattr(legacy_app, "SECRETARIAT_FORMATIONS", {})
    options = []
    for code in _TRAINING_ORDER:
        details = catalogue.get(code) or {}
        label = str(details.get("label") or details.get("short") or code).strip()
        options.append({"code": code, "label": label})
    return options


def _centre_key(legacy_app: Any, value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalizer = getattr(legacy_app, "_normalize_centre_code", None)
    if callable(normalizer):
        normalized = str(normalizer(raw) or "").strip()
        if normalized in _CENTRE_LABELS:
            return normalized
    key = _compact(raw)
    if any(marker in key for marker in ("cotedazur", "puget", "var")):
        return "cote_azur"
    if any(marker in key for marker in ("auvergne", "aurillac", "cantal")):
        return "auvergne"
    if "paris" in key or "iledefrance" in key:
        return "paris"
    return ""


def _is_full_session(row: Mapping[str, Any]) -> bool:
    text = _compact(f"{row.get('label', '')} {row.get('badge', '')}")
    return "complet" in text or "complete" in text


def _session_rows(
    legacy_app: Any,
    data: Mapping[str, Any],
    training_code: str,
    centre_code: str = "",
) -> list[dict[str, Any]]:
    sessions = legacy_app.get_upcoming_formation_sessions(data)
    rows = []
    for current_centre, formations in sessions.items():
        if centre_code and current_centre != centre_code:
            continue
        for row in formations.get(training_code, []):
            if not isinstance(row, Mapping) or not str(row.get("label") or "").strip():
                continue
            item = dict(row)
            item["centre_code"] = current_centre
            item["centre"] = _CENTRE_LABELS.get(
                current_centre,
                str(getattr(legacy_app, "FORMATION_CENTRES", {}).get(current_centre) or current_centre),
            )
            item["full"] = _is_full_session(item)
            rows.append(item)

    def sort_key(item: Mapping[str, Any]):
        parser = getattr(legacy_app, "_session_start_date", None)
        start = parser(item.get("label")) if callable(parser) else None
        return start or dt.date.max, str(item.get("label") or "")

    return sorted(rows, key=sort_key)


def _spoken_training_information(
    details: Mapping[str, Any], rows: list[dict[str, Any]],
) -> str:
    label = str(details.get("label") or details.get("short") or "Cette formation")
    sentences = []
    if rows:
        first = rows[0]
        first_label = str(first.get("label") or "").strip()
        centre = str(first.get("centre") or "").strip()
        sentences.append(
            f"Pour la formation {label}, la prochaine session est {first_label}"
            + (f", à {centre}" if centre else "") + "."
        )
        available = next((row for row in rows if not row.get("full")), None)
        if first.get("full"):
            sentences.append("Cette session est indiquée complète.")
            if available and available is not first:
                available_centre = str(available.get("centre") or "").strip()
                sentences.append(
                    f"La prochaine session non complète indiquée est {available.get('label')}"
                    + (f", à {available_centre}" if available_centre else "") + "."
                )
    else:
        sentences.append(
            f"Je ne dispose pas actuellement d'une prochaine date confirmée pour la formation {label}."
        )
    if details.get("duration"):
        sentences.append(f"Sa durée est de {details['duration']}.")
    if details.get("price"):
        sentences.append(f"Son tarif est de {details['price']}.")
    if details.get("format"):
        sentences.append(f"Le format est : {details['format']}.")
    return " ".join(sentences)


def _training_information_payload(
    legacy_app: Any, data: Mapping[str, Any], payload: Mapping[str, Any],
) -> tuple[dict[str, Any], int]:
    code = _training_key(payload.get("formation") or payload.get("training"))
    catalogue = getattr(legacy_app, "SECRETARIAT_FORMATIONS", {})
    if not code or code not in catalogue:
        return {
            "ok": True,
            "success": False,
            "requires_clarification": True,
            "formations": _training_options(legacy_app),
            "spoken_response": (
                "Je n'ai pas identifié précisément la formation. "
                "Pouvez-vous me redonner son intitulé ?"
            ),
        }, 200
    centre_code = _centre_key(legacy_app, payload.get("centre") or payload.get("location"))
    details = copy.deepcopy(catalogue[code])
    details.update(_AIRCALL_TRAINING_OVERRIDES.get(code, {}))
    rows = _session_rows(legacy_app, data, code, centre_code)
    first_available = next((row for row in rows if not row.get("full")), None)
    return {
        "ok": True,
        "success": True,
        "formation_code": code,
        "formation_label": details.get("label") or details.get("short") or code,
        "duration": details.get("duration", ""),
        "price": details.get("price", ""),
        "format": details.get("format", ""),
        "location": details.get("location", ""),
        "centre_code": centre_code,
        "next_session": rows[0] if rows else None,
        "next_available_session": first_available,
        "next_sessions": rows[:5],
        "spoken_response": _spoken_training_information(details, rows),
    }, 200


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _parse_iso(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_PARIS)
    return parsed


def _record_expired(record: Mapping[str, Any], now: dt.datetime | None = None) -> bool:
    expires_at = _parse_iso(record.get("expires_at"))
    return not expires_at or expires_at <= (now or _now())


def _public_base_url(request_obj: Any) -> str:
    base_url = (
        os.getenv("PUBLIC_BASE_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
        or request_obj.url_root
    ).rstrip("/")
    forwarded = str(request_obj.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
    if forwarded == "https":
        base_url = re.sub(r"^http://", "https://", base_url, count=1)
    return base_url


def _sms_text(url: str) -> str:
    # Les accents utilisés appartiennent à l'alphabet GSM-7 : le message reste
    # professionnel sans déclencher inutilement l'encodage Unicode.
    return f"Intégrale Academy : complétez le formulaire. Un expert formation vous rappellera : {url}"


def _callback_entry(record: Mapping[str, Any], phone: str, label: str) -> dict[str, Any]:
    now = _now()
    notes = "Appel reçu par l’assistante IA Aircall."
    if label:
        notes += f" Formation demandée : {label}."
    notes += " Coordonnées attendues via le formulaire envoyé par SMS."
    return {
        "id": record["id"],
        "type": "autre",
        "source": AIRCALL_SMS_SOURCE,
        "formation": record.get("formation_code", ""),
        "nom": "",
        "prenom": "",
        "nom_famille": "",
        "telephone": phone,
        "email": "",
        "notes": notes,
        "rdv": "À rappeler",
        "crm_contact_id": "",
        "callback_status": "pending",
        "callback_status_updated_at": now.isoformat(),
        "callback_processed_at": "",
        "callback_processed_by": "",
        "statut": "À traiter",
        "created_at": now.isoformat(),
        "date": now.strftime("%d/%m/%Y %H:%M"),
    }


def _recent_request(
    data: Mapping[str, Any], phone: str, call_id: str, now: dt.datetime,
) -> Mapping[str, Any] | None:
    candidates = []
    for record in data.get(AIRCALL_REQUESTS_KEY, []):
        if not isinstance(record, Mapping) or record.get("status") != "pending":
            continue
        if _record_expired(record, now) or _normalize_phone(record.get("caller_phone")) != phone:
            continue
        created_at = _parse_iso(record.get("created_at"))
        exact_call = bool(call_id and str(record.get("call_id") or "") == call_id)
        recent_phone = bool(
            created_at and (now - created_at).total_seconds() <= _IDEMPOTENCY_WINDOW_SECONDS
        )
        if exact_call or recent_phone:
            candidates.append(record)
    return max(candidates, key=lambda item: str(item.get("created_at") or ""), default=None)


def _sms_rate_limited(data: Mapping[str, Any], phone: str, now: dt.datetime) -> bool:
    sent = 0
    for record in data.get(AIRCALL_REQUESTS_KEY, []):
        if not isinstance(record, Mapping) or record.get("sms_status") != "sent":
            continue
        if _normalize_phone(record.get("caller_phone")) != phone:
            continue
        sent_at = _parse_iso(record.get("sms_sent_at") or record.get("created_at"))
        if sent_at and 0 <= (now - sent_at).total_seconds() <= 3600:
            sent += 1
    return sent >= _MAX_SMS_PER_HOUR


def _find_record_by_id(data: Mapping[str, Any], request_id: str) -> dict[str, Any] | None:
    return next((
        record for record in data.get(AIRCALL_REQUESTS_KEY, [])
        if isinstance(record, dict) and str(record.get("id") or "") == request_id
    ), None)


def _find_record_by_token(data: Mapping[str, Any], token: str) -> dict[str, Any] | None:
    digest = _token_hash(token)
    return next((
        record for record in data.get(AIRCALL_REQUESTS_KEY, [])
        if isinstance(record, dict)
        and hmac.compare_digest(str(record.get("token_hash") or ""), digest)
    ), None)


def _callback_by_id(data: Mapping[str, Any], request_id: str) -> dict[str, Any] | None:
    return next((
        entry for entry in data.get("secretariat_demandes", [])
        if isinstance(entry, dict) and str(entry.get("id") or "") == request_id
    ), None)


def attach_aircall_summary_to_pending_request(
    data: dict[str, Any], lead: Mapping[str, Any], call_id: str,
) -> dict[str, Any] | None:
    """Conserve le résumé sans créer de piste tant que le formulaire est attendu."""
    phone = _normalize_phone(lead.get("telephone"))
    if not phone:
        return None
    now = _now()
    candidates = [
        record for record in data.get(AIRCALL_REQUESTS_KEY, [])
        if isinstance(record, dict)
        and record.get("status") == "pending"
        and record.get("sms_status") == "sent"
        and not _record_expired(record, now)
        and _normalize_phone(record.get("caller_phone")) == phone
    ]
    exact = [record for record in candidates if call_id and record.get("call_id") == call_id]
    candidates = exact or candidates
    if not candidates:
        return None
    record = max(candidates, key=lambda item: str(item.get("created_at") or ""))
    created_at = _parse_iso(record.get("created_at"))
    if not exact and (not created_at or (now - created_at).total_seconds() > 12 * 3600):
        return None

    record["call_id"] = call_id or record.get("call_id", "")
    record["call_summary"] = str(lead.get("summary") or lead.get("motif") or "").strip()[:3000]
    record["raw_training"] = str(lead.get("raw_training") or "").strip()[:300]
    if not record.get("formation_code"):
        record["formation_code"] = _training_key(
            lead.get("formation") or lead.get("raw_training")
        )
    record["updated_at"] = _iso(now)

    callback = _callback_by_id(data, str(record.get("id") or ""))
    if callback:
        details = ["Appel reçu par l’assistante IA Aircall."]
        if record.get("formation_code"):
            details.append(f"Formation demandée : {record['formation_code']}.")
        if record.get("call_summary"):
            details.append(f"Résumé : {record['call_summary']}")
        details.append("Coordonnées attendues via le formulaire envoyé par SMS.")
        callback["notes"] = " ".join(details)
        callback["formation"] = record.get("formation_code", "")
    return record


def _display_phone(value: Any) -> str:
    digits = _normalize_phone(value)
    if digits.startswith("33") and len(digits) == 11:
        local = "0" + digits[2:]
        return " ".join(local[index:index + 2] for index in range(0, 10, 2))
    return str(value or "").strip()


def _valid_name(value: Any) -> bool:
    text = " ".join(str(value or "").strip().split())
    return 2 <= len(text) <= 120 and any(char.isalpha() for char in text) and not any(
        char.isdigit() for char in text
    )


def _valid_email(value: Any) -> bool:
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", str(value or "").strip()))


def _form_values(request_obj: Any, record: Mapping[str, Any]) -> dict[str, str]:
    return {
        "prenom": " ".join(str(request_obj.form.get("prenom") or "").strip().split()),
        "nom": " ".join(str(request_obj.form.get("nom") or "").strip().split()),
        "email": str(request_obj.form.get("email") or "").strip().casefold(),
        "telephone": str(
            request_obj.form.get("telephone") or record.get("caller_phone") or ""
        ).strip(),
        "formation": str(request_obj.form.get("formation") or "").strip(),
        "message": str(request_obj.form.get("message") or "").strip()[:1500],
        "consent": str(request_obj.form.get("consent") or "").strip(),
    }


def _form_errors(values: Mapping[str, str]) -> dict[str, str]:
    errors = {}
    if not _valid_name(values.get("prenom")):
        errors["prenom"] = "Indiquez votre prénom."
    if not _valid_name(values.get("nom")):
        errors["nom"] = "Indiquez votre nom."
    if not _valid_email(values.get("email")):
        errors["email"] = "Indiquez une adresse e-mail valide."
    if not _normalize_phone(values.get("telephone")):
        errors["telephone"] = "Indiquez un numéro de téléphone valide."
    if not _training_key(values.get("formation")):
        errors["formation"] = "Choisissez la formation qui vous intéresse."
    if values.get("consent") not in {"1", "on", "oui", "true"}:
        errors["consent"] = "Votre accord est nécessaire pour être recontacté."
    return errors


def _activity_detail(record: Mapping[str, Any], values: Mapping[str, str], label: str) -> str:
    lines = [f"Formation demandée : {label}.", "Coordonnées transmises via le SMS Aircall."]
    if values.get("message"):
        lines.append(f"Précisions du prospect : {values['message']}")
    if record.get("call_summary"):
        lines.append(f"Résumé de l’appel : {record['call_summary']}")
    if record.get("call_id"):
        lines.append(f"Identifiant Aircall : {record['call_id']}.")
    return "\n".join(lines)[:6000]


def _complete_form(
    legacy_app: Any, data: dict[str, Any], record: dict[str, Any], values: Mapping[str, str],
) -> tuple[dict[str, Any] | None, bool]:
    code = _training_key(values.get("formation"))
    details = getattr(legacy_app, "SECRETARIAT_FORMATIONS", {}).get(code) or {}
    label = str(details.get("label") or details.get("short") or code)
    crm_formation = _CRM_TRAINING_LABELS.get(code, code)
    payload = {
        "prenom": values["prenom"],
        "nom": values["nom"],
        "mail": values["email"],
        "telephone": values["telephone"],
        "formation": crm_formation,
        "desp_type": "VAE" if code == "DESP_VAE" else ("INITIAL" if code == "DESP_INIT" else ""),
        "origine": AIRCALL_SMS_ORIGIN,
        "commentaire": values.get("message") or record.get("call_summary") or "",
        "notes": values.get("message") or record.get("call_summary") or "",
        "aircall_call_id": record.get("call_id", ""),
    }
    contact, inbound, created = legacy_app.find_or_create_crm_contact(
        data,
        payload,
        AIRCALL_SMS_SOURCE,
        external_id=str(record["id"]),
        ordered_coordinates=True,
        record_activity=False,
    )
    if contact:
        if created:
            contact.update({
                "source": AIRCALL_SMS_SOURCE,
                "origine": AIRCALL_SMS_ORIGIN,
                "statut": "Nouveaux",
            })
        if payload["desp_type"] and not str(contact.get("desp_type") or "").strip():
            contact["desp_type"] = payload["desp_type"]
        legacy_app._crm_activity(
            contact,
            "creation" if created else "inbound_request",
            "Formulaire SMS Aircall complété",
            _activity_detail(record, values, label),
            author_name="Formulaire Aircall",
        )
        contact["updated_at"] = legacy_app._crm_now()

    now = _now()
    submitted_status = "submitted" if contact else "submitted_review"
    record.update({
        "status": submitted_status,
        "submitted_at": _iso(now),
        "updated_at": _iso(now),
        "consent_at": _iso(now),
        "consent_version": "rappel-formation-v1",
        "contact_id": contact.get("id") if contact else "",
        "inbound_request_id": inbound.get("id") if inbound else "",
        "formation_code": code,
        "form_data": {
            "prenom": values["prenom"], "nom": values["nom"],
            "email": values["email"], "telephone": values["telephone"],
            "formation": code, "message": values.get("message", ""),
        },
    })
    callback = _callback_by_id(data, str(record["id"]))
    if callback:
        callback_notes = _activity_detail(record, values, label)
        if not contact:
            callback_notes += (
                "\nLes coordonnées correspondent à plusieurs fiches : "
                "rapprochement CRM à vérifier manuellement."
            )
        callback.update({
            "nom": f"{values['prenom']} {values['nom']}".strip(),
            "prenom": values["prenom"],
            "nom_famille": values["nom"],
            "email": values["email"],
            "telephone": values["telephone"],
            "formation": code,
            "notes": callback_notes,
            "crm_contact_id": contact.get("id") if contact else "",
            "callback_status": "processed" if contact else "pending",
            "callback_status_updated_at": _iso(now),
            "callback_processed_at": _iso(now) if contact else "",
            "callback_processed_by": "Formulaire SMS Aircall" if contact else "",
            "statut": "Traité" if contact else "À traiter",
        })
        ensure_callback_activity = getattr(legacy_app, "_crm_ensure_callback_request_activity", None)
        if contact and callable(ensure_callback_activity):
            ensure_callback_activity(contact, callback)

    if created and contact:
        data.setdefault("crm_notifications", []).insert(0, {
            "id": str(uuid.uuid4()),
            "date": legacy_app._crm_now(),
            "kind": "aircall_sms_lead",
            "text": "Nouvelle piste créée depuis le formulaire SMS Aircall.",
            "read": False,
            "contact_id": contact.get("id"),
            "contact_name": f"{contact.get('prenom', '')} {contact.get('nom', '')}".strip(),
        })
    return contact, created


def register_aircall_lead_capture(legacy_app: Any) -> None:
    """Enregistre les deux actions Aircall et le formulaire public à jeton unique."""
    if getattr(legacy_app, "_aircall_lead_capture_registered", False):
        return
    app = legacy_app.app
    request_obj = legacy_app.request
    jsonify_fn = legacy_app.jsonify
    render_template_fn = legacy_app.render_template

    def render_lead_form_response(*, state: str, status: int = 200, **context: Any):
        html = render_template_fn("aircall_lead_form.html", state=state, **context)
        response = app.make_response((html, status))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    def training_information():
        if failure := _authenticate(request_obj, jsonify_fn):
            return failure
        payload = request_obj.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify_fn({"ok": False, "error": "Le corps JSON est invalide."}), 400
        body, status = _training_information_payload(legacy_app, legacy_app.load_data(), payload)
        return jsonify_fn(body), status

    def send_lead_sms():
        if failure := _authenticate(request_obj, jsonify_fn):
            return failure
        payload = request_obj.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify_fn({"ok": False, "error": "Le corps JSON est invalide."}), 400
        phone_raw = payload.get("caller_phone") or payload.get("telephone")
        phone = _normalize_phone(phone_raw)
        if not phone:
            return jsonify_fn({
                "ok": True,
                "success": False,
                "sms_sent": False,
                "requires_human": True,
                "message": (
                    "Je ne peux pas envoyer le formulaire car le numéro de l'appelant "
                    "n'est pas disponible."
                ),
            }), 200
        call_id = str(payload.get("call_id") or "").strip()[:200]
        code = _training_key(payload.get("formation") or payload.get("training"))
        catalogue = getattr(legacy_app, "SECRETARIAT_FORMATIONS", {})
        details = catalogue.get(code) or {}
        label = str(details.get("label") or details.get("short") or code).strip()

        with _REQUEST_LOCK:
            now = _now()
            with legacy_app._CRM_RECONCILIATION_LOCK:
                data = legacy_app.load_data()
                recent = _recent_request(data, phone, call_id, now)
                if recent and recent.get("sms_status") == "sent":
                    return jsonify_fn({
                        "ok": True,
                        "success": True,
                        "sms_sent": True,
                        "already_sent": True,
                        "request_id": recent.get("id"),
                        "message": "Le SMS contenant le formulaire a déjà été envoyé.",
                    }), 200
                if recent and recent.get("sms_status") == "sending":
                    return jsonify_fn({
                        "ok": True,
                        "success": False,
                        "sms_sent": False,
                        "retry_allowed": True,
                        "message": "L'envoi du SMS est encore en cours."
                    }), 200
                if _sms_rate_limited(data, phone, now):
                    return jsonify_fn({
                        "ok": True,
                        "success": False,
                        "sms_sent": False,
                        "requires_human": True,
                        "message": (
                            "Plusieurs SMS ont déjà été demandés pour ce numéro. "
                            "Un membre de l'équipe doit reprendre la demande."
                        ),
                    }), 200

                token = secrets.token_urlsafe(16)
                request_id = str(uuid.uuid4())
                record = {
                    "id": request_id,
                    "token_hash": _token_hash(token),
                    "caller_phone": str(phone_raw or "").strip(),
                    "normalized_phone": phone,
                    "formation_code": code,
                    "call_id": call_id,
                    "status": "pending",
                    "sms_status": "sending",
                    "created_at": _iso(now),
                    "updated_at": _iso(now),
                    "expires_at": _iso(now + dt.timedelta(days=_TOKEN_TTL_DAYS)),
                    "contact_id": "",
                }
                data.setdefault(AIRCALL_REQUESTS_KEY, []).insert(0, record)
                callback = _callback_entry(record, str(phone_raw or "").strip(), label)
                data.setdefault("secretariat_demandes", []).append(callback)
                prepare_callback = getattr(legacy_app, "_crm_prepare_callback_request", None)
                if callable(prepare_callback):
                    prepare_callback(data, callback)
                legacy_app.save_data(data)

            form_url = f"{_public_base_url(request_obj)}/rappel-formation/{token}"
            sms_body = _sms_text(form_url)
            sms_sent = bool(legacy_app.send_sms(str(phone_raw or ""), sms_body))

            with legacy_app._CRM_RECONCILIATION_LOCK:
                data = legacy_app.load_data()
                stored = _find_record_by_id(data, request_id)
                if stored:
                    stored["sms_status"] = "sent" if sms_sent else "failed"
                    stored["updated_at"] = _iso()
                    if sms_sent:
                        stored["sms_sent_at"] = _iso()
                    else:
                        stored["sms_failed_at"] = _iso()
                    callback = _callback_by_id(data, request_id)
                    if callback and not sms_sent:
                        callback["notes"] += " L’envoi automatique du SMS a échoué."
                    legacy_app.save_data(data)

        if not sms_sent:
            return jsonify_fn({
                "ok": True,
                "success": False,
                "sms_sent": False,
                "requires_human": True,
                "request_id": request_id,
                "message": (
                    "Le SMS n'a pas pu être envoyé. La demande a tout de même été "
                    "enregistrée pour l'équipe."
                ),
            }), 200
        return jsonify_fn({
            "ok": True,
            "success": True,
            "sms_sent": True,
            "already_sent": False,
            "request_id": request_id,
            "message": "Le SMS contenant le formulaire vient d'être envoyé.",
        }), 200

    def lead_form(token: str):
        with legacy_app._CRM_RECONCILIATION_LOCK:
            data = legacy_app.load_data()
            record = _find_record_by_token(data, token)
            record_copy = copy.deepcopy(record) if record else None
        if not record_copy:
            return render_lead_form_response(
                state="invalid", status=404, errors={}, values={},
                formations=_training_options(legacy_app), selected_formation="",
                display_phone="",
            )
        if _record_expired(record_copy):
            return render_lead_form_response(
                state="expired", status=410, errors={}, values={},
                formations=_training_options(legacy_app), selected_formation="",
                display_phone="",
            )
        if record_copy.get("status") in {"submitted", "submitted_review"}:
            return render_lead_form_response(
                state="submitted", errors={}, values={},
                formations=_training_options(legacy_app), selected_formation="",
                display_phone="",
            )

        values = {
            "prenom": "", "nom": "", "email": "",
            "telephone": str(record_copy.get("caller_phone") or ""),
            "formation": str(record_copy.get("formation_code") or ""),
            "message": "", "consent": "",
        }
        errors = {}
        if request_obj.method == "POST":
            values = _form_values(request_obj, record_copy)
            errors = _form_errors(values)
            if not errors:
                with legacy_app._CRM_RECONCILIATION_LOCK:
                    data = legacy_app.load_data()
                    stored = _find_record_by_token(data, token)
                    if not stored or _record_expired(stored):
                        return render_lead_form_response(
                            state="expired", status=410, errors={}, values={},
                            formations=_training_options(legacy_app), selected_formation="",
                            display_phone="",
                        )
                    if stored.get("status") not in {"submitted", "submitted_review"}:
                        _complete_form(legacy_app, data, stored, values)
                        legacy_app.save_data(data)
                return render_lead_form_response(
                    state="submitted", errors={}, values={},
                    formations=_training_options(legacy_app), selected_formation="",
                    display_phone="",
                )

        return render_lead_form_response(
            state="form",
            status=400 if errors else 200,
            errors=errors,
            values=values,
            formations=_training_options(legacy_app),
            selected_formation=values.get("formation") or record_copy.get("formation_code") or "",
            display_phone=_display_phone(values.get("telephone") or record_copy.get("caller_phone")),
        )

    app.add_url_rule(
        AIRCALL_TRAINING_INFORMATION_PATH,
        endpoint="aircall_training_information",
        view_func=training_information,
        methods=["POST"],
    )
    app.add_url_rule(
        AIRCALL_LEAD_SMS_PATH,
        endpoint="aircall_lead_capture_sms",
        view_func=send_lead_sms,
        methods=["POST"],
    )
    app.add_url_rule(
        AIRCALL_LEAD_FORM_PATH,
        endpoint="aircall_lead_capture_form",
        view_func=lead_form,
        methods=["GET", "POST"],
    )
    legacy_app._aircall_lead_capture_registered = True
