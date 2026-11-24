import datetime
import hashlib
import hmac
import json
import subprocess
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
    canceled_payload["updated_at"] = "2099-08-04T23:30:00Z"
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
    assert updated_contact["relance_date"] == "2099-08-05"
    assert updated_contact["relances"][0]["source"] == "calendly_cancellation"
    assert updated_contact["relances"][0]["motif"] == "Suite annulation du rendez-vous"
    assert any(
        relance["scheduled_date"] == "2099-09-03"
        and relance["status"] == "reprogrammed"
        for relance in updated_contact["relances"]
    )
    assert any(
        activity["title"] == "Statut : A relancer"
        and activity["detail"] == "Ancien statut : RDV programmé"
        for activity in updated_contact["activities"]
    )


def test_cancellation_schedules_same_day_follow_up_then_new_booking_replaces_it(tmp_path, monkeypatch):
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
    assert refreshed["relances"] == []
    assert any(
        activity["title"] == "Statut : RDV programmé"
        and activity["detail"] == "Ancien statut : A relancer"
        for activity in refreshed["activities"]
    )

    canceled_payload = calendly_payload(status="canceled")
    canceled_payload["scheduled_event"]["start_time"] = "2099-08-12T08:00:00Z"
    canceled_payload["scheduled_event"]["end_time"] = "2099-08-12T08:30:00Z"
    canceled_payload["cancellation"] = {"reason": "Indisponible"}
    canceled_payload["updated_at"] = "2099-08-05T08:15:00Z"
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
    assert after_cancellation["statut"] == "A relancer"
    assert after_cancellation["relance_date"] == "2099-08-05"
    assert len(after_cancellation["relances"]) == 1
    assert after_cancellation["relances"][0]["status"] == "scheduled"
    assert after_cancellation["relances"][0]["source"] == "calendly_cancellation"
    assert any(
        activity["title"] == "Statut : A relancer"
        and activity["detail"] == "Ancien statut : RDV programmé"
        for activity in after_cancellation["activities"]
    )

    new_payload = calendly_payload()
    new_payload["uri"] = (
        "https://api.calendly.com/scheduled_events/EVENT2/invitees/INVITEE2"
    )
    new_payload["event"] = "https://api.calendly.com/scheduled_events/EVENT2"
    new_payload["scheduled_event"]["uri"] = new_payload["event"]
    new_payload["scheduled_event"]["start_time"] = "2099-08-14T08:00:00Z"
    new_payload["scheduled_event"]["end_time"] = "2099-08-14T08:30:00Z"

    rebooked = signed_webhook(
        client,
        monkeypatch,
        "invitee.created",
        new_payload,
    )

    assert rebooked.status_code == 200
    after_rebooking = client.get(
        f"/api/crm/contacts/{contact['id']}"
    ).get_json()
    assert after_rebooking["statut"] == "RDV programmé"
    assert after_rebooking["relance_date"] == ""
    assert after_rebooking["relances"] == []
    assert any(
        activity["title"] == "Statut : RDV programmé"
        and activity["detail"] == "Ancien statut : A relancer"
        for activity in after_rebooking["activities"]
    )


def test_full_sync_repairs_follow_up_created_before_cached_booking(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin", "formation": "APS"},
    ).get_json()
    client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={
            "mail": "lina@example.com",
            "statut": "A relancer",
            "relance_date": "2099-08-10",
        },
    )

    data = application.load_data()
    stored_contact = next(
        item for item in data["crm_contacts"] if item["id"] == contact["id"]
    )
    stored_contact["relances"][0]["created_at"] = "2099-08-01T08:00:00+00:00"
    payload = calendly_payload()
    data["crm_calendly_appointments"] = [{
        "id": "cached-before-deployment",
        "contact_id": contact["id"],
        "invitee_uri": payload["uri"],
        "event_uri": payload["event"],
        "invitee_email": payload["email"],
        "status": "active",
        "start_time": payload["scheduled_event"]["start_time"],
        "calendly_created_at": "2099-08-03T08:00:00Z",
        "created_at": "2099-08-03T08:00:00+00:00",
    }]
    application.save_data(data)

    replayed = signed_webhook(
        client,
        monkeypatch,
        "invitee.created",
        payload,
    )

    assert replayed.status_code == 200
    repaired = client.get(f"/api/crm/contacts/{contact['id']}").get_json()
    assert repaired["statut"] == "RDV programmé"
    assert repaired["relance_date"] == ""
    assert repaired["relances"] == []


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
    payload = calendly_payload()
    payload["scheduled_event"].update(start_time="2020-08-12T08:00:00Z", end_time="2020-08-12T08:30:00Z")
    created = signed_webhook(client, monkeypatch, "invitee.created", payload)
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
                     + application.datetime.timedelta(days=7)).isoformat()
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
    data = application.load_data()
    data["crm_calendly_appointments"] = [{
        "id": "appointment-orphan",
        "invitee_uri": "https://api.calendly.com/scheduled_events/EVENT1/invitees/INVITEE1",
        "event_uri": "https://api.calendly.com/scheduled_events/EVENT1",
        "name": "Appel découverte",
        "start_time": "2099-08-12T08:00:00Z",
        "end_time": "2099-08-12T08:30:00Z",
        "status": "active",
        "invitee_name": "Lina Martin",
        "invitee_email": "rattachement@example.com",
        "invitee_phone": "",
        "contact_id": None,
        "created_at": "2099-08-03T08:00:00Z",
        "updated_at": "2099-08-03T08:00:00Z",
    }]
    application.save_data(data)

    linked = client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"mail": "rattachement@example.com"},
    ).get_json()

    assert linked["statut"] == "RDV programmé"
    assert linked["relance_date"] == ""
    assert linked["relances"] == []
    appointments = client.get(
        f"/api/crm/contacts/{contact['id']}/calendly/appointments"
    ).get_json()["appointments"]
    assert len(appointments) == 1
    assert appointments[0]["contact_id"] == contact["id"]


def test_webhook_creates_one_lead_for_unmatched_future_appointments(
        tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    payload = calendly_payload(email="goeffoy@example.com")
    payload.update({"name": "goeffoy galli"})
    payload.pop("first_name")
    payload.pop("last_name")
    payload["scheduled_event"]["name"] = (
        "RDV téléphonique formation garde du corps (APR)"
    )

    response = signed_webhook(
        client,
        monkeypatch,
        "invitee.created",
        payload,
    )

    assert response.status_code == 200
    contact_id = response.get_json()["contact_id"]
    assert contact_id
    contacts = client.get("/api/crm/contacts").get_json()
    assert len(contacts) == 1
    assert contacts[0]["id"] == contact_id
    assert contacts[0]["prenom"] == "Goeffoy"
    assert contacts[0]["nom"] == "GALLI"
    assert contacts[0]["mail"] == "goeffoy@example.com"
    assert contacts[0]["telephone"] == "+33612345678"
    assert contacts[0]["formation"] == "A3P"
    assert contacts[0]["statut"] == "RDV programmé"
    assert contacts[0]["origine"] == "Calendly"
    assert contacts[0]["prix_vente"] == 4200

    second_payload = calendly_payload(email="goeffoy@example.com")
    second_payload.update({
        "uri": "https://api.calendly.com/scheduled_events/EVENT2/invitees/INVITEE2",
        "event": "https://api.calendly.com/scheduled_events/EVENT2",
        "name": "Goeffoy Galli",
        "first_name": "Goeffoy",
        "last_name": "Galli",
    })
    second_payload["scheduled_event"].update({
        "uri": second_payload["event"],
        "name": "RDV téléphonique formation garde du corps (APR)",
        "start_time": "2099-08-12T08:45:00Z",
        "end_time": "2099-08-12T09:15:00Z",
    })
    second = signed_webhook(
        client,
        monkeypatch,
        "invitee.created",
        second_payload,
    )

    assert second.status_code == 200
    assert second.get_json()["contact_id"] == contact_id
    assert len(client.get("/api/crm/contacts").get_json()) == 1
    appointments = client.get("/api/crm/calendly/appointments").get_json()["appointments"]
    assert len(appointments) == 2
    assert {item["contact_id"] for item in appointments} == {contact_id}


def test_webhook_reuses_contact_for_same_booking_name_event_and_day(
        tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    first_payload = calendly_payload(email="tusher-one@example.com")
    first_payload.update({
        "name": "Tusher Tuhin",
        "first_name": "Tusher",
        "last_name": "Tuhin",
        "text_reminder_number": "+33611111111",
    })
    first_payload["scheduled_event"].update({
        "name": "RDV téléphonique formation garde du corps (APR)",
        "start_time": "2099-08-12T12:15:00Z",
        "end_time": "2099-08-12T12:30:00Z",
    })
    first = signed_webhook(
        client,
        monkeypatch,
        "invitee.created",
        first_payload,
    )

    second_payload = calendly_payload(email="tusher-two@example.com")
    second_payload.update({
        "uri": "https://api.calendly.com/scheduled_events/EVENT2/invitees/INVITEE2",
        "event": "https://api.calendly.com/scheduled_events/EVENT2",
        "name": "Tusher Tuhin",
        "first_name": "Tusher",
        "last_name": "Tuhin",
        "text_reminder_number": "+33622222222",
    })
    second_payload["scheduled_event"].update({
        "uri": second_payload["event"],
        "name": "RDV téléphonique formation garde du corps (APR)",
        "start_time": "2099-08-12T12:45:00Z",
        "end_time": "2099-08-12T13:00:00Z",
    })
    second = signed_webhook(
        client,
        monkeypatch,
        "invitee.created",
        second_payload,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.get_json()["contact_id"] == first.get_json()["contact_id"]
    assert len(client.get("/api/crm/contacts").get_json()) == 1
    appointments = client.get("/api/crm/calendly/appointments").get_json()["appointments"]
    assert len(appointments) == 2
    assert {item["contact_id"] for item in appointments} == {
        first.get_json()["contact_id"]
    }


def test_webhook_keeps_unidentifiable_appointment_without_creating_a_lead(
        tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    payload = calendly_payload(email="")
    payload["text_reminder_number"] = ""
    payload["scheduled_event"]["location"] = None

    response = signed_webhook(
        client,
        monkeypatch,
        "invitee.created",
        payload,
    )

    assert response.status_code == 200
    assert response.get_json()["contact_id"] is None
    assert client.get("/api/crm/contacts").get_json() == []
    appointment = client.get(
        "/api/crm/calendly/appointments"
    ).get_json()["appointments"][0]
    assert appointment["contact_id"] is None


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


def test_event_types_for_ssiap_exclude_desp(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    monkeypatch.setattr(application, "_calendly_event_types_for_context", lambda data: [
        {
            "uri": "type-desp",
            "name": "RDV téléphonique formation DESP",
            "active": True,
        },
        {
            "uri": "type-ssiap",
            "name": "RDV téléphonique formation SSIAP 1 incendie",
            "active": True,
        },
    ])

    response = client.get("/api/crm/calendly/event-types?formation=SSIAP%201")

    assert response.status_code == 200
    assert [item["name"] for item in response.get_json()] == [
        "RDV téléphonique formation SSIAP 1 incendie"
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
                "name": "RDV téléphonique agent de sécurité (APS)",
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
    assert captured["invitee"]["first_name"] == "Lina"
    assert captured["invitee"]["last_name"] == "MARTIN"
    assert captured["questions_and_answers"] == [{
        "question": "Votre projet",
        "answer": "Formation APS",
        "position": 0,
    }]
    assert response.get_json()["appointment"]["contact_id"] == contact["id"]


def test_booking_normalizes_calendly_phone_question_for_text_reminders(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post(
        "/api/crm/contacts",
        json={"prenom": "Yanis", "nom": "Exemple", "formation": "SSIAP 1"},
    ).get_json()
    client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"mail": "yanis@example.com", "telephone": "06 12 34 56 78"},
    )
    captured = {}

    def fake_calendly(method, path, **kwargs):
        if method == "GET" and path == "/event_types/TYPE1":
            return {"resource": {
                "uri": "https://api.calendly.com/event_types/TYPE1",
                "name": "RDV téléphonique formation SSIAP 1 incendie",
                "active": True,
                "duration": 15,
                "is_paid": False,
                "booking_method": "instant",
                "pooling_type": "round_robin",
                "locations": [{"kind": "custom", "location": "Appel téléphonique"}],
                "custom_questions": [
                    {
                        "name": "Veuillez partager tout ce qui pourra être utile",
                        "position": 0,
                        "enabled": True,
                        "required": False,
                        "type": "text",
                    },
                    {
                        "name": "Numéro de téléphone",
                        "position": 1,
                        "enabled": True,
                        "required": True,
                        "type": "phone_number",
                    },
                ],
            }}
        if method == "POST" and path == "/invitees":
            captured.update(kwargs["json_body"])
            return {"resource": {
                "uri": "https://api.calendly.com/scheduled_events/EVENT1/invitees/INVITEE1",
                "event": "https://api.calendly.com/scheduled_events/EVENT1",
                "name": "Yanis EXEMPLE",
                "email": "yanis@example.com",
                "status": "active",
                "timezone": "Europe/Paris",
            }}
        if method == "GET" and path == "/scheduled_events/EVENT1":
            event = calendly_payload()["scheduled_event"]
            event["name"] = "RDV téléphonique formation SSIAP 1 incendie"
            return {"resource": event}
        raise AssertionError(f"Unexpected Calendly call: {method} {path}")

    monkeypatch.setattr(application, "_calendly_request", fake_calendly)

    response = client.post(
        f"/api/crm/contacts/{contact['id']}/calendly/appointments",
        json={
            "event_type": "https://api.calendly.com/event_types/TYPE1",
            "start_time": "2099-09-02T11:45:00Z",
            "timezone": "Europe/Paris",
            "location": {"kind": "custom", "location": "Appel téléphonique"},
            "answers": {"0": "Préparer le dossier SSIAP", "1": "06 12 34 56 78"},
        },
    )

    assert response.status_code == 201
    assert captured["location"] == {
        "kind": "custom",
        "location": "Appel téléphonique",
    }
    assert "tracking" not in captured
    assert captured["invitee"] == {
        "name": "Yanis EXEMPLE",
        "first_name": "Yanis",
        "last_name": "EXEMPLE",
        "email": "yanis@example.com",
        "timezone": "Europe/Paris",
        "text_reminder_number": "+33612345678",
    }
    assert captured["questions_and_answers"] == [
        {
            "question": "Veuillez partager tout ce qui pourra être utile",
            "answer": "Préparer le dossier SSIAP",
            "position": 0,
        },
        {
            "question": "Numéro de téléphone",
            "answer": "+33612345678",
            "position": 1,
        },
    ]


def test_booking_rejects_an_event_type_for_another_formation(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post(
        "/api/crm/contacts",
        json={"prenom": "Yanis", "nom": "Exemple", "formation": "SSIAP 1"},
    ).get_json()
    client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"mail": "yanis@example.com", "telephone": "06 12 34 56 78"},
    )

    def fake_calendly(method, path, **kwargs):
        if method == "GET" and path == "/event_types/DESP1":
            return {"resource": {
                "uri": "https://api.calendly.com/event_types/DESP1",
                "name": "RDV téléphonique formation DESP",
                "active": True,
                "duration": 15,
                "is_paid": False,
                "booking_method": "instant",
                "locations": [],
                "custom_questions": [],
            }}
        raise AssertionError(f"Calendly ne doit pas créer ce rendez-vous : {method} {path}")

    monkeypatch.setattr(application, "_calendly_request", fake_calendly)

    response = client.post(
        f"/api/crm/contacts/{contact['id']}/calendly/appointments",
        json={
            "event_type": "https://api.calendly.com/event_types/DESP1",
            "start_time": "2099-09-02T11:45:00Z",
            "timezone": "Europe/Paris",
        },
    )

    assert response.status_code == 409
    assert "ne correspond pas à la formation SSIAP 1" in response.get_json()["error"]


def test_round_robin_booking_keeps_the_event_type_required_location():
    event_type = {
        "pooling_type": "round_robin",
        "locations": [{"kind": "outbound_call"}],
    }

    location = application._calendly_booking_location(
        event_type,
        {"kind": "outbound_call", "location": "06 12 34 56 78"},
        {"telephone": "06 12 34 56 78"},
    )

    assert location == {
        "kind": "outbound_call",
        "location": "+33612345678",
    }


def test_full_sync_creates_a_lead_for_a_stored_unmatched_future_appointment(
        tmp_path, monkeypatch):
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
    third_event = {
        **first_event,
        "uri": "https://api.calendly.com/scheduled_events/EVENT3",
        "event_type": "https://api.calendly.com/event_types/TYPE3",
        "name": "RDV téléphonique formation garde du corps (APR)",
    }
    unmatched_invitee = calendly_payload(email="goeffoy-sync@example.com")
    unmatched_invitee.update({
        "uri": "https://api.calendly.com/scheduled_events/EVENT3/invitees/INVITEE3",
        "event": third_event["uri"],
        "name": "Goeffoy Galli",
        "first_name": "Goeffoy",
        "last_name": "Galli",
    })
    data = application.load_data()
    data["crm_calendly_appointments"] = [{
        "id": "stored-orphan",
        "invitee_uri": unmatched_invitee["uri"],
        "event_uri": third_event["uri"],
        "event_type_uri": third_event["event_type"],
        "name": third_event["name"],
        "start_time": third_event["start_time"],
        "end_time": third_event["end_time"],
        "status": "active",
        "invitee_name": unmatched_invitee["name"],
        "invitee_email": unmatched_invitee["email"],
        "invitee_phone": unmatched_invitee["text_reminder_number"],
        "contact_id": None,
        "created_at": unmatched_invitee["created_at"],
        "updated_at": unmatched_invitee["updated_at"],
    }]
    application.save_data(data)

    def fake_calendly(method, path, **kwargs):
        if path == "/scheduled_events":
            return {
                # EVENT3 is intentionally absent from the current API page: a
                # full sync must still repair its previously cached orphan.
                "collection": [first_event, second_event],
                "pagination": {"next_page_token": None},
            }
        if path.endswith("/EVENT1/invitees"):
            return {"collection": [calendly_payload()], "pagination": {"next_page_token": None}}
        if path.endswith("/EVENT2/invitees"):
            invitee = calendly_payload(email="sans-piste@example.com", status="canceled")
            invitee["uri"] = "https://api.calendly.com/scheduled_events/EVENT2/invitees/INVITEE2"
            invitee["event"] = second_event["uri"]
            invitee["text_reminder_number"] = "+33699999999"
            return {"collection": [invitee], "pagination": {"next_page_token": None}}
        raise AssertionError(f"Unexpected Calendly call: {method} {path}")

    monkeypatch.setattr(application, "_calendly_request", fake_calendly)

    response = client.post("/api/crm/calendly/sync", json={"restart": True})

    assert response.status_code == 200
    assert response.get_json()["complete"] is True
    assert response.get_json()["repaired_appointments"] == 1
    sync_state = application.load_data()["crm_calendly"]
    assert sync_state["last_sync_at"]
    assert sync_state["last_full_sync_at"] == sync_state["last_sync_at"]
    stored = application.load_data()["crm_calendly_appointments"]
    assert len(stored) == 3
    assert {item["event_type_uri"] for item in stored} == {
        "https://api.calendly.com/event_types/TYPE1",
        "https://api.calendly.com/event_types/TYPE2",
        "https://api.calendly.com/event_types/TYPE3",
    }
    matched = next(item for item in stored if item["invitee_email"] == "lina@example.com")
    canceled = next(item for item in stored if item["invitee_email"] == "sans-piste@example.com")
    repaired = next(
        item for item in stored
        if item["invitee_email"] == "goeffoy-sync@example.com"
    )
    assert matched["contact_id"] == contact["id"]
    assert canceled["contact_id"] is None
    assert repaired["contact_id"]
    created = next(
        item for item in client.get("/api/crm/contacts").get_json()
        if item["id"] == repaired["contact_id"]
    )
    assert created["prenom"] == "Goeffoy"
    assert created["nom"] == "GALLI"
    assert created["formation"] == "A3P"
    assert created["origine"] == "Calendly"


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
            invitee.update(name="Tony Arribas", first_name="Tony", last_name="Arribas")
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
    payload.update(name="Tony Arribas", first_name="Tony", last_name="Arribas")
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
        "calendarSelectedDate=initialCalendarDay()",
        'id="calendarDate" type="date"',
        "a.calendar_slot?.day===calendarSelectedDate",
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


def test_calendar_appointments_expose_and_render_contact_scores(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin", "formation": "APS"},
    ).get_json()
    updated = client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={
            "mail": "lina@example.com", "telephone": "+33612345678",
            "lieu": "Côte d’Azur", "dates_formation": "Septembre",
            "origine": "Site internet", "cpf": "NON",
            "identite_creation": "NON", "financement_ft": "NON",
            "inscrit_ft": "NON", "carte_pro": "OUI",
        },
    )
    assert updated.status_code == 200
    signed_webhook(client, monkeypatch, "invitee.created", calendly_payload())

    linked = client.get(
        "/api/crm/calendly/appointments"
    ).get_json()["appointments"][0]["contact"]

    assert linked["id"] == contact["id"]
    assert linked["lieu"] == "Côte d’Azur"
    assert linked["carte_pro"] == "OUI"
    assert isinstance(linked["integration_score"]["score"], int)
    assert "personal_remainder_applicable" in linked["integration_score"]

    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()
    with open(application.app.root_path + "/static/crm.css", encoding="utf-8") as source:
        crm_css = source.read()
    helper = crm_js[
        crm_js.index("function calendarContactMetrics"):
        crm_js.index("function changeCalendarDate")
    ]
    script = "const window={CRMWorkspace:{contactCompleteness:c=>c.complete}};" + helper + r"""
const assert=require('node:assert/strict');
const rendered=calendarContactMetrics({id:'contact-1',complete:82,integration_score:{score:64}});
assert.match(rendered,/Score d’intégration/);
assert.match(rendered,/64 \/ 100/);
assert.match(rendered,/Complétude du dossier/);
assert.match(rendered,/82 %/);
assert.match(calendarContactMetrics({id:'contact-2',complete:null,integration_score:{score:null}}),/Indisponible/);
assert.equal(calendarContactMetrics({}), '');
"""
    completed = subprocess.run(
        ["node", "-e", script], check=False, capture_output=True, text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "${calendarContactMetrics(c)}" in crm_js
    assert ".calendar-contact-metrics{" in crm_css
    assert ".calendar-contact-metric strong{" in crm_css


def test_calendar_appointments_open_linked_contacts_in_new_tabs():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    bind_rows = crm_js[
        crm_js.index("function bindRows()"):
        crm_js.index("function plannedRelances")
    ]

    assert (
        "const opensNewTab=C.section==='calendrier'"
        "&&Boolean(row.closest('#calendarContent'))"
    ) in bind_rows
    assert "opensNewTab?openContactInNewTab(row.dataset.id)" in bind_rows


def test_calendly_appointment_stays_upcoming_for_two_hours_after_start():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()
    with open(application.app.root_path + "/templates/crm.html", encoding="utf-8") as source:
        crm_template = source.read()

    helpers = crm_js[
        crm_js.index("const CALENDLY_APPOINTMENT_PAST_DELAY_MS="):
        crm_js.index("const esc=")
    ]
    script = helpers + r"""
const assert=require('node:assert/strict');
const start=Date.parse('2026-08-27T07:00:00Z');
const appointment={start_time:'2026-08-27T07:00:00Z',status:'active'};

assert.equal(CALENDLY_APPOINTMENT_PAST_DELAY_MS,2*60*60*1000);
assert.equal(calendlyAppointmentIsUpcoming(appointment,start+8*60*1000),true);
assert.equal(calendlyAppointmentIsPast(appointment,start+8*60*1000),false);
assert.equal(calendlyAppointmentIsUpcoming({...appointment,response_status:'answered'},start+8*60*1000),false);
assert.equal(calendlyAppointmentIsPast({...appointment,response_status:'answered'},start+8*60*1000),true);
assert.equal(calendlyAppointmentIsUpcoming({...appointment,response_status:'no_answer'},start+8*60*1000),false);
assert.equal(calendlyAppointmentIsPast({...appointment,response_status:'no_answer'},start+8*60*1000),true);
assert.equal(calendlyAppointmentIsUpcoming(appointment,start+2*60*60*1000),true);
assert.equal(calendlyAppointmentIsPast(appointment,start+2*60*60*1000),false);
assert.equal(calendlyAppointmentIsUpcoming(appointment,start+2*60*60*1000+1),false);
assert.equal(calendlyAppointmentIsPast(appointment,start+2*60*60*1000+1),true);
assert.equal(calendlyAppointmentIsUpcoming({...appointment,status:'canceled'},start),false);
assert.equal(calendlyAppointmentIsPast({...appointment,status:'canceled'},start+3*60*60*1000),false);
"""

    completed = subprocess.run(
        ["node", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "const upcoming=ordered.filter(a=>calendlyAppointmentIsUpcoming(a,now));" in crm_js
    assert "const past=ordered.filter(a=>calendlyAppointmentIsPast(a,now));" in crm_js
    assert "calendlyAppointmentGroups(ordered,now)" in crm_js
    assert "calendly_timing_version='20260827-calendly-two-hour-delay-2'" in crm_template


def test_spotlight_appointment_displays_the_result_control():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()
    with open(application.app.root_path + "/static/crm.css", encoding="utf-8") as source:
        crm_css = source.read()
    with open(application.app.root_path + "/templates/crm.html", encoding="utf-8") as source:
        crm_template = source.read()

    start = crm_js.index("const nextCard=spotlight.length?")
    spotlight = crm_js[start:crm_js.index("const sections=", start)]

    assert "next-appointment-side" in spotlight
    assert "appointmentResponseControl(a,'Résultat du rendez-vous')" in spotlight
    assert "calendlyActions(a,true)" in spotlight
    assert ".next-appointment-side .appointment-response{" in crm_css
    assert ".next-appointment-side .appointment-response select{" in crm_css
    assert crm_template.count("calendly_result_version='20260901-calendar-result-history-1'") == 2


def test_calendar_visually_marks_completed_appointments_as_treated():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()
    with open(application.app.root_path + "/static/crm.css", encoding="utf-8") as source:
        crm_css = source.read()
    with open(application.app.root_path + "/templates/crm.html", encoding="utf-8") as source:
        crm_template = source.read()

    helper = crm_js[
        crm_js.index("function calendarAppointmentTreatment"):
        crm_js.index("function changeCalendarDate")
    ]
    script = helper + r"""
const assert=require('node:assert/strict');
assert.deepEqual(calendarAppointmentTreatment({response_status:'answered'}),{
  tone:'answered',label:'Traité · A répondu'
});
assert.deepEqual(calendarAppointmentTreatment({response_status:'no_answer'}),{
  tone:'no-answer',label:'Traité · Sans réponse'
});
assert.equal(calendarAppointmentTreatment({response_status:''}),null);
"""

    completed = subprocess.run(
        ["node", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "calendar-treated-${treatment.tone}" in crm_js
    assert "calendar-treatment-badge ${treatment.tone}" in crm_js
    assert ".calendar-event.calendar-treated-answered{" in crm_css
    assert ".calendar-event.calendar-treated-no-answer{" in crm_css
    assert ".calendar-treatment-badge.answered{" in crm_css
    assert ".calendar-treatment-badge.no-answer{" in crm_css
    assert crm_template.count("calendly_result_version='20260901-calendar-result-history-1'") == 2


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
                "name": "RDV téléphonique agent de sécurité (APS)",
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
                "name": "RDV téléphonique agent de sécurité (APS)",
                "active": True,
                "duration": 30,
                "locations": [{"kind": "outbound_call"}],
                "custom_questions": [],
            }}
        if method == "POST" and path == "/invitees":
            assert "text_reminder_number" not in kwargs["json_body"]["invitee"]
            raise application.CalendlyAPIError(
                400,
                {
                    "title": "Invalid Argument",
                    "message": "The supplied parameters are invalid.",
                    "details": [{
                        "parameter": "invitee.text_reminder_number",
                        "message": "Must use E.164 format.",
                    }],
                },
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

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["stage"] == "la création du rendez-vous"
    assert "Vérifiez le type de rendez-vous" in payload["error"]
    assert "invitee.text_reminder_number : Must use E.164 format." in payload["error"]


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


def test_round_robin_booking_browser_sends_the_required_location():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "if(!locations.length){root.innerHTML='';return}" in crm_js
    assert "selectedLocation=locations[Number(locationSelect?.value||0)]||locations[0]||null" in crm_js
    assert "selectedType.pooling_type==='round_robin'?null" not in crm_js


def test_shared_sms_number_does_not_identify_another_invitee(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    original = client.post("/api/crm/contacts", json={"prenom": "Alex", "nom": "Robert"}).get_json()
    client.patch(f"/api/crm/contacts/{original['id']}", json={"telephone": "0612345678", "mail": "alex@example.test"})
    guest = calendly_payload("lina@example.test")
    guest["scheduled_event"]["location"] = None
    guest["questions_and_answers"] = [{"question": "Numéro de téléphone", "answer": "+33699887766"}]

    first = signed_webhook(client, monkeypatch, "invitee.created", guest).get_json()
    second = signed_webhook(client, monkeypatch, "invitee.created", guest).get_json()
    assert first["contact_id"] == second["contact_id"] != original["id"]
    record = client.get(f"/api/crm/contacts/{first['contact_id']}").get_json()
    assert (record["prenom"], record["mail"], record["telephone"]) == ("Lina", guest["email"], "+33699887766")

    guest.pop("questions_and_answers")
    guest["email"] = ""
    assert application._crm_calendly_payload_phone(guest) == ""
    # A cache populated by the previous version cannot identify people by their SMS number.
    legacy = {"invitee_name": "Lina Martin", "invitee_phone": "0612345678"}
    assert application._crm_calendly_resolve_identity(application.load_data(), legacy) == (None, False)


def test_conflicting_callback_and_email_are_held_for_review(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    for name, email, phone in (("Lina", "lina@example.test", "0611111111"), ("Alain", "alain@example.test", "0622222222")):
        person = client.post("/api/crm/contacts", json={"prenom": name, "nom": "Martin"}).get_json()
        client.patch(f"/api/crm/contacts/{person['id']}", json={"mail": email, "telephone": phone})
    guest = calendly_payload("lina@example.test")
    guest["questions_and_answers"] = [{"question": "Téléphone", "answer": "0622222222"}]
    response = signed_webhook(client, monkeypatch, "invitee.created", guest).get_json()
    appointment = application.load_data()["crm_calendly_appointments"][0]
    assert response["contact_id"] is None and appointment["identity_conflict"]
    assert len(application.load_data()["crm_contacts"]) == 2


def test_no_answer_selector_allows_future_appointments():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()
    helper = crm_js[
        crm_js.index("function appointmentResponseControl(a,"):
        crm_js.index("function bindAppointmentResponseControls(", crm_js.index("function appointmentResponseControl(a,"))
    ]
    script = (
        "const assert=require('node:assert/strict');\n"
        "const esc=value=>String(value??'');\n"
        "const appointmentEnrollmentControl=()=>'';\n"
        + helper
        + r"""
const future={id:'tomorrow',status:'active',start_time:new Date(Date.now()+86400000).toISOString()};
const futureHtml=appointmentResponseControl(future);
assert.match(futureHtml, /<option value="no_answer"[^>]*>Sans réponse<\/option>/);
assert.doesNotMatch(futureHtml, /<option value="no_answer"[^>]*disabled/);
const canceledHtml=appointmentResponseControl({...future,status:'canceled'});
assert.match(canceledHtml, /<option value="no_answer"[^>]*disabled/);
"""
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_future_appointment_can_be_marked_no_answer_before_start(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(
        application, "_crm_send_appointment_followup",
        lambda data, contact, label: sent.append(label) or {"sms": True, "email": True},
    )
    contact = client.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin", "formation": "APS"},
    ).get_json()
    client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"mail": "lina@example.com", "telephone": "+33612345678"},
    )
    signed_webhook(client, monkeypatch, "invitee.created", calendly_payload())
    appointment = application.load_data()["crm_calendly_appointments"][0]
    assert appointment["start_time"].startswith("2099-")
    url = f"/api/crm/calendly/appointments/{appointment['id']}"

    response = client.patch(url, json={"response_status": "no_answer"})

    assert response.status_code == 200
    assert response.get_json()["response_status"] == "no_answer"
    assert response.get_json()["delivery"] == {"sms": True, "email": True}
    updated = application.load_data()
    assert updated["crm_calendly_appointments"][0]["response_status"] == "no_answer"
    updated_contact = next(c for c in updated["crm_contacts"] if c["id"] == contact["id"])
    assert updated_contact["statut"] == "A relancer"
    assert updated_contact["relance_date"] == (
        application.datetime.datetime.now(application.pytz.timezone("Europe/Paris")).date()
        + application.datetime.timedelta(days=7)
    ).isoformat()
    assert updated_contact["relances"][0]["source"] == "calendly_no_answer"
    assert sent == ["Pas de réponse appel"]

    duplicate = client.patch(url, json={"response_status": "no_answer"})
    assert duplicate.status_code == 200
    assert "delivery" not in duplicate.get_json()
    assert sent == ["Pas de réponse appel"]


def test_verify_links_restores_original_booking_and_does_not_resend(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    old = client.post("/api/crm/contacts", json={"prenom": "Alex", "nom": "Robert"}).get_json()
    client.patch(f"/api/crm/contacts/{old['id']}", json={"mail": "alex@example.test", "telephone": "0612345678"})
    guest = calendly_payload("lina@example.test")
    guest["questions_and_answers"] = [{"question": "Numéro de téléphone", "answer": "+33699887766"}]
    record = {"id": "booking-1", "contact_id": old["id"], "name": "Formation Chauffeur VTC",
              "start_time": "2099-01-15T08:00:00Z", "invitee_uri": guest["uri"], "event_uri": guest["event"],
              "invitee_name": guest["name"], "invitee_email": guest["email"], "invitee_phone": "+33612345678",
              "status": "active", "response_status": "no_answer", "response_status_updated_at": "2026-01-01T10:00:00Z"}
    data = application.load_data()
    data["crm_calendly_appointments"] = [record]
    application.save_data(data)
    calls = []
    def provider(method, path, **kwargs):
        calls.append((method, path))
        return {"resource": guest}
    monkeypatch.setattr(application, "_calendly_request", provider)
    def unexpected(*args):
        raise AssertionError("Historical repair must not send a follow-up")
    monkeypatch.setattr(application, "_crm_send_appointment_followup", unexpected)
    url = f"/api/crm/contacts/{old['id']}/calendly/verify-links"
    repaired = client.post(url, json={}).get_json()
    assert (repaired["moved"], repaired["checked"], repaired["pending"]) == (1, 1, 0)
    current = application.load_data()
    row, = current["crm_calendly_appointments"]
    new = application._crm_contact(current, row["contact_id"])
    assert (new["mail"], new["telephone"], new["statut"]) == ("lina@example.test", "+33699887766", "RDV programmé")
    assert (row["id"], row["start_time"], row["status"], row["response_status"]) == ("booking-1", record["start_time"], "active", "")
    assert row["identity_corrections"][0]["previous_contact_id"] == old["id"]
    assert client.post(url, json={}).get_json()["checked"] == 0
    assert calls == [("GET", "/scheduled_events/EVENT1/invitees/INVITEE1")]


def test_historical_repair_preserves_cancellation_and_existing_contact(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    wrong = client.post("/api/crm/contacts", json={"prenom": "Alex", "nom": "Robert"}).get_json()
    correct = client.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin"}).get_json()
    client.patch(f"/api/crm/contacts/{correct['id']}", json={"mail": "lina@example.test", "telephone": "0699887766"})
    data = application.load_data()
    guest = calendly_payload("lina@example.test", status="canceled")
    guest["questions_and_answers"] = [{"question": "Téléphone", "answer": "0699887766"}]
    appointment = {"id": "original", "invitee_name": "Lina Martin", "contact_id": wrong["id"], "status": "canceled",
                   "start_time": "2021-01-01T08:00:00Z", "name": "Appel VTC", "cancellation": {"reason": "Choix de la candidate"}}
    data["crm_calendly_appointments"] = [appointment]
    with application.app.test_request_context():
        assert application._crm_calendly_repair_invitee(data, appointment, guest)
        assert not application._crm_calendly_repair_invitee(data, appointment, guest)
    assert len(data["crm_contacts"]) == 2
    assert (appointment["contact_id"], appointment["id"], appointment["status"]) == (correct["id"], "original", "canceled")
    assert appointment["cancellation"] == {"reason": "Choix de la candidate"}
    assert not data["crm_contacts"][0]["relance_date"]


def test_historical_name_disagreement_keeps_the_recorded_call_and_owner(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    owner = client.post("/api/crm/contacts", json={"prenom": "Lucie", "nom": "Morel"}).get_json()
    client.patch(f"/api/crm/contacts/{owner['id']}", json={"mail": "lucie@example.test", "telephone": "0699887766"})
    other = client.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin"}).get_json()
    client.patch(f"/api/crm/contacts/{other['id']}", json={"mail": "lina@example.test", "telephone": "0612345678"})
    guest = calendly_payload("lucie@example.test")
    guest["scheduled_event"].update(start_time="2020-08-12T08:00:00Z", end_time="2020-08-12T08:30:00Z")
    url = f"/api/crm/contacts/{other['id']}/calendly/verify-links"
    for callback, disputed in (("0699887766", False), ("0612345678", True)):
        guest["questions_and_answers"] = [{"question": "Téléphone", "answer": callback}]
        data = application.load_data()
        data["crm_calendly_appointments"] = [{
            "id": "historical", "contact_id": None, "status": "active", "name": "Appel VTC",
            "event_uri": guest["event"], "invitee_uri": guest["uri"], "invitee_name": guest["name"],
            "invitee_email": guest["email"], "invitee_phone": callback, "callback_checked": True,
            "start_time": guest["scheduled_event"]["start_time"], "response_status": "",
            "identity_conflict": True, "identity_checked_at": "2026-01-01T10:00:00Z",
            "identity_corrections": [{"previous_contact_id": owner["id"], "previous_response": "answered",
                                      "response_at": "2020-08-12T08:15:00Z"}],
        }]
        application.save_data(data)
        monkeypatch.setattr(application, "_calendly_request", lambda *args, **kwargs: {"resource": guest})
        assert client.post(url, json={}).get_json()["checked"] == 1
        row = application.load_data()["crm_calendly_appointments"][0]
        assert row["contact_id"] == owner["id"] and row["response_status"] == "answered"
        assert row["identity_conflict"] is disputed
        assert row["response_status_updated_at"] == "2020-08-12T08:15:00Z"
        signed_webhook(client, monkeypatch, "invitee.created", guest)
        row = application.load_data()["crm_calendly_appointments"][0]
        assert (row["contact_id"], row["response_status"], row["identity_conflict"]) == (owner["id"], "answered", disputed)
        if disputed:
            assert client.patch("/api/crm/calendly/appointments/historical", json={"response_status":"no_answer"}).status_code == 409


def legacy_calendar_row(guest, contact_id=None):
    return {
        "id": "booking-to-check", "contact_id": contact_id, "status": "active",
        "invitee_uri": guest["uri"], "event_uri": guest["event"], "invitee_name": guest["name"],
        "invitee_email": guest["email"], "invitee_phone": guest["text_reminder_number"],
        "start_time": guest["scheduled_event"]["start_time"], "name": guest["scheduled_event"]["name"],
    }


def unexpected_followup(*args):
    raise AssertionError("Verifying an identity must not send a follow-up")


def test_automatic_review_moves_future_booking_and_updates_pipeline_without_opening_sheet(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    correct = client.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin",
                          "mail": "lina@example.com", "telephone": "0699887766"}).get_json()
    other = client.post("/api/crm/contacts", json={"prenom": "Pierre", "nom": "Legrand",
                        "telephone": "0612345678"}).get_json()
    guest = calendly_payload()
    guest["questions_and_answers"] = [{"question": "Numéro de téléphone", "answer": "+33699887766"}]
    data = application.load_data()
    row = legacy_calendar_row(guest, other["id"])
    data["crm_calendly_appointments"] = [row]
    application._crm_contact(data, other["id"])["statut"] = "RDV programmé"
    application.save_data(data)
    calls = []

    def fetch(method, path, **kwargs):
        assert method == "GET" and path.endswith("/EVENT1/invitees/INVITEE1")
        calls.append(path)
        return {"resource": guest}

    monkeypatch.setattr(application, "_calendly_request", fetch)
    monkeypatch.setattr(application, "_crm_send_appointment_followup", unexpected_followup)
    application._crm_calendly_auto_review()
    application._crm_calendly_auto_review()
    data = application.load_data()
    booking, = data["crm_calendly_appointments"]
    assert (booking["id"], booking["contact_id"], booking["identity_conflict"]) == (row["id"], correct["id"], False)
    assert application._crm_contact(data, correct["id"])["statut"] == "RDV programmé"
    assert application._crm_contact(data, other["id"])["statut"] == "En cours"
    assert len(calls) == 1
    # An action from the previous owner's open tab must never reach the new owner.
    assert client.patch("/api/crm/calendly/appointments/booking-to-check",
                        json={"contact_id": other["id"], "response_status": "no_answer"}).status_code == 409
    fresh = client.get(f"/api/crm/contacts/{correct['id']}/calendly/appointments").get_json()
    assert fresh["contact_status"] == "RDV programmé"
    assert [item["id"] for item in fresh["appointments"]] == [row["id"]]


def test_incoming_form_links_a_prior_booking_before_any_contact_read(tmp_path, monkeypatch):
    authenticated_client(tmp_path, monkeypatch)
    data = application.load_data()
    guest = calendly_payload()
    data["crm_calendly_appointments"] = [legacy_calendar_row(guest)]
    fields = {"prenom": "Lina", "nom": "Martin", "mail": guest["email"],
              "telephone": "0609090909", "formation": "VTC"}
    with application.app.test_request_context():
        record = {"form_id": "test-abandoned-first"}
        contact = application._crm_create_or_match_abandoned_form_contact(data, record, fields, application._crm_now())
    assert data["crm_calendly_appointments"][0]["contact_id"] == contact["id"]
    assert contact["statut"] == "RDV programmé"


def test_no_answer_confirms_legacy_spelling_with_two_coordinates_and_sends_only_once(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    owner = client.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin",
                        "mail": "lina@example.com", "telephone": "0699887766"}).get_json()
    guest = calendly_payload()
    guest["name"] = "Liina Martin"
    guest["scheduled_event"]["start_time"] = "2020-11-23T13:15:00Z"
    guest["questions_and_answers"] = [{"question": "Numéro de téléphone", "answer": "+33699887766"}]
    data = application.load_data()
    data["crm_calendly_appointments"] = [legacy_calendar_row(guest, owner["id"])]
    application.save_data(data)
    reads, sends = [], []
    def provider(method, path, **kwargs):
        reads.append((method, path))
        return {"resource": guest}
    def send(data, contact, label):
        sends.append(contact["id"])
        return {"sms": True, "email": True}
    monkeypatch.setattr(application, "_calendly_request", provider)
    monkeypatch.setattr(application, "_crm_send_appointment_followup", send)
    request = {"contact_id": owner["id"], "response_status": "no_answer"}
    for _ in range(2):
        response = client.patch("/api/crm/calendly/appointments/booking-to-check", json=request)
        assert response.status_code == 200
    stored = application.load_data()
    appointment, = stored["crm_calendly_appointments"]
    assert appointment["historical_identity_contact_id"] == owner["id"]
    assert appointment["response_status"] == "no_answer"
    saved = application._crm_contact(stored, owner["id"])
    assert saved["statut"] == "A relancer" and len(saved["relances"]) == 1
    assert reads == [("GET", "/scheduled_events/EVENT1/invitees/INVITEE1")]
    assert sends == [owner["id"]]


def test_automatic_check_keeps_genuine_coordinate_conflicts_blocked(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    owner = client.post("/api/crm/contacts", json={"prenom":"Lina","nom":"Martin",
                        "mail":"lina@example.com","telephone":"0612345678"}).get_json()
    client.post("/api/crm/contacts", json={"prenom":"Jean","nom":"Robin","telephone":"0699887766"})
    guest = calendly_payload()
    guest["name"] = "Liina Martin"
    guest["questions_and_answers"] = [{"question":"Numéro de téléphone","answer":"0699887766"}]
    data = application.load_data()
    data["crm_calendly_appointments"] = [legacy_calendar_row(guest, owner["id"])]
    application.save_data(data)
    monkeypatch.setattr(application, "_calendly_request", lambda *a, **kw: {"resource": guest})
    monkeypatch.setattr(application, "_crm_send_appointment_followup", unexpected_followup)
    response = client.patch("/api/crm/calendly/appointments/booking-to-check",
                            json={"contact_id":owner["id"],"response_status":"no_answer"})
    assert response.status_code == 409
    stored = application.load_data()["crm_calendly_appointments"][0]
    assert stored["identity_conflict"] and not stored.get("response_status")


def test_slow_identity_check_cannot_overwrite_an_intervening_cancellation(tmp_path, monkeypatch):
    authenticated_client(tmp_path, monkeypatch)
    guest = calendly_payload()
    data = application.load_data()
    data["crm_calendly_appointments"] = [legacy_calendar_row(guest)]
    application.save_data(data)
    def provider(*args, **kwargs):
        live = application.load_data()
        live["crm_calendly_appointments"][0]["status"] = "canceled"
        application.save_data(live)
        return {"resource": guest}
    monkeypatch.setattr(application, "_calendly_request", provider)
    application._crm_calendly_auto_review()
    data = application.load_data()
    assert not data["crm_contacts"]
    assert data["crm_calendly_appointments"][0]["status"] == "canceled"
    assert not data["crm_calendly_appointments"][0].get("identity_checked_at")


def test_same_name_on_the_wrong_sheet_cannot_authorize_a_followup(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    # Legacy imports can contain homonyms. The appointment was linked to the wrong one.
    data = application.load_data()
    data["crm_contacts"] = [
        {"id": "first", "prenom": "Lina", "nom": "Martin", "mail": "first@example.test", "telephone": "0600000001"},
        {"id": "second", "prenom": "Lina", "nom": "Martin", "mail": "lina@example.com", "telephone": "0612345678"},
    ]
    guest = calendly_payload()
    row = dict(legacy_calendar_row(guest, "first"), callback_checked=True)
    data["crm_calendly_appointments"] = [row]
    application.save_data(data)
    assert [a["id"] for a in application._crm_calendly_pending_reviews({})] == [row["id"]]
    for values in ({"response_status": "answered"}, {"enrollment_outcome": "enrolled"}):
        result = client.patch("/api/crm/calendly/appointments/booking-to-check", json={"contact_id": "first", **values})
        assert result.status_code == 409
    monkeypatch.setattr(application, "_calendly_request", lambda *a, **kw: {"resource": guest})
    monkeypatch.setattr(application, "_crm_send_appointment_followup", unexpected_followup)
    response = client.patch("/api/crm/calendly/appointments/booking-to-check",
                            json={"contact_id": "first", "response_status": "no_answer"})
    assert response.status_code == 409
    current = application.load_data()["crm_calendly_appointments"][0]
    assert current["contact_id"] == "second" and not current.get("response_status")


def test_no_answer_honors_provider_cancellation_and_rejects_subsequent_attempts(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin",
                          "mail": "lina@example.com", "telephone": "0612345678"}).get_json()
    guest = calendly_payload(status="canceled")
    guest.update(name="Liina Martin", cancellation={"reason": "Indisponible"})
    guest["scheduled_event"]["start_time"] = "2020-11-23T13:15:00Z"
    data = application.load_data()
    data["crm_calendly_appointments"] = [legacy_calendar_row(guest, contact["id"])]
    application.save_data(data)
    reads = []
    def provider(*args, **kwargs):
        reads.append(args)
        return {"resource": guest}
    monkeypatch.setattr(application, "_calendly_request", provider)
    monkeypatch.setattr(application, "_crm_send_appointment_followup", unexpected_followup)
    for _ in range(2):
        response = client.patch("/api/crm/calendly/appointments/booking-to-check",
                                json={"contact_id": contact["id"], "response_status": "no_answer"})
        assert response.status_code == 409 and "annulé" in response.get_json()["error"]
    assert len(reads) == 1
    current = application.load_data()["crm_calendly_appointments"][0]
    assert current["status"] == "canceled" and current["cancellation"] == guest["cancellation"]
    assert not current.get("response_status")


def test_new_booking_uses_the_callback_and_email_even_with_another_surname(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    owner = client.post("/api/crm/contacts", json={
        "prenom": "Lina", "nom": "Durand", "telephone": "06 99 88 77 66", "mail": "lina@example.test",
    }).get_json()
    # A legacy import contains another person with the same family email only.
    data = application.load_data()
    data["crm_contacts"].append({"id": "other", "prenom": "Autre", "nom": "Personne", "mail": "lina@example.test"})
    application.save_data(data)
    guest = calendly_payload("LINA@example.test")
    guest["name"] = "Martin Lina"
    guest["questions_and_answers"] = [{"question": "Téléphone", "answer": "0033 6 99 88 77 66"}]
    sends = []
    monkeypatch.setattr(application, "_crm_send_appointment_followup",
                        lambda data, contact, label: sends.append(contact["id"]) or {"sms": True, "email": True})
    for _ in range(2):
        assert signed_webhook(client, monkeypatch, "invitee.created", guest).get_json()["contact_id"] == owner["id"]
    assert sends == []
    data = application.load_data()
    assert len(data["crm_contacts"]) == 2
    row, = data["crm_calendly_appointments"]
    agenda, = client.get("/api/crm/calendly/appointments?date=2099-08-12").get_json()["appointments"]
    assert agenda["contact"]["id"] == owner["id"] and not agenda["identity_conflict"]
    assert agenda["contact"]["integration_score"]["score"] is not None
    fiche = client.get(f"/api/crm/contacts/{owner['id']}/calendly/appointments").get_json()
    assert fiche["contact_status"] == "RDV programmé" and fiche["appointments"][0]["id"] == row["id"]
    assert application._crm_next_phone_appointment(data, {}, application._crm_contact(data, owner["id"]))[1]["id"] == row["id"]
    for _ in range(2):
        result = client.patch(f"/api/crm/calendly/appointments/{row['id']}",
                              json={"contact_id": owner["id"], "response_status": "no_answer"})
        assert result.status_code == 200
    assert sends == [owner["id"]]


def test_exact_pair_skips_an_explicit_duplicate_but_not_other_disqualified_people():
    kept = {"id": "kept", "prenom": "Lina", "nom": "Martin", "telephone": "0699887766", "mail": "x@example.test"}
    duplicate = dict(kept, id="duplicate", statut="Disqualifié",
                     disqualification_reason="Autre", disqualification_detail="Doublon.")
    row = {"invitee_name": "Lina Mr Martin", "invitee_phone": "+33699887766", "callback_checked": True,
           "invitee_email": "X@example.test"}
    for contacts in ([kept, duplicate], [duplicate, kept]):
        assert application._crm_calendly_resolve_identity({"crm_contacts": contacts}, row, duplicate) == (kept, False)
    duplicate["disqualification_detail"] = "En recherche de financement"
    assert application._crm_calendly_resolve_identity({"crm_contacts": [kept, duplicate]}, row, kept) == (None, True)
    assert application._crm_calendly_resolve_identity({"crm_contacts": [duplicate]}, row) == (duplicate, False)
    assert application._crm_calendly_resolve_identity({"crm_contacts": [kept]}, dict(row, callback_checked=False)) == (None, True)
    assert application._crm_calendly_resolve_identity({"crm_contacts": [kept]}, dict(row, invitee_email="other@example.test")) == (None, True)


def test_pair_selects_the_matching_course_only_within_that_person():
    owner = {"id": "main", "formation": "APS", "nom": "Martin", "telephone": "0699887766", "mail": "x@example.test"}
    course = dict(owner, id="course", formation="A3P", meta_course_root_id="main")
    row = {"name": "Formation garde du corps (APR)", "invitee_name": "Autre Nom",
           "invitee_email": owner["mail"], "invitee_phone": owner["telephone"], "callback_checked": True}
    assert application._crm_calendly_resolve_identity({"crm_contacts": [owner, course]}, row, owner) == (course, False)
    course["telephone"] = "0611000000"
    assert application._crm_calendly_resolve_identity({"crm_contacts": [owner, course]}, row, course) == (owner, False)


def test_background_revisits_v2_conflicts_and_old_calls_without_creating_leads_or_resending(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    owner = client.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin",
                        "telephone": "0699887766", "mail": "lina@example.test"}).get_json()
    guests = {}
    rows = []
    for number, archived in enumerate((True, True, True, False, False), 1):
        guest = calendly_payload("unknown@example.test" if number == 3 else "lina@example.test")
        guest["name"] = "Autre Nom"
        guest["uri"] = f"{guest['event']}/invitees/INVITEE{number}"
        guest["questions_and_answers"] = [{"question": "Téléphone de rappel", "answer":
                                          "0633000000" if number == 3 else "0699887766"}]
        if archived:
            guest["scheduled_event"]["start_time"] = f"2020-01-0{number}T08:00:00Z"
        row = legacy_calendar_row(guest, owner["id"] if number == 2 else None)
        row["id"] = str(number)
        if number == 2:
            row.update(response_status="answered", response_status_updated_at="2020-01-02T08:15:00Z")
        if not archived:
            row.update(identity_conflict=True, identity_review_version=2, callback_checked=True,
                       invitee_phone="+33699887766")
        rows.append(row)
        guests[guest["uri"].rsplit("/", 1)[-1]] = guest
    data = application.load_data()
    data["crm_calendly_appointments"] = rows
    application.save_data(data)
    calls = []
    def provider(method, path, **kwargs):
        assert method == "GET"
        key = path.rsplit("/", 1)[-1]
        calls.append(key)
        return {"resource": guests[key]}
    monkeypatch.setattr(application, "_calendly_request", provider)
    monkeypatch.setattr(application, "_crm_send_appointment_followup", unexpected_followup)
    for _ in range(3):
        application._crm_calendly_auto_review()
    assert calls[:2] == ["INVITEE4", "INVITEE5"] and len(calls) == len(set(calls)) == 5
    result = application.load_data()
    assert len(result["crm_contacts"]) == 1
    checked = {r["id"]: r for r in result["crm_calendly_appointments"]}
    for number in ("1", "2", "4", "5"):
        assert checked[number]["contact_id"] == owner["id"] and not checked[number]["identity_conflict"]
        assert application._crm_calendly_display_owner(checked[number], owner)
    assert checked["2"]["response_status"] == "answered"
    assert checked["2"]["response_status_updated_at"] == "2020-01-02T08:15:00Z"
    assert not checked["3"].get("contact_id")


def test_an_explicit_duplicate_keeps_the_recorded_call_on_the_retained_dossier(tmp_path, monkeypatch):
    authenticated_client(tmp_path, monkeypatch)
    kept = {"id": "kept", "prenom": "Lina", "nom": "Martin", "telephone": "0699887766", "mail": "lina@example.test"}
    dup = dict(kept, id="duplicate", statut="Disqualifié", disqualification_reason="Doublon")
    data = application.load_data()
    data["crm_contacts"] = [dup, kept]
    guest = calendly_payload("lina@example.test")
    guest.update(name="Lina Martin", questions_and_answers=[{"question": "Téléphone", "answer": "+33699887766"}])
    row = legacy_calendar_row(guest, dup["id"])
    row.update(start_time="2020-01-01T08:00:00Z", response_status="no_answer", response_status_updated_at="2020-01-01T08:03:00Z")
    data["crm_calendly_appointments"] = [row]
    application.save_data(data)
    monkeypatch.setattr(application, "_calendly_request", lambda *a, **kw: {"resource": guest})
    monkeypatch.setattr(application, "_crm_send_appointment_followup", unexpected_followup)
    application._crm_calendly_auto_review()
    updated, = application.load_data()["crm_calendly_appointments"]
    assert updated["contact_id"] == "kept" and not updated["identity_conflict"]
    assert updated["response_status"] == "no_answer" and updated["response_status_updated_at"] == "2020-01-01T08:03:00Z"


def test_late_import_preserves_a_followup_planned_after_the_actual_booking(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    owner = client.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin",
                        "telephone": "0699887766", "mail": "lina@example.test"}).get_json()
    scheduled = client.patch(f"/api/crm/contacts/{owner['id']}",
                             json={"statut": "A relancer", "relance_date": "2099-08-10"})
    assert scheduled.status_code == 200
    data = application.load_data()
    contact = application._crm_contact(data, owner["id"])
    contact["relances"][0]["created_at"] = "2099-08-02T08:00:00Z"
    guest = calendly_payload("lina@example.test")
    guest.update(name="Autre Nom", created_at="2099-08-01T08:00:00Z",
                 questions_and_answers=[{"question": "Téléphone", "answer": "+33699887766"}])
    row = dict(legacy_calendar_row(guest), created_at="2099-08-03T08:00:00Z")
    data["crm_calendly_appointments"] = [row]
    application._crm_calendly_repair_invitee(data, row, guest)
    assert row["contact_id"] == contact["id"] and row["calendly_created_at"] == guest["created_at"]
    assert (contact["statut"], contact["relance_date"]) == ("A relancer", "2099-08-10")


def test_a_disqualified_course_duplicate_never_owns_a_followup_or_a_replayed_booking():
    old = {"id": "main", "formation": "APS", "prenom": "Lina", "nom": "Martin",
           "telephone": "0699887766", "mail": "lina@example.test"}
    dup = dict(old, id="dup", telephone="0611000000", formation="A3P", statut="Disqualifié",
               disqualification_reason="Doublon", meta_course_root_id="main")
    row = {"id": "one", "contact_id": "dup", "name": "Formation garde du corps (APR)",
           "start_time": "2099-11-25T09:30:00Z", "invitee_name": "Lina Martin", "invitee_email": old["mail"]}
    data = {"crm_contacts": [old, dup], "crm_calendly_appointments": [row]}
    assert application._crm_calendly_resolve_identity(data, row, dup) == (old, False)
    assert not application._crm_calendly_followup_owner(data, row, dup)
    assert application._crm_calendly_duplicate_booking_contact(data, dict(row, id="two")) is None
    agenda = application._crm_calendly_appointments_payload(data)["appointments"]
    assert not agenda[0]["contact"].get("id")


def test_new_course_does_not_take_another_dossiers_confirmed_booking_or_outcome():
    old = {"id": "old", "formation": "APS", "prenom": "Lina", "nom": "Martin",
           "telephone": "0699887766", "mail": "lina@example.test"}
    course = dict(old, id="course", formation="A3P", meta_course_root_id="old")
    other = dict(old, id="other", telephone="0611000000", mail="other@example.test")
    row = {"contact_id": "old", "name": "Formation garde du corps (APR)", "start_time": "2099-11-25T09:30:00Z",
           "invitee_name": "Autre Nom", "invitee_email": other["mail"], "invitee_phone": other["telephone"],
           "callback_checked": True, "response_status": "answered"}
    data = {"crm_contacts": [old, course, other], "crm_calendly_appointments": [row]}
    application._crm_move_course_bookings(data, course)
    assert (row["contact_id"], row["response_status"]) == ("old", "answered")


def test_partial_notification_never_replaces_a_confirmed_callback_with_a_reminder(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    owner = client.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin",
                        "telephone": "0699887766", "mail": "lina@example.test"}).get_json()
    guest = calendly_payload("lina@example.test")
    guest.update(name="Autre Nom", questions_and_answers=[{"question": "Téléphone", "answer": "+33699887766"}])
    assert signed_webhook(client, monkeypatch, "invitee.created", guest).get_json()["contact_id"] == owner["id"]
    del guest["questions_and_answers"], guest["scheduled_event"]["location"]
    guest["text_reminder_number"] = "+33611000000"
    assert signed_webhook(client, monkeypatch, "invitee.created", guest).get_json()["contact_id"] == owner["id"]
    current, = application.load_data()["crm_calendly_appointments"]
    assert current["invitee_phone"] == "+33699887766" and not current["identity_conflict"]
