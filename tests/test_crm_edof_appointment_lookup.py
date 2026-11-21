"""Tests unitaires de l'API CRM->EDOF, sans donnée réelle ni Calendly distant."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hmac
import json
import re
import sqlite3
from types import SimpleNamespace
import unicodedata

from flask import Flask

from crm_edof_appointment_lookup import (
    SIGNATURE_HEADER, SIGNING_CONTEXT, appointment_for, register_edof_appointment_lookup,
)


def normalized(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in value if not unicodedata.combining(c)).lower())


def fixture(tmp_path):
    path = tmp_path / "edof.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE wedof_resources(
            resource_type TEXT NOT NULL,stable_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,PRIMARY KEY(resource_type,stable_id));
    """)
    folders = [
        ("111111", {"externalId":"111111", "attendee":{
            "firstName":"Édouard","lastName":"Martin","email":"edouard@example.test"}}),
        ("222222", {"externalId":"222222", "attendee":{
            "firstName":"Lina","lastName":"Exemple","phoneNumber":"0611223344"}}),
        ("333333", {"externalId":"333333", "attendee":{
            "firstName":"Zoé","lastName":"Inconnue","email":"personne@example.test"}}),
    ]
    db.executemany(
        "INSERT INTO wedof_resources VALUES('registrationFolder',?,?)",
        [(k,json.dumps(v)) for k,v in folders],
    )
    db.commit()
    db.close()
    present = datetime.now(timezone.utc)
    contacts = [
        {"id":"first", "prenom":"Edouard", "nom":"MARTIN",
         "mail":"edouard@example.test", "telephone":""},
        {"id":"second", "prenom":"Lina", "nom":"Exemple",
         "mail":"", "telephone":"0611223344"},
        {"id":"homonym", "prenom":"Edouard", "nom":"Martin",
         "mail":"un-autre@example.test", "telephone":"0600000000"},
    ]
    appointments = [
        {"contact_id":"first", "status":"active","name":"Appel découverte téléphonique",
         "invitee_name":"Edouard Martin","start_time":(present+timedelta(days=1)).isoformat(),
         "host_name":"Cassandre","identity_conflict":False},
        {"contact_id":"second", "status":"active", "name":"Rendez-vous en visioconférence",
         "invitee_name":"Lina Exemple","start_time":(present+timedelta(days=1)).isoformat()},
    ]
    data={"crm_contacts":contacts, "crm_calendly_appointments":appointments}
    app=Flask(__name__)
    @contextmanager
    def connect():
        db=sqlite3.connect(path)
        db.row_factory=sqlite3.Row
        try:
            yield db
        finally:
            db.close()
    def next_phone(data, entry, contact=None, now=None, include_current=False):
        selected=[]
        for a in data.get("crm_calendly_appointments", []):
            if a.get("contact_id") != contact.get("id") or a.get("identity_conflict"):
                continue
            if a.get("status") in ("canceled","cancelled") or "téléphonique" not in a.get("name",""):
                continue
            start=datetime.fromisoformat(a["start_time"])
            if start > (now or present):
                selected.append((start,a))
        return min(selected,key=lambda item:item[0]) if selected else None

    host=SimpleNamespace(
        app=app,
        _edof_token=lambda:"unit-test-only-secret",
        _load_data_snapshot=lambda:data,
        _wedof_connect=connect,
        _wedof_contact_name_matches=lambda f,c:
            normalized((f.get("attendee") or {}).get("firstName")) == normalized(c.get("prenom")) and
            normalized((f.get("attendee") or {}).get("lastName")) == normalized(c.get("nom")),
        _wedof_attendee_values=lambda f:(
            {str(f.get("attendee",{}).get("email") or "").lower()} - {""},
            {re.sub(r"\D","",str(f.get("attendee",{}).get("phoneNumber") or ""))} - {""},
            ""),
        _crm_normalize_email=lambda s:str(s or "").strip().lower(),
        _crm_normalize_phone=lambda s:re.sub(r"\D","",str(s or "")),
        _crm_next_phone_appointment=next_phone,
        _crm_calendly_name_match=lambda c,guest: (
            None if not guest else normalized(guest) in (
                normalized(c.get("prenom","")+c.get("nom","")),
                normalized(c.get("nom","")+c.get("prenom","")),
            )
        ),
    )
    register_edof_appointment_lookup(host)
    return app.test_client(), data, appointments, host


def signed(client, ids, token="unit-test-only-secret"):
    data=json.dumps({"folder_ids":ids},separators=(",",":")).encode()
    signature=hmac.digest(token.encode(),SIGNING_CONTEXT+data,"sha256").hex()
    return client.post("/api/integrations/edof-appointments",data=data,content_type="application/json",
                       headers={SIGNATURE_HEADER:signature})


def test_signed_batch_returns_only_phone_meetings_for_verified_identities(tmp_path):
    client, _, _, _=fixture(tmp_path)
    response=signed(client,["111111","222222","333333","444444"])
    assert response.status_code == 200
    payload=response.get_json()["appointments"]
    assert payload["111111"]["status"] == "scheduled"
    assert payload["111111"]["host"] == "Cassandre"
    assert payload["111111"]["crm_url"] == (
        "https://assistance-alw9.onrender.com/crm/contacts?fiche=first&retour=pistes"
    )
    assert payload["222222"]["status"] == "none"
    assert payload["333333"]["status"] == "unmatched"
    assert payload["444444"]["status"] == "unmatched"
    assert "edouard@example.test" not in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "private, no-store"


def test_no_authentication_or_malformed_batch_is_rejected(tmp_path):
    client,_,_,_=fixture(tmp_path)
    assert client.post("/api/integrations/edof-appointments",json={"folder_ids":["111111"]}).status_code == 401
    assert signed(client,["111111"],"wrong-secret").status_code == 401
    for ids in ([],["abc"],["111111","111111"],["111111"]*31):
        assert signed(client,ids).status_code == 400
    assert client.get("/api/integrations/edof-appointments").status_code == 405


def test_homonym_and_changed_contact_address_cannot_inherit_rdv(tmp_path):
    client,data,_,_=fixture(tmp_path)
    data["crm_contacts"][0]["mail"]="mauvais@example.test"
    payload=signed(client,["111111"]).get_json()["appointments"]
    assert payload["111111"]["status"] == "unmatched"
    # A name-only CRM match must not establish an appointment.
    assert "crm_url" not in payload["111111"]


def test_cancellation_and_past_appointments_are_not_scheduled(tmp_path):
    client,_,appointments,_=fixture(tmp_path)
    appointments[0]["status"]="canceled"
    assert signed(client,["111111"]).get_json()["appointments"]["111111"]["status"]=="none"
    appointments[0]["status"]="active"
    appointments[0]["start_time"]="2020-01-01T10:00:00+00:00"
    assert signed(client,["111111"]).get_json()["appointments"]["111111"]["status"]=="none"


def test_phone_type_from_crm_logic_is_authoritative(tmp_path):
    client,data,appointments,host=fixture(tmp_path)
    appointments[1]["name"]="Appel téléphonique"
    payload=signed(client,["222222"]).get_json()["appointments"]
    assert payload["222222"]["status"]=="scheduled"
    assert payload["222222"]["crm_url"].endswith("fiche=second&retour=pistes")
    appointments[1]["identity_conflict"]=True
    assert signed(client,["222222"]).get_json()["appointments"]["222222"]["status"]=="none"


def test_shared_email_with_wrong_calendly_invitee_is_not_attributed(tmp_path):
    client,_,appointments,_=fixture(tmp_path)
    # Lien contact connu mais nom Calendly d'une autre personne.
    appointments[0]["invitee_name"]="Une Autre Personne"
    result=signed(client,["111111"]).get_json()["appointments"]
    assert result["111111"]["status"]=="none"
