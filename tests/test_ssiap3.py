import datetime
import json

import pytest

from crm_app import legacy_app as app
from crm_aircall_ai import normalize_training
from crm_public_sessions import _public_calendar, website_ssiap_selection
from secretariat_catalogue import SSIAP3_BOOKING_URL


@pytest.fixture
def intake(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DATA_FILE", str(tmp_path / "data.json"))
    monkeypatch.setitem(app.app.config, "CRM_USE_PUBLIC_CALENDAR", False)
    monkeypatch.setattr(app, "creer_piste_salesforce", lambda *_: None)
    monkeypatch.setattr(app, "send_email_html", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "send_sms", lambda *_: True)
    client = app.app.test_client()
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return client


def test_form_contact_quote_and_followup(intake):
    rows = app.get_formation_sessions({})["cote_azur"]["SSIAP3"]
    assert [r["date_examen"] for r in rows] == ["2027-06-03", "2027-12-02"]
    assert all("SSIAP3" not in app.get_formation_sessions({})[centre] for centre in ("paris", "auvergne"))
    response = intake.post("/demande-informations-formations", data={
        "prenom": "Camille", "nom": "Exemple", "mail": "test@example.invalid",
        "telephone": "0600000000", "formation": "SSIAP3", "centre": "cote_azur",
        "dates": rows[1]["label"], "date_examen": rows[1]["date_examen"],
        "souhaite_devis": "OUI", "ssiap_secourisme_valide": "OUI",
    })
    assert response.status_code == 302 and "formation=SSIAP3" in response.location
    data = app.load_data()
    contact, = data["crm_contacts"]
    assert contact["formation"] == "SSIAP 3" and contact["dates_formation"] == rows[1]["label"]
    request_row = next(row for row in data["demandes"] if row.get("id") == contact["source_demande_id"])
    quote = next(row for row in data["demandes"] if row.get("id") == contact["source_devis_id"])
    details = json.loads(quote["details"])
    assert details["formation"] == "SSIAP3" and details["date_examen"] == rows[1]["date_examen"]
    page = intake.get("/plan/" + quote["token_plan"]).get_data(as_text=True)
    assert "Chef de service de sécurité incendie" in page and "3900 €" in page
    for text in (request_row["mail_contenu"], request_row["mail_html"]):
        assert "SSIAP 3" in text and "3 900 € TTC" in text and "216 heures" in text
        assert "SSIAP 1" not in text and "/ssiap1" not in text
        assert "/documents/programme-ssiap-3.pdf" in text and SSIAP3_BOOKING_URL in text
        assert "18 octobre" in text and "2 décembre" in text
    assert "SSIAP 3" in request_row["sms_body"] and SSIAP3_BOOKING_URL in request_row["sms_body"]
    model = next(m for m in intake.get("/api/crm/templates").get_json()["automatic_email"] if m["id"] == "automatic-ssiap3")
    assert model["formation"] == "SSIAP3" and "SSIAP 3" in model["sujet"]
    assert app._crm_formation_code(contact) == "SSIAP3"
    assert app._crm_default_sale_price(contact) == 3900
    assert "SSIAP 3" in app._crm_no_answer_message(contact)


def test_secretariat_separates_levels_for_same_person(intake):
    old = intake.post("/api/crm/contacts", json={
        "prenom": "Camille", "nom": "Exemple", "mail": "desk@example.invalid",
        "telephone": "0600000000", "formation": "SSIAP 1",
    }).get_json()
    page = intake.get("/secretariat").get_data(as_text=True)
    assert 'data-formation-code="SSIAP3"' in page and 'data-formation-code="SSIAP"' in page
    chosen = app.get_formation_sessions({})["cote_azur"]["SSIAP3"][0]["label"]
    submission = {"type": "formation", "nom": "Camille Exemple", "prenom": "Camille",
                  "nom_famille": "Exemple", "email": "desk@example.invalid", "telephone": "0600000000",
                  "formation": "SSIAP3", "formation_session_label": chosen, "formation_centre": "cote_azur",
                  "devis": "OUI", "call_key": "1" * 32}
    assert intake.post("/api/secretariat/demandes", json=submission).status_code == 201
    assert intake.post("/api/secretariat/demandes", json=submission).status_code in (200, 201)
    data = app.load_data()
    assert len(data["crm_contacts"]) == 2
    by_level = {c["formation"]: c for c in data["crm_contacts"]}
    assert by_level["SSIAP 1"]["id"] == old["id"]
    entry, = data["secretariat_demandes"]
    assert entry["crm_contact_id"] == by_level["SSIAP 3"]["id"]
    assert by_level["SSIAP 3"]["dates_formation"] == chosen
    assert entry["information_email_template_id"] == "automatic-ssiap3"
    assert "SSIAP 3" in entry["information_sms_content"] and "/ssiap1" not in entry["information_sms_content"]


def test_unavailable_centre_is_rejected_before_contact_or_message(intake, monkeypatch):
    def unexpected_delivery(*args, **kwargs):
        pytest.fail("Une demande SSIAP 3 pour Paris a déclenché un envoi")
    for name in ("creer_piste_salesforce", "send_email_html", "send_sms"):
        monkeypatch.setattr(app, name, unexpected_delivery)
    assert intake.post("/demande-informations-formations", data={"centre": "paris", "formation": "SSIAP3"}).status_code == 400
    assert intake.post("/api/secretariat/demandes", json={
        "type": "formation", "formation": "SSIAP3", "formation_centre": "paris",
    }).status_code == 400
    data = app.load_data()
    assert not data.get("crm_contacts") and not data.get("demandes") and not data.get("secretariat_demandes")


def test_session_handoff_respects_course_and_availability(monkeypatch, intake):
    def row(course, sid, status="OPEN"):
        return {"id": sid, "training": {"slug": course}, "title": course, "location": "Puget-sur-Argens",
                "status": status, "startDate": "2030-10-18", "endDate": "2030-12-01", "examDate": "2030-12-02"}
    rows = [row("ssiap-3", "open"), row("ssiap-3", "full", "FULL"), row("ssiap-1", "other")]
    monkeypatch.setattr(app.crm_public_sessions, "_published_sessions", lambda: _public_calendar(rows))
    for sid in ("other", "full"):
        assert website_ssiap_selection(sid, None, datetime.date(2030, 1, 1), "SSIAP3") is None
    assert website_ssiap_selection("open", None, datetime.date(2031, 1, 1), "SSIAP3") is None
    page = intake.get("/demande-informations-formations?formation=ssiap-3&session=open").get_data(as_text=True)
    assert "Votre session SSIAP 3" in page and "3 900 € TTC" in page and "18 octobre 2030" in page


@pytest.mark.parametrize("label", ["SSIAP3", "SSIAP 3", "SSIAP-3", "chef de service de sécurité incendie"])
def test_inbound_channels_keep_level_three(label):
    assert normalize_training(label) == app._wedof_crm_training(label) == app._meta_formation(label) == ("SSIAP 3", "")
    assert app._crm_calendly_formation({"scheduled_event": {"name": label}}) == ("SSIAP 3", "")


def test_booking_matching_uses_cassandre_agenda_and_preserves_ssiap1():
    level1 = {"name": "RDV téléphonique formation SSIAP 1 incendie"}
    level3 = {"name": "RDV téléphonique SSIAP 3"}
    general = {"name": "RDV Intégrale Academy - Cassandre", "scheduling_url": SSIAP3_BOOKING_URL.split("?")[0]}
    assert app._calendly_event_type_matches_formation(level1, "SSIAP 1")
    assert app._calendly_event_type_matches_formation(general, "SSIAP 3")
    assert app._calendly_event_type_matches_formation(level3, "SSIAP 3")
    assert not app._calendly_event_type_matches_formation(level1, "SSIAP 3")
    assert not app._calendly_event_type_matches_formation(level3, "SSIAP 1")
    assert app._crm_calendly_formation({"scheduled_event": general}) == ("", "")
    assert app._crm_calendly_formation({"scheduled_event": general, "tracking": {"utm_campaign": "ssiap3"}}) == ("SSIAP 3", "")
