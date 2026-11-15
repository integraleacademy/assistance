from copy import deepcopy

import pytest

import app as crm
from test_meta_zapier_webhook import lead, post, setup_client


FORM = "2316311965770128"
QUESTION = "Souhaitez-vous demander un financement à France Travail pour cette formation ?"
RECEIVED = "2026-11-15T00:52:26+01:00"


def old_lead():
    return {
        "id": "previous-a3p", "prenom": "Lina", "nom": "MARTIN",
        "mail": "lina@example.fr", "telephone": "0612345678", "formation": "A3P",
        "origine": "META", "commercial": "Cassandre", "statut": "Disqualifié",
        "created_at": "2026-09-10T09:31:00+02:00", "financement_ft": "NON",
        "activities": [{"id": "history", "kind": "statut", "title": "Piste disqualifiée"}],
    }


def desp(lead_id):
    return lead(**{"leadgen_id": lead_id, "form_id": FORM, "Form Name": "DESP_VIDEO_INITIALE_VAE",
                   "Quel parcours souhaitez-vous étudier ?": "Formation initiale", QUESTION: "Oui"})


def appointment(name, ref):
    return {"id": ref, "invitee_uri": "calendly/" + ref, "contact_id": "previous-a3p",
            "name": name, "start_time": "2099-11-18T13:30:00Z",
            "invitee_email": "lina@example.fr", "status": "active"}


@pytest.mark.parametrize("old_status", ["Disqualifié", "En cours", "Converti"])
def test_new_course_is_independent_and_retry_is_idempotent(tmp_path, monkeypatch, old_status):
    client = setup_client(tmp_path, monkeypatch)
    old = old_lead()
    old["statut"] = old_status
    data = crm.load_data()
    data["crm_contacts"] = [old]
    crm.save_data(data)
    result = post(client, desp("first"))
    assert result.status_code == 201
    current = result.json["contact_id"]
    assert current != old["id"]
    assert post(client, desp("first")).json == {
        "success": True, "result": "already_processed", "contact_id": current,
    }
    assert post(client, desp("second")).json["contact_id"] == current
    data = crm.load_data()
    new, original = data["crm_contacts"]
    assert original == old
    assert new["formation"] == "DESP" and new["statut"] == "Nouveaux"
    assert new["financement_ft"] == "OUI" and new["commercial"] == "Cassandre"
    assert new["origine"] == "META" and new["created_at"] != old["created_at"]
    assert {r["contact_id"] for r in data["crm_meta_lead_submissions"]} == {current}
    assert post(client, lead(**{"leadgen_id": "a3p", "Ad Name": "A3P – vidéo"})).json["contact_id"] == old["id"]


def test_shared_phone_does_not_transfer_another_persons_booking(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    data = crm.load_data()
    data["crm_contacts"] = [old_lead()]
    data["crm_calendly_appointments"] = [appointment("RDV DESP", "other")]
    crm.save_data(data)
    payload = {**desp("new"), "Full Name": "Luc Morel", "Email": "luc@example.fr"}
    created = post(client, payload).json["contact_id"]
    assert post(client, {**payload, "leadgen_id": "later"}).json["contact_id"] == created
    data = crm.load_data()
    assert not crm._crm_contact(data, created).get("meta_course_root_id")
    assert data["crm_calendly_appointments"][0]["contact_id"] == "previous-a3p"


def historical_request(data, lead_id, received):
    data["crm_inbound_requests"].append({"source": "meta_zapier", "external_id": lead_id,
        "status": "matched", "differences": ["formation"], "contact_id": "previous-a3p"})
    data["crm_meta_lead_submissions"].append({"meta_lead_id": lead_id,
        "received_at": received, "raw_payload": desp(lead_id), "contact_id": "previous-a3p",
        "automatic_delivery": {"email": True, "sms": True}})


def test_repair_preserves_date_answers_status_and_unique_booking(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    data = crm.load_data()
    old = old_lead()
    data["crm_contacts"] = [old]
    data["crm_calendly_appointments"] = [
        appointment("RDV téléphonique formation dirigeant d'entreprise", "desp"),
        appointment("Formation protection rapprochée (A3P)", "old-booking"),
    ]
    historical_request(data, "later", "2026-11-17T12:00:00+01:00")
    historical_request(data, "initial", RECEIVED)
    assert crm._crm_repair_attached_meta_courses(data) == 2
    new, original = data["crm_contacts"]
    assert original == old_lead()
    assert (new["formation"], new["statut"], new["created_at"], new["received_at"]) == (
        "DESP", "RDV programmé", RECEIVED, RECEIVED,
    )
    assert new["meta_source"]["form_id"] == FORM and new["financement_ft"] == "OUI"
    assert {row["contact_id"] for row in data["crm_inbound_requests"]} == {new["id"]}
    assert {row["contact_id"] for row in data["crm_meta_lead_submissions"]} == {new["id"]}
    assert [a["contact_id"] for a in data["crm_calendly_appointments"]] == [new["id"], old["id"]]
    unchanged = deepcopy(data)
    assert not crm._crm_repair_attached_meta_courses(data)
    assert data == unchanged
    item, linked = crm._crm_upsert_calendly_appointment(data, {
        "uri": "calendly/desp", "email": old["mail"],
        "scheduled_event": {"name": "RDV formation dirigeant", "start_time": "2099-11-18T13:30:00Z"},
    }, contact_id=old["id"], record_activity=False)
    assert item["contact_id"] == linked["id"] == new["id"]
    crm.save_data(data)
    assert post(client, desp("initial")).json["result"] == "already_processed"


def test_repair_checks_original_conflict_and_reuses_a_separate_manual_fiche():
    data = {"crm_contacts": [old_lead()], "crm_inbound_requests": [], "crm_meta_lead_submissions": []}
    historical_request(data, "manual", RECEIVED)
    data["crm_inbound_requests"][0]["differences"] = []
    unchanged = deepcopy(data)
    assert not crm._crm_repair_attached_meta_courses(data)
    assert data == unchanged
    data["crm_inbound_requests"][0]["differences"] = ["formation"]
    data["crm_contacts"].append({**old_lead(), "id": "manual-desp", "formation": "DESP",
                                 "statut": "En cours", "created_at": RECEIVED})
    assert crm._crm_repair_attached_meta_courses(data) == 1
    assert len(data["crm_contacts"]) == 2
    assert data["crm_meta_lead_submissions"][0]["contact_id"] == "manual-desp"
