"""Consultation sécurisée du statut CNAPS pendant un appel Aircall IA."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
import unicodedata
import uuid
from collections.abc import Mapping
from typing import Any, Callable
from zoneinfo import ZoneInfo

from candidate_scoring import normalize_cnaps_tracking_status


AIRCALL_DOSSIER_HEALTH_PATH = "/api/integrations/aircall/dossier/health"
AIRCALL_DOSSIER_START_PATH = "/api/integrations/aircall/dossier/verification/start"
AIRCALL_DOSSIER_STATUS_PATH = "/api/integrations/aircall/dossier/status"
AIRCALL_ACTIONS_SECRET_ENV = "AIRCALL_ACTIONS_API_KEY"
AIRCALL_ACTIONS_KEY_HEADER = "X-Aircall-Actions-Key"

_VERIFICATION_LOCK = threading.RLock()
_VERIFICATIONS: dict[str, dict[str, Any]] = {}
_START_HISTORY: dict[str, list[float]] = {}

_STATUS_MESSAGES = {
    "registered": (
        "Votre demande CNAPS est enregistrée, mais elle n'est pas encore indiquée comme transmise.",
        "Notre équipe doit encore vérifier ou finaliser sa transmission.",
        False,
    ),
    "transmitted": (
        "Votre demande a bien été transmise au CNAPS et attend maintenant son traitement.",
        "Aucune action particulière n'est indiquée pour le moment.",
        False,
    ),
    "in_review": (
        "Votre demande CNAPS est actuellement en cours d'instruction. Aucune décision définitive n'est encore indiquée.",
        "Aucune action particulière n'est indiquée pour le moment.",
        False,
    ),
    "accepted": (
        "Votre demande CNAPS est indiquée comme acceptée.",
        "Un membre de notre équipe pourra vous accompagner pour la suite de votre inscription.",
        False,
    ),
    "refused": (
        "Votre demande CNAPS est indiquée comme refusée.",
        "Pour des raisons de confidentialité, un membre de notre équipe doit reprendre votre dossier avec vous.",
        True,
    ),
    "no_result": (
        "Aucun statut CNAPS suffisamment fiable n'a été retrouvé pour votre dossier.",
        "Un membre de notre équipe doit vérifier la situation et revenir vers vous.",
        True,
    ),
    "unknown": (
        "Je ne dispose pas actuellement d'un statut CNAPS suffisamment fiable pour vous répondre.",
        "Un membre de notre équipe doit vérifier la situation et revenir vers vous.",
        True,
    ),
}

_MONTHS_FR = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name) or default).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _ttl_seconds() -> int:
    return _int_env("AIRCALL_DOSSIER_CODE_TTL_SECONDS", 300, 120, 900)


def _max_code_attempts() -> int:
    return _int_env("AIRCALL_DOSSIER_MAX_CODE_ATTEMPTS", 5, 3, 10)


def _start_limit() -> int:
    return _int_env("AIRCALL_DOSSIER_START_LIMIT", 3, 1, 10)


def _start_window_seconds() -> int:
    return _int_env("AIRCALL_DOSSIER_START_WINDOW_SECONDS", 900, 300, 3600)


def _compact(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text)


def _normalize_email(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or "").strip().casefold())
    if text.count("@") != 1:
        return ""
    local, domain = text.split("@", 1)
    return text if local and "." in domain and not domain.startswith(".") else ""


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


def _find_verified_contact(data: Mapping[str, Any], payload: Mapping[str, Any]):
    first_name = _compact(payload.get("first_name") or payload.get("prenom"))
    last_name = _compact(payload.get("last_name") or payload.get("nom"))
    phone = _normalize_phone(payload.get("caller_phone") or payload.get("telephone"))
    email = _normalize_email(payload.get("email") or payload.get("mail"))
    if not first_name or not last_name or not (phone or email):
        return None

    contacts = [item for item in data.get("crm_contacts", []) if isinstance(item, dict)]
    phone_matches = {
        str(contact.get("id")): contact
        for contact in contacts
        if phone and _normalize_phone(contact.get("telephone")) == phone
    }
    email_matches = {
        str(contact.get("id")): contact
        for contact in contacts
        if email and _normalize_email(contact.get("mail")) == email
    }

    if phone_matches and email_matches:
        candidates = {
            contact_id: contact
            for contact_id, contact in phone_matches.items()
            if contact_id in email_matches
        }
        if not candidates:
            return None
    else:
        candidates = phone_matches or email_matches

    identity_matches = [
        contact for contact in candidates.values()
        if _compact(contact.get("prenom")) == first_name
        and _compact(contact.get("nom")) == last_name
    ]
    return identity_matches[0] if len(identity_matches) == 1 else None


def _hash_code(secret: str, verification_id: str, code: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"{verification_id}:{code}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _cleanup_verifications_locked(now: float) -> None:
    for verification_id in [
        key for key, record in _VERIFICATIONS.items()
        if float(record.get("expires_at") or 0) <= now
    ]:
        _VERIFICATIONS.pop(verification_id, None)
    window_start = now - _start_window_seconds()
    for key, timestamps in list(_START_HISTORY.items()):
        kept = [timestamp for timestamp in timestamps if timestamp >= window_start]
        if kept:
            _START_HISTORY[key] = kept
        else:
            _START_HISTORY.pop(key, None)


def _register_verification(contact_id: str, secret: str) -> tuple[str, str] | None:
    now = time.time()
    with _VERIFICATION_LOCK:
        _cleanup_verifications_locked(now)
        history = _START_HISTORY.setdefault(contact_id, [])
        if len(history) >= _start_limit():
            return None
        history.append(now)
        verification_id = f"verif_{uuid.uuid4().hex}"
        code = str(100_000 + secrets.randbelow(900_000))
        _VERIFICATIONS[verification_id] = {
            "contact_id": contact_id,
            "code_hash": _hash_code(secret, verification_id, code),
            "expires_at": now + _ttl_seconds(),
            "attempts": 0,
        }
        return verification_id, code


def _delete_verification(verification_id: str) -> None:
    with _VERIFICATION_LOCK:
        _VERIFICATIONS.pop(verification_id, None)


def _verify_code(secret: str, verification_id: str, code: str) -> tuple[str, str]:
    now = time.time()
    with _VERIFICATION_LOCK:
        _cleanup_verifications_locked(now)
        record = _VERIFICATIONS.get(verification_id)
        if not record:
            return "", "expired_or_unknown"
        attempts = int(record.get("attempts") or 0)
        if attempts >= _max_code_attempts():
            _VERIFICATIONS.pop(verification_id, None)
            return "", "too_many_attempts"
        expected = str(record.get("code_hash") or "")
        supplied = _hash_code(secret, verification_id, code)
        if not expected or not hmac.compare_digest(expected, supplied):
            record["attempts"] = attempts + 1
            if record["attempts"] >= _max_code_attempts():
                _VERIFICATIONS.pop(verification_id, None)
                return "", "too_many_attempts"
            return "", "invalid_code"
        contact_id = str(record.get("contact_id") or "")
        _VERIFICATIONS.pop(verification_id, None)
        return contact_id, "verified"


def _extract_response(remote: Any) -> tuple[dict[str, Any], int]:
    response, status = remote, None
    if isinstance(remote, tuple):
        response = remote[0]
        status = remote[1] if len(remote) > 1 else None
    if status is None:
        status = int(getattr(response, "status_code", 200) or 200)
    if hasattr(response, "get_json"):
        payload = response.get_json(silent=True) or {}
    elif isinstance(response, dict):
        payload = response
    else:
        payload = {}
    return payload if isinstance(payload, dict) else {}, status


def _default_cnaps_lookup(legacy_app: Any, contact: dict[str, Any]):
    from crm_cnaps_tracking import proxy_reglementaire, scoring_snapshot_from_remote

    try:
        payload, status = _extract_response(proxy_reglementaire(legacy_app.app, contact))
    except Exception as exc:  # pragma: no cover - réseau réel
        legacy_app.app.logger.warning(
            "Suivi CNAPS Aircall indisponible (%s)", type(exc).__name__,
        )
        return None
    if status == 200 or (status == 404 and payload.get("reason") == "cnaps_not_found"):
        return scoring_snapshot_from_remote(payload, http_status=status)
    return None


def _cached_snapshot(data: Mapping[str, Any], contact_id: str):
    snapshot = (data.get("crm_cnaps_scoring_snapshots") or {}).get(str(contact_id))
    return copy.deepcopy(snapshot) if isinstance(snapshot, dict) else None


def _format_checked_at(snapshot: Mapping[str, Any] | None) -> str:
    if not snapshot:
        return ""
    raw = snapshot.get("last_checked_at") or snapshot.get("synced_at")
    if not raw:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        parsed = parsed.astimezone(ZoneInfo("Europe/Paris"))
    except (TypeError, ValueError):
        return ""
    return (
        f"Dernière vérification le {parsed.day} {_MONTHS_FR[parsed.month - 1]} "
        f"{parsed.year} à {parsed.hour} h {parsed.minute:02d}."
    )


def _status_response(snapshot: Mapping[str, Any] | None, formation: str, *, live: bool):
    status = normalize_cnaps_tracking_status(snapshot or {})
    if status not in _STATUS_MESSAGES:
        status = "unknown"
    status_message, next_step_message, requires_human = _STATUS_MESSAGES[status]
    last_checked_message = _format_checked_at(snapshot)
    if snapshot and not live:
        freshness_message = "Il s'agit du dernier statut enregistré dans votre dossier."
    else:
        freshness_message = ""
    spoken_response = " ".join(filter(None, [
        status_message,
        last_checked_message,
        freshness_message,
        next_step_message,
    ]))
    return {
        "ok": True,
        "success": True,
        "identity_verified": True,
        "formation": formation,
        "cnaps_status": status,
        "status_message": status_message,
        "last_checked_message": last_checked_message,
        "freshness_message": freshness_message,
        "next_step_message": next_step_message,
        "spoken_response": spoken_response,
        "requires_human": requires_human,
    }


def register_aircall_dossier_actions(
    legacy_app: Any,
    *,
    cnaps_lookup: Callable[[Any, dict[str, Any]], dict[str, Any] | None] | None = None,
) -> None:
    """Enregistre les actions Aircall de vérification et de consultation CNAPS."""
    if getattr(legacy_app, "_aircall_dossier_actions_registered", False):
        return

    app = legacy_app.app
    request_obj = legacy_app.request
    jsonify_fn = legacy_app.jsonify
    lookup = cnaps_lookup or _default_cnaps_lookup

    def health():
        if failure := _authenticate(request_obj, jsonify_fn):
            return failure
        return jsonify_fn({"ok": True, "service": "aircall_dossier_actions"}), 200

    def start_verification():
        if failure := _authenticate(request_obj, jsonify_fn):
            return failure
        payload = request_obj.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify_fn({"ok": False, "error": "Le corps JSON est invalide."}), 400

        with legacy_app._CRM_RECONCILIATION_LOCK:
            data = legacy_app.load_data()
            contact = _find_verified_contact(data, payload)
            contact_copy = copy.deepcopy(contact) if contact else None

        generic_failure = {
            "ok": True,
            "success": False,
            "verification_sent": False,
            "requires_human": True,
            "reason": "automatic_verification_unavailable",
            "message": (
                "Je n'ai pas pu vérifier automatiquement votre identité. "
                "Un membre de notre équipe doit reprendre votre demande."
            ),
        }
        if not contact_copy:
            return jsonify_fn(generic_failure), 200
        destination = str(contact_copy.get("telephone") or "").strip()
        if not _normalize_phone(destination):
            return jsonify_fn(generic_failure), 200

        secret = str(os.getenv(AIRCALL_ACTIONS_SECRET_ENV) or "").strip()
        registered = _register_verification(str(contact_copy.get("id") or ""), secret)
        if not registered:
            return jsonify_fn({
                **generic_failure,
                "reason": "rate_limited",
                "message": (
                    "Trop de codes ont été demandés récemment. "
                    "Un membre de notre équipe doit reprendre votre demande."
                ),
            }), 200
        verification_id, code = registered
        minutes = max(1, _ttl_seconds() // 60)
        sms_body = (
            f"Intégrale Academy : votre code de vérification est {code}. "
            f"Il expire dans {minutes} minutes. Communiquez-le uniquement à "
            "l'assistante virtuelle pendant cet appel."
        )
        if not legacy_app.send_sms(destination, sms_body):
            _delete_verification(verification_id)
            return jsonify_fn({
                **generic_failure,
                "reason": "sms_unavailable",
                "message": (
                    "Le code de vérification n'a pas pu être envoyé. "
                    "Un membre de notre équipe doit reprendre votre demande."
                ),
            }), 200

        return jsonify_fn({
            "ok": True,
            "success": True,
            "verification_sent": True,
            "verification_id": verification_id,
            "requires_human": False,
            "message": (
                "Un code de vérification à six chiffres a été envoyé au numéro "
                "enregistré dans votre dossier."
            ),
        }), 200

    def dossier_status():
        if failure := _authenticate(request_obj, jsonify_fn):
            return failure
        payload = request_obj.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify_fn({"ok": False, "error": "Le corps JSON est invalide."}), 400
        verification_id = str(payload.get("verification_id") or "").strip()
        code = re.sub(r"\D", "", str(payload.get("verification_code") or ""))
        if not verification_id or len(code) != 6:
            return jsonify_fn({
                "ok": True,
                "success": False,
                "identity_verified": False,
                "retry_allowed": True,
                "requires_human": False,
                "reason": "invalid_code_format",
                "message": "Le code doit contenir exactement six chiffres.",
            }), 200

        secret = str(os.getenv(AIRCALL_ACTIONS_SECRET_ENV) or "").strip()
        contact_id, outcome = _verify_code(secret, verification_id, code)
        if outcome != "verified":
            retry_allowed = outcome in {"invalid_code"}
            return jsonify_fn({
                "ok": True,
                "success": False,
                "identity_verified": False,
                "retry_allowed": retry_allowed,
                "requires_human": not retry_allowed,
                "reason": outcome,
                "message": (
                    "Le code est incorrect. Vous pouvez le dicter une nouvelle fois."
                    if retry_allowed else
                    "Le code n'est plus valide. Un membre de notre équipe doit reprendre votre demande."
                ),
            }), 200

        with legacy_app._CRM_RECONCILIATION_LOCK:
            data = legacy_app.load_data()
            contact = legacy_app._crm_contact(data, contact_id)
            if not contact:
                return jsonify_fn({
                    "ok": True,
                    "success": False,
                    "identity_verified": True,
                    "requires_human": True,
                    "reason": "contact_unavailable",
                    "message": "Le dossier ne peut pas être consulté automatiquement.",
                }), 200
            contact_copy = copy.deepcopy(contact)
            cached = _cached_snapshot(data, contact_id)

        formation = str(contact_copy.get("formation") or "").strip()
        live_snapshot = None
        if formation.upper() in {"APS", "A3P"}:
            live_snapshot = lookup(legacy_app, contact_copy)
        snapshot = live_snapshot or cached
        response_payload = _status_response(
            snapshot,
            formation,
            live=bool(live_snapshot),
        )

        with legacy_app._CRM_RECONCILIATION_LOCK:
            latest_data = legacy_app.load_data()
            latest_contact = legacy_app._crm_contact(latest_data, contact_id)
            if latest_contact:
                if live_snapshot:
                    latest_data.setdefault("crm_cnaps_scoring_snapshots", {})[
                        str(contact_id)
                    ] = live_snapshot
                detail = (
                    "Identité vérifiée par code SMS. "
                    f"Information communiquée : {response_payload['status_message']}"
                )
                if response_payload["last_checked_message"]:
                    detail += f" {response_payload['last_checked_message']}"
                legacy_app._crm_activity(
                    latest_contact,
                    "dossier",
                    "Statut CNAPS consulté par l'assistante IA",
                    detail,
                    author_name="Assistante IA Aircall",
                )
                latest_contact["updated_at"] = legacy_app._crm_now()
                legacy_app.save_data(latest_data)

        return jsonify_fn(response_payload), 200

    app.add_url_rule(
        AIRCALL_DOSSIER_HEALTH_PATH,
        endpoint="aircall_dossier_actions_health",
        view_func=health,
        methods=["GET"],
    )
    app.add_url_rule(
        AIRCALL_DOSSIER_START_PATH,
        endpoint="aircall_dossier_verification_start",
        view_func=start_verification,
        methods=["POST"],
    )
    app.add_url_rule(
        AIRCALL_DOSSIER_STATUS_PATH,
        endpoint="aircall_dossier_status",
        view_func=dossier_status,
        methods=["POST"],
    )
    legacy_app._aircall_dossier_actions_registered = True
