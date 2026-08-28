"""Contexte d'accueil personnalisé Aircall à partir du CRM.

Cette action est conçue pour être exécutée à la connexion de l'appel. Elle ne
révèle qu'un prénom avant confirmation et garde la formation / les dates pour
la question suivante, après que l'appelant a confirmé son identité.
"""

from __future__ import annotations

import datetime as dt
import hmac
import os
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping
from typing import Any, Callable
from zoneinfo import ZoneInfo


AIRCALL_CALLER_CONTEXT_PATH = "/api/integrations/aircall/caller-context"
AIRCALL_ACTIONS_SECRET_ENV = "AIRCALL_ACTIONS_API_KEY"
AIRCALL_ACTIONS_KEY_HEADER = "X-Aircall-Actions-Key"

_PARIS = ZoneInfo("Europe/Paris")
_MONTH_NAMES = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)
_MONTHS = {
    "janvier": 1,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
}
_FINAL_STATUSES = {"disqualifie"}
_CANCELED_APPOINTMENT_STATUSES = {"canceled", "cancelled", "annule", "annulee"}


def _strip_accents(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in text if not unicodedata.combining(char))


def _compact(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _strip_accents(value).casefold())


def _normalize_email(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().casefold())


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


def _display_first_name(value: Any) -> str:
    text = re.sub(r"[^A-Za-zÀ-ÿ'’\-\s]", "", str(value or ""))
    text = " ".join(text.replace("’", "'").split()).strip(" -'")
    if not text:
        return ""
    if text.isupper() or text.islower():
        text = "-".join(
            "'".join(part.capitalize() for part in token.split("'"))
            for token in text.split("-")
        )
    return text[:80]


def _contact_is_eligible(contact: Mapping[str, Any]) -> bool:
    if str(contact.get("archived_at") or "").strip():
        return False
    return _compact(contact.get("statut")) not in _FINAL_STATUSES


def _identity_key(contact: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        _compact(contact.get("prenom")),
        _compact(contact.get("nom")),
        _normalize_email(contact.get("mail")),
    )


def _contact_rank(contact: Mapping[str, Any]) -> tuple[int, str]:
    score = 0
    if str(contact.get("dates_formation") or "").strip():
        score += 40
    if str(contact.get("formation") or "").strip():
        score += 25
    if _compact(contact.get("statut")) == "converti":
        score += 20
    if str(contact.get("mail") or "").strip():
        score += 5
    if str(contact.get("prenom") or "").strip() and str(contact.get("nom") or "").strip():
        score += 10
    return score, str(contact.get("updated_at") or contact.get("created_at") or "")


def _select_contact(data: Mapping[str, Any], caller_phone: str) -> tuple[dict[str, Any] | None, bool]:
    phone = _normalize_phone(caller_phone)
    if not phone:
        return None, False
    matches = [
        contact for contact in data.get("crm_contacts", [])
        if isinstance(contact, dict)
        and _contact_is_eligible(contact)
        and _normalize_phone(contact.get("telephone")) == phone
    ]
    if not matches:
        return None, False
    if len(matches) == 1:
        return matches[0], False

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for contact in matches:
        key = _identity_key(contact)
        if not key[0] or not key[1]:
            key = (*key[:2], f"{key[2]}#{contact.get('id')}")
        groups[key].append(contact)
    if len(groups) != 1:
        return None, True
    same_person = next(iter(groups.values()))
    return max(same_person, key=_contact_rank), False


def _safe_date(year: int, month: int, day: int) -> dt.date | None:
    try:
        return dt.date(year, month, day)
    except (TypeError, ValueError):
        return None


def _extract_numeric_dates(text: str) -> list[dt.date]:
    dates: list[dt.date] = []
    for year, month, day in re.findall(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", text):
        if parsed := _safe_date(int(year), int(month), int(day)):
            dates.append(parsed)
    if len(dates) >= 2:
        return dates
    dates = []
    for day, month, year in re.findall(
        r"\b(\d{1,2})[/. -](\d{1,2})[/. -](20\d{2}|\d{2})\b",
        text,
    ):
        numeric_year = int(year)
        if numeric_year < 100:
            numeric_year += 2000
        if parsed := _safe_date(numeric_year, int(month), int(day)):
            dates.append(parsed)
    return dates


def _extract_textual_dates(text: str) -> list[dt.date]:
    normalized = _strip_accents(text).casefold()
    month_pattern = "|".join(_MONTHS)
    matches = list(re.finditer(
        rf"\b(\d{{1,2}})(?:er)?\s+({month_pattern})(?:\s+(20\d{{2}}))?\b",
        normalized,
    ))
    if len(matches) < 2:
        return []
    raw = [
        [int(match.group(1)), _MONTHS[match.group(2)], int(match.group(3)) if match.group(3) else None]
        for match in matches[:2]
    ]
    first, second = raw
    if first[2] is None and second[2] is not None:
        first[2] = second[2] if first[1] <= second[1] else second[2] - 1
    elif first[2] is not None and second[2] is None:
        second[2] = first[2] if second[1] >= first[1] else first[2] + 1
    elif first[2] is None and second[2] is None:
        years = [int(year) for year in re.findall(r"\b(20\d{2})\b", normalized)]
        if not years:
            return []
        second[2] = years[-1]
        first[2] = second[2] if first[1] <= second[1] else second[2] - 1
    parsed = [
        _safe_date(int(item[2]), int(item[1]), int(item[0]))
        for item in (first, second)
    ]
    return [item for item in parsed if item is not None]


def _date_range(value: Any) -> tuple[dt.date, dt.date] | None:
    text = str(value or "").strip()
    if not text:
        return None
    dates = _extract_numeric_dates(text)
    if len(dates) < 2:
        dates = _extract_textual_dates(text)
    if len(dates) < 2:
        return None
    start, end = dates[0], dates[1]
    if end < start:
        return None
    return start, end


def _day_label(day: int) -> str:
    return "1er" if day == 1 else str(day)


def _format_date_range(value: Any) -> str:
    parsed = _date_range(value)
    if not parsed:
        return ""
    start, end = parsed
    if start.year == end.year and start.month == end.month:
        return (
            f"du {_day_label(start.day)} au {_day_label(end.day)} "
            f"{_MONTH_NAMES[start.month - 1]} {start.year}"
        )
    if start.year == end.year:
        return (
            f"du {_day_label(start.day)} {_MONTH_NAMES[start.month - 1]} "
            f"au {_day_label(end.day)} {_MONTH_NAMES[end.month - 1]} {start.year}"
        )
    return (
        f"du {_day_label(start.day)} {_MONTH_NAMES[start.month - 1]} {start.year} "
        f"au {_day_label(end.day)} {_MONTH_NAMES[end.month - 1]} {end.year}"
    )


def _formation_spoken(contact: Mapping[str, Any]) -> str:
    formation = str(contact.get("formation") or "").strip()
    key = _compact(formation)
    if key == "a3p":
        return "A trois P"
    if key == "aps":
        return "A P S"
    if key in {"ssiap", "ssiap1"}:
        return "Siappe un"
    if key in {"chauffeurvtc", "vtc"}:
        return "chauffeur V T C"
    if key == "desp":
        subtype = _compact(contact.get("desp_type"))
        if subtype == "vae":
            return "D E S P, parcours V A E"
        if subtype == "initial":
            return "D E S P, parcours initial"
        return "D E S P"
    if key.startswith("bts"):
        suffix = formation[3:].strip()
        return "B T S" + (f" {suffix}" if suffix else "")
    return formation[:120]


def _parse_datetime(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(_PARIS)


def _next_appointment_label(data: Mapping[str, Any], contact_id: str, now: dt.datetime | None = None) -> str:
    reference = now or dt.datetime.now(_PARIS)
    candidates: list[dt.datetime] = []
    for appointment in data.get("crm_calendly_appointments", []):
        if not isinstance(appointment, dict):
            continue
        if str(appointment.get("contact_id") or "") != str(contact_id):
            continue
        if _compact(appointment.get("status")) in _CANCELED_APPOINTMENT_STATUSES:
            continue
        if _compact(appointment.get("response_status")) in {"answered", "noanswer"}:
            continue
        if (start := _parse_datetime(appointment.get("start_time"))) and start >= reference:
            candidates.append(start)
    if not candidates:
        return ""
    start = min(candidates)
    minute = f" {start.minute:02d}" if start.minute else ""
    return (
        f"le {_day_label(start.day)} {_MONTH_NAMES[start.month - 1]} {start.year} "
        f"à {start.hour} heures{minute}"
    )


def build_caller_context(
    data: Mapping[str, Any],
    caller_phone: Any,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    contact, ambiguous = _select_contact(data, str(caller_phone or ""))
    generic = {
        "ok": True,
        "success": True,
        "matched": False,
        "ambiguous": ambiguous,
        "personalization_available": False,
        "identity_confirmation_required": False,
        "first_name": "",
        "formation": "",
        "formation_spoken": "",
        "session_label": "",
        "session_spoken": "",
        "location": "",
        "next_appointment_label": "",
        "greeting_message": "",
        "context_question": "",
        "fallback_prompt": "Comment puis-je vous renseigner ?",
    }
    if not contact:
        return generic

    first_name = _display_first_name(contact.get("prenom"))
    if not first_name:
        return {**generic, "matched": True}

    formation = str(contact.get("formation") or "").strip()
    formation_spoken = _formation_spoken(contact)
    session_label = str(contact.get("dates_formation") or "").strip()
    session_spoken = _format_date_range(session_label)
    next_appointment = _next_appointment_label(
        data,
        str(contact.get("id") or ""),
        now=now,
    )

    if formation_spoken and session_spoken:
        context_question = (
            f"Appelez-vous au sujet de votre formation {formation_spoken}, "
            f"prévue {session_spoken}, ou pour une autre demande ?"
        )
    elif formation_spoken:
        context_question = (
            f"Appelez-vous au sujet de votre formation {formation_spoken}, "
            "ou pour une autre demande ?"
        )
    elif next_appointment:
        context_question = (
            f"Appelez-vous au sujet de votre rendez-vous prévu {next_appointment}, "
            "ou pour une autre demande ?"
        )
    else:
        context_question = (
            "Appelez-vous au sujet de votre dossier chez Intégrale Academy, "
            "ou pour une autre demande ?"
        )

    return {
        **generic,
        "matched": True,
        "personalization_available": True,
        "identity_confirmation_required": True,
        "first_name": first_name,
        "formation": formation,
        "formation_spoken": formation_spoken,
        "session_label": session_label,
        "session_spoken": session_spoken,
        "location": str(contact.get("lieu") or "").strip(),
        "next_appointment_label": next_appointment,
        "greeting_message": (
            f"{first_name}, j'espère que vous allez bien. "
            f"Est-ce bien {first_name} à l'appareil ?"
        ),
        "context_question": context_question,
        "fallback_prompt": "Comment puis-je vous renseigner ?",
    }


def register_aircall_caller_context(legacy_app: Any) -> None:
    """Enregistre la recherche CRM exécutée à la connexion d'un appel."""
    if getattr(legacy_app, "_aircall_caller_context_registered", False):
        return

    app = legacy_app.app
    request_obj = legacy_app.request
    jsonify_fn = legacy_app.jsonify

    def caller_context():
        if failure := _authenticate(request_obj, jsonify_fn):
            return failure
        payload = request_obj.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify_fn({"ok": False, "error": "Le corps JSON est invalide."}), 400
        caller_phone = (
            payload.get("caller_phone")
            or payload.get("external_caller_number")
            or payload.get("phone")
            or payload.get("telephone")
            or ""
        )
        with legacy_app._CRM_RECONCILIATION_LOCK:
            data = legacy_app.load_data()
            response = build_caller_context(data, caller_phone)
        return jsonify_fn(response), 200

    app.add_url_rule(
        AIRCALL_CALLER_CONTEXT_PATH,
        endpoint="aircall_caller_crm_context",
        view_func=caller_context,
        methods=["POST"],
    )
    legacy_app._aircall_caller_context_registered = True
