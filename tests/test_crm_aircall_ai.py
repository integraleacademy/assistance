from __future__ import annotations

import copy
import threading
import uuid

import pytest

import crm_aircall_ai as integration


class FakeRequest:
    def __init__(self):
        self.payload = None
        self.headers = {}

    def get_json(self, silent=True):
        return copy.deepcopy(self.payload)


class FakeApp:
    def __init__(self):
        self.routes = {}

    def add_url_rule(self, path, endpoint, view_func, methods):
        self.routes[(path, tuple(methods))] = view_func


class FakeLegacyApp:
    def __init__(self):
        self.app = FakeApp()
        self.request = FakeRequest()
        self.jsonify = lambda payload: payload
        self._CRM_RECONCILIATION_LOCK = threading.RLock()
        self._store = {"crm_contacts": [], "crm_inbound_requests": []}
        self.saved = 0

    def load_data(self):
        return self._store

    def save_data(self, data):
        assert data is self._store
        self.saved += 1

    def _crm_now(self):
        return "2026-08-27T14:30:00+02:00"

    def _crm_activity(self, contact, kind, title, detail="", author_name=""):
        contact.setdefault("activities", []).insert(0, {
            "id": str(uuid.uuid4()),
            "date": self._crm_now(),
            "kind": kind,
            "title": title,
            "detail": detail,
            "author": author_name,
        })

    def find_or_create_crm_contact(self, data, payload, source, **options):
        external_id = str(options.get("external_id") or "")
        existing_inbound = next((
            row for row in data["crm_inbound_requests"]
            if row.get("source") == source and row.get("external_id") == external_id
        ), None)
        if existing_inbound:
            contact = next((
                item for item in data["crm_contacts"]
                if item.get("id") == existing_inbound.get("contact_id")
            ), None)
            return contact, existing_inbound, False

        phone = payload.get("telephone")
        email = payload.get("mail")
        contact = next((
            item for item in data["crm_contacts"]
            if (phone and item.get("telephone") == phone)
            or (email and item.get("mail") == email)
        ), None)
        created = contact is None
        if contact is None:
            contact = {
                "id": str(uuid.uuid4()),
                "prenom": payload.get("prenom", ""),
                "nom": payload.get("nom", ""),
                "mail": payload.get("mail", ""),
                "telephone": payload.get("telephone", ""),
                "formation": payload.get("formation", ""),
                "statut": "Nouveaux",
                "origine": source,
                "created_at": self._crm_now(),
                "updated_at": self._crm_now(),
                "activities": [],
            }
            data["crm_contacts"].insert(0, contact)
        else:
            for field in ("prenom", "nom", "mail", "telephone", "formation"):
                if not contact.get(field) and payload.get(field):
                    contact[field] = payload[field]

        inbound = {
            "id": str(uuid.uuid4()),
            "source": source,
            "external_id": external_id,
            "contact_id": contact["id"],
            "status": "created" if created else "matched",
            "raw_payload": copy.deepcopy(payload),
        }
        data["crm_inbound_requests"].insert(0, inbound)
        return contact, inbound, created

    def call_route(self, payload, headers=None):
        self.request.payload = payload
        self.request.headers = headers or {}
        result = self.app.routes[(integration.AIRCALL_AI_WEBHOOK_PATH, ("POST",))]()
        if isinstance(result, tuple):
            body, status = result
        else:
            body, status = result, 200
        return body, status


@pytest.fixture
def legacy(monkeypatch):
    monkeypatch.setenv("AIRCALL_WEBHOOK_TOKEN", "secret-aircall-token")
    app = FakeLegacyApp()
    integration.register_aircall_ai_crm(app)
    return app


def _summary_payload(*, call_id="call-123", formation="APS"):
    return {
        "event": "ai_voice_agent.summary",
        "token": "secret-aircall-token",
        "data": {
            "id": call_id,
            "raw_digits": "+33612345678",
            "summary": {
                "text": f"Le candidat souhaite des renseignements et veut s'inscrire en formation {formation}."
            },
            "intake_questions": [
                {"question": "Pour commencer, quel est votre prénom ?", "answer": "Jean"},
                {"question": "Et quel est votre nom de famille ?", "answer": "Dupont"},
                {"question": "Pour confirmer, quelle formation concerne votre demande ?", "answer": formation},
                {"question": "Enfin, quelle est votre adresse e-mail ?", "answer": "Jean.Dupont@example.com"},
            ],
        },
    }


def test_parse_aircall_lead_extracts_intake_fields_and_caller_number():
    lead = integration.parse_aircall_lead(_summary_payload())
    assert lead == {
        "prenom": "Jean",
        "nom": "Dupont",
        "mail": "jean.dupont@example.com",
        "telephone": "+33612345678",
        "formation": "APS",
        "raw_training": "APS",
        "desp_type": "",
        "summary": "Le candidat souhaite des renseignements et veut s'inscrire en formation APS.",
        "interested": True,
        "explicitly_not_interested": False,
    }


@pytest.mark.parametrize("raw,expected,desp_type", [
    ("formation SSIAP 1 incendie", "SSIAP 1", ""),
    ("BTS Management Opérationnel de la Sécurité", "BTS MOS", ""),
    ("BTS MCO", "BTS MCO", ""),
    ("VAE dirigeant entreprise de sécurité DESP", "DESP", "VAE"),
    ("garde du corps APR", "A3P", ""),
])
def test_normalize_training(raw, expected, desp_type):
    assert integration.normalize_training(raw) == (expected, desp_type)


def test_webhook_requires_configured_matching_token(legacy, monkeypatch):
    payload = _summary_payload()
    payload["token"] = "wrong"
    body, status = legacy.call_route(payload)
    assert status == 401
    assert body["error"] == "Signature Aircall invalide."
    assert legacy._store["crm_contacts"] == []

    monkeypatch.delenv("AIRCALL_WEBHOOK_TOKEN")
    body, status = legacy.call_route(_summary_payload())
    assert status == 503
    assert body["error"] == "La connexion Aircall n’est pas configurée."
    assert legacy._store["crm_contacts"] == []


def test_webhook_accepts_token_header(legacy):
    payload = _summary_payload()
    payload.pop("token")
    body, status = legacy.call_route(
        payload,
        headers={"X-Aircall-Webhook-Token": "secret-aircall-token"},
    )
    assert status == 200
    assert body["result"] == "created"


def test_webhook_ignores_unrelated_aircall_event(legacy):
    payload = _summary_payload()
    payload["event"] = "ai_voice_agent.started"
    body, status = legacy.call_route(payload)
    assert status == 200
    assert body["result"] == "ignored"
    assert legacy._store["crm_contacts"] == []


def test_webhook_creates_new_crm_lead_and_call_activity(legacy):
    body, status = legacy.call_route(_summary_payload())
    assert status == 200
    assert body["result"] == "created"
    assert body["formation"] == "APS"

    contact = legacy._store["crm_contacts"][0]
    assert contact["prenom"] == "Jean"
    assert contact["nom"] == "Dupont"
    assert contact["mail"] == "jean.dupont@example.com"
    assert contact["telephone"] == "+33612345678"
    assert contact["formation"] == "APS"
    assert contact["source"] == integration.AIRCALL_AI_SOURCE
    assert contact["origine"] == integration.AIRCALL_AI_ORIGIN
    assert contact["statut"] == "Nouveaux"
    assert contact["activities"][0]["title"] == "Appel reçu par l’assistante IA"
    assert "Identifiant Aircall : call-123" in contact["activities"][0]["detail"]
    assert contact["activities"][0]["author"] == "Assistante IA Aircall"

    inbound = legacy._store["crm_inbound_requests"][0]
    assert inbound["external_id"] == "aircall:call-123"
    assert "token" not in inbound["raw_payload"]


def test_webhook_is_idempotent_for_same_call_id(legacy):
    first_body, first_status = legacy.call_route(_summary_payload())
    second_body, second_status = legacy.call_route(_summary_payload())
    assert first_status == 200
    assert first_body["result"] == "created"
    assert second_status == 200
    assert second_body["result"] == "duplicate"
    assert len(legacy._store["crm_contacts"]) == 1
    assert len(legacy._store["crm_inbound_requests"]) == 1
    assert len(legacy._store["crm_contacts"][0]["activities"]) == 1


def test_webhook_matches_existing_contact_instead_of_duplicating(legacy):
    existing = {
        "id": "existing-contact",
        "prenom": "Jean",
        "nom": "Dupont",
        "mail": "jean.dupont@example.com",
        "telephone": "+33612345678",
        "formation": "",
        "statut": "Nouveaux",
        "origine": "Site internet",
        "activities": [],
    }
    legacy._store["crm_contacts"].append(existing)
    body, status = legacy.call_route(
        _summary_payload(call_id="call-existing", formation="SSIAP 1")
    )
    assert status == 200
    assert body["result"] == "matched"
    assert len(legacy._store["crm_contacts"]) == 1
    assert existing["formation"] == "SSIAP 1"
    assert existing["origine"] == "Site internet"
    assert existing["activities"][0]["title"] == "Appel reçu par l’assistante IA"


def test_webhook_ignores_call_without_recognized_training(legacy):
    payload = _summary_payload(formation="une question sur l'adresse")
    payload["data"]["summary"]["text"] = "L'appelant demande uniquement l'adresse du centre."
    body, status = legacy.call_route(payload)
    assert status == 200
    assert body == {
        "ok": True,
        "result": "ignored",
        "reason": "not_a_training_prospect",
    }
    assert legacy._store["crm_contacts"] == []


def test_parse_parallel_extracted_data_arrays():
    payload = {
        "event": "ai_voice_agent.summary",
        "token": "secret-aircall-token",
        "data": {
            "id": "parallel-1",
            "raw_digits": "0611223344",
            "summary": "The caller is interested in training.",
            "extracted_data": {
                "questions": ["First name", "Last name", "Email", "Which course are you interested in?"],
                "answers": ["Alice", "Martin", "alice@example.com", "BTS MCO"],
            },
        },
    }
    lead = integration.parse_aircall_lead(payload)
    assert lead["prenom"] == "Alice"
    assert lead["nom"] == "Martin"
    assert lead["mail"] == "alice@example.com"
    assert lead["telephone"] == "0611223344"
    assert lead["formation"] == "BTS MCO"
    assert lead["interested"] is True


def test_webhook_creates_generic_training_lead_when_course_is_not_yet_decided(legacy):
    payload = _summary_payload(formation="Je ne sais pas encore quelle formation choisir")
    payload["data"]["summary"]["text"] = (
        "Le candidat a un projet de formation dans la sécurité mais doit encore être orienté."
    )
    body, status = legacy.call_route(payload)
    assert status == 200
    assert body["result"] == "created"
    assert body["formation"] == "Je ne sais pas encore quelle formation choisir"
    contact = legacy._store["crm_contacts"][0]
    assert contact["formation"] == ""
    assert "Formation demandée : Je ne sais pas encore quelle formation choisir" in (
        contact["activities"][0]["detail"]
    )


def test_specific_training_practical_question_without_intake_does_not_create_lead(legacy):
    payload = {
        "event": "ai_voice_agent.summary",
        "token": "secret-aircall-token",
        "data": {
            "id": "practical-aps",
            "raw_digits": "0612345678",
            "summary": "L'appelant demande uniquement l'adresse du centre pour la formation APS.",
        },
    }
    body, status = legacy.call_route(payload)
    assert status == 200
    assert body["reason"] == "not_a_training_prospect"
    assert legacy._store["crm_contacts"] == []


def test_explicit_interest_boolean_creates_lead_without_named_course(legacy):
    payload = {
        "event": "ai_voice_agent.summary",
        "token": "secret-aircall-token",
        "data": {
            "id": "interest-boolean",
            "raw_digits": "0612345678",
            "summary": "La personne souhaite être orientée vers une formation adaptée.",
            "interested": True,
            "first_name": "Paul",
            "last_name": "Durand",
        },
    }
    body, status = legacy.call_route(payload)
    assert status == 200
    assert body["result"] == "created"
    assert legacy._store["crm_contacts"][0]["formation"] == ""


def test_webhook_ignores_explicitly_uninterested_caller(legacy):
    payload = _summary_payload()
    payload["data"]["intake_questions"].append({
        "question": "La personne est-elle intéressée ?",
        "answer": "Non",
    })
    body, status = legacy.call_route(payload)
    assert status == 200
    assert body["reason"] == "not_interested"
    assert legacy._store["crm_contacts"] == []
