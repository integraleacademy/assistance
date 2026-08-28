from __future__ import annotations

import crm_aircall_ai as integration
from crm_aircall_call_capture_patch import install_aircall_call_capture_patch


install_aircall_call_capture_patch(integration)


def _official_payload():
    return {
        "resource": "ai_voice_agent",
        "event": "ai_voice_agent.summary",
        "token": "secret",
        "data": {
            "id": 987654,
            "call_id": 987654,
            "external_caller_number": "+33 6 11 22 33 44",
            "aircall_number": "+33 4 22 47 07 68",
            "initial_direction": "inbound",
            "extracted_data": {
                "question": [
                    "Quel est votre prénom ?",
                    "Quel est votre nom de famille ?",
                    "Quelle formation vous intéresse ?",
                    "Pouvez-vous préciser le motif de votre appel ?",
                    "Quelle est votre adresse e-mail ?",
                ],
                "answer": [
                    "Clément",
                    "Cundy",
                    "A trois P",
                    "Je souhaite connaître les prochaines dates et les possibilités de financement.",
                    "clement.annecy@gmail.com",
                ],
            },
        },
    }


def test_official_summary_payload_populates_identity_phone_training_and_request():
    lead = integration.parse_aircall_lead(_official_payload())

    assert lead["prenom"] == "Clément"
    assert lead["nom"] == "Cundy"
    assert lead["telephone"] == "+33 6 11 22 33 44"
    assert lead["mail"] == "clement.annecy@gmail.com"
    assert lead["formation"] == "A3P"
    assert lead["motif"] == (
        "Je souhaite connaître les prochaines dates et les possibilités de financement."
    )
    assert "Demande de l'appelant" in lead["summary"]


def test_activity_always_contains_collected_answers_even_without_aircall_narrative_summary():
    lead = integration.parse_aircall_lead(_official_payload())
    detail = integration._activity_detail(lead, "987654")

    assert "Formation demandée : A3P." in detail
    assert "Demande de l'appelant" in detail
    assert "Informations recueillies pendant l'appel :" in detail
    assert "- Quel est votre prénom ? : Clément" in detail
    assert "- Quel est votre nom de famille ? : Cundy" in detail
    assert "- Pouvez-vous préciser le motif de votre appel ?" in detail
    assert "Numéro appelant : +33 6 11 22 33 44." in detail
    assert "Identifiant Aircall : 987654." in detail


def test_combined_first_and_last_name_question_is_split_correctly():
    payload = {
        "event": "ai_voice_agent.summary",
        "data": {
            "id": 1001,
            "external_caller_number": "+33699887766",
            "extracted_data": {
                "question": [
                    "Quel est votre prénom et votre nom ?",
                    "Quelle formation vous intéresse ?",
                    "Quelle est votre demande ?",
                ],
                "answer": [
                    "Clément Vaillant",
                    "Siappe un",
                    "Je souhaite connaître l'état de mon inscription.",
                ],
            },
        },
    }

    lead = integration.parse_aircall_lead(payload)

    assert lead["prenom"] == "Clément"
    assert lead["nom"] == "Vaillant"
    assert lead["telephone"] == "+33699887766"
    assert lead["formation"] == "SSIAP 1"
    assert "état de mon inscription" in lead["summary"]


def test_direct_question_answer_map_is_supported():
    payload = {
        "event": "ai_voice_agent.summary",
        "data": {
            "id": 1002,
            "external_caller_number": "+33655443322",
            "extracted_data": {
                "Prénom de l'appelant": "Alice",
                "Nom de l'appelant": "Martin",
                "Formation concernée": "V T C",
                "Motif de l'appel": "Elle souhaite connaître le tarif et les dates.",
                "Adresse e-mail": "alice.martin@example.com",
            },
        },
    }

    lead = integration.parse_aircall_lead(payload)

    assert lead["prenom"] == "Alice"
    assert lead["nom"] == "Martin"
    assert lead["telephone"] == "+33655443322"
    assert lead["formation"] == "Chauffeur VTC"
    assert lead["mail"] == "alice.martin@example.com"
    assert "tarif et les dates" in lead["summary"]


def test_opaque_question_labels_use_safe_positional_name_fallback():
    payload = {
        "event": "ai_voice_agent.summary",
        "data": {
            "id": 1003,
            "external_caller_number": "+33612344321",
            "extracted_data": {
                "question": ["question_1", "question_2", "question_3", "question_4"],
                "answer": [
                    "Jean",
                    "Dupont",
                    "A trois P",
                    "Je veux être renseigné sur le financement.",
                ],
            },
        },
    }

    lead = integration.parse_aircall_lead(payload)

    assert lead["prenom"] == "Jean"
    assert lead["nom"] == "Dupont"
    assert lead["telephone"] == "+33612344321"
    assert lead["formation"] == "A3P"
    assert "financement" in lead["summary"]


def test_official_external_number_overrides_an_unrelated_internal_aircall_number():
    payload = _official_payload()
    payload["data"]["phone_number"] = "+33422470768"
    payload["data"]["number"] = {"digits": "+33422470768"}

    lead = integration.parse_aircall_lead(payload)

    assert lead["telephone"] == "+33 6 11 22 33 44"
