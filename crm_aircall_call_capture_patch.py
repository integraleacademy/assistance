"""Fiabilise les données d'appel Aircall enregistrées dans le CRM.

Aircall transmet officiellement le numéro externe dans ``data.external_caller_number``
et les réponses d'admission dans ``data.extracted_data``. Ce module complète le
parseur initial sans modifier le contrat du webhook existant.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_PATCH_FLAG = "_aircall_call_capture_patch_installed"

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

_QUESTION_KEYS = {
    "question", "questions", "label", "title", "prompt", "field", "fieldname",
    "questiontext", "name", "key",
}
_ANSWER_KEYS = {
    "rawanswer", "rawanswers", "rawresponse", "rawresponses", "answer", "answers",
    "value", "values", "response", "responses", "text", "content",
}
_TECHNICAL_DIRECT_KEYS = _QUESTION_KEYS | _ANSWER_KEYS | {
    "id", "type", "fieldid", "questionid", "createdat", "updatedat", "metadata",
}
_PHONE_PATHS = (
    ("data", "external_caller_number"),
    ("data", "caller_number"),
    ("data", "call", "external_caller_number"),
    ("data", "call", "external_number"),
    ("data", "call", "raw_digits"),
    ("data", "call", "phone_number"),
    ("data", "call", "from_number"),
    ("data", "call", "from"),
    ("data", "raw_digits"),
    ("data", "phone_number"),
    ("data", "external_number"),
    ("data", "contact", "phone_number"),
    ("data", "contact", "phone"),
    ("external_caller_number",),
    ("caller_number",),
    ("raw_digits",),
    ("phone_number",),
)

_NON_NAME_VALUES = {
    "oui", "non", "yes", "no", "true", "false", "interesse", "intéressé",
    "prospect", "lead", "nouveau", "nouveaux", "aucun", "aucune",
}
_NON_NAME_WORDS = {
    "bonjour", "formation", "financement", "inscription", "rendez", "vous",
    "souhaite", "voudrais", "demande", "besoin", "connaitre", "connaître",
    "prix", "tarif", "date", "dates", "session", "sessions", "cpf", "cnaps",
    "france", "travail", "adresse", "email", "mail", "telephone", "téléphone",
}


def _path(payload: Mapping[str, Any], *parts: str) -> Any:
    current: Any = payload
    for part in parts:
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def _extract_pairs(
    value: Any,
    integration: Any,
    *,
    direct_pairs: bool = False,
    depth: int = 0,
) -> list[tuple[str, str]]:
    if depth > 8:
        return []

    pairs: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        question = answer = None
        for raw_key, item in value.items():
            key = integration._compact(raw_key)
            if key in _QUESTION_KEYS and question is None:
                question = item
            if key in _ANSWER_KEYS and answer is None:
                answer = item

        if question is not None and answer is not None:
            questions = _as_list(question)
            answers = _as_list(answer)
            if len(questions) == len(answers):
                candidates = zip(questions, answers)
            elif len(questions) == 1:
                candidates = ((questions[0], answer),)
            else:
                candidates = ()
            for raw_question, raw_answer in candidates:
                label = integration._label_text(raw_question)
                response = integration._answer_text(raw_answer)
                if label and response:
                    pairs.append((label[:500], response[:3000]))

        if direct_pairs:
            for raw_key, item in value.items():
                key = integration._compact(raw_key)
                if key in _TECHNICAL_DIRECT_KEYS or isinstance(item, (Mapping, list, tuple)):
                    continue
                label = str(raw_key or "").strip()
                response = integration._answer_text(item)
                if label and response:
                    pairs.append((label[:500], response[:3000]))

        for item in value.values():
            if isinstance(item, (Mapping, list, tuple)):
                pairs.extend(
                    _extract_pairs(
                        item,
                        integration,
                        direct_pairs=direct_pairs,
                        depth=depth + 1,
                    )
                )
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            pairs.extend(
                _extract_pairs(
                    item,
                    integration,
                    direct_pairs=direct_pairs,
                    depth=depth + 1,
                )
            )
    return pairs


def _intake_entries(payload: Mapping[str, Any], integration: Any) -> list[dict[str, str]]:
    pairs: list[tuple[str, str]] = []
    for path in _INTAKE_PATHS:
        value = _path(payload, *path)
        if value is not None:
            pairs.extend(_extract_pairs(value, integration, direct_pairs=True))

    entries: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for label, answer in pairs:
        normalized_label = " ".join(str(label).split())
        normalized_answer = " ".join(str(answer).split())
        marker = (
            integration._compact(normalized_label),
            normalized_answer.casefold(),
        )
        if not all(marker) or marker in seen:
            continue
        seen.add(marker)
        entries.append({
            "question": normalized_label[:500],
            "answer": normalized_answer[:3000],
        })
        if len(entries) >= 20:
            break
    return entries


def _is_combined_name_label(key: str) -> bool:
    # ``nom`` est contenu dans le mot ``prenom`` : retire d'abord ce dernier
    # avant de rechercher un véritable champ combiné prénom + nom.
    key_without_first_name = key.replace("prenom", "")
    return (
        ("prenom" in key and "nom" in key_without_first_name)
        or key in {"fullname", "nomcomplet", "identitecomplete", "contactname"}
        or any(marker in key for marker in (
            "nometprenom", "prenometnom", "votrenomcomplet", "identiteappelant",
        ))
    )


def _is_first_name_label(key: str, integration: Any) -> bool:
    return integration._is_first_name(key) or any(marker in key for marker in (
        "prenomdelappelant", "prenomduclient", "prenomducontact",
    ))


def _is_last_name_label(key: str, integration: Any) -> bool:
    # « nom de l'appelant » est aussi une sous-chaîne de « prénom de
    # l'appelant » : un champ prénom ne doit donc jamais être repris comme nom.
    if _is_combined_name_label(key) or _is_first_name_label(key, integration):
        return False
    return integration._is_last_name(key) or any(marker in key for marker in (
        "nomdelappelant", "nomduclient", "nomducontact", "familyname",
    ))


def _is_email_label(key: str, integration: Any) -> bool:
    return integration._is_email(key)


def _is_phone_label(key: str, integration: Any) -> bool:
    return integration._is_phone(key) or any(marker in key for marker in (
        "numeroappelant", "numerodetelephone", "portable",
    ))


def _is_training_label(key: str, integration: Any) -> bool:
    return integration._is_training(key)


def _is_interest_label(key: str, integration: Any) -> bool:
    return integration._is_interest(key)


def _is_request_label(key: str) -> bool:
    return any(marker in key for marker in (
        "motif", "objetdelappel", "raisondelappel", "votredemande",
        "demandeprecise", "demandeprincipale", "questionprincipale",
        "besoin", "precisions", "precision", "descriptiondelademande",
        "pourquoivousappelez", "sujetdelappel",
    ))


def _is_location_label(key: str) -> bool:
    return any(marker in key for marker in (
        "lieu", "centre", "campus", "ville", "adresse", "codepostal",
    ))


def _person_name_candidate(value: Any, integration: Any) -> str:
    raw = " ".join(str(value or "").strip().split())
    if not raw or "@" in raw or any(character.isdigit() for character in raw):
        return ""
    if raw.casefold() in _NON_NAME_VALUES:
        return ""
    if integration.normalize_training(raw)[0]:
        return ""

    normalized = integration._normalize_person_name(raw)
    parts = normalized.split()
    if not 1 <= len(parts) <= 4:
        return ""
    words = {integration._words(part) for part in parts}
    if words & _NON_NAME_WORDS:
        return ""
    if not all(re.fullmatch(r"[A-Za-zÀ-ÿ'’-]{1,40}", part) for part in parts):
        return ""
    return normalized


def _phone_from_value(value: Any, integration: Any) -> str:
    if isinstance(value, Mapping):
        for key in (
            "external_caller_number", "caller_number", "raw_digits", "phone_number",
            "external_number", "from_number", "from", "digits", "number", "phone", "value",
        ):
            if key in value and (phone := _phone_from_value(value[key], integration)):
                return phone
        for item in value.values():
            if phone := _phone_from_value(item, integration):
                return phone
        return ""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            if phone := _phone_from_value(item, integration):
                return phone
        return ""
    return integration._normalize_phone(value)


def _official_caller_phone(payload: Mapping[str, Any], integration: Any) -> str:
    for path in _PHONE_PATHS:
        value = _path(payload, *path)
        if value is not None and (phone := _phone_from_value(value, integration)):
            return phone
    return ""


def _answer_for(
    entries: Sequence[dict[str, str]],
    predicate: Any,
    integration: Any,
) -> str:
    for entry in entries:
        key = integration._compact(entry.get("question"))
        if predicate(key):
            return str(entry.get("answer") or "")
    return ""


def _non_identity_entries(
    entries: Sequence[dict[str, str]],
    integration: Any,
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for entry in entries:
        key = integration._compact(entry.get("question"))
        if (
            _is_first_name_label(key, integration)
            or _is_last_name_label(key, integration)
            or _is_combined_name_label(key)
            or _is_email_label(key, integration)
            or _is_phone_label(key, integration)
            or _is_training_label(key, integration)
            or _is_interest_label(key, integration)
            or integration.normalize_training(entry.get("answer"))[0]
        ):
            continue
        result.append(entry)
    return result


def _build_fallback_summary(
    lead: Mapping[str, Any],
    entries: Sequence[dict[str, str]],
    motif: str,
    integration: Any,
) -> str:
    if motif:
        return f"Demande de l'appelant : {motif}"[:3000]

    identity_values = {
        str(lead.get(field) or "").strip().casefold()
        for field in ("prenom", "nom", "mail", "telephone", "raw_training", "formation")
        if lead.get(field)
    }
    contextual = [
        entry for entry in _non_identity_entries(entries, integration)
        if str(entry.get("answer") or "").strip().casefold() not in identity_values
        and not integration._normalize_email(entry.get("answer"))
        and not integration._normalize_phone(entry.get("answer"))
    ]
    if contextual:
        excerpts = [
            f"{entry['question']} : {entry['answer']}"
            for entry in contextual[:3]
        ]
        return ("Informations recueillies : " + " ; ".join(excerpts))[:3000]

    training = str(lead.get("formation") or lead.get("raw_training") or "").strip()
    if training:
        return f"L'appelant souhaite des renseignements sur la formation {training}."[:3000]
    return ""


def install_aircall_call_capture_patch(integration: Any = None) -> None:
    """Complète le parseur Aircall avant l'enregistrement du webhook."""
    if integration is None:
        import crm_aircall_ai as integration_module
        integration = integration_module
    if getattr(integration, _PATCH_FLAG, False):
        return

    original_parse = integration.parse_aircall_lead

    def parse_aircall_lead(payload: Mapping[str, Any]) -> dict[str, Any]:
        lead = dict(original_parse(payload))
        entries = _intake_entries(payload, integration)

        combined = _answer_for(entries, _is_combined_name_label, integration)
        if combined and (not lead.get("prenom") or not lead.get("nom")):
            first, last = integration._split_full_name(combined)
            existing_first = str(lead.get("prenom") or "").strip()
            existing_last = str(lead.get("nom") or "").strip()
            if not existing_first or (not existing_last and len(existing_first.split()) >= 2):
                lead["prenom"] = first
            if not existing_last:
                lead["nom"] = last

        if not lead.get("prenom"):
            first = _answer_for(
                entries,
                lambda key: _is_first_name_label(key, integration),
                integration,
            )
            lead["prenom"] = integration._normalize_person_name(first)

        if not lead.get("nom"):
            last = _answer_for(
                entries,
                lambda key: _is_last_name_label(key, integration),
                integration,
            )
            lead["nom"] = integration._normalize_person_name(last)

        # Une question combinée peut avoir été classée comme prénom par l'ancien
        # parseur. Dans ce cas, sépare proprement les deux parties.
        if lead.get("prenom") and not lead.get("nom") and len(str(lead["prenom"]).split()) >= 2:
            first, last = integration._split_full_name(lead["prenom"])
            if first and last:
                lead["prenom"], lead["nom"] = first, last

        # Repli prudent pour les payloads dont Aircall renvoie des libellés
        # techniques ou opaques ("question_1", "question_2", etc.).
        used_values = {
            str(lead.get(field) or "").strip().casefold()
            for field in ("prenom", "nom", "mail", "telephone", "raw_training")
            if lead.get(field)
        }
        candidates: list[str] = []
        for entry in entries:
            key = integration._compact(entry.get("question"))
            if (
                _is_first_name_label(key, integration)
                or _is_last_name_label(key, integration)
                or _is_combined_name_label(key)
                or _is_email_label(key, integration)
                or _is_phone_label(key, integration)
                or _is_training_label(key, integration)
                or _is_interest_label(key, integration)
                or _is_request_label(key)
                or _is_location_label(key)
            ):
                continue
            candidate = _person_name_candidate(entry.get("answer"), integration)
            if candidate and candidate.casefold() not in used_values:
                candidates.append(candidate)
                used_values.add(candidate.casefold())

        if not lead.get("prenom") and candidates:
            lead["prenom"] = candidates.pop(0)
        if not lead.get("nom") and candidates:
            lead["nom"] = candidates.pop(0)

        official_phone = _official_caller_phone(payload, integration)
        if official_phone:
            lead["telephone"] = official_phone
        elif not lead.get("telephone"):
            phone_answer = _answer_for(
                entries,
                lambda key: _is_phone_label(key, integration),
                integration,
            )
            lead["telephone"] = integration._normalize_phone(phone_answer)

        # Rejoue la détection de formation sur toutes les réponses structurées.
        if not lead.get("formation"):
            for entry in entries:
                formation, desp_type = integration.normalize_training(entry.get("answer"))
                if formation:
                    lead["formation"] = formation
                    lead["desp_type"] = desp_type
                    lead["raw_training"] = str(entry.get("answer") or "")[:300]
                    break

        motif = _answer_for(entries, _is_request_label, integration)
        if not lead.get("summary"):
            lead["summary"] = _build_fallback_summary(
                lead,
                entries,
                motif,
                integration,
            )

        lead["motif"] = motif[:3000]
        lead["intake_answers"] = entries
        return lead

    def activity_detail(lead: Mapping[str, Any], call_id: str) -> str:
        training = str(
            lead.get("formation") or lead.get("raw_training") or "À préciser"
        ).strip()
        lines = [f"Formation demandée : {training}."]

        motif = str(lead.get("motif") or "").strip()
        summary = str(lead.get("summary") or "").strip()
        if motif:
            lines.append(f"Demande de l'appelant : {motif}")
        if summary and summary != f"Demande de l'appelant : {motif}":
            lines.append(f"Résumé de l'appel : {summary}")

        answers = lead.get("intake_answers")
        if isinstance(answers, Sequence) and not isinstance(answers, (str, bytes, bytearray)):
            clean_answers = [
                item for item in answers
                if isinstance(item, Mapping)
                and str(item.get("question") or "").strip()
                and str(item.get("answer") or "").strip()
            ]
            if clean_answers:
                lines.append("Informations recueillies pendant l'appel :")
                for item in clean_answers[:12]:
                    question = " ".join(str(item["question"]).split())[:180]
                    answer = " ".join(str(item["answer"]).split())[:500]
                    lines.append(f"- {question} : {answer}")

        if lead.get("telephone"):
            lines.append(f"Numéro appelant : {lead['telephone']}.")
        if call_id:
            lines.append(f"Identifiant Aircall : {call_id}.")
        return "\n".join(lines)[:6000]

    integration.parse_aircall_lead = parse_aircall_lead
    integration._activity_detail = activity_detail
    setattr(integration, _PATCH_FLAG, True)
