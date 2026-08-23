import datetime
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
        "created_at": "2099-08-03T08:00:00Z",
        "updated_at": "2099-08-03T08:00:00Z",
        "scheduled_event": {
            "uri": "https://api.calendly.com/scheduled_events/EVENT1",
            "name": "Appel découverte",
            "status": status,
            "start_time": "2099-08-12T08:00:00Z",
            "end_time": "2099-08-12T08:30:00Z",
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
    assert application.load_data()["crm_calendly"]["last_sync_at"]
    appointments = client.get(
        f"/api/crm/contacts/{contact['id']}/calendly/appointments"
    ).get_json()["appointments"]
    assert len(appointments) == 1
    assert appointments[0]["name"] == "Appel découverte"
    assert appointments[0]["status"] == "active"
    scheduled_contact = client.get(f"/api/crm/contacts/{contact['id']}").get_json()
    assert scheduled_contact["statut"] == "RDV programmé"
    assert any(activity["title"] == "Statut : RDV programmé" for activity in scheduled_contact["activities"])

    with_follow_up = client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"statut": "A relancer", "relance_date": "2099-09-03"},
    ).get_json()
    assert with_follow_up["statut"] == "A relancer"
    assert with_follow_up["relance_date"] == "2099-09-03"

    replayed = signed_webhook(
        client,
        monkeypatch,
        "invitee.created",
        calendly_payload(),
    )
    assert replayed.status_code == 200
    after_replay = client.get(
        f"/api/crm/contacts/{contact['id']}"
    ).get_json()
    assert after_replay["statut"] == "A relancer"
    assert after_replay["relance_date"] == "2099-09-03"
    assert [
        item["status"] for item in after_replay["relances"]
    ] == ["scheduled"]

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
    assert updated_contact["statut"] == "A relancer"
    assert updated_contact["relance_date"] == "2099-09-03"
    assert any(
        activity["title"] == "Statut : A relancer"
        and activity["detail"] == "Ancien statut : RDV programmé"
        for activity in updated_contact["activities"]
    )


def test_upcoming_appointment_replaces_and_cancels_a_scheduled_follow_up(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin", "formation": "APS"},
    ).get_json()
    client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"mail": "lina@example.com", "statut": "A relancer", "relance_date": "2099-08-10"},
    )
    payload = calendly_payload()
    payload["scheduled_event"]["start_time"] = "2099-08-12T08:00:00Z"
    payload["scheduled_event"]["end_time"] = "2099-08-12T08:30:00Z"

    response = signed_webhook(client, monkeypatch, "invitee.created", payload)

    assert response.status_code == 200
    refreshed = client.get(f"/api/crm/contacts/{contact['id']}").get_json()
    assert refreshed["statut"] == "RDV programmé"
    assert refreshed["relance_date"] == ""
    assert len(refreshed["relances"]) == 1
    assert refreshed["relances"][0]["scheduled_date"] == "2099-08-10"
    assert refreshed["relances"][0]["status"] == "cancelled"
    assert any(
        activity["title"] == "Statut : RDV programmé"
        and activity["detail"] == "Ancien statut : A relancer"
        for activity in refreshed["activities"]
    )

    canceled_payload = calendly_payload(status="canceled")
    canceled_payload["scheduled_event"]["start_time"] = "2099-08-12T08:00:00Z"
    canceled_payload["scheduled_event"]["end_time"] = "2099-08-12T08:30:00Z"
    canceled_payload["cancellation"] = {"reason": "Indisponible"}
    canceled = signed_webhook(
        client,
        monkeypatch,
        "invitee.canceled",
        canceled_payload,
    )

    assert canceled.status_code == 200
    after_cancellation = client.get(
        f"/api/crm/contacts/{contact['id']}"
    ).get_json()
    assert after_cancellation["statut"] == "En cours"
    assert after_cancellation["relance_date"] == ""
    assert [item["status"] for item in after_cancellation["relances"]] == [
        "cancelled"
    ]


def test_upcoming_appointment_preserves_final_statuses_and_relances(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)

    for index, final_status in enumerate(("Converti", "Disqualifié"), start=1):
        email = f"final-{index}@example.com"
        contact = client.post(
            "/api/crm/contacts",
            json={
                "prenom": "Lina",
                "nom": f"Finale {index}",
                "formation": "APS",
            },
        ).get_json()
        client.patch(
            f"/api/crm/contacts/{contact['id']}",
            json={
                "mail": email,
                "statut": final_status,
                "relance_date": "2099-08-10",
            },
        )
        payload = calendly_payload(email=email)
        payload["uri"] = (
            "https://api.calendly.com/scheduled_events/"
            f"EVENT{index}/invitees/INVITEE{index}"
        )
        payload["event"] = (
            f"https://api.calendly.com/scheduled_events/EVENT{index}"
        )
        payload["scheduled_event"]["uri"] = payload["event"]

        response = signed_webhook(
            client,
            monkeypatch,
            "invitee.created",
            payload,
        )

        assert response.status_code == 200
        refreshed = client.get(
            f"/api/crm/contacts/{contact['id']}"
        ).get_json()
        assert refreshed["statut"] == final_status
        assert refreshed["relance_date"] == "2099-08-10"
        assert refreshed["relances"][0]["status"] == "scheduled"

def test_appointment_response_status_can_be_updated_from_calendar_or_contact(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    deliveries = {"sms": [], "email": []}
    monkeypatch.setattr(application, "send_sms", lambda phone, body: deliveries["sms"].append((phone, body)) or True)
    monkeypatch.setattr(application, "send_email_html", lambda mail, subject, plain, html: deliveries["email"].append((mail, subject, plain, html)) or True)
    contact = client.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin", "formation": "APS"},
    ).get_json()
    client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"mail": "lina@example.com", "telephone": "+33612345678"},
    )
    created = signed_webhook(client, monkeypatch, "invitee.created", calendly_payload())
    assert created.get_json()["contact_id"] == contact["id"]
    appointment_id = client.get(
        "/api/crm/calendly/appointments"
    ).get_json()["appointments"][0]["id"]

    response = client.patch(
        f"/api/crm/calendly/appointments/{appointment_id}",
        json={"response_status": "no_answer"},
    )

    assert response.status_code == 200
    assert response.get_json()["response_status"] == "no_answer"
    assert response.get_json()["contact"]["statut"] == "A relancer"
    assert response.get_json()["delivery"] == {"sms": True, "email": True}
    updated_contact = client.get(f"/api/crm/contacts/{created.get_json()['contact_id']}").get_json()
    assert updated_contact["statut"] == "A relancer"
    expected_date = (application.datetime.datetime.now(application.pytz.timezone("Europe/Paris")).date()
                     + application.datetime.timedelta(days=2)).isoformat()
    assert updated_contact["relance_date"] == expected_date
    assert len(deliveries["sms"]) == len(deliveries["email"]) == 1
    assert "APS – Agent de Prévention et de Sécurité" in deliveries["sms"][0][1]
    assert "https://calendly.com/integraleacademy/aps" in deliveries["email"][0][2]
    assert "Cassandre MENARD" in deliveries["email"][0][2]
    calendar_appointment = client.get(
        "/api/crm/calendly/appointments"
    ).get_json()["appointments"][0]
    assert calendar_appointment["response_status"] == "no_answer"
    contact_appointment = client.get(
        f"/api/crm/contacts/{created.get_json()['contact_id']}/calendly/appointments"
    ).get_json()["appointments"][0]
    assert contact_appointment["response_status"] == "no_answer"

    # Recording the same result again must not send duplicate follow-ups.
    duplicate = client.patch(
        f"/api/crm/calendly/appointments/{appointment_id}",
        json={"response_status": "no_answer"},
    )
    assert duplicate.status_code == 200
    assert "delivery" not in duplicate.get_json()
    assert len(deliveries["sms"]) == len(deliveries["email"]) == 1

    invalid = client.patch(
        f"/api/crm/calendly/appointments/{appointment_id}",
        json={"response_status": "unknown"},
    )
    assert invalid.status_code == 400


def test_answered_appointment_patch_does_not_send_followup_before_call_log(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    deliveries = {"sms": [], "email": []}
    monkeypatch.setattr(application, "send_sms", lambda phone, body: deliveries["sms"].append((phone, body)) or True)
    monkeypatch.setattr(application, "send_email_html", lambda mail, subject, plain, html: deliveries["email"].append((mail, subject, plain, html)) or True)
    contact = client.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin", "formation": "APS"}).get_json()
    client.patch(f"/api/crm/contacts/{contact['id']}", json={"mail": "lina@example.com", "telephone": "+33612345678"})
    client.post("/api/crm/templates", json={"type": "sms", "nom": "Suite appel répondu", "contenu": "Merci {{ prenom }} pour notre appel."})
    client.post("/api/crm/templates", json={"type": "email", "nom": "Suite appel répondu", "sujet": "La suite pour {{ formation }}", "contenu": "<p>Bonjour {{ prenom }}, suite à notre appel.</p>"})
    signed_webhook(client, monkeypatch, "invitee.created", calendly_payload())
    appointment_id = client.get("/api/crm/calendly/appointments").get_json()["appointments"][0]["id"]

    response = client.patch(f"/api/crm/calendly/appointments/{appointment_id}", json={"response_status": "answered"})

    assert response.status_code == 200
    assert "delivery" not in response.get_json()
    assert deliveries == {"sms": [], "email": []}
    duplicate = client.patch(f"/api/crm/calendly/appointments/{appointment_id}", json={"response_status": "answered"})
    assert "delivery" not in duplicate.get_json()
    assert deliveries == {"sms": [], "email": []}


def test_answered_followup_is_sent_only_when_the_call_is_logged(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    deliveries = {"sms": [], "email": []}
    monkeypatch.setattr(application, "send_sms", lambda phone, body: deliveries["sms"].append((phone, body)) or True)
    monkeypatch.setattr(application, "send_email_html", lambda mail, subject, plain, html: deliveries["email"].append((mail, subject)) or True)
    contact = client.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin", "formation": "APS"}).get_json()
    client.patch(f"/api/crm/contacts/{contact['id']}", json={"mail": "lina@example.com", "telephone": "+33612345678"})
    client.post("/api/crm/templates", json={"type": "sms", "nom": "Suite appel répondu", "contenu": "Merci {{ prenom }}."})
    client.post("/api/crm/templates", json={"type": "email", "nom": "Suite appel répondu", "sujet": "Suite", "contenu": "<p>Merci.</p>"})
    signed_webhook(client, monkeypatch, "invitee.created", calendly_payload())
    appointment_id = client.get("/api/crm/calendly/appointments").get_json()["appointments"][0]["id"]

    assert deliveries == {"sms": [], "email": []}
    response = client.post(
        f"/api/crm/contacts/{contact['id']}/appel",
        json={"commentaire": "Échange concluant.", "appointment_id": appointment_id},
    )

    assert response.status_code == 200
    assert response.get_json()["appointment"]["response_status"] == "answered"
    assert response.get_json()["delivery"] == {"sms": True, "email": True}
    assert len(deliveries["sms"]) == len(deliveries["email"]) == 1
    assert client.get(f"/api/crm/contacts/{contact['id']}").get_json()["activities"][0]["detail"] == "Échange concluant."


def test_cached_appointment_replaces_follow_up_when_later_linked_to_contact(
        tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin", "formation": "APS"},
    ).get_json()
    client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"statut": "A relancer", "relance_date": "2099-08-10"},
    )

    cached = signed_webhook(
        client,
        monkeypatch,
        "invitee.created",
        calendly_payload(email="rattachement@example.com"),
    )
    assert cached.status_code == 200
    assert cached.get_json()["contact_id"] is None

    linked = client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"mail": "rattachement@example.com"},
    ).get_json()

    assert linked["statut"] == "RDV programmé"
    assert linked["relance_date"] == ""
    assert linked["relances"][0]["status"] == "cancelled"
    appointments = client.get(
        f"/api/crm/contacts/{contact['id']}/calendly/appointments"
    ).get_json()["appointments"]
    assert len(appointments) == 1
    assert appointments[0]["contact_id"] == contact["id"]


def test_webhook_keeps_an_unmatched_appointment_without_creating_a_lead(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)

    response = signed_webhook(
        client,
        monkeypatch,
        "invitee.created",
        calendly_payload(email="nouveau@example.com"),
    )

    assert response.status_code == 200
    assert response.get_json()["contact_id"] is None
    assert client.get("/api/crm/contacts").get_json() == []
    appointments = client.get("/api/crm/calendly/appointments").get_json()["appointments"]
    assert len(appointments) == 1
    assert appointments[0]["invitee_email"] == "nouveau@example.com"
    assert appointments[0]["contact_id"] is None


def test_webhook_infers_dirigeant_formation_from_event_name(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin", "formation": ""},
    ).get_json()
    client.patch(f"/api/crm/contacts/{contact['id']}", json={"mail": "dirigeant@example.com"})
    payload = calendly_payload(email="dirigeant@example.com")
    payload["scheduled_event"]["name"] = "Dirigeant d'entreprise de sécurité privée"

    response = signed_webhook(client, monkeypatch, "invitee.created", payload)

    assert response.status_code == 200
    updated = client.get(f"/api/crm/contacts/{response.get_json()['contact_id']}").get_json()
    assert updated["formation"] == "DESP"
    assert updated["desp_type"] == "INITIAL"


def test_webhook_fills_missing_formation_on_an_existing_contact(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin", "formation": ""},
    ).get_json()
    client.patch(f"/api/crm/contacts/{contact['id']}", json={"mail": "lina@example.com"})
    payload = calendly_payload()
    payload["scheduled_event"]["name"] = "Accompagnement VAE Dirigeant DESP"

    response = signed_webhook(client, monkeypatch, "invitee.created", payload)

    assert response.status_code == 200
    updated = client.get(f"/api/crm/contacts/{contact['id']}").get_json()
    assert updated["formation"] == "DESP"
    assert updated["desp_type"] == "VAE"


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


def test_event_types_are_filtered_for_aps_formation(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    monkeypatch.setattr(application, "_calendly_event_types_for_context", lambda data: [
        {"uri": "type-aps", "name": "RDV téléphonique agent de sécurité", "active": True},
        {"uri": "type-vtc", "name": "RDV téléphonique formation Chauffeur VTC", "active": True},
    ])

    response = client.get("/api/crm/calendly/event-types?formation=APS")

    assert response.status_code == 200
    assert [item["name"] for item in response.get_json()] == [
        "RDV téléphonique agent de sécurité"
    ]


def test_event_types_match_apr_calendly_name_for_a3p_formation(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    monkeypatch.setattr(application, "_calendly_event_types_for_context", lambda data: [
        {
            "uri": "type-apr",
            "name": "RDV téléphonique formation garde du corps (APR)",
            "active": True,
        },
        {
            "uri": "type-vtc",
            "name": "RDV téléphonique formation Chauffeur VTC",
            "active": True,
        },
    ])

    response = client.get("/api/crm/calendly/event-types?formation=A3P")

    assert response.status_code == 200
    assert [item["name"] for item in response.get_json()] == [
        "RDV téléphonique formation garde du corps (APR)"
    ]


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
            "start_time": "2099-08-12T08:00:00Z",
            "timezone": "Europe/Paris",
            "location": {"kind": "outbound_call", "location": "06 12 34 56 78"},
            "answers": {"0": "Formation APS"},
        },
    )

    assert response.status_code == 201
    assert captured["location"] == {"kind": "outbound_call", "location": "+33612345678"}
    assert "text_reminder_number" not in captured["invitee"]
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
    sync_state = application.load_data()["crm_calendly"]
    assert sync_state["last_sync_at"]
    assert sync_state["last_full_sync_at"] == sync_state["last_sync_at"]
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

    url = f"/api/crm/contacts/{contact['id']}/calendly/appointments"
    local_response = client.get(url)

    assert local_response.status_code == 200
    assert local_response.get_json()["lookup"] == {
        "method": "local",
        "processed_events": 0,
        "matched_appointments": 0,
    }
    assert calls == []

    response = client.get(f"{url}?refresh=1")

    assert response.status_code == 200
    result = response.get_json()
    assert result["lookup"] == {
        "method": "email",
        "processed_events": 1,
        "matched_appointments": 1,
    }
    assert len(result["appointments"]) == 1
    assert result["appointments"][0]["contact_id"] == contact["id"]
    assert result["integration"]["last_sync_at"]
    updated_contact = client.get(f"/api/crm/contacts/{contact['id']}").get_json()
    assert updated_contact["statut"] == "RDV programmé"
    assert [path for _, path, _ in calls] == [
        "/scheduled_events",
        "/scheduled_events/EVENT1/invitees",
    ]


def test_cached_active_appointment_updates_contact_pipeline_status(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    monkeypatch.delenv("CALENDLY_ACCESS_TOKEN")
    contact = client.post(
        "/api/crm/contacts",
        json={"prenom": "Tony", "nom": "Arribas", "formation": "Chauffeur VTC"},
    ).get_json()
    data = application.load_data()
    data["crm_calendly_appointments"] = [
        {
            "id": "appointment-1",
            "contact_id": contact["id"],
            "status": "active",
            "start_time": "2099-08-12T08:00:00Z",
        }
    ]
    application.save_data(data)

    response = client.get(
        f"/api/crm/contacts/{contact['id']}/calendly/appointments"
    )

    assert response.status_code == 200
    assert len(response.get_json()["appointments"]) == 1
    updated_contact = client.get(f"/api/crm/contacts/{contact['id']}").get_json()
    assert updated_contact["statut"] == "RDV programmé"


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
        "b.dataset.primaryStep==='Converti'",
        "calendarSelectedDate=calendarDateKey(new Date())",
        'id="calendarDate" type="date"',
        "calendarDateKey(new Date(a.start_time))===calendarSelectedDate",
        "changeCalendarDate(-1)",
        "changeCalendarDate(1)",
        "Interlocuteur :",
        "appointmentResponseControl(a)",
        "bindAppointmentResponseControls(items,()=>loadCalendar())",
        "A répondu",
        "Sans réponse",
        "calendarFormationTone",
        "calendar-training-${calendarFormationTone(a,c)}",
        "value.includes('DIRIGEANT')",
        "value.includes('APR')",
        "value.includes('SECURITE INCENDIE')",
        'id="deleteBtn"',
        "method:'DELETE'",
    ]
    for marker in required_markers:
        assert marker in crm_js
    assert "Promise.race" not in crm_js
    assert "La recherche Calendly a pris trop de temps" not in crm_js
    assert "Les rendez-vous déjà enregistrés restent affichés." in crm_js
    assert 'id="retryCalendlyLookup"' in crm_js
    assert "integration.last_sync_at||new Date().toISOString()" not in crm_js
    assert "rendez-vous traités" not in crm_js
    assert "appointment-row" not in crm_js
    assert "grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr))" in crm_css
    assert ".calendly-card{container-type:inline-size" in crm_css
    assert "@container (max-width:620px)" in crm_css
    assert ".next-appointment .appointment-actions{grid-column:1/-1;width:100%;max-width:none" in crm_css
    for marker in [
        ".calendly-card{",
        ".next-appointment{",
        ".appointment-grid{",
        ".appointment-status.canceled{",
        ".calendly-modal{",
        ".calendar-date-picker label{",
        ".calendar-empty{",
        ".appointment-response{",
        ".calendar-training-desp{",
        ".calendar-training-aps{",
        ".calendar-training-a3p{",
        ".calendar-training-vtc{",
        ".calendar-training-ssiap{",
    ]:
        assert marker in crm_css


def test_no_answer_updates_the_in_memory_contact_without_refresh():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "if(contact&&updated.contact)Object.assign(contact,updated.contact)" in crm_js


def test_booking_rejects_invalid_outbound_phone_before_calling_invitees(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin", "formation": "APS"},
    ).get_json()
    client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"mail": "lina@example.com", "telephone": "123"},
    )
    calls = []

    def fake_calendly(method, path, **kwargs):
        calls.append((method, path))
        if method == "GET" and path == "/event_types/TYPE1":
            return {"resource": {
                "uri": "https://api.calendly.com/event_types/TYPE1",
                "name": "Appel découverte",
                "active": True,
                "duration": 30,
                "locations": [{"kind": "outbound_call"}],
                "custom_questions": [],
            }}
        raise AssertionError(f"Unexpected Calendly call: {method} {path}")

    monkeypatch.setattr(application, "_calendly_request", fake_calendly)
    response = client.post(
        f"/api/crm/contacts/{contact['id']}/calendly/appointments",
        json={
            "event_type": "https://api.calendly.com/event_types/TYPE1",
            "start_time": "2099-08-12T08:00:00Z",
            "timezone": "Europe/Paris",
            "location": {"kind": "outbound_call", "location": "123"},
        },
    )

    assert response.status_code == 400
    assert "numéro de téléphone" in response.get_json()["error"]
    assert ("POST", "/invitees") not in calls


def test_booking_invalid_argument_returns_actionable_retry_error(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin", "formation": "APS"},
    ).get_json()
    client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"mail": "lina@example.com", "telephone": "06 12 34 56 78"},
    )

    def fake_calendly(method, path, **kwargs):
        if method == "GET" and path == "/event_types/TYPE1":
            return {"resource": {
                "uri": "https://api.calendly.com/event_types/TYPE1",
                "name": "Appel découverte",
                "active": True,
                "duration": 30,
                "locations": [{"kind": "outbound_call"}],
                "custom_questions": [],
            }}
        if method == "POST" and path == "/invitees":
            assert "text_reminder_number" not in kwargs["json_body"]["invitee"]
            raise application.CalendlyAPIError(
                400,
                {"title": "Invalid Argument", "message": "The supplied parameters are invalid."},
            )
        raise AssertionError(f"Unexpected Calendly call: {method} {path}")

    monkeypatch.setattr(application, "_calendly_request", fake_calendly)
    response = client.post(
        f"/api/crm/contacts/{contact['id']}/calendly/appointments",
        json={
            "event_type": "https://api.calendly.com/event_types/TYPE1",
            "start_time": "2099-08-12T08:00:00Z",
            "timezone": "Europe/Paris",
            "location": {"kind": "outbound_call", "location": "06 12 34 56 78"},
        },
    )

    assert response.status_code == 502
    payload = response.get_json()
    assert payload["stage"] == "la création du rendez-vous"
    assert "Vérifiez le type de rendez-vous" in payload["error"]



def test_availability_forwards_a_seven_day_window(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    captured = {}

    def fake_calendly(method, path, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return {"collection": [{
            "status": "available",
            "start_time": "2099-08-12T08:00:00Z",
        }]}

    monkeypatch.setattr(application, "_calendly_request", fake_calendly)
    response = client.get(
        "/api/crm/calendly/availability",
        query_string={
            "event_type": "https://api.calendly.com/event_types/TYPE1",
            "start_time": "2099-08-12T00:00:00Z",
            "end_time": "2099-08-19T00:00:00Z",
        },
    )

    assert response.status_code == 200
    assert response.get_json()[0]["start_time"] == "2099-08-12T08:00:00Z"
    assert captured["method"] == "GET"
    assert captured["path"] == "/event_type_available_times"
    assert captured["params"]["start_time"] == "2099-08-12T00:00:00Z"
    assert captured["params"]["end_time"] == "2099-08-19T00:00:00Z"


def test_availability_rejects_windows_longer_than_seven_days(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        application,
        "_calendly_request",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    response = client.get(
        "/api/crm/calendly/availability",
        query_string={
            "event_type": "https://api.calendly.com/event_types/TYPE1",
            "start_time": "2099-08-12T00:00:00Z",
            "end_time": "2099-08-19T00:00:01Z",
        },
    )

    assert response.status_code == 400
    assert "ne peut pas dépasser 7 jours" in response.get_json()["error"]
    assert calls == []


def test_availability_moves_a_stale_start_time_into_the_future(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    captured = {}
    now = datetime.datetime.now(datetime.timezone.utc)
    requested_end = now + datetime.timedelta(days=6)

    def fake_calendly(method, path, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return {"collection": []}

    monkeypatch.setattr(application, "_calendly_request", fake_calendly)
    response = client.get(
        "/api/crm/calendly/availability",
        query_string={
            "event_type": "https://api.calendly.com/event_types/TYPE1",
            "start_time": (now - datetime.timedelta(seconds=5)).isoformat(),
            "end_time": requested_end.isoformat(),
        },
    )

    assert response.status_code == 200
    forwarded_start = datetime.datetime.fromisoformat(
        captured["params"]["start_time"].replace("Z", "+00:00")
    )
    assert forwarded_start > datetime.datetime.now(datetime.timezone.utc)
    assert captured["params"]["end_time"] == requested_end.isoformat()
    assert forwarded_start - now >= datetime.timedelta(seconds=50)
    assert captured["method"] == "GET"
    assert captured["path"] == "/event_type_available_times"


def test_calendly_booking_browser_uses_seven_day_windows():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "CALENDLY_AVAILABILITY_WINDOW_DAYS=7" in crm_js
    assert 'id="calPrev">← 7 jours</button>' in crm_js
    assert 'id="calNext">7 jours →</button>' in crm_js
    assert "windowEnd.setDate(windowEnd.getDate()+CALENDLY_AVAILABILITY_WINDOW_DAYS)" in crm_js
    assert "maxCalendlyEnd=new Date(actualStart.getTime()+CALENDLY_AVAILABILITY_WINDOW_DAYS*86400000)" in crm_js
    assert "Math.min(windowEnd.getTime(),maxCalendlyEnd.getTime())" in crm_js
    assert "rangeStart.setDate(rangeStart.getDate()-CALENDLY_AVAILABILITY_WINDOW_DAYS)" in crm_js
    assert "rangeStart.setDate(rangeStart.getDate()+CALENDLY_AVAILABILITY_WINDOW_DAYS)" in crm_js
    assert "14 jours" not in crm_js
