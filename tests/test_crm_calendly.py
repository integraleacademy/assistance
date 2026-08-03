import hashlib
import hmac
import json
import time

import app as application


def authenticated_client(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    monkeypatch.setenv("CALENDLY_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("CALENDLY_WEBHOOK_SIGNING_KEY", "test-signing-key")
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    client = application.app.test_client()
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return client


def calendly_payload(email="lina@example.com", status="active"):
    return {
        "uri": "https://api.calendly.com/scheduled_events/EVENT1/invitees/INVITEE1",
        "event": "https://api.calendly.com/scheduled_events/EVENT1",
        "name": "Lina Martin",
        "first_name": "Lina",
        "last_name": "Martin",
        "email": email,
        "status": status,
        "timezone": "Europe/Paris",
        "text_reminder_number": "+33612345678",
        "cancel_url": "https://calendly.com/cancellations/INVITEE1",
        "reschedule_url": "https://calendly.com/reschedulings/INVITEE1",
        "created_at": "2026-08-03T08:00:00Z",
        "updated_at": "2026-08-03T08:00:00Z",
        "scheduled_event": {
            "uri": "https://api.calendly.com/scheduled_events/EVENT1",
            "name": "Appel découverte",
            "status": status,
            "start_time": "2026-08-12T08:00:00Z",
            "end_time": "2026-08-12T08:30:00Z",
            "event_type": "https://api.calendly.com/event_types/TYPE1",
            "location": {"type": "outbound_call", "location": "+33612345678"},
            "event_memberships": [{
                "user_name": "Clément",
                "user_email": "clement@integraleacademy.com",
            }],
        },
    }


def signed_webhook(client, monkeypatch, event_name, payload):
    body = json.dumps(
        {"event": event_name, "payload": payload},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = hmac.new(
        b"test-signing-key",
        timestamp.encode("utf-8") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return client.post(
        "/api/crm/calendly/webhook",
        data=body,
        content_type="application/json",
        headers={"Calendly-Webhook-Signature": f"t={timestamp},v1={signature}"},
    )


def test_webhook_links_all_appointments_to_contact_and_updates_cancellation(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin", "formation": "APS"},
    ).get_json()
    client.patch(f"/api/crm/contacts/{contact['id']}", json={"mail": "LINA@example.com"})

    created = signed_webhook(client, monkeypatch, "invitee.created", calendly_payload())

    assert created.status_code == 200
    assert created.get_json()["contact_id"] == contact["id"]
    appointments = client.get(
        f"/api/crm/contacts/{contact['id']}/calendly/appointments"
    ).get_json()["appointments"]
    assert len(appointments) == 1
    assert appointments[0]["name"] == "Appel découverte"
    assert appointments[0]["status"] == "active"

    canceled_payload = calendly_payload(status="canceled")
    canceled_payload["cancellation"] = {"reason": "Indisponible"}
    canceled = signed_webhook(client, monkeypatch, "invitee.canceled", canceled_payload)

    assert canceled.status_code == 200
    appointments = client.get(
        f"/api/crm/contacts/{contact['id']}/calendly/appointments"
    ).get_json()["appointments"]
    assert len(appointments) == 1
    assert appointments[0]["status"] == "canceled"
    updated_contact = client.get(f"/api/crm/contacts/{contact['id']}").get_json()
    assert updated_contact["activities"][0]["title"] == "Rendez-vous Calendly annulé"


def test_webhook_creates_a_lead_for_a_new_invitee(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)

    response = signed_webhook(
        client,
        monkeypatch,
        "invitee.created",
        calendly_payload(email="nouveau@example.com"),
    )

    assert response.status_code == 200
    contacts = client.get("/api/crm/contacts").get_json()
    assert len(contacts) == 1
    assert contacts[0]["mail"] == "nouveau@example.com"
    assert contacts[0]["origine"] == "Calendly"
    assert contacts[0]["statut"] == "RDV programmé"


def test_webhook_rejects_an_invalid_signature(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/crm/calendly/webhook",
        json={"event": "invitee.created", "payload": calendly_payload()},
        headers={"Calendly-Webhook-Signature": "t=1,v1=invalid"},
    )

    assert response.status_code == 401
    assert client.get("/api/crm/contacts").get_json() == []


def test_event_types_endpoint_returns_every_active_calendly_type(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    calls = []

    def fake_calendly(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/users/me":
            return {"resource": {
                "uri": "https://api.calendly.com/users/USER1",
                "current_organization": "https://api.calendly.com/organizations/ORG1",
            }}
        if path == "/event_types":
            return {"collection": [
                {
                    "uri": "https://api.calendly.com/event_types/TYPE1",
                    "name": "Appel 15 minutes",
                    "active": True,
                    "duration": 15,
                    "locations": [{"kind": "outbound_call"}],
                    "custom_questions": [],
                },
                {
                    "uri": "https://api.calendly.com/event_types/TYPE2",
                    "name": "Entretien d'inscription",
                    "active": True,
                    "duration": 45,
                    "locations": [{"kind": "zoom_conference"}],
                    "custom_questions": [],
                },
            ], "pagination": {"next_page_token": None}}
        raise AssertionError(f"Unexpected Calendly call: {method} {path}")

    monkeypatch.setattr(application, "_calendly_request", fake_calendly)

    response = client.get("/api/crm/calendly/event-types")

    assert response.status_code == 200
    assert [item["name"] for item in response.get_json()] == [
        "Appel 15 minutes",
        "Entretien d'inscription",
    ]
    event_call = next(call for call in calls if call[1] == "/event_types")
    assert event_call[2]["params"]["organization"].endswith("/ORG1")
    assert event_call[2]["params"]["active"] == "true"


def test_booking_from_contact_uses_location_questions_and_saves_appointment(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin", "formation": "APS"},
    ).get_json()
    client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"mail": "lina@example.com", "telephone": "06 12 34 56 78"},
    )
    captured = {}

    def fake_calendly(method, path, **kwargs):
        if method == "GET" and path == "/event_types/TYPE1":
            return {"resource": {
                "uri": "https://api.calendly.com/event_types/TYPE1",
                "name": "Appel découverte",
                "active": True,
                "duration": 30,
                "is_paid": False,
                "booking_method": "instant",
                "pooling_type": None,
                "locations": [{"kind": "outbound_call"}],
                "custom_questions": [{
                    "name": "Votre projet",
                    "position": 0,
                    "enabled": True,
                    "required": True,
                    "type": "text",
                }],
            }}
        if method == "POST" and path == "/invitees":
            captured.update(kwargs["json_body"])
            return {"resource": {
                "uri": "https://api.calendly.com/scheduled_events/EVENT1/invitees/INVITEE1",
                "event": "https://api.calendly.com/scheduled_events/EVENT1",
                "name": "Lina Martin",
                "email": "lina@example.com",
                "status": "active",
                "timezone": "Europe/Paris",
                "cancel_url": "https://calendly.com/cancellations/INVITEE1",
                "reschedule_url": "https://calendly.com/reschedulings/INVITEE1",
            }}
        if method == "GET" and path == "/scheduled_events/EVENT1":
            event = calendly_payload()["scheduled_event"]
            return {"resource": event}
        raise AssertionError(f"Unexpected Calendly call: {method} {path}")

    monkeypatch.setattr(application, "_calendly_request", fake_calendly)

    response = client.post(
        f"/api/crm/contacts/{contact['id']}/calendly/appointments",
        json={
            "event_type": "https://api.calendly.com/event_types/TYPE1",
            "start_time": "2026-08-12T08:00:00Z",
            "timezone": "Europe/Paris",
            "location": {"kind": "outbound_call", "location": "06 12 34 56 78"},
            "answers": {"0": "Formation APS"},
        },
    )

    assert response.status_code == 201
    assert captured["location"] == {"kind": "outbound_call", "location": "+33612345678"}
    assert captured["invitee"]["text_reminder_number"] == "+33612345678"
    assert captured["questions_and_answers"] == [{
        "question": "Votre projet",
        "answer": "Formation APS",
        "position": 0,
    }]
    assert response.get_json()["appointment"]["contact_id"] == contact["id"]


def test_full_sync_imports_every_event_and_keeps_unmatched_appointments(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin", "formation": "APS"},
    ).get_json()
    client.patch(f"/api/crm/contacts/{contact['id']}", json={"mail": "lina@example.com"})
    data = application.load_data()
    data["crm_calendly"] = {
        "webhook_uri": "https://api.calendly.com/webhook_subscriptions/HOOK1",
        "scope": "organization",
        "organization": "https://api.calendly.com/organizations/ORG1",
        "user": "https://api.calendly.com/users/USER1",
        "sync_complete": False,
    }
    application.save_data(data)
    first_event = calendly_payload()["scheduled_event"]
    second_event = {
        **first_event,
        "uri": "https://api.calendly.com/scheduled_events/EVENT2",
        "event_type": "https://api.calendly.com/event_types/TYPE2",
        "name": "Rendez-vous VAE",
        "status": "canceled",
    }

    def fake_calendly(method, path, **kwargs):
        if path == "/scheduled_events":
            return {
                "collection": [first_event, second_event],
                "pagination": {"next_page_token": None},
            }
        if path.endswith("/EVENT1/invitees"):
            return {"collection": [calendly_payload()], "pagination": {"next_page_token": None}}
        if path.endswith("/EVENT2/invitees"):
            invitee = calendly_payload(email="sans-piste@example.com", status="canceled")
            invitee["uri"] = "https://api.calendly.com/scheduled_events/EVENT2/invitees/INVITEE2"
            invitee["event"] = second_event["uri"]
            return {"collection": [invitee], "pagination": {"next_page_token": None}}
        raise AssertionError(f"Unexpected Calendly call: {method} {path}")

    monkeypatch.setattr(application, "_calendly_request", fake_calendly)

    response = client.post("/api/crm/calendly/sync", json={"restart": True})

    assert response.status_code == 200
    assert response.get_json()["complete"] is True
    stored = application.load_data()["crm_calendly_appointments"]
    assert len(stored) == 2
    assert {item["event_type_uri"] for item in stored} == {
        "https://api.calendly.com/event_types/TYPE1",
        "https://api.calendly.com/event_types/TYPE2",
    }
    matched = next(item for item in stored if item["invitee_email"] == "lina@example.com")
    unmatched = next(item for item in stored if item["invitee_email"] == "sans-piste@example.com")
    assert matched["contact_id"] == contact["id"]
    assert unmatched["contact_id"] is None


def test_contact_appointments_are_fetched_directly_by_email(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post(
        "/api/crm/contacts",
        json={"prenom": "Tony", "nom": "Arribas", "formation": "Chauffeur VTC"},
    ).get_json()
    client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"mail": "TonyArribasFT@icloud.com", "telephone": "06 41 57 92 65"},
    )
    data = application.load_data()
    data["crm_calendly"] = {
        "webhook_uri": "https://api.calendly.com/webhook_subscriptions/HOOK1",
        "scope": "organization",
        "organization": "https://api.calendly.com/organizations/ORG1",
        "user": "https://api.calendly.com/users/USER1",
    }
    application.save_data(data)
    calls = []

    def fake_calendly(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/scheduled_events":
            params = kwargs["params"]
            assert params["invitee_email"] == "tonyarribasft@icloud.com"
            assert params["organization"].endswith("/ORG1")
            assert params["count"] == 100
            assert "event_type" not in params
            return {
                "collection": [calendly_payload()["scheduled_event"]],
                "pagination": {"next_page_token": None},
            }
        if path == "/scheduled_events/EVENT1/invitees":
            assert kwargs["params"]["email"] == "tonyarribasft@icloud.com"
            invitee = calendly_payload(email="tonyarribasft@icloud.com")
            return {
                "collection": [invitee],
                "pagination": {"next_page_token": None},
            }
        raise AssertionError(f"Unexpected Calendly call: {method} {path}")

    monkeypatch.setattr(application, "_calendly_request", fake_calendly)

    response = client.get(
        f"/api/crm/contacts/{contact['id']}/calendly/appointments"
    )

    assert response.status_code == 200
    result = response.get_json()
    assert result["lookup"] == {
        "method": "email",
        "processed_events": 1,
        "matched_appointments": 1,
    }
    assert len(result["appointments"]) == 1
    assert result["appointments"][0]["contact_id"] == contact["id"]
    assert [path for _, path, _ in calls] == [
        "/scheduled_events",
        "/scheduled_events/EVENT1/invitees",
    ]


def test_webhook_matches_a_contact_by_normalized_phone_question(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post(
        "/api/crm/contacts",
        json={"prenom": "Tony", "nom": "Arribas", "formation": "Chauffeur VTC"},
    ).get_json()
    client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"telephone": "06 41 57 92 65"},
    )
    payload = calendly_payload(email="")
    payload["text_reminder_number"] = ""
    payload["questions_and_answers"] = [{
        "question": "Numéro de téléphone",
        "answer": "+33 6 41 57 92 65",
    }]

    response = signed_webhook(client, monkeypatch, "invitee.created", payload)

    assert response.status_code == 200
    assert response.get_json()["contact_id"] == contact["id"]
    assert len(client.get("/api/crm/contacts").get_json()) == 1
    appointments = client.get(
        f"/api/crm/contacts/{contact['id']}/calendly/appointments"
    ).get_json()["appointments"]
    assert len(appointments) == 1
    assert appointments[0]["invitee_phone"] == "+33 6 41 57 92 65"


def test_crm_javascript_loads_and_binds_calendly_without_losing_conversion():
    javascript = application.app.root_path + "/static/crm.js"
    stylesheet = application.app.root_path + "/static/crm.css"
    with open(javascript, encoding="utf-8") as source:
        crm_js = source.read()
    with open(stylesheet, encoding="utf-8") as source:
        crm_css = source.read()

    required_markers = [
        "function renderCalendlyAppointments",
        "async function loadCalendlyAppointments",
        "async function calendlyModal",
        "calendarBtn.onclick=()=>calendlyModal(c)",
        "syncCalendlyBtn.onclick=()=>loadCalendlyAppointments(c,true)",
        "loadCalendlyAppointments(c)",
        "Prochain rendez-vous",
        "Rendez-vous passés",
        "Rendez-vous annulés",
        "tone==='upcoming'?calendlyActions(a):''",
        "if(b.dataset.step==='Converti')return conversionModal(c)",
    ]
    for marker in required_markers:
        assert marker in crm_js
    assert "rendez-vous traités" not in crm_js
    assert "appointment-row" not in crm_js
    for marker in [
        ".calendly-card{",
        ".next-appointment{",
        ".appointment-grid{",
        ".appointment-status.canceled{",
        ".calendly-modal{",
    ]:
        assert marker in crm_css
