from __future__ import annotations

import datetime as dt
import threading

import pytest

import crm_aircall_caller_context as integration


def contact(**overrides):
    value = {
        "id": "contact-1",
        "prenom": "Clément",
        "nom": "Vaillant",
        "telephone": "06 12 34 56 78",
        "mail": "clement@example.com",
        "formation": "A3P",
        "dates_formation": "Du 11 septembre au 12 octobre 2026",
        "lieu": "Côte d’Azur",
        "statut": "Converti",
        "updated_at": "2026-08-28T10:00:00+02:00",
    }
    value.update(overrides)
    return value


def test_unique_phone_match_returns_safe_personalized_context():
    result = integration.build_caller_context(
        {"crm_contacts": [contact()], "crm_calendly_appointments": []},
        "+33 6 12 34 56 78",
    )

    assert result["matched"] is True
    assert result["personalization_available"] is True
    assert result["first_name"] == "Clément"
    assert result["formation"] == "A3P"
    assert result["session_spoken"] == "du 11 septembre au 12 octobre 2026"
    assert result["greeting_message"] == (
        "Clément, j'espère que vous allez bien. Est-ce bien Clément à l'appareil ?"
    )
    assert result["context_question"] == (
        "Appelez-vous au sujet de votre formation A trois P, prévue "
        "du 11 septembre au 12 octobre 2026, ou pour une autre demande ?"
    )
    assert result["identity_confirmation_required"] is True


def test_unknown_phone_stays_generic_without_leaking_data():
    result = integration.build_caller_context(
        {"crm_contacts": [contact()]},
        "07 99 99 99 99",
    )

    assert result["matched"] is False
    assert result["personalization_available"] is False
    assert result["first_name"] == ""
    assert result["context_question"] == ""
    assert result["fallback_prompt"] == "Comment puis-je vous renseigner ?"


def test_different_people_sharing_phone_are_ambiguous():
    data = {
        "crm_contacts": [
            contact(id="one", prenom="Clément", nom="Vaillant"),
            contact(id="two", prenom="Elsa", nom="Duquesne", mail="elsa@example.com"),
        ],
    }

    result = integration.build_caller_context(data, "0612345678")

    assert result["matched"] is False
    assert result["ambiguous"] is True
    assert result["personalization_available"] is False


def test_duplicates_for_same_identity_choose_most_complete_record():
    data = {
        "crm_contacts": [
            contact(
                id="old",
                formation="",
                dates_formation="",
                updated_at="2026-01-01T09:00:00+01:00",
            ),
            contact(
                id="current",
                formation="SSIAP 1",
                dates_formation="15/09/2026 au 26/09/2026",
                updated_at="2026-08-20T09:00:00+02:00",
            ),
        ],
    }

    result = integration.build_caller_context(data, "0612345678")

    assert result["matched"] is True
    assert result["formation"] == "SSIAP 1"
    assert result["session_spoken"] == "du 15 au 26 septembre 2026"
    assert "Siappe un" in result["context_question"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Du 9 novembre 2026 au 19 janvier 2027", "du 9 novembre 2026 au 19 janvier 2027"),
        ("01/09/2026 - 27/10/2026", "du 1er septembre au 27 octobre 2026"),
        ("2026-09-11 au 2026-10-12", "du 11 septembre au 12 octobre 2026"),
    ],
)
def test_session_date_formatting(raw, expected):
    assert integration._format_date_range(raw) == expected


def test_upcoming_calendly_appointment_is_available_as_optional_context():
    now = dt.datetime(2026, 8, 28, 12, 0, tzinfo=integration._PARIS)
    data = {
        "crm_contacts": [contact(formation="", dates_formation="")],
        "crm_calendly_appointments": [
            {
                "contact_id": "contact-1",
                "status": "active",
                "start_time": "2026-09-03T08:30:00Z",
            },
        ],
    }

    result = integration.build_caller_context(data, "0612345678", now=now)

    assert result["next_appointment_label"] == "le 3 septembre 2026 à 10 heures 30"
    assert "rendez-vous prévu le 3 septembre 2026 à 10 heures 30" in result["context_question"]


class FakeRequest:
    def __init__(self):
        self.headers = {}
        self.payload = None

    def get_json(self, silent=True):
        return self.payload


class FakeFlaskApp:
    def __init__(self):
        self.routes = {}

    def add_url_rule(self, path, endpoint, view_func, methods):
        self.routes[(path, tuple(methods))] = view_func


class FakeLegacy:
    def __init__(self):
        self.app = FakeFlaskApp()
        self.request = FakeRequest()
        self.jsonify = lambda payload: payload
        self._CRM_RECONCILIATION_LOCK = threading.RLock()
        self.data = {"crm_contacts": [contact()], "crm_calendly_appointments": []}

    def load_data(self):
        return self.data


def test_route_requires_key_and_returns_context(monkeypatch):
    monkeypatch.setenv("AIRCALL_ACTIONS_API_KEY", "secret")
    legacy = FakeLegacy()
    integration.register_aircall_caller_context(legacy)
    route = legacy.app.routes[(integration.AIRCALL_CALLER_CONTEXT_PATH, ("POST",))]

    legacy.request.payload = {"caller_phone": "+33612345678"}
    body, status = route()
    assert status == 401

    legacy.request.headers = {"X-Aircall-Actions-Key": "secret"}
    body, status = route()
    assert status == 200
    assert body["matched"] is True
    assert body["first_name"] == "Clément"
