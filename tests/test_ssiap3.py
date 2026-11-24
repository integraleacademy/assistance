import datetime
import json
import re

import pytest

from crm_app import legacy_app as app
from crm_aircall_ai import normalize_training
from crm_public_sessions import _public_calendar, website_ssiap_selection
from secretariat_catalogue import SSIAP_BOOKING_URL, SSIAP3_BOOKING_URL


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
        assert "SSIAP 1" not in text and "cassandre-integraleacademy/15min" not in text
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
    sms = app._build_secretariat_followup_sms("SSIAP").replace(
        app.SECRETARIAT_FORMATIONS["SSIAP"]["label"], app.SECRETARIAT_FORMATIONS["SSIAP3"]["label"]
    ).replace(app.SECRETARIAT_DOSSIER_URL, app.SECRETARIAT_FORMATIONS["SSIAP3"]["dossier_url"])
    assert entry["information_sms_content"] == sms


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


def test_shared_booking_link_preserves_course_in_webhook_and_cached_appointment():
    assert SSIAP_BOOKING_URL == "https://calendly.com/integraleacademy/ssiap1"
    assert SSIAP3_BOOKING_URL == SSIAP_BOOKING_URL + "?utm_campaign=ssiap3"
    shared = {"name": "RDV téléphonique formation SSIAP 1 incendie", "scheduling_url": SSIAP_BOOKING_URL}
    personal = {"name": "RDV Intégrale Academy - Cassandre", "scheduling_url": "https://calendly.com/cassandre-integraleacademy/15min"}
    assert all(app._calendly_event_type_matches_formation(shared, label) for label in ("SSIAP 1", "SSIAP 3"))
    assert not app._calendly_event_type_matches_formation(personal, "SSIAP 3")
    assert not app._calendly_event_type_matches_formation({"name": "RDV téléphonique SSIAP 3"}, "SSIAP 1")
    assert app._crm_calendly_formation({"scheduled_event": shared}) == ("SSIAP 1", "")
    tracking = {"utm_campaign": "ssiap3"}
    for event in (shared, personal):
        assert app._crm_calendly_formation({"scheduled_event": event, "tracking": tracking}) == ("SSIAP 3", "")
    cached = app._crm_calendly_cached_payload({"name": shared["name"], "booking_course": "ssiap3"})
    assert app._crm_calendly_formation(cached) == ("SSIAP 3", "")


def test_crm_preview_uses_same_sections_as_level_one_and_shows_price_in_header(intake):
    models = {row["id"]: row["contenu"] for row in intake.get("/api/crm/templates").get_json()["automatic_email"]}
    level_one = models["automatic-ssiap1"]
    level_three = models["automatic-ssiap3"]
    first = level_three.split('class="cover-art"', 1)[0]
    assert re.search(r'Tarif de la formation.*?<strong[^>]*>3 900 € TTC</strong>', first, re.S)
    assert re.findall(r"<h2[^>]*>(.*?)</h2>", level_three, re.S) == re.findall(r"<h2[^>]*>(.*?)</h2>", level_one, re.S)
    for marker in ("Identité Numérique La Poste", "Lire les avis sur Google", "Certification Nationale QUALIOPI", "Télécharger mon devis détaillé"):
        assert marker in level_one and marker in level_three
    assert "216 heures" in level_three and "67 heures" not in level_three
    assert "So Gaming Harder" in level_three and "didier" not in level_three
    assert "/documents/programme-ssiap-3.pdf" in level_three
    assert f'href="{SSIAP3_BOOKING_URL}"' in level_three
    assert len(re.findall(r'<h2\b', level_three)) >= 8
    assert app.build_training_information_sms_text("SSIAP3") == app.build_training_information_sms_text("SSIAP").replace(
        app._formation_sms_context("SSIAP")["formation_name"], app._formation_sms_context("SSIAP3")["formation_name"]
    ).replace(SSIAP_BOOKING_URL, SSIAP3_BOOKING_URL)


def test_untagged_rescheduling_keeps_the_project_only_for_the_same_invitee(intake):
    data = app.load_data()
    base = {"id": "one", "formation": "SSIAP 1", "prenom": "Camille", "nom": "Exemple",
            "telephone": "0600000000", "mail": "test@example.invalid", "statut": "Nouveaux"}
    level3 = dict(base, id="three", formation="SSIAP 3", meta_course_root_id="one")
    data["crm_contacts"] = [base, level3]

    def invitation(key):
        uri = "https://api.calendly.com/scheduled_events/" + key
        return {"uri": uri + "/invitees/guest", "event": uri, "email": base["mail"],
                "name": "Camille Exemple", "questions_and_answers": [
                    {"question": "Votre numéro de téléphone", "answer": base["telephone"]}],
                "scheduled_event": {"name": "RDV téléphonique formation SSIAP 1 incendie",
                                    "start_time": "2099-10-15T10:00:00Z",
                                    "end_time": "2099-10-15T10:15:00Z"}}

    initial = dict(invitation("first"), tracking={"utm_campaign": "ssiap3"})
    app._crm_upsert_calendly_appointment(data, initial)
    moved = dict(invitation("second"), old_invitee=initial["uri"])
    appointment, contact = app._crm_upsert_calendly_appointment(data, moved)
    assert appointment["booking_course"] == "ssiap3" and contact["id"] == "three"
    replay, contact = app._crm_upsert_calendly_appointment(data, invitation("second"))
    assert replay["id"] == appointment["id"] and contact["id"] == "three"
    unrelated = dict(invitation("another"), old_invitee=initial["uri"], email="other@example.invalid",
                     name="Autre Personne", questions_and_answers=[])
    other, _ = app._crm_upsert_calendly_appointment(data, unrelated)
    assert not other.get("booking_course") and other.get("contact_id") != "three"
