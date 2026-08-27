"""Création sécurisée de pistes CRM depuis l'agent vocal IA Aircall."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any, Callable

AIRCALL_AI_WEBHOOK_PATH = "/api/integrations/aircall/ai-voice-agent"
AIRCALL_AI_SOURCE = "aircall_ai_voice_agent"
AIRCALL_AI_ORIGIN = "Aircall – Assistante IA"
AIRCALL_AI_SUMMARY_EVENT = "ai_voice_agent.summary"

_QUESTION_KEYS = {
    "question", "questions", "label", "title", "prompt", "field", "fieldname",
    "questiontext", "name", "key",
}
_ANSWER_KEYS = {"answer", "answers", "value", "values", "response", "responses", "text"}


def _compact(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text)


def _words(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _text(value: Any, limit: int = 3_000) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Mapping):
        for key in ("text", "answer", "value", "response", "content", "summary"):
            if key in value and (result := _text(value[key], limit)):
                return result
        return ""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return ", ".join(filter(None, (_text(item, limit) for item in value)))[:limit]
    return " ".join(str(value).strip().split())[:limit]


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def _flatten(value: Any, prefix: str = "", depth: int = 0) -> list[tuple[str, str]]:
    """Flatten nested Aircall data and preserve question/answer associations."""
    if depth > 8:
        return []
    rows: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        question = answer = None
        for key, item in value.items():
            canonical = _compact(key)
            if canonical in _QUESTION_KEYS and question is None:
                question = item
            if canonical in _ANSWER_KEYS and answer is None:
                answer = item
        if question is not None and answer is not None:
            questions, answers = _as_list(question), _as_list(answer)
            if len(questions) == len(answers):
                for raw_question, raw_answer in zip(questions, answers):
                    if (question_text := _text(raw_question, 500)) and (
                        answer_text := _text(raw_answer)
                    ):
                        rows.append((_compact(question_text), answer_text))

        for key, item in value.items():
            child_prefix = f"{prefix} {key}".strip()
            if isinstance(item, (Mapping, list, tuple)):
                rows.extend(_flatten(item, child_prefix, depth + 1))
            elif scalar := _text(item):
                rows.append((_compact(key), scalar))
                if prefix:
                    rows.append((_compact(child_prefix), scalar))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            rows.extend(_flatten(item, prefix, depth + 1))
    return rows


def _pick(rows: list[tuple[str, str]], predicate: Callable[[str], bool]) -> str:
    return next((value for key, value in rows if value and predicate(key)), "")


def _is_first_name(key: str) -> bool:
    return key in {"prenom", "firstname", "givenname"} or any(
        marker in key for marker in ("votreprenom", "callerfirstname", "clientfirstname")
    )


def _is_last_name(key: str) -> bool:
    if "prenom" in key or "firstname" in key:
        return False
    return key == "nom" or any(
        marker in key for marker in ("nomdefamille", "lastname", "surname", "familyname")
    )


def _is_email(key: str) -> bool:
    return any(marker in key for marker in ("email", "adressemail", "adresseemail", "courriel"))


def _is_phone(key: str) -> bool:
    return any(marker in key for marker in (
        "telephone", "phone", "rawdigits", "callernumber", "externalnumber", "mobile",
    ))


def _is_training(key: str) -> bool:
    return any(marker in key for marker in ("formation", "training", "course"))


def _is_interest(key: str) -> bool:
    return any(marker in key for marker in (
        "interess", "interest", "prospect", "qualification", "readytoapply",
        "souhaitezvousvousinscrire", "wanttoenrol", "wanttoenroll",
    ))


def _path(payload: Mapping[str, Any], *parts: str) -> Any:
    current: Any = payload
    for part in parts:
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _first_path(payload: Mapping[str, Any], paths: Sequence[Sequence[str]], limit=500) -> str:
    for path in paths:
        if value := _text(_path(payload, *path), limit):
            return value
    return ""


def _normalize_email(value: str) -> str:
    email = re.sub(r"\s+", "", value or "").casefold()
    if email.count("@") != 1:
        return ""
    local, domain = email.split("@", 1)
    return email[:254] if local and "." in domain and not domain.startswith(".") else ""


def _normalize_phone(value: str) -> str:
    raw = " ".join(str(value or "").strip().split())
    return raw[:40] if 8 <= len(re.sub(r"\D", "", raw)) <= 15 else ""


def normalize_training(value: Any) -> tuple[str, str]:
    """Return the CRM training label and optional DESP subtype."""
    text = _words(value)
    compact = text.replace(" ", "")
    if not text:
        return "", ""

    bts = {
        "BTS MOS": (r"\bmos\b", "management operationnel de la securite"),
        "BTS MCO": (r"\bmco\b", "management commercial operationnel"),
        "BTS NDRC": (r"\bndrc\b", "negociation et digitalisation"),
        "BTS PI": (r"\bpi\b", "professions immobilieres"),
        "BTS CG": (r"\bcg\b", "comptabilite gestion"),
        "BTS CI": (r"\bci\b", "commerce international"),
    }
    if "bts" in text or any(phrase in text for _, phrase in bts.values()):
        for label, (pattern, phrase) in bts.items():
            if re.search(pattern, text) or phrase in text:
                return label, ""

    if any(term in text for term in (
        "protection physique", "protection rapprochee", "garde du corps",
    )) or "a3p" in compact or re.search(r"\bapr\b", text):
        return "A3P", ""
    if "desp" in text or ("dirigeant" in text and "securite" in text):
        subtype = "VAE" if "vae" in text or "validation des acquis" in text else ""
        return "DESP", subtype or ("INITIAL" if "initial" in text else "")
    if "ssiap" in text or "securite incendie" in text:
        return "SSIAP 1", ""
    if re.search(r"\bvtc\b", text) or "chauffeur" in text:
        return "Chauffeur VTC", ""
    if re.search(r"\baps\b", text) or any(term in text for term in (
        "agent de prevention", "agent de securite privee",
    )):
        return "APS", ""
    return "", ""


def _interest_state(rows: list[tuple[str, str]], summary: str) -> bool | None:
    value = _words(_pick(rows, _is_interest))
    if value in {"non", "no", "false", "0", "pas interesse", "non interesse"}:
        return False
    if value in {"oui", "yes", "true", "1", "interesse", "prospect", "lead", "qualified"}:
        return True
    summary_words = _words(summary)
    if any(phrase in summary_words for phrase in (
        "ne souhaite pas la formation", "n est pas interesse", "pas interesse par la formation",
    )):
        return False
    return None


def _is_training_prospect(raw_training: str, summary: str, explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    raw_words = _words(raw_training)
    if raw_words:
        practical = any(term in raw_words for term in (
            "adresse", "horaires", "horaire", "stationnement", "parking", "acces",
        )) and not any(term in raw_words for term in (
            "formation", "training", "course", "inscription", "financement",
        ))
        return not practical
    summary_words = _words(summary)
    return any(phrase in summary_words for phrase in (
        "interesse par une formation", "interesse par la formation",
        "souhaite des renseignements sur une formation",
        "souhaite des renseignements sur la formation", "veut s inscrire",
        "souhaite s inscrire", "projet de formation", "demande un devis",
        "financement de la formation", "demande le tarif de la formation",
        "souhaite connaitre le tarif", "demande les prochaines dates",
        "souhaite connaitre les prochaines dates", "demande des renseignements sur",
        "interested in a course", "interested in training", "wants to enroll",
        "wants to enrol",
    ))


def parse_aircall_lead(payload: Mapping[str, Any]) -> dict[str, str | bool]:
    rows = _flatten(payload)
    first_name = _pick(rows, _is_first_name)
    last_name = _pick(rows, _is_last_name)
    if not first_name and not last_name:
        full_name = _pick(rows, lambda key: key in {"fullname", "nomcomplet", "identitecomplete"})
        parts = full_name.split()
        first_name, last_name = (parts[0], " ".join(parts[1:])) if parts else ("", "")

    email = _normalize_email(_pick(rows, _is_email))
    caller_phone = _first_path(payload, (
        ("data", "call", "raw_digits"), ("data", "call", "phone_number"),
        ("data", "raw_digits"), ("data", "caller_number"), ("data", "phone_number"),
        ("call", "raw_digits"), ("call", "phone_number"), ("raw_digits",),
    ))
    phone = _normalize_phone(_pick(rows, _is_phone)) or _normalize_phone(caller_phone)
    raw_training = _pick(rows, _is_training)
    summary = _first_path(payload, (
        ("data", "summary", "text"), ("data", "summary", "content"),
        ("data", "summary"), ("data", "call_summary"), ("data", "call", "summary"),
        ("data", "ai_voice_agent", "summary"), ("summary", "text"), ("summary",),
    ), 3_000) or _pick(rows, lambda key: key in {
        "summary", "callsummary", "resume", "resumedelappel", "notes", "commentaire",
    })
    formation, desp_type = normalize_training(raw_training or summary)
    interest = _interest_state(rows, summary)
    return {
        "prenom": first_name[:120], "nom": last_name[:120], "mail": email,
        "telephone": phone, "formation": formation, "raw_training": raw_training[:300],
        "desp_type": desp_type, "summary": summary[:3_000],
        "interested": _is_training_prospect(raw_training, summary, interest),
        "explicitly_not_interested": interest is False,
    }


def _call_id(payload: Mapping[str, Any], lead: Mapping[str, Any]) -> str:
    call_id = _first_path(payload, (
        ("data", "call", "id"), ("data", "call_id"), ("data", "id"),
        ("call", "id"), ("call_id",), ("id",),
    ), 200)
    if call_id:
        return call_id
    fingerprint = json.dumps({
        key: lead.get(key, "") for key in (
            "prenom", "nom", "mail", "telephone", "formation", "raw_training", "summary",
        )
    }, sort_keys=True, ensure_ascii=False)
    return "fingerprint-" + hashlib.sha256(fingerprint.encode()).hexdigest()[:32]


def _provided_token(request_obj: Any, payload: Mapping[str, Any]) -> str:
    if token := str(request_obj.headers.get("X-Aircall-Webhook-Token") or "").strip():
        return token
    authorization = str(request_obj.headers.get("Authorization") or "").strip()
    return authorization[7:].strip() if authorization.casefold().startswith("bearer ") else str(
        payload.get("token") or ""
    ).strip()


def _activity_detail(lead: Mapping[str, Any], call_id: str) -> str:
    training = lead.get("formation") or lead.get("raw_training") or "À préciser"
    lines = [f"Formation demandée : {training}."]
    if lead.get("summary"):
        lines.append(f"Résumé de l’appel : {lead['summary']}")
    if call_id:
        lines.append(f"Identifiant Aircall : {call_id}.")
    return "\n".join(lines)[:4_000]


def register_aircall_ai_crm(legacy_app: Any) -> None:
    """Register the authenticated Aircall AI summary webhook on the CRM app."""
    if getattr(legacy_app, "_aircall_ai_crm_registered", False):
        return
    app, request_obj, jsonify_fn = legacy_app.app, legacy_app.request, legacy_app.jsonify

    def webhook():
        payload = request_obj.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify_fn({"ok": False, "error": "Le corps JSON Aircall est invalide."}), 400
        expected = str(os.getenv("AIRCALL_WEBHOOK_TOKEN") or "").strip()
        if not expected:
            return jsonify_fn({"ok": False, "error": "La connexion Aircall n’est pas configurée."}), 503
        if not hmac.compare_digest(_provided_token(request_obj, payload), expected):
            return jsonify_fn({"ok": False, "error": "Signature Aircall invalide."}), 401
        event = str(payload.get("event") or payload.get("type") or "").strip().casefold()
        if event != AIRCALL_AI_SUMMARY_EVENT:
            return jsonify_fn({"ok": True, "result": "ignored", "reason": "event_not_supported"})

        lead = parse_aircall_lead(payload)
        if lead["explicitly_not_interested"]:
            return jsonify_fn({"ok": True, "result": "ignored", "reason": "not_interested"})
        if not lead["interested"]:
            return jsonify_fn({"ok": True, "result": "ignored", "reason": "not_a_training_prospect"})
        if not (lead["telephone"] or lead["mail"]):
            return jsonify_fn({"ok": True, "result": "ignored", "reason": "contact_details_missing"})

        call_id = _call_id(payload, lead)
        external_id = f"aircall:{call_id}"
        display_training = lead["formation"] or lead["raw_training"]
        crm_payload = {
            "prenom": lead["prenom"], "nom": lead["nom"], "mail": lead["mail"],
            "telephone": lead["telephone"], "formation": lead["formation"],
            "formation_libre": lead["raw_training"], "desp_type": lead["desp_type"],
            "origine": AIRCALL_AI_ORIGIN, "commentaire": lead["summary"],
            "notes": lead["summary"], "aircall_call_id": call_id, "aircall_event": event,
        }
        with legacy_app._CRM_RECONCILIATION_LOCK:
            data = legacy_app.load_data()
            duplicate = next((row for row in data.setdefault("crm_inbound_requests", [])
                if row.get("source") == AIRCALL_AI_SOURCE
                and str(row.get("external_id") or "") == external_id), None)
            if duplicate:
                return jsonify_fn({"ok": True, "result": "duplicate",
                    "contact_id": duplicate.get("contact_id"), "formation": display_training})

            contact, inbound, created = legacy_app.find_or_create_crm_contact(
                data, crm_payload, AIRCALL_AI_SOURCE, external_id=external_id,
                ordered_coordinates=True, record_activity=False,
            )
            if contact is None:
                legacy_app.save_data(data)
                return jsonify_fn({"ok": True, "result": "pending_review",
                    "request_id": inbound.get("id"), "formation": display_training})
            if created:
                contact.update({"source": AIRCALL_AI_SOURCE, "origine": AIRCALL_AI_ORIGIN})
                if lead["summary"]:
                    contact["commentaires"] = lead["summary"]
            if lead["desp_type"] and not str(contact.get("desp_type") or "").strip():
                contact["desp_type"] = lead["desp_type"]
            legacy_app._crm_activity(contact, "appel", "Appel reçu par l’assistante IA",
                _activity_detail(lead, call_id), author_name="Assistante IA Aircall")
            contact["updated_at"] = legacy_app._crm_now()
            legacy_app.save_data(data)
        return jsonify_fn({"ok": True, "result": "created" if created else "matched",
            "contact_id": contact.get("id"), "formation": display_training}), 200

    app.add_url_rule(AIRCALL_AI_WEBHOOK_PATH, endpoint="aircall_ai_voice_agent_webhook",
        view_func=webhook, methods=["POST"])
    legacy_app._aircall_ai_crm_registered = True
