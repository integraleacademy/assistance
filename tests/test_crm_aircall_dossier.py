from __future__ import annotations

import copy
import re
import threading
import uuid

import pytest

import crm_aircall_dossier as integration


class FakeRequest:
    def __init__(self):
        self.payload = None
        self.headers = {}

    def get_json(self, silent=True):
        return copy.deepcopy(self.payload)


class FakeApp:
    def __init__(self):
        self.routes = {}
        self.logger = type("Logger", (), {"warning": lambda *args, **kwargs: None})()

    def add_url_rule(self, path, endpoint, view_func, methods):
        self.routes[(path, tuple(methods))] = view_func


class FakeLegacyApp:
    def __init__(self, lookup):
        self.app = FakeApp()
        self.request = FakeRequest()
        self.jsonify = lambda payload: payload
        self._CRM_RECONCILIATION_LOCK = threading.RLock()
        self._store = {
            "crm_contacts": [{
                "id": "contact-1",
                "prenom": "Jean",
                "nom": "Dupont",
                "mail": "jean.dupont@example.com",
                "telephone": "06 12 34 56 78",
                "formation": "APS",
                "activities": [],
            }],
            "crm_cnaps_scoring_snapshots": {
                "contact-1": {
                    "normalized_status": "transmitted",
                    "raw_status": "TRANSMIS",
                    "synced_at": "2026-08-27T14:00:00+00:00",
                },
            },
        }
        self.sent_sms = []
        self.saved = 0
        integration.register_aircall_dossier_actions(self, cnaps_lookup=lookup)

    def load_data(self):
        return self._store

    def save_data(self, data):
        assert data is self._store
        self.saved += 1

    def _crm_contact(self, data, contact_id):
        return next((item for item in data["crm_contacts"] if item["id"] == contact_id), None)

    def _crm_now(self):
        return "2026-08-28T10:00:00+02:00"

    def _crm_activity(self, contact, kind, title, detail="", author_name=""):
        contact.setdefault("activities", []).insert(0, {
            "id": str(uuid.uuid4()),
            "kind": kind,
            "title": title,
            "detail": detail,
            "author": author_name,
        })

    def send_sms(self, phone, body):
        self.sent_sms.append((phone, body))
        return True

    def call(self, path, method, payload=None, key="actions-secret"):
        self.request.payload = payload
        self.request.headers = {integration.AIRCALL_ACTIONS_KEY_HEADER: key}
        result = self.app.routes[(path, (method,))]()
        if isinstance(result, tuple):
            return result
        return result, 200


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setenv(integration.AIRCALL_ACTIONS_SECRET_ENV, "actions-secret")
    monkeypatch.setenv("AIRCALL_DOSSIER_START_LIMIT", "3")
    monkeypatch.setattr(integration.secrets, "randbelow", lambda maximum: 382731)
    with integration._VERIFICATION_LOCK:
        integration._VERIFICATIONS.clear()
        integration._START_HISTORY.clear()


@pytest.fixture
def live_lookup():
    return lambda legacy, contact: {
        "normalized_status": "in_review",
        "raw_status": "EN INSTRUCTION",
        "last_checked_at": "2026-08-28T07:30:00Z",
        "synced_at": "2026-08-28T07:30:00Z",
    }


def start_payload(**overrides):
    payload = {
        "caller_phone": "+33612345678",
        "first_name": "Jean",
        "last_name": "Dupont",
        "email": "jean.dupont@example.com",
    }
    payload.update(overrides)
    return payload


def start_and_get_code(app):
    body, status = app.call(
        integration.AIRCALL_DOSSIER_START_PATH,
        "POST",
        start_payload(),
    )
    assert status == 200 and body["verification_sent"] is True
    code = re.search(r"\b(\d{6})\b", app.sent_sms[-1][1]).group(1)
    return body["verification_id"], code


def test_health_requires_matching_api_key(live_lookup):
    app = FakeLegacyApp(live_lookup)
    body, status = app.call(integration.AIRCALL_DOSSIER_HEALTH_PATH, "GET", key="wrong")
    assert status == 401
    body, status = app.call(integration.AIRCALL_DOSSIER_HEALTH_PATH, "GET")
    assert status == 200 and body["ok"] is True


def test_start_sends_one_time_code_to_phone_stored_in_crm(live_lookup):
    app = FakeLegacyApp(live_lookup)
    body, status = app.call(
        integration.AIRCALL_DOSSIER_START_PATH,
        "POST",
        start_payload(caller_phone="0612345678"),
    )
    assert status == 200
    assert body["verification_sent"] is True
    assert body["verification_id"].startswith("verif_")
    assert app.sent_sms[0][0] == "06 12 34 56 78"
    assert "482731" in app.sent_sms[0][1]


def test_start_does_not_reveal_whether_unknown_or_mismatched_identity_exists(live_lookup):
    app = FakeLegacyApp(live_lookup)
    unknown, _ = app.call(
        integration.AIRCALL_DOSSIER_START_PATH,
        "POST",
        start_payload(caller_phone="0699999999", email="unknown@example.com"),
    )
    mismatched, _ = app.call(
        integration.AIRCALL_DOSSIER_START_PATH,
        "POST",
        start_payload(last_name="Martin"),
    )
    assert unknown == mismatched
    assert unknown["requires_human"] is True
    assert app.sent_sms == []


def test_invalid_code_never_returns_dossier_data_and_allows_retry(live_lookup):
    app = FakeLegacyApp(live_lookup)
    verification_id, _ = start_and_get_code(app)
    body, status = app.call(
        integration.AIRCALL_DOSSIER_STATUS_PATH,
        "POST",
        {"verification_id": verification_id, "verification_code": "111111"},
    )
    assert status == 200
    assert body["identity_verified"] is False
    assert body["retry_allowed"] is True
    assert "cnaps_status" not in body


def test_valid_code_returns_live_cnaps_status_and_is_one_time(live_lookup):
    app = FakeLegacyApp(live_lookup)
    verification_id, code = start_and_get_code(app)
    body, status = app.call(
        integration.AIRCALL_DOSSIER_STATUS_PATH,
        "POST",
        {"verification_id": verification_id, "verification_code": code},
    )
    assert status == 200
    assert body["identity_verified"] is True
    assert body["cnaps_status"] == "in_review"
    assert "en cours d'instruction" in body["spoken_response"]
    assert body["requires_human"] is False
    assert app._store["crm_cnaps_scoring_snapshots"]["contact-1"]["raw_status"] == "EN INSTRUCTION"
    activity = app._store["crm_contacts"][0]["activities"][0]
    assert activity["title"] == "Statut CNAPS consulté par l'assistante IA"
    assert activity["author"] == "Assistante IA Aircall"
    assert "482731" not in activity["detail"]

    replay, _ = app.call(
        integration.AIRCALL_DOSSIER_STATUS_PATH,
        "POST",
        {"verification_id": verification_id, "verification_code": code},
    )
    assert replay["identity_verified"] is False


def test_cached_status_is_used_when_remote_cnaps_is_unavailable():
    app = FakeLegacyApp(lambda legacy, contact: None)
    verification_id, code = start_and_get_code(app)
    body, _ = app.call(
        integration.AIRCALL_DOSSIER_STATUS_PATH,
        "POST",
        {"verification_id": verification_id, "verification_code": code},
    )
    assert body["cnaps_status"] == "transmitted"
    assert "dernier statut enregistré" in body["freshness_message"]


def test_refused_status_requires_human_follow_up():
    app = FakeLegacyApp(lambda legacy, contact: {
        "normalized_status": "refused",
        "raw_status": "REFUSE",
        "synced_at": "2026-08-28T07:30:00Z",
    })
    verification_id, code = start_and_get_code(app)
    body, _ = app.call(
        integration.AIRCALL_DOSSIER_STATUS_PATH,
        "POST",
        {"verification_id": verification_id, "verification_code": code},
    )
    assert body["cnaps_status"] == "refused"
    assert body["requires_human"] is True
    assert "confidentialité" in body["next_step_message"]


def test_verification_requests_are_rate_limited(live_lookup):
    app = FakeLegacyApp(live_lookup)
    for _ in range(3):
        body, _ = app.call(
            integration.AIRCALL_DOSSIER_START_PATH,
            "POST",
            start_payload(),
        )
        assert body["verification_sent"] is True
    blocked, _ = app.call(
        integration.AIRCALL_DOSSIER_START_PATH,
        "POST",
        start_payload(),
    )
    assert blocked["reason"] == "rate_limited"
    assert len(app.sent_sms) == 3


def test_sms_failure_invalidates_verification(live_lookup):
    app = FakeLegacyApp(live_lookup)
    app.send_sms = lambda phone, body: False
    body, _ = app.call(
        integration.AIRCALL_DOSSIER_START_PATH,
        "POST",
        start_payload(),
    )
    assert body["reason"] == "sms_unavailable"
    assert integration._VERIFICATIONS == {}
