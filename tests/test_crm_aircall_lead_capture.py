from __future__ import annotations

import copy
import datetime as dt
import re
import threading
import uuid
from pathlib import Path

import pytest
from flask import Flask, jsonify, render_template, request

import crm_aircall_lead_capture as integration


ROOT = Path(__file__).resolve().parents[1]


class FakeLegacyApp:
    def __init__(self):
        self.app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))
        self.app.config.update(TESTING=True, SECRET_KEY="test")
        self.request = request
        self.jsonify = jsonify
        self.render_template = render_template
        self._CRM_RECONCILIATION_LOCK = threading.RLock()
        self._store = {
            "crm_contacts": [],
            "crm_inbound_requests": [],
            "crm_aircall_lead_requests": [],
            "secretariat_demandes": [],
            "crm_notifications": [],
        }
        self.saved = 0
        self.sms_ok = True
        self.sent_sms = []
        self.SECRETARIAT_FORMATIONS = {
            "A3P": {
                "short": "A3P", "label": "A3P – Agent de Protection Physique des Personnes",
                "duration": "328 h", "price": "4 200 € TTC", "format": "Présentiel",
            },
            "APS": {"short": "APS", "duration": "175 h", "price": "1 650 € TTC", "format": "Présentiel"},
            "SSIAP": {"short": "SSIAP 1", "duration": "70 h", "price": "980 € TTC", "format": "Présentiel"},
            "DESP_INIT": {"short": "DESP initial", "duration": "245 h", "price": "4 300 € TTC", "format": "Hybride"},
            "DESP_VAE": {"short": "VAE DESP", "duration": "3 semaines", "price": "3 800 € TTC", "format": "À distance"},
            "VTC": {"short": "Chauffeur VTC", "duration": "105 h", "price": "1 500 € TTC", "format": "Hybride"},
            "BTS_MOS": {"short": "BTS MOS"}, "BTS_MCO": {"short": "BTS MCO"},
            "BTS_NDRC": {"short": "BTS NDRC"}, "BTS_CI": {"short": "BTS CI"},
            "BTS_PI": {"short": "BTS PI"}, "BTS_CG": {"short": "BTS CG"},
        }
        self.FORMATION_CENTRES = {
            "cote_azur": "Côte d’Azur", "paris": "Paris", "auvergne": "Auvergne",
        }
        self.sessions = {
            "cote_azur": {
                "A3P": [
                    {"label": "Du 1er septembre au 27 octobre 2026", "badge": "COMPLET"},
                    {"label": "Du 9 novembre 2026 au 19 janvier 2027", "badge": "4 places"},
                ],
                "APS": [{"label": "Du 3 novembre au 8 décembre 2026", "badge": ""}],
            },
            "paris": {"DESP_INIT": [{"label": "Du 24 septembre au 9 novembre 2026", "badge": ""}]},
        }
        integration.register_aircall_lead_capture(self)

    def load_data(self):
        return copy.deepcopy(self._store)

    def save_data(self, data):
        self._store = copy.deepcopy(data)
        self.saved += 1

    def send_sms(self, phone, body):
        self.sent_sms.append((phone, body))
        return self.sms_ok

    def get_upcoming_formation_sessions(self, data):
        return copy.deepcopy(self.sessions)

    @staticmethod
    def _session_start_date(label):
        if "1er septembre" in str(label):
            return dt.date(2026, 9, 1)
        if "24 septembre" in str(label):
            return dt.date(2026, 9, 24)
        if "3 novembre" in str(label):
            return dt.date(2026, 11, 3)
        if "9 novembre" in str(label):
            return dt.date(2026, 11, 9)
        return None

    @staticmethod
    def _normalize_centre_code(value):
        text = str(value or "").casefold()
        if "paris" in text:
            return "paris"
        if "aurillac" in text or "auvergne" in text:
            return "auvergne"
        if "puget" in text or "azur" in text:
            return "cote_azur"
        return ""

    @staticmethod
    def _crm_now():
        return "2026-08-30T12:00:00+02:00"

    def _crm_activity(self, contact, kind, title, detail="", author_name="", *args):
        contact.setdefault("activities", []).insert(0, {
            "id": str(uuid.uuid4()), "date": self._crm_now(), "kind": kind,
            "title": title, "detail": detail, "author": author_name,
        })

    def _crm_prepare_callback_request(self, data, entry):
        normalized = integration._normalize_phone(entry.get("telephone"))
        contact = next((
            item for item in data.get("crm_contacts", [])
            if normalized and integration._normalize_phone(item.get("telephone")) == normalized
        ), None)
        entry["crm_contact_id"] = contact.get("id") if contact else ""
        return bool(contact), contact

    def _crm_ensure_callback_request_activity(self, contact, entry):
        if any(item.get("callback_request_id") == entry.get("id") for item in contact.get("activities", [])):
            return False
        self._crm_activity(contact, "demande_rappel", "Demande de rappel reçue", entry.get("notes", ""), "Aircall")
        contact["activities"][0]["callback_request_id"] = entry.get("id")
        contact["activities"][0]["callback_status"] = entry.get("callback_status")
        return True

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

        normalized_phone = integration._normalize_phone(payload.get("telephone"))
        email = str(payload.get("mail") or "").casefold()
        contact = next((
            item for item in data["crm_contacts"]
            if (normalized_phone and integration._normalize_phone(item.get("telephone")) == normalized_phone)
            or (email and str(item.get("mail") or "").casefold() == email)
        ), None)
        created = contact is None
        if contact is None:
            contact = {
                "id": str(uuid.uuid4()), "prenom": payload.get("prenom", ""),
                "nom": payload.get("nom", ""), "mail": payload.get("mail", ""),
                "telephone": payload.get("telephone", ""), "formation": payload.get("formation", ""),
                "statut": "Nouveaux", "origine": source, "activities": [],
            }
            data["crm_contacts"].insert(0, contact)
        else:
            for field in ("prenom", "nom", "mail", "telephone", "formation"):
                if not contact.get(field) and payload.get(field):
                    contact[field] = payload[field]
        inbound = {
            "id": str(uuid.uuid4()), "source": source, "external_id": external_id,
            "contact_id": contact["id"], "status": "created" if created else "matched",
        }
        data["crm_inbound_requests"].insert(0, inbound)
        return contact, inbound, created


@pytest.fixture
def legacy(monkeypatch):
    monkeypatch.setenv("AIRCALL_ACTIONS_API_KEY", "aircall-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://assistance.example.test")
    return FakeLegacyApp()


def action_headers():
    return {"X-Aircall-Actions-Key": "aircall-secret"}


def _send_sms(legacy, *, call_id="call-123", formation="A trois P"):
    client = legacy.app.test_client()
    response = client.post(
        integration.AIRCALL_LEAD_SMS_PATH,
        json={"caller_phone": "+33 6 12 34 56 78", "formation": formation, "call_id": call_id},
        headers=action_headers(),
    )
    assert response.status_code == 200
    return response.get_json()


def _token_from_last_sms(legacy):
    match = re.search(r"/rappel-formation/([A-Za-z0-9_-]+)$", legacy.sent_sms[-1][1])
    assert match
    return match.group(1)


def test_training_action_returns_live_next_session_duration_and_price(legacy):
    response = legacy.app.test_client().post(
        integration.AIRCALL_TRAINING_INFORMATION_PATH,
        json={"formation": "A trois P", "centre": "Puget-sur-Argens"},
        headers=action_headers(),
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["formation_code"] == "A3P"
    assert body["duration"] == "327 h · 9 semaines"
    assert body["price"] == "4 200 € TTC"
    assert body["next_session"]["full"] is True
    assert body["next_available_session"]["label"] == "Du 9 novembre 2026 au 19 janvier 2027"
    assert "session est indiquée complète" in body["spoken_response"]


def test_training_action_asks_for_clarification_instead_of_inventing(legacy):
    response = legacy.app.test_client().post(
        integration.AIRCALL_TRAINING_INFORMATION_PATH,
        json={"formation": "formation inconnue"},
        headers=action_headers(),
    )

    body = response.get_json()
    assert response.status_code == 200
    assert body["success"] is False
    assert body["requires_clarification"] is True
    assert len(body["formations"]) == 12


def test_sms_action_is_idempotent_and_creates_visible_pending_callback(legacy):
    first = _send_sms(legacy)
    second = _send_sms(legacy)

    assert first["success"] is True
    assert first["sms_sent"] is True
    assert second["already_sent"] is True
    assert len(legacy.sent_sms) == 1
    assert legacy.sent_sms[0][1].startswith(
        "Intégrale Academy : complétez le formulaire. "
        "Un expert formation vous rappellera : https://assistance.example.test/rappel-formation/"
    )
    assert len(legacy._store["crm_aircall_lead_requests"]) == 1
    assert len(legacy._store["secretariat_demandes"]) == 1
    record = legacy._store["crm_aircall_lead_requests"][0]
    assert record["status"] == "pending"
    assert record["sms_status"] == "sent"
    assert record["token_hash"]
    assert "token" not in record
    callback = legacy._store["secretariat_demandes"][0]
    assert callback["type"] == "autre"
    assert callback["callback_status"] == "pending"
    assert "Coordonnées attendues" in callback["notes"]


def test_form_creates_one_lead_then_becomes_idempotent(legacy):
    _send_sms(legacy)
    token = _token_from_last_sms(legacy)
    client = legacy.app.test_client()

    page = client.get(f"/rappel-formation/{token}")
    assert page.status_code == 200
    assert page.headers["Cache-Control"].startswith("no-store")
    assert page.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    assert "A3P – Agent de Protection".encode() in page.data
    assert b'value="A3P" selected' in page.data

    form = {
        "prenom": "Jean", "nom": "Dupont", "email": "JEAN@example.com",
        "telephone": "06 12 34 56 78", "formation": "A3P",
        "message": "Je souhaite étudier un financement.", "consent": "1",
    }
    submitted = client.post(f"/rappel-formation/{token}", data=form)
    repeated = client.post(f"/rappel-formation/{token}", data=form)

    assert submitted.status_code == 200
    assert "Votre demande est bien transmise".encode("utf-8") in submitted.data
    assert repeated.status_code == 200
    assert len(legacy._store["crm_contacts"]) == 1
    assert len(legacy._store["crm_inbound_requests"]) == 1
    contact = legacy._store["crm_contacts"][0]
    assert contact["prenom"] == "Jean"
    assert contact["nom"] == "Dupont"
    assert contact["mail"] == "jean@example.com"
    assert contact["formation"] == "A3P"
    assert contact["origine"] == integration.AIRCALL_SMS_ORIGIN
    assert any(item["title"] == "Formulaire SMS Aircall complété" for item in contact["activities"])
    request_record = legacy._store["crm_aircall_lead_requests"][0]
    assert request_record["status"] == "submitted"
    assert request_record["consent_at"]
    assert request_record["consent_version"] == "rappel-formation-v1"
    callback = legacy._store["secretariat_demandes"][0]
    assert callback["callback_status"] == "processed"
    assert callback["crm_contact_id"] == contact["id"]


def test_form_enriches_existing_contact_without_duplicate(legacy):
    existing = {
        "id": "existing", "prenom": "", "nom": "", "mail": "",
        "telephone": "+33612345678", "formation": "", "statut": "Nouveaux",
        "origine": "Site internet", "activities": [],
    }
    legacy._store["crm_contacts"].append(existing)
    _send_sms(legacy, call_id="existing-call", formation="APS")
    token = _token_from_last_sms(legacy)

    response = legacy.app.test_client().post(f"/rappel-formation/{token}", data={
        "prenom": "Alice", "nom": "Martin", "email": "alice@example.com",
        "telephone": "0612345678", "formation": "APS", "consent": "1",
    })

    assert response.status_code == 200
    assert len(legacy._store["crm_contacts"]) == 1
    assert legacy._store["crm_contacts"][0]["id"] == "existing"
    assert legacy._store["crm_contacts"][0]["origine"] == "Site internet"
    assert legacy._store["crm_contacts"][0]["formation"] == "APS"


def test_ambiguous_existing_contacts_stay_pending_for_manual_review(legacy):
    for identifier in ("existing-1", "existing-2"):
        legacy._store["crm_contacts"].append({
            "id": identifier, "prenom": "", "nom": "", "mail": "",
            "telephone": "+33612345678", "formation": "", "statut": "Nouveaux",
            "origine": "Site internet", "activities": [],
        })

    def ambiguous_match(data, payload, source, **options):
        inbound = {
            "id": "review-1", "source": source,
            "external_id": str(options.get("external_id") or ""),
            "contact_id": None, "status": "pending_review",
        }
        data["crm_inbound_requests"].insert(0, inbound)
        return None, inbound, False

    legacy.find_or_create_crm_contact = ambiguous_match
    _send_sms(legacy, call_id="ambiguous-call", formation="APS")
    token = _token_from_last_sms(legacy)

    response = legacy.app.test_client().post(f"/rappel-formation/{token}", data={
        "prenom": "Alice", "nom": "Martin", "email": "alice@example.com",
        "telephone": "0612345678", "formation": "APS", "consent": "1",
    })

    assert response.status_code == 200
    assert len(legacy._store["crm_contacts"]) == 2
    assert legacy._store["crm_inbound_requests"][0]["status"] == "pending_review"
    assert legacy._store["crm_aircall_lead_requests"][0]["status"] == "submitted_review"
    callback = legacy._store["secretariat_demandes"][0]
    assert callback["callback_status"] == "pending"
    assert callback["statut"] == "À traiter"
    assert "rapprochement CRM à vérifier" in callback["notes"]


def test_sms_failure_keeps_callback_request_for_human_followup(legacy):
    legacy.sms_ok = False
    result = _send_sms(legacy, call_id="failed-call", formation="SSIAP 1")

    assert result["success"] is False
    assert result["sms_sent"] is False
    assert result["requires_human"] is True
    record = legacy._store["crm_aircall_lead_requests"][0]
    assert record["sms_status"] == "failed"
    callback = legacy._store["secretariat_demandes"][0]
    assert callback["callback_status"] == "pending"
    assert "SMS a échoué" in callback["notes"]


def test_summary_is_attached_to_sent_form_request_without_creating_contact(legacy):
    _send_sms(legacy, call_id="summary-call", formation="A3P")
    data = legacy.load_data()

    record = integration.attach_aircall_summary_to_pending_request(data, {
        "telephone": "+33 6 12 34 56 78",
        "formation": "A3P",
        "raw_training": "A trois P",
        "summary": "Le prospect souhaite connaître les possibilités de financement.",
    }, "summary-call")

    assert record is not None
    assert record["call_summary"].startswith("Le prospect")
    assert data["crm_contacts"] == []
    callback = data["secretariat_demandes"][0]
    assert "possibilités de financement" in callback["notes"]
