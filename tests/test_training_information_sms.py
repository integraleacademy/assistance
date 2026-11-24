import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from hashlib import sha256
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from crm_app import legacy_app as app
import training_information_sms as sms
from secretariat_catalogue import SSIAP3_BOOKING_URL


@pytest.fixture
def intake(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DATA_FILE", str(tmp_path / "data.json"))
    monkeypatch.setitem(app.app.config, "CRM_USE_PUBLIC_CALENDAR", False)
    monkeypatch.setattr(app, "creer_piste_salesforce", Mock())
    monkeypatch.setattr(app, "send_email_html", Mock(return_value=True))
    send = Mock(return_value=True)
    monkeypatch.setattr(app, "send_sms", send)
    return app.app.test_client(), send


@pytest.mark.parametrize("channels", [
    ("desk", "abandoned", "submitted"),
    ("abandoned", "desk", "submitted"),
    ("submitted", "desk", "abandoned"),
])
@pytest.mark.parametrize("desk_course", ["SSIAP3", "chef de service de sécurité incendie"])
def test_only_first_information_channel_sends(intake, channels, desk_course):
    client, send = intake
    fields = {"prenom": "Camille", "nom": "Exemple", "mail": "form@example.invalid",
              "telephone": "+33 6 00 00 00 00", "formation": "SSIAP3",
              "centre": "cote_azur", "souhaite_devis": "OUI"}
    for i, channel in enumerate(channels):
        expected = "sent" if i == 0 else "already_sent"
        if channel == "desk":
            result = client.post("/api/secretariat/demandes", json={
                "type": "formation", "prenom": "Camille", "nom": "Camille Exemple",
                "nom_famille": "Exemple", "email": "desk@example.invalid" if i == 0 else fields["mail"],
                "telephone": "06 00 00 00 00", "formation": desk_course,
                "formation_centre": "cote_azur", "call_key": "1" * 32,
            })
            assert result.status_code == 201
            entry, = app.load_data()["secretariat_demandes"]
            assert entry["sms_summary_status"] == expected
            assert entry["email_summary_status"] == "sent"
            assert result.get_json()["messages"]["sms"] == expected
        elif channel == "abandoned":
            result = client.post("/api/demande-informations-formations/autosave", json={
                "form_id": "test-abandoned", "status": "abandoned", "fields": fields,
            })
            assert result.status_code == 204
            draft, = app.load_data()["formulaires_abandonnes"]
            assert draft["abandoned_sms_status"] == expected
            assert draft["abandoned_sms_sent_at"] and draft["abandoned_mail_sent_at"]
            assert ("abandoned_sms_body" in draft) == (i == 0)
        else:
            result = client.post("/demande-informations-formations", data=fields)
            assert result.status_code == 302
            data = app.load_data()
            entry = next(row for row in data["demandes"] if row.get("source") == "demande_infos_formations")
            assert entry["sms_status"] == expected and entry["mail_html"]
            assert ("sms_body" in entry) == (i == 0)
            events = [event for contact in data["crm_contacts"] for event in contact["activities"]]
            assert any(event["title"] == "SMS automatique envoyé" for event in events) == (i == 0)
        assert send.call_count == 1
    body = send.call_args.args[1]
    assert body == app.build_training_information_sms_text("SSIAP3")
    assert "SSIAP 3" in body and SSIAP3_BOOKING_URL in body
    assert body.endswith("Clément VAILLANT - Directeur Intégrale Academy")


def deliver(path, phone, course, send, history=dict):
    return sms.deliver(path, phone, course, send, history, app._normaliser_telephone_sms)


def test_window_is_measured_from_last_actual_send_and_is_specific_to_phone_and_course(tmp_path, monkeypatch):
    when = [1_800_000_000.0]
    monkeypatch.setattr(sms, "time", SimpleNamespace(time=lambda: when[0]))
    send = Mock(return_value=True)
    path = tmp_path / "data.json"
    first = deliver(path, "0600000000", "SSIAP3", send)
    assert first.status == "sent"
    when[0] += sms.WINDOW_SECONDS - 1
    duplicate = deliver(path, "0033600000000", "SSIAP 3", send)
    assert duplicate.status == "already_sent" and duplicate.timestamp == first.timestamp
    assert deliver(path, "0600000000", "SSIAP", send).status == "sent"
    assert deliver(path, "0600000000", "SSIAP 1", send).status == "already_sent"
    assert deliver(path, "0600000001", "SSIAP3", send).status == "sent"
    when[0] += 1
    assert deliver(path, "0600000000", "SSIAP3", send).status == "sent"
    assert send.call_count == 4


def test_two_simultaneous_requests_only_contact_provider_once(tmp_path):
    send = Mock(return_value=True)
    barrier = Barrier(2)
    def submit(number):
        barrier.wait(timeout=5)
        return deliver(tmp_path / "data.json", number, "SSIAP3", send).status
    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(submit, ["0600000000", "+33 6 00 00 00 00"]))
    assert sorted(states) == ["already_sent", "sent"]
    send.assert_called_once_with()


@pytest.mark.parametrize(("provider_reply", "first", "second", "calls"), [
    (False, "failed", "sent", 2),
    (None, "unconfirmed", "unconfirmed", 1),
    (TimeoutError(), "unconfirmed", "unconfirmed", 1),
])
def test_retry_depends_on_whether_provider_definitely_rejected(tmp_path, provider_reply, first, second, calls):
    send = Mock(side_effect=[provider_reply, True])
    assert deliver(tmp_path / "data.json", "0600000000", "SSIAP3", send).status == first
    assert deliver(tmp_path / "data.json", "0600000000", "SSIAP3", send).status == second
    assert send.call_count == calls


def test_existing_receipts_protect_each_channel_after_deployment(tmp_path):
    now = datetime.now(sms.PARIS)
    local = now.strftime("%d/%m/%Y %H:%M")
    fields = {"telephone": "0600000000", "formation": "SSIAP3"}
    history = {"secretariat_demandes": [{**fields, "formation": "APS", "sms_summary_sent_at": now.isoformat()}],
               "formulaires_abandonnes": [{"fields": {**fields, "formation": "SSIAP"}, "abandoned_sms_sent_at": local}],
               "demandes": [{"details": json.dumps(fields), "sms_sent_at": local}]}
    send = Mock(return_value=True)
    for course in ("SSIAP3", "APS", "SSIAP1"):
        assert deliver(tmp_path / "data.json", "+33600000000", course, send, lambda: history).status == "already_sent"
    send.assert_not_called()


def test_abandoned_form_with_uncertain_delivery_is_not_retried_on_next_visit(intake, monkeypatch):
    client, send = intake
    send.return_value = None
    fields = {"prenom": "Camille", "nom": "Exemple", "mail": "form@example.invalid",
              "telephone": "0600000000", "formation": "SSIAP3"}
    payload = {"form_id": "test-abandoned", "status": "abandoned", "fields": fields}
    assert client.post("/api/demande-informations-formations/autosave", json=payload).status_code == 204
    draft, = app.load_data()["formulaires_abandonnes"]
    assert draft["abandoned_sms_status"] == "unconfirmed" and not draft.get("abandoned_sms_sent_at")
    assert not app._formulaire_abandonne_doit_declencher_relance(draft)
    monkeypatch.setattr(app, "_deliver_training_information_sms", Mock(side_effect=AssertionError("Retried")))
    assert client.post("/api/demande-informations-formations/autosave", json=payload).status_code == 204
    app._deliver_training_information_sms.assert_not_called()
    send.assert_called_once()


@pytest.mark.parametrize(("explicit_cleanup", "legacy_course"), [(True, "SSIAP3"), (False, "SSIAP3"), (True, "")])
def test_first_submission_after_upgrade_keeps_receipt_of_removed_draft(intake, explicit_cleanup, legacy_course):
    client, send = intake
    fields = {"prenom": "Camille", "nom": "Exemple", "mail": "form@example.invalid",
              "telephone": "0600000000", "formation": "SSIAP3", "centre": "cote_azur", "souhaite_devis": "OUI"}
    data = app.load_data()
    data["formulaires_abandonnes"] = [{"form_id": "old-draft", "fields": {**fields, "formation": legacy_course},
                                      "abandoned_sms_sent_at": datetime.now(sms.PARIS).strftime("%d/%m/%Y %H:%M")}]
    app.save_data(data)
    if explicit_cleanup:
        assert client.post("/api/demande-informations-formations/autosave", json={
            "form_id": "old-draft", "status": "submitted", "fields": fields}).status_code == 204
    assert client.post("/demande-informations-formations", data=fields).status_code == 302
    data = app.load_data()
    assert not data["formulaires_abandonnes"]
    assert any(row.get("sms_status") == "already_sent" for row in data["demandes"])
    send.assert_not_called()


def test_abandonment_before_course_selection_waits_for_the_course(intake):
    client, send = intake
    fields = {"prenom": "Camille", "nom": "Exemple", "mail": "form@example.invalid", "telephone": "0600000000"}
    assert client.post("/api/demande-informations-formations/autosave", json={
        "form_id": "initial", "fields": fields, "status": "abandoned"}).status_code == 204
    draft, = app.load_data()["formulaires_abandonnes"]
    assert draft["abandoned_sms_status"] == "waiting_for_course"
    send.assert_not_called()
    assert client.post("/demande-informations-formations", data={
        **fields, "formation": "SSIAP3", "centre": "cote_azur", "souhaite_devis": "OUI",
        "draft_form_id": "initial",
    }).status_code == 302
    send.assert_called_once()
    assert "SSIAP 3" in send.call_args.args[1]


@pytest.mark.parametrize(("state", "expected"), [("sent", "already_sent"), ("unconfirmed", "unconfirmed")])
def test_v1_receipt_for_full_ssiap3_label_survives_draft_cleanup(tmp_path, state, expected):
    path = tmp_path / "data.json"
    phone = "33600000000"
    label = "Chef de service de sécurité incendie"
    now = datetime.now(sms.PARIS)
    old_key = sha256(f"{phone}:{label.upper()}".encode()).hexdigest()
    with sqlite3.connect(str(path) + ".formation-sms.sqlite3") as db:
        db.execute("CREATE TABLE receipt (key TEXT PRIMARY KEY, state TEXT NOT NULL, stamp REAL NOT NULL)")
        db.execute("PRAGMA user_version = 1")
        db.execute("INSERT INTO receipt VALUES (?, ?, ?)", (old_key, state, now.timestamp()))
    draft = {"fields": {"telephone": phone, "formation": label}, "abandoned_sms_status": state}
    if state == "sent":
        draft["abandoned_sms_sent_at"] = now.strftime("%d/%m/%Y %H:%M")
    sms.preserve_history_before_cleanup(path, {"formulaires_abandonnes": [draft]}, app._normaliser_telephone_sms)
    send = Mock()
    receipt = deliver(path, "0600000000", "SSIAP3", send)
    assert receipt.status == expected and receipt.timestamp == now.timestamp()
    send.assert_not_called()
