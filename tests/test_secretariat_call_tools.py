"""Call desk scenarios across intake, CRM handover and delivery providers."""

import copy
import datetime
import json

import pytest

from crm_app import legacy_app as app


@pytest.fixture
def desk(monkeypatch, tmp_path):
    data = copy.deepcopy(app.DEFAULT_DATA)
    data.update(crm_contacts=[], secretariat_demandes=[], demandes=[])
    monkeypatch.setattr(app, "DATA_FILE", str(tmp_path / "app.json"))
    monkeypatch.setattr(app, "load_data", lambda: data)
    monkeypatch.setattr(app, "_load_data_snapshot", lambda: data)
    monkeypatch.setattr(app, "save_data", lambda *_: None)
    monkeypatch.setattr(app, "_secretariat_refresh_calendly_appointments", lambda *_: None)
    monkeypatch.setattr(app, "_secretariat_information_email", lambda *_: (
        {"id": "training", "usage_count": 0}, "Formation", "Formation", "<p>Formation</p>"))
    deliveries = {"email": [], "sms": [], "salesforce": []}
    monkeypatch.setattr(app, "_crm_send_email_html", lambda address, *a, **kw: deliveries["email"].append(address) or True)
    monkeypatch.setattr(app, "send_sms", lambda number, *_: deliveries["sms"].append(number) or number == "0600000042")
    monkeypatch.setattr(app, "creer_piste_salesforce", deliveries["salesforce"].append)
    app.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    return app.app.test_client(), data, deliveries


def intake(**kwargs):
    return {"call_key": "a" * 32, "type": "formation", "formation": "APS",
            "nom": "Camille Martin", "prenom": "Camille", "nom_famille": "Martin",
            "telephone": "0600000000", "email": "camille@example.test", "notes": "Question sur le calendrier.",
            "rdv": "Non souhaité", **kwargs}


def test_receipt_and_retries_are_owned_by_one_call_and_only_send_the_failed_channel(desk):
    client, data, deliveries = desk
    payload = intake()
    first = client.post("/api/secretariat/demandes", json=payload)
    assert first.status_code == 201
    receipt = first.get_json()
    assert receipt["messages"] == {"email": "sent", "sms": "failed"}
    request_id = receipt["demande"]["id"]
    assert "call_key_hash" not in receipt["demande"]
    assert client.post("/api/secretariat/demandes", json=payload).get_json() == receipt
    url = f"/api/secretariat/demandes/{request_id}/renvoyer/sms"
    assert client.post(url, json={"call_key": "b" * 32, "recipient": "0600000042"}).status_code == 404
    assert client.post(url, json={"call_key": payload["call_key"], "recipient": "abc"}).status_code == 400
    retry = {"call_key": payload["call_key"], "recipient": "0600000042"}
    assert client.post(url, json=retry).get_json()["messages"] == {"email": "sent", "sms": "sent"}
    assert client.post(url, json=retry).get_json()["messages"]["sms"] == "sent"
    assert deliveries["email"] == [payload["email"]]
    assert deliveries["sms"] == [payload["telephone"], "0600000042"]
    assert len(deliveries["salesforce"]) == len(data["crm_contacts"]) == len(data["secretariat_demandes"]) == 1
    assert data["secretariat_demandes"][0]["telephone"] == data["crm_contacts"][0]["telephone"] == "0600000042"


def test_retry_existing_recipient_requires_staff_for_changes_and_rejects_other_people(desk):
    client, data, deliveries = desk
    original = {"id": "known", "prenom": "Camille", "nom": "Martin", "mail": "camille@example.test",
                "telephone": "0600000000", "activities": [], "publications": []}
    data["crm_contacts"] = [original]
    receipt = client.post("/api/secretariat/demandes", json=intake()).get_json()
    assert receipt["demande"]["linked"]
    url = "/api/secretariat/demandes/" + receipt["demande"]["id"] + "/renvoyer/sms"
    payload = {"call_key": "a" * 32, "recipient": "0600000042"}
    assert client.post(url, json=payload).status_code == 403
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    data["crm_contacts"].append({"id": "other", "telephone": "0600000042"})
    assert client.post(url, json=payload).status_code == 409
    assert deliveries["sms"] == ["0600000000"]
    data["crm_contacts"].pop()
    assert client.post(url, json=payload).get_json()["messages"]["sms"] == "sent"


def test_corrected_email_reuses_the_quote_and_never_sends_the_sms_again(desk, monkeypatch):
    client, data, deliveries = desk
    def mail_to(address, *args, **kwargs):
        deliveries["email"].append(address)
        return address == "correct@example.test"

    monkeypatch.setattr(app, "_crm_send_email_html", mail_to)
    receipt = client.post("/api/secretariat/demandes", json=intake(telephone="0600000042", devis="OUI")).get_json()
    assert receipt["messages"] == {"email": "failed", "sms": "sent"}
    assert receipt["demande"]["quote_created"]
    url = "/api/secretariat/demandes/" + receipt["demande"]["id"] + "/renvoyer/email"
    result = client.post(url, json={"call_key": "a" * 32, "recipient": "correct@example.test"}).get_json()
    assert result["messages"] == {"email": "sent", "sms": "sent"}
    assert deliveries["email"] == ["camille@example.test", "correct@example.test"]
    assert deliveries["sms"] == ["0600000042"] and len(deliveries["salesforce"]) == 1
    quote, = data["demandes"]
    assert quote["statut_devis"] == "Envoyé" and quote["mail"] == "correct@example.test"
    assert json.loads(quote["details"])["mail"] == "correct@example.test"


def test_undecided_finance_is_distinct_from_no_or_zero_in_the_crm_and_quote(desk):
    client, data, deliveries = desk
    payload = intake(cpf_consulte="OUI", cpf_montant_statut="a_confirmer", cpf_montant="100",
                     france_travail="A_CONFIRMER", identite_numerique="A_CONFIRMER", devis="OUI")
    receipt = client.post("/api/secretariat/demandes", json=payload).get_json()
    entry, contact = data["secretariat_demandes"][0], data["crm_contacts"][0]
    assert receipt["demande"]["quote_created"]
    assert receipt["demande"]["financement_a_confirmer"] == ["solde CPF", "financement France Travail", "Identité Numérique La Poste"]
    assert entry["cpf_montant"] == entry["france_travail"] == ""
    assert not contact.get("cpf_montant") and not contact.get("financement_ft")
    assert "solde CPF" in contact["publications"][0]["texte"]
    assert "financement France Travail" in deliveries["salesforce"][0]["infos_complementaires"]
    details = json.loads(data["demandes"][0]["details"])
    assert details["cpf_montant"] == details["france_travail"] == details["identite_numerique"] == ""
    assert details["financement_a_confirmer"] == entry["financement_a_confirmer"]


def test_interrupted_call_records_partial_intake_once_with_a_paris_deadline(desk):
    client, data, deliveries = desk
    recipient = next(iter(app.USERS.values()))["name"]
    payload = intake(type="autre", callback_motif="formation", email="", nom="", prenom="", nom_famille="",
                     callback_recipient=recipient, callback_due_at="2027-06-10T12:00", formation_date_souhaitee="Session juin")
    receipt = client.post("/api/secretariat/demandes", json=payload).get_json()
    assert receipt["messages"] == {}
    assert client.post("/api/secretariat/demandes", json=payload).status_code == 200
    row, = app._crm_callback_requests_payload(data)
    assert (row["recipient"], row["notes"], row["motif"]) == (recipient, payload["notes"], "formation")
    assert row["due_at"].endswith("+02:00") and row["due_label"] == "10/06/2027 à 12:00"
    assert row["formation"] and row["session_label"] == "Session juin"
    assert not any(deliveries.values()) and not data["crm_contacts"]
    other = {**payload, "call_key": "b" * 32, "callback_due_at": "incorrect"}
    assert client.post("/api/secretariat/demandes", json=other).status_code == 400
    assert len(data["secretariat_demandes"]) == 1
    assert client.post("/api/secretariat/demandes", json={**other, "callback_due_at": ""}).status_code == 201
    assert len(data["secretariat_demandes"]) == 2


def test_contact_overview_requires_staff_and_explicitly_confirmed_unique_identity(desk):
    client, data, _ = desk
    future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=10)).isoformat()
    data["crm_contacts"] = [{"id": "known", "prenom": "Camille", "nom": "Martin",
                             "telephone": "0600000000", "formation": "APS", "salesforce_owner": "Conseiller A",
                             "activities": [{"kind": "appel", "title": "Appel sortant", "date": "2026-11-01T11:00:00Z", "preview": "Confidentiel"}]}]
    data["crm_calendly_appointments"] = [{"contact_id": "known", "start_time": future, "name": "Appel téléphonique", "host_name": "Conseiller A"}]
    data["secretariat_demandes"] = [{"type": "autre", "crm_contact_id": "known", "callback_status": "pending", "notes": "Question devis",
                                    "callback_due_at": "2026-12-02T09:30:00+01:00"}]
    url = "/api/secretariat/crm-contact"
    lookup = {"telephone": "0600000000", "confirmed_contact_id": "known"}
    assert "summary" not in client.post(url, json=lookup).get_json()
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    assert "summary" not in client.post(url, json={"telephone": lookup["telephone"]}).get_json()
    assert "summary" not in client.post(url, json={**lookup, "confirmed_contact_id": "wrong"}).get_json()
    result = client.post(url, json=lookup)
    facts = result.get_json()["summary"]
    assert facts["formation"] == "APS" and facts["conseiller"] == "Conseiller A"
    assert "Appel sortant" in facts["dernier_echange"] and "Confidentiel" not in result.get_data(as_text=True)
    assert "Conseiller A" in facts["rdv"] and "Question devis" in facts["rappel"]
    data["crm_contacts"].append({"id": "other", "telephone": lookup["telephone"]})
    assert client.post(url, json=lookup).get_json() == {"contact_id": None}


@pytest.mark.parametrize("tuition,deposit,balance", [(3850, "1 155", "2 695"), (3800, "1 140", "2 660")])
def test_vae_email_retries_use_the_same_installments_as_the_initial_email(tuition, deposit, balance):
    content = app.build_vae_desp_email_html("Camille", "", tuition=tuition)
    assert deposit + " €" in content and balance + " €" in content
