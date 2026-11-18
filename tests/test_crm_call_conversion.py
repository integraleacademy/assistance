from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess

import app
import crm_call_conversion as scoring


NOW = datetime(2026, 11, 18, 8, tzinfo=timezone.utc)
TITLE = "RDV téléphonique formation agent de sécurité privée"


def appointment(cid, when, response="", outcome="", name=TITLE):
    row = {"id": f"{cid}-{when.isoformat()}", "contact_id": cid, "name": name,
           "start_time": when.isoformat(), "response_status": response}
    if response:
        row["response_status_updated_at"] = (when + timedelta(minutes=12)).isoformat()
    if outcome:
        row["enrollment_outcome"] = outcome
    return row


def sample(*, exact=False):
    contacts, calls = [], []
    for i in range(40):
        cid = str(i)
        at = NOW - timedelta(days=30 if i < 30 else 10)
        answered = i % 4 != 3
        positive = i % 4 == 0
        outcome = ("confirmed" if positive else "pending") if answered and exact else ""
        calls.append(appointment(cid, at, "answered" if answered else "no_answer", outcome))
        activity = [{"kind": "statut", "title": "Statut : Converti",
                     "date": (at + timedelta(days=2)).isoformat()}] if positive else []
        contacts.append({"id": cid, "statut": "Converti" if positive else "En cours",
                         "activities": activity})
    target = appointment("target", NOW + timedelta(days=1))
    return {"crm_contacts": contacts + [{"id": "target"}],
            "crm_calendly_appointments": calls + [target]}, target


def test_dated_proxy_and_time_separated_direct_results():
    data, target = sample()
    assert scoring.forecasts(data, NOW)[target["id"]] == {
        "mode": "seven_days", "percent": 25, "observed": 40, "positive": 10, "contacts": 40,
        "basis": "Même type de rendez-vous, historique d’appels et d’échanges comparable",
    }
    data, target = sample(exact=True)
    prediction = scoring.forecasts(data, NOW)[target["id"]]
    assert (prediction["mode"], prediction["positive"], prediction["observed"]) == ("during_call", 10, 40)
    for row in data["crm_calendly_appointments"]:
        if row["response_status"] == "answered" and row["start_time"].startswith("2026-11-08"):
            row["enrollment_outcome"] = "confirmed"
    assert scoring.forecasts(data, NOW)[target["id"]]["mode"] == "seven_days"


def test_missing_responses_sparse_bts_and_mature_cohorts():
    data, target = sample()
    calls = data["crm_calendly_appointments"]
    for i in range(15):
        calls[i]["response_status"] = ""
    assert scoring.forecasts(data, NOW)[target["id"]] == {"mode": "insufficient"}
    data, target = sample()
    calls = data["crm_calendly_appointments"]
    for row in calls[:-1]:
        row["start_time"] = (NOW - timedelta(days=2)).isoformat()
    assert scoring.forecasts(data, NOW)[target["id"]]["mode"] == "insufficient"
    data, target = sample()
    target["name"] = "RDV téléphonique BTS MOS"
    assert scoring.forecasts(data, NOW)[target["id"]]["mode"] == "insufficient"
    target["status"] = "canceled"
    assert target["id"] not in scoring.forecasts(data, NOW)


def test_excludes_target_legacy_invented_dates_and_ambiguous_appointment():
    data, target = sample()
    data["crm_contacts"][0]["activities"] = []
    data["crm_contacts"][0]["converted_at"] = (NOW - timedelta(days=28)).isoformat()
    data["crm_calendly_appointments"].append(appointment("4", NOW - timedelta(days=29)))
    # Target previously answered. Its own history cannot enter the comparison.
    data["crm_calendly_appointments"].append(appointment("target", NOW - timedelta(days=18), "answered"))
    prediction = scoring.forecasts(data, NOW)[target["id"]]
    assert prediction["observed"] == 38 and prediction["positive"] == 8
    assert prediction["contacts"] == 38
    data["crm_calendly_appointments"][-1]["enrollment_outcome"] = "confirmed"
    assert target["id"] not in scoring.forecasts(data, NOW)


def test_attribution_once_and_no_post_call_features():
    data, target = sample()
    calls = data["crm_calendly_appointments"]
    historical = calls[0]
    calls.append(appointment("0", NOW - timedelta(days=29), "answered"))
    calls.append(dict(historical))  # Duplicate delivery of the same Calendly meeting.
    for contact in data["crm_contacts"][:-1]:
        contact.setdefault("activities", []).append({"kind": "email", "title": "Devis",
                                                     "date": NOW.isoformat()})
        contact["dates_formation"] = "Saisie après le rendez-vous"
    result = scoring.forecasts(data, NOW)[target["id"]]
    assert result["positive"] == 9 and result["observed"] == 40  # First-call comparison.
    assert "échanges" in result["basis"]
    # The next appointment of a dossier after its recorded conversion is never predicted.
    calls.append(appointment("0", NOW + timedelta(days=1)))
    assert calls[-1]["id"] not in scoring.forecasts(data, NOW)


def test_undated_conversions_count_against_coverage():
    data, target = sample()
    for i in range(30):
        cid = f"legacy-{i}"
        data["crm_contacts"].append({"id": cid, "statut": "Converti",
                                     "converted_at": (NOW - timedelta(days=29)).isoformat()})
        data["crm_calendly_appointments"].append(appointment(cid, NOW - timedelta(days=30), "answered"))
    assert scoring.forecasts(data, NOW)[target["id"]] == {"mode": "insufficient"}


def test_frozen_commercial_context_does_not_use_undated_historical_fields():
    data, target = sample()
    for c, a in zip(data["crm_contacts"], data["crm_calendly_appointments"]):
        c.update(dates_formation="Janvier", statut_demande_financement_ft="acceptee", origine="Site")
        before = datetime.fromisoformat(a["start_time"]) - timedelta(days=2)
        scoring.remember_booking_facts(a, c, before.isoformat())
    assert "financement" in scoring.forecasts(data, NOW)[target["id"]]["basis"]
    for a in data["crm_calendly_appointments"][:-1]:
        a["conversion_context"]["as_of"] = NOW.isoformat()
    assert "financement" not in scoring.forecasts(data, NOW)[target["id"]]["basis"]


def test_as_of_bookings_and_edited_call_text():
    cutoff = NOW.timestamp()
    a = appointment("a", NOW)
    a["created_at"] = (NOW - timedelta(days=1)).isoformat()
    note = {"kind": "appel", "date": (NOW - timedelta(days=3)).isoformat(),
            "edited_at": (NOW + timedelta(days=1)).isoformat(), "detail": "Devis et financement CPF"}
    events, _ = scoring._events({"activities": [note]})
    # A later booking and a later note edit cannot inform the historical comparison.
    bookings = ([cutoff - 10, cutoff + 10], [cutoff, cutoff + 30])
    historical = scoring._keys(a, {}, cutoff, [], events, False, bookings)
    features = historical[0][0]
    assert features[-3:] == (False, False, False)
    bookings = ([cutoff - 20, cutoff - 10], [cutoff, cutoff + 30])
    assert scoring._keys(a, {}, cutoff, [], events, False, bookings)[0][0][-2] is True


def test_outcome_through_call_log_edit_retry_and_clear(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DATA_FILE", str(tmp_path / "crm.json"))
    monkeypatch.setattr(app, "_crm_now", lambda: NOW.isoformat())
    deliveries = []
    monkeypatch.setattr(app, "_crm_send_appointment_followup",
                        lambda *args: deliveries.append(True) or {"email": True, "sms": True})
    app.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    client = app.app.test_client()
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    data = app.load_data()
    meeting = appointment("lead", NOW - timedelta(hours=2))
    future = appointment("lead", NOW + timedelta(days=2))
    data["crm_contacts"] = [{"id": "lead", "formation": "APS", "activities": [], "statut": "En cours"}]
    data["crm_calendly_appointments"] = [meeting, future]
    app.save_data(data)
    path = "/api/crm/calendly/appointments/" + meeting["id"]
    log = "/api/crm/contacts/lead/appel"
    assert client.patch(path, json={"enrollment_outcome": "confirmed"}).status_code == 400
    assert client.post(log, json={"commentaire": "Accord", "appointment_id": future["id"],
                                  "enrollment_outcome": "confirmed"}).status_code == 400
    assert not deliveries
    logged = client.post(log, json={"commentaire": "Accord", "appointment_id": meeting["id"],
                                    "enrollment_outcome": "confirmed"}).get_json()
    assert logged["appointment"]["enrollment_outcome"] == "confirmed"
    assert logged["contact"]["statut"] == "En cours"
    assert len(deliveries) == 1
    for value in ("pending", "pending", "declined"):
        assert client.patch(path, json={"enrollment_outcome": value}).get_json()["enrollment_outcome"] == value
    assert len(deliveries) == 1
    assert client.patch(path, json={"enrollment_outcome": {"bad": True}}).status_code == 400
    client.patch(path, json={"response_status": ""})
    stored = app.load_data()["crm_calendly_appointments"][0]
    assert not stored["enrollment_outcome"] and not stored["response_status"]


def test_forecasts_in_calendar_only_and_ui_modes(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DATA_FILE", str(tmp_path / "crm.json"))
    data, target = sample()
    now = datetime.now(timezone.utc)
    target["start_time"] = (now + timedelta(days=2)).isoformat()
    before = deepcopy(data)
    standard = app._crm_calendly_appointments_payload(data)["appointments"]
    assert all("conversion_forecast" not in a for a in standard)
    calendar = app._crm_calendly_appointments_payload(data, with_forecasts=True)["appointments"]
    assert next(a for a in calendar if a["id"] == target["id"])["conversion_forecast"]["mode"]
    assert data == before
    js = (Path(__file__).parents[1] / "static/crm.js").read_text()
    assert "if(section==='calendrier'&&!id){await loadCalendar();" in js
    app.save_data(data)
    client = app.app.test_client()
    with client.session_transaction() as session:
        session["user_email"] = next(iter(app.USERS))
    day = (now + timedelta(days=2)).astimezone(app.secretariat_calls.PARIS).date().isoformat()
    day_rows = client.get("/api/crm/calendly/appointments?date=" + day).get_json()["appointments"]
    assert next(row for row in day_rows if row["id"] == target["id"])["conversion_forecast"]["mode"]
    helper = js[js.index("function calendarConversion("):js.index("let calendarAvailability;")]
    subprocess.run(["node", "-e", "const esc=String;" + helper + """
const assert=require('node:assert/strict');
const a={start_time:'2100-01-01',conversion_forecast:{mode:'seven_days',percent:25,observed:40,positive:10,contacts:35,basis:'APS'}};
assert.match(calendarConversion(a),/Conversion à 7 jours/);
assert.match(calendarConversion(a),/25 %/);
a.conversion_forecast.mode='during_call';
assert.match(calendarConversion(a),/Inscription pendant l’appel/);
a.response_status='no_answer';
assert.equal(calendarConversion(a),'');
"""], check=True)
