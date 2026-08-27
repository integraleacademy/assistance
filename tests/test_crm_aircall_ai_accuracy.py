from __future__ import annotations

import crm_aircall_ai as integration


def test_production_spelling_is_normalized_and_formation_is_detected():
    payload = {
        "event": "ai_voice_agent.summary",
        "token": "secret",
        "data": {
            "id": "call-accuracy",
            "raw_digits": "+33665245271",
            "name": "Accueil IA",
            "training_status": "completed",
            "summary": {
                "text": "Le candidat souhaite des renseignements sur la formation A trois P et veut être rappelé."
            },
            "extracted_data": {
                "question": [
                    "Quel est votre prénom ?",
                    "Quel est votre nom de famille ?",
                    "Quelle est votre adresse e-mail ?",
                    "Quelle formation vous intéresse ?",
                ],
                "response": [
                    "Clément",
                    "VAI2LANT",
                    "clément point A deux N E C Y arobase g mail point com",
                    "A trois P",
                ],
            },
        },
    }

    lead = integration.parse_aircall_lead(payload)

    assert lead["prenom"] == "Clément"
    assert lead["nom"] == "VAILLANT"
    assert lead["mail"] == "clement.annecy@gmail.com"
    assert lead["telephone"] == "+33665245271"
    assert lead["formation"] == "A3P"
    assert lead["raw_training"] == "A trois P"


def test_first_name_falls_back_to_email_local_part():
    payload = {
        "event": "ai_voice_agent.summary",
        "data": {
            "id": "call-email-name",
            "raw_digits": "0611223344",
            "summary": "Le candidat souhaite s'inscrire en formation SSIAP 1.",
            "extracted_data": {
                "questions": ["Nom de famille", "Adresse e-mail", "Formation"],
                "answers": ["VAILLANT", "clement.annecy@gmail.com", "Siappe un"],
            },
        },
    }

    lead = integration.parse_aircall_lead(payload)

    assert lead["prenom"] == "Clement"
    assert lead["nom"] == "VAILLANT"
    assert lead["formation"] == "SSIAP 1"


def test_recognized_training_candidate_wins_over_internal_training_metadata():
    payload = {
        "event": "ai_voice_agent.summary",
        "data": {
            "id": "call-training",
            "raw_digits": "0611223344",
            "training_status": "completed",
            "summary": "La personne demande le tarif de la formation SSIAP 1.",
            "extracted_data": [
                {"field": {"name": "first_name"}, "response": {"value": "Paul"}},
                {"field": {"name": "last_name"}, "response": {"value": "Martin"}},
                {"field": {"name": "email"}, "response": {"value": "paul@example.com"}},
                {"field": {"name": "formation"}, "response": {"value": "Siappe un"}},
            ],
        },
    }

    lead = integration.parse_aircall_lead(payload)

    assert lead["prenom"] == "Paul"
    assert lead["nom"] == "Martin"
    assert lead["mail"] == "paul@example.com"
    assert lead["formation"] == "SSIAP 1"


def test_training_falls_back_to_summary_when_answer_is_generic():
    payload = {
        "event": "ai_voice_agent.summary",
        "data": {
            "id": "call-summary-training",
            "raw_digits": "0611223344",
            "summary": "Le candidat veut s'inscrire à la formation Chauffeur VTC.",
            "extracted_data": {
                "questions": ["Prénom", "Nom", "E-mail", "Formation"],
                "answers": ["Samir", "Benali", "samir@example.com", "Je ne connais pas le nom exact"],
            },
        },
    }

    lead = integration.parse_aircall_lead(payload)

    assert lead["formation"] == "Chauffeur VTC"


def test_full_name_is_split_when_aircall_returns_one_field():
    payload = {
        "event": "ai_voice_agent.summary",
        "data": {
            "id": "call-full-name",
            "raw_digits": "0611223344",
            "summary": "Le candidat est intéressé par la formation APS.",
            "extracted_data": {
                "questions": ["Nom complet", "Adresse e-mail", "Formation"],
                "answers": ["Louise Martin", "louise@example.com", "APS"],
            },
        },
    }

    lead = integration.parse_aircall_lead(payload)

    assert lead["prenom"] == "Louise"
    assert lead["nom"] == "Martin"
