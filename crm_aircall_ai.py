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
_ANSWER_KEYS = {
    "rawanswer", "rawanswers", "rawresponse", "rawresponses", "answer", "answers",
    "value", "values", "response", "responses", "text", "content",
}
_INTAKE_PATHS = (
    ("data", "extracted_data"),
    ("data", "intake_questions"),
    ("data", "admission_questions"),
    ("data", "questions_and_answers"),
    ("data", "qualification_questions"),
    ("data", "intake"),
    ("extracted_data",),
    ("intake_questions",),
    ("admission_questions",),
    ("questions_and_answers",),
)
_EMAIL_LOCAL_BLACKLIST = {
    "accueil", "admin", "administration", "commercial", "contact", "direction",
    "formation", "formations", "info", "secretariat", "support",
}


def _strip_accents(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in text if not unicodedata.combining(char))


def _compact(value: Any) -> str:
    text = _strip_accents(value).strip().casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


def _words(value: Any) -> str:
    text = _strip_accents(value).strip().casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _text(value: Any, limit: int = 3_000) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Mapping):
        for key in (
            "raw_response", "raw_answer", "response", "answer", "value", "text",
            "content", "summary", "display_value", "normalized_value",
        ):
            if key in value and (result := _text(value[key], limit)):
                return result
        return ""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return ", ".join(filter(None, (_text(item, limit) for item in value)))[:limit]
    return " ".join(str(value).strip().split())[:limit]


def _label_text(value: Any, limit: int = 500) -> str:
    if isinstance(value, Mapping):
        for key in ("question", "label", "title", "prompt", "field_name", "name", "key", "field"):
            if key in value:
                nested = value[key]
                if isinstance(nested, Mapping):
                    if result := _label_text(nested, limit):
                        return result
                elif result := _text(nested, limit):
                    return result
        return ""
    return _text(value, limit)


def _answer_text(value: Any, limit: int = 3_000) -> str:
    if isinstance(value, Mapping):
        for key in (
            "raw_response", "raw_answer", "response", "answer", "value", "text",
            "content", "display_value", "normalized_value",
        ):
            if key in value and (result := _answer_text(value[key], limit)):
                return result
        return ""
    return _text(value, limit)


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
                    question_text = _label_text(raw_question)
                    answer_text = _answer_text(raw_answer)
                    if question_text and answer_text:
                        rows.append((_compact(question_text), answer_text))
            elif len(questions) == 1:
                question_text = _label_text(questions[0])
                answer_text = _answer_text(answer)
                if question_text and answer_text:
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


def _dedupe_rows(rows: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for key, value in rows:
        marker = (str(key or ""), str(value or "").strip().casefold())
        if not all(marker) or marker in seen:
            continue
        seen.add(marker)
        result.append((key, value))
    return result


def _path(payload: Mapping[str, Any], *parts: str) -> Any:
    current: Any = payload
    for part in parts:
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _intake_rows(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for path in _INTAKE_PATHS:
        value = _path(payload, *path)
        if value is not None:
            rows.extend(_flatten(value, " ".join(path)))
    return _dedupe_rows(rows)


def _pick(rows: Sequence[tuple[str, str]], predicate: Callable[[str], bool]) -> str:
    return next((value for key, value in rows if value and predicate(key)), "")


def _values(rows: Sequence[tuple[str, str]], predicate: Callable[[str], bool]) -> list[str]:
    return [value for key, value in rows if value and predicate(key)]


def _is_first_name(key: str) -> bool:
    return key in {"prenom", "firstname", "givenname"} or any(
        marker in key for marker in (
            "votreprenom", "quelestvotreprenom", "callerfirstname", "clientfirstname",
            "contactfirstname",
        )
    )


def _is_last_name(key: str) -> bool:
    if "prenom" in key or "firstname" in key:
        return False
    return key in {"nom", "lastname", "surname", "familyname"} or any(
        marker in key for marker in (
            "nomdefamille", "votrenom", "quelestvotrenom", "callerlastname",
            "clientlastname", "contactlastname",
        )
    )


def _is_full_name(key: str) -> bool:
    return key in {"fullname", "nomcomplet", "identitecomplete"} or any(
        marker in key for marker in ("votrenomcomplet", "fullcontactname", "callerfullname")
    )


def _is_email(key: str) -> bool:
    return any(marker in key for marker in (
        "email", "adressemail", "adresseemail", "courriel", "mailaddress",
    ))


def _is_phone(key: str) -> bool:
    return any(marker in key for marker in (
        "telephone", "phone", "rawdigits", "callernumber", "externalnumber", "mobile",
        "contactphone", "phonenumber",
    ))


def _is_training(key: str) -> bool:
    return any(marker in key for marker in (
        "formation", "training", "course", "parcours", "programmeinteresse",
    ))


def _is_interest(key: str) -> bool:
    return any(marker in key for marker in (
        "interess", "interest", "prospect", "qualification", "readytoapply",
        "souhaitezvousvousinscrire", "wanttoenrol", "wanttoenroll",
    ))


def _first_path(payload: Mapping[str, Any], paths: Sequence[Sequence[str]], limit=500) -> str:
    for path in paths:
        if value := _text(_path(payload, *path), limit):
            return value
    return ""


def _expand_spoken_repetitions(value: Any, *, inline_digits: bool = False) -> str:
    text = str(value or "")

    def repeat_word(match: re.Match[str]) -> str:
        count = {"deux": 2, "double": 2, "trois": 3, "triple": 3}.get(
            _strip_accents(match.group(1)).casefold(), 1
        )
        return match.group(2) * count

    text = re.sub(
        r"(?i)\b(deux|double|trois|triple)\s+(?:fois\s+)?(?:la\s+lettre\s+)?([a-zà-ÿ])\b",
        repeat_word,
        text,
    )
    if inline_digits:
        text = re.sub(
            r"([234])\s*([A-Za-zÀ-ÿ])",
            lambda match: match.group(2) * int(match.group(1)),
            text,
        )
    return text


def _collapse_spelled_letters(value: Any) -> str:
    text = " ".join(str(value or "").strip().split())
    tokens = text.split()
    if len(tokens) >= 3 and all(re.fullmatch(r"[A-Za-zÀ-ÿ]{1,3}", token) for token in tokens):
        return "".join(tokens)
    return text


def _normalize_person_name(value: Any) -> str:
    text = _expand_spoken_repetitions(value, inline_digits=True)
    text = _collapse_spelled_letters(text)
    text = re.sub(r"[^A-Za-zÀ-ÿ'’\-\s]", "", text)
    text = " ".join(text.replace("’", "'").split()).strip(" -'")
    return text[:120]


def _split_full_name(value: Any) -> tuple[str, str]:
    text = _normalize_person_name(value)
    if not text:
        return "", ""
    if "," in str(value or ""):
        raw_last, raw_first = str(value).split(",", 1)
        return _normalize_person_name(raw_first), _normalize_person_name(raw_last)
    parts = text.split()
    if len(parts) < 2:
        return "", text
    return parts[0], " ".join(parts[1:])


def _normalize_email(value: Any) -> str:
    text = _strip_accents(_expand_spoken_repetitions(value)).casefold()
    replacements = (
        (r"\btiret\s+bas\b|\bunderscore\b", "_"),
        (r"\barobase\b|\bat\b", "@"),
        (r"\bpoint\b|\bdot\b", "."),
        (r"\btiret\b|\bdash\b", "-"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"\bg\s*mail\b", "gmail", text)
    text = re.sub(r"\bhot\s*mail\b", "hotmail", text)
    text = re.sub(r"\bout\s*look\b", "outlook", text)
    text = re.sub(r"\s+", "", text)
    text = text.strip(".,;:")
    if text.count("@") != 1:
        return ""
    local, domain = text.split("@", 1)
    if not local or not domain or domain.startswith(".") or "." not in domain:
        return ""
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local):
        return ""
    if not re.fullmatch(r"[a-z0-9.-]+", domain) or ".." in domain:
        return ""
    return text[:254]


def _first_name_from_email(email: str) -> str:
    local = str(email or "").split("@", 1)[0]
    candidate = re.split(r"[._+\-]", local, maxsplit=1)[0]
    if candidate in _EMAIL_LOCAL_BLACKLIST or not re.fullmatch(r"[a-z]{2,40}", candidate):
        return ""
    return candidate.capitalize()


def _normalize_phone(value: Any) -> str:
    raw = " ".join(str(value or "").strip().split())
    return raw[:40] if 8 <= len(re.sub(r"\D", "", raw)) <= 15 else ""


def normalize_training(value: Any) -> tuple[str, str]:
    """Return the CRM training label and optional DESP subtype."""
    text = _words(value)
    compact = text.replace(" ", "")
    if not text:
        return "", ""

    bts = {
        "BTS MOS": ("mos", "management operationnel de la securite"),
        "BTS MCO": ("mco", "management commercial operationnel"),
        "BTS NDRC": ("ndrc", "negociation et digitalisation"),
        "BTS PI": ("pi", "professions immobilieres"),
        "BTS CG": ("cg", "comptabilite gestion"),
        "BTS CI": ("ci", "commerce international"),
    }
    if "bts" in compact or any(phrase in text for _, phrase in bts.values()):
        for label, (code, phrase) in bts.items():
            if code in compact or phrase in text:
                return label, ""

    if any(term in text for term in (
        "protection physique", "protection rapprochee", "garde du corps",
    )) or any(term in compact for term in ("a3p", "atroisp", "apr")):
        return "A3P", ""
    if any(term in compact for term in ("desp", "deesp")) or (
        "dirigeant" in text and "securite" in text
    ):
        subtype = "VAE" if "vae" in compact or "validation des acquis" in text else ""
        return "DESP", subtype or ("INITIAL" if "initial" in text else "")
    if any(term in compact for term in ("ssiap", "siappe", "ssiape")) or "securite incendie" in text:
        return "SSIAP 1", ""
    if "vtc" in compact or "chauffeur" in text:
        return "Chauffeur VTC", ""
    if "aps" in compact or any(term in text for term in (
        "agent de prevention", "agent de securite privee",
    )):
        return "APS", ""
    return "", ""


def _interest_state(rows: Sequence[tuple[str, str]], summary: str) -> bool | None:
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
    summary_words = _words(summary)
    practical_terms = ("adresse", "horaires", "horaire", "stationnement", "parking", "acces")
    training_intent_terms = (
        "inscription", "financement", "prix", "tarif", "date", "dates",
        "session", "sessions", "devis", "prerequis",
    )
    if (
        any(term in summary_words for term in practical_terms)
        and not any(term in summary_words for term in training_intent_terms)
        and any(term in summary_words for term in ("uniquement", "seulement", "juste"))
    ):
        return False
    raw_words = _words(raw_training)
    if raw_words:
        practical = any(term in raw_words for term in (
            "adresse", "horaires", "horaire", "stationnement", "parking", "acces",
        )) and not any(term in raw_words for term in training_intent_terms)
        return not practical
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


def _summary_text(payload: Mapping[str, Any], rows: Sequence[tuple[str, str]]) -> str:
    return _first_path(payload, (
        ("data", "summary", "text"), ("data", "summary", "content"),
        ("data", "summary"), ("data", "call_summary"), ("data", "call", "summary"),
        ("data", "ai_voice_agent", "summary"), ("summary", "text"), ("summary",),
    ), 3_000) or _pick(rows, lambda key: key in {
        "summary", "callsummary", "resume", "resumedelappel", "notes", "commentaire",
    })


def _transcript_text(payload: Mapping[str, Any]) -> str:
    return _first_path(payload, (
        ("data", "transcript"), ("data", "transcription"),
        ("data", "call", "transcript"), ("data", "call", "transcription"),
        ("data", "ai_voice_agent", "transcript"), ("transcript",), ("transcription",),
    ), 8_000)


def parse_aircall_lead(payload: Mapping[str, Any]) -> dict[str, str | bool]:
    intake_rows = _intake_rows(payload)
    rows = _dedupe_rows([*intake_rows, *_flatten(payload)])

    first_name = next(
        (name for value in _values(intake_rows, _is_first_name)
         if (name := _normalize_person_name(value))),
        "",
    ) or next(
        (name for value in _values(rows, _is_first_name)
         if (name := _normalize_person_name(value))),
        "",
    )
    last_name = next(
        (name for value in _values(intake_rows, _is_last_name)
         if (name := _normalize_person_name(value))),
        "",
    ) or next(
        (name for value in _values(rows, _is_last_name)
         if (name := _normalize_person_name(value))),
        "",
    )

    full_name = _pick(intake_rows, _is_full_name) or _pick(rows, _is_full_name)
    if full_name and (not first_name or not last_name):
        split_first, split_last = _split_full_name(full_name)
        first_name = first_name or split_first
        last_name = last_name or split_last
    elif last_name and not first_name and len(last_name.split()) >= 2:
        split_first, split_last = _split_full_name(last_name)
        first_name, last_name = split_first, split_last

    email = next(
        (email for value in _values(intake_rows, _is_email)
         if (email := _normalize_email(value))),
        "",
    ) or next(
        (email for value in _values(rows, _is_email)
         if (email := _normalize_email(value))),
        "",
    )
    if not first_name and email:
        first_name = _first_name_from_email(email)

    caller_phone = _first_path(payload, (
        ("data", "call", "raw_digits"), ("data", "call", "phone_number"),
        ("data", "call", "external_number"), ("data", "number", "raw_digits"),
        ("data", "raw_digits"), ("data", "caller_number"), ("data", "phone_number"),
        ("data", "external_number"), ("data", "contact_phone"),
        ("call", "raw_digits"), ("call", "phone_number"), ("raw_digits",),
    ))
    phone = next(
        (phone for value in _values(intake_rows, _is_phone)
         if (phone := _normalize_phone(value))),
        "",
    ) or _normalize_phone(caller_phone) or next(
        (phone for value in _values(rows, _is_phone)
         if (phone := _normalize_phone(value))),
        "",
    )

    summary = _summary_text(payload, rows)
    transcript = _transcript_text(payload)
    training_candidates = _dedupe_rows([
        *(row for row in intake_rows if _is_training(row[0])),
        *(row for row in rows if _is_training(row[0])),
    ])
    raw_training = ""
    formation = desp_type = ""
    for _, candidate in training_candidates:
        candidate_formation, candidate_desp_type = normalize_training(candidate)
        if candidate_formation:
            raw_training = candidate
            formation, desp_type = candidate_formation, candidate_desp_type
            break
    if not raw_training and training_candidates:
        raw_training = training_candidates[0][1]
    if not formation:
        for candidate in (summary, transcript, " ".join(value for _, value in intake_rows)):
            candidate_formation, candidate_desp_type = normalize_training(candidate)
            if candidate_formation:
                formation, desp_type = candidate_formation, candidate_desp_type
                if not raw_training:
                    raw_training = candidate[:300]
                break

    interest = _interest_state(intake_rows or rows, summary)
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
            # Le nouveau parcours envoie d'abord un formulaire par SMS. Tant
            # que ce formulaire est attendu, conserver le résumé dans la
            # demande de rappel sans créer une fiche partielle. Si l'action SMS
            # n'a pas été utilisée ou a échoué, le comportement historique
            # ci-dessous reste le filet de sécurité.
            from crm_aircall_lead_capture import (
                attach_aircall_summary_to_pending_request,
            )

            pending_form = attach_aircall_summary_to_pending_request(
                data, lead, call_id,
            )
            if pending_form:
                legacy_app.save_data(data)
                return jsonify_fn({
                    "ok": True,
                    "result": "awaiting_form",
                    "request_id": pending_form.get("id"),
                    "formation": display_training,
                }), 200
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
