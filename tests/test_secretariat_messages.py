import datetime as dt
from urllib.parse import urlsplit

import pytest

from crm_app import legacy_app as app
from secretariat_calls import PARIS


def as_staff(email):
    client = app.app.test_client()
    with client.session_transaction() as session:
        session["user_email"] = email
    return client


def csrf(client):
    client.get("/secretariat")
    with client.session_transaction() as session:
        return {"X-CSRF-Token": session["external_access_csrf"]}


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DATA_FILE", str(tmp_path / "messages.json"))
    app.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    now = dt.datetime.now(PARIS)
    yesterday = now.date() - dt.timedelta(days=1)
    start = PARIS.localize(dt.datetime.combine(yesterday, dt.time(15))).isoformat()
    data = app.load_data()
    data["crm_contacts"] = [{"id": "d1", "prenom": "Camille", "nom": "Test", "formation": "BTS_MOS",
                             "telephone": "0600000011", "mail": "person@example.test"},
                            {"id": "d2", "prenom": "Anne", "nom": "Test", "telephone": "0600000022"}]
    meeting = {"id": "a1", "name": "Appel formation", "status": "active", "invitee_name": "Camille Test",
               "start_time": start, "contact_id": "d1", "host_email": "cassandre@integraleacademy.com",
               "host_name": "Cassandre MENARD", "invitee_phone": "0600000011"}
    data["crm_calendly_appointments"] = [meeting, dict(meeting, id="future", start_time=(now + dt.timedelta(days=1)).isoformat())]
    app.save_data(data)
    admin = as_staff("clement@integraleacademy.com")
    login = {"email": "accueil@example.test", "password": "fixture-account-1234"}
    assert admin.post("/api/crm/external-accounts", json={**login, "name": "Accueil test", "desk_access": True}, headers=csrf(admin)).status_code == 201
    operator = app.app.test_client()
    assert operator.post("/login?next=/secretariat", data=login).location == "/secretariat"
    return operator, csrf(operator), as_staff("cassandre@integraleacademy.com"), yesterday.isoformat()


IDENTITY = {"nom": "Test", "prenom": "Camille", "telephone": "0600000011", "confirmed_contact_id": "d1", "verify_identity": True}
AGENDA = "/api/secretariat/agenda"
WAIT = AGENDA + "/attente"
LOOKUP = "/api/secretariat/crm-contact"
QUEUE = "/api/crm/callback-requests"


@pytest.mark.parametrize(("formation", "expected"), [
    ("Parcours sécurité en alternance", "https://calendly.com/integraleacademy/parcourssecurite"),
    ("Formation à préciser", app.SECRETARIAT_GENERIC_CALENDLY_URL),
])
def test_followup_booking_opens_the_course_calendar_or_school_directory(desk, formation, expected):
    operator, headers, _, _ = desk
    data = app.load_data()
    data["crm_contacts"][0]["formation"] = formation
    app.save_data(data)
    summary = operator.post(LOOKUP, json=IDENTITY, headers=headers).json["summary"]
    assert summary["phone_booking_url"] == expected


def test_waiting_alert_is_scoped_idempotent_and_tracks_the_original_slot(desk):
    operator, headers, owner, day = desk
    rows = operator.get(AGENDA, query_string={"date": day}).json["appointments"]
    assert len(rows) == 1 and "0600000011" not in str(rows)
    action = rows[0]["waiting"]
    assert action["recipient"] == "Cassandre MENARD"
    body = {"day": day, "key": action["key"], "recipient": "Aurélie CHAUSSEZ"}
    assert app.app.test_client().post(WAIT, json=body).status_code == 401
    assert operator.post(WAIT, json=body).status_code == 403
    for expected in (201, 200):
        result = operator.post(WAIT, json=body, headers=headers)
        assert result.status_code == expected and result.json["recipient"] == "Cassandre MENARD"
    assert operator.get(AGENDA, query_string={"date": day}).json["appointments"][0]["waiting"]["state"] == "sent"
    queue = owner.get(QUEUE).json["callback_requests"]
    assert len(queue) == 1 and queue[0]["rdv_time"] == "15:00" and queue[0]["crm_contact_id"] == "d1"
    assert queue[0]["rdv_date"] == dt.date.fromisoformat(day).strftime("%d/%m/%Y")
    notification = owner.get("/api/crm/notifications").json[0]
    assert notification["callback_request_id"] == queue[0]["id"] and "15:00" in notification["text"]
    assert owner.get("/api/crm/notifications?kind=waiting_call").json == [notification]
    assert not as_staff("aurelie@integraleacademy.com").get("/api/crm/notifications").json
    stale = app.load_data()
    owner.patch("/api/crm/notifications", json={"id": notification["id"]})
    app.save_data(stale)
    assert owner.get("/api/crm/notifications").json[0]["read"]
    assert owner.get("/api/crm/notifications?kind=waiting_call").json == []
    assert len(app.load_data()["crm_contacts"]) == 2


def test_two_operators_signalling_together_create_one_callback(desk):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    _, _, _, day = desk
    operators = [as_staff("clement@integraleacademy.com"), as_staff("aurelie@integraleacademy.com")]
    sessions = [(client, csrf(client)) for client in operators]
    meeting = operators[0].get(AGENDA, query_string={"date": day}).json["appointments"][0]
    start = Barrier(2)
    def send(session):
        client, headers = session
        start.wait(timeout=5)
        return client.post(WAIT, json={"key": meeting["waiting"]["key"], "day": day}, headers=headers).status_code
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(send, sessions)) == [200, 201]
    assert len(app.load_data()["secretariat_demandes"]) == 1


def test_changed_slots_and_unknown_hosts_are_handled_at_send_time(desk):
    operator, headers, _, day = desk
    data = app.load_data()
    meeting = data["crm_calendly_appointments"][0]
    meeting.update(host_email="external@example.test", host_name="Accueil Calendly")
    app.save_data(data)
    key = operator.get(AGENDA, query_string={"date": day}).json["appointments"][0]["waiting"]["key"]
    body = {"key": key, "day": day}
    assert operator.post(WAIT, json=body, headers=headers).status_code == 400
    future = (dt.datetime.now(PARIS).date() + dt.timedelta(days=1)).isoformat()
    assert "waiting" not in operator.get(AGENDA, query_string={"date": future}).json["appointments"][0]
    for changes in ({"response_status": "answered"}, {"status": "canceled"}, {"start_time": day + "T17:00:00+01:00"}):
        next_data = app.load_data()
        next_data["crm_calendly_appointments"][0] = dict(meeting, **changes)
        app.save_data(next_data)
        assert operator.post(WAIT, json={**body, "recipient": "Aurélie CHAUSSEZ"}, headers=headers).status_code == 409
    app.save_data(data)
    result = operator.post(WAIT, json={**body, "recipient": "Aurélie CHAUSSEZ"}, headers=headers)
    assert result.status_code == 201 and result.json["recipient"] == "Aurélie CHAUSSEZ"


def test_message_can_be_claimed_supplemented_and_closed_with_an_explicit_outcome(desk):
    operator, headers, owner, _ = desk
    data = app.load_data()
    entry = {"id": "msg", "type": "autre", "crm_contact_id": "d1", "notes": "note historique réservée",
             "callback_recipient": "Cassandre MENARD", "callback_comment": "commentaire interne",
             "created_at": "2026-05-02T09:00:00+02:00"}
    data["secretariat_demandes"] = [entry, dict(entry, id="closed", created_at="2026-05-01T09:00:00+02:00",
        callback_status="processed", callback_processed_at="2026-05-01T11:00:00+02:00", callback_processed_by="Équipe")]
    app.save_data(data)
    messages = operator.post(LOOKUP, json=IDENTITY, headers=headers).json["summary"]["messages"]
    assert [m["id"] for m in messages] == ["msg", "closed"]
    assert "réservée" not in str(messages) and "commentaire interne" not in str(messages)
    assert messages[1]["events"][0]["label"] == "Traitement terminé"
    claim = owner.patch(QUEUE + "/msg", json={"status": "in_progress"}).json
    assert claim["callback_pending_count"] == 1 and claim["request"]["claimed_by"] == "Cassandre MENARD"
    supplement = "/api/secretariat/messages/msg/complements"
    body = {**IDENTITY, "call_key": "a" * 32, "note": "Disponible après 16 h."}
    assert operator.post(supplement, json={**body, "prenom": "Inconnu"}, headers=headers).status_code == 403
    other = {**body, "prenom": "Anne", "telephone": "0600000022", "confirmed_contact_id": "d2"}
    assert operator.post(supplement, json=other, headers=headers).status_code == 404
    stale = app.load_data()
    assert operator.post(supplement, json=body, headers=headers).status_code == 200
    assert operator.post(supplement, json=body, headers=headers).status_code == 200
    assert operator.post(supplement, json={**body, "note": "Autre texte"}, headers=headers).status_code == 409
    app.save_data(stale)
    row = next(r for r in owner.get(QUEUE).json["callback_requests"] if r["id"] == "msg")
    assert sum(e["text"] == body["note"] for e in row["events"]) == 1
    result = owner.patch(QUEUE + "/msg", json={"status": "in_progress", "outcome": "no_answer", "call_key": "b" * 32})
    assert result.json["callback_pending_count"] == 1
    owner.patch(QUEUE + "/msg", json={"status": "in_progress", "outcome": "no_answer", "call_key": "b" * 32})
    result = owner.patch(QUEUE + "/msg", json={"status": "processed", "outcome": "reached"})
    assert result.json["callback_pending_count"] == 0
    conflict = operator.post(supplement, json={**body, "call_key": "c" * 32}, headers=headers)
    assert conflict.status_code == 409
    assert "réservez un rendez-vous téléphonique" in conflict.json["error"]
    labels = [e["label"] for e in conflict.json["messages"][0]["events"]]
    assert labels == ["Pris en charge", "Complément transmis", "Appel tenté, sans réponse", "Personne rappelée et jointe"]
    assert operator.post(supplement, json=body, headers=headers).status_code == 200
    owner.patch(QUEUE + "/msg", json={"status": "pending"})
    reopened = operator.post(LOOKUP, json=IDENTITY, headers=headers).json["summary"]["messages"][0]
    assert reopened["status"] == "pending" and reopened["events"][-1]["label"] == "Demande rouverte"


def test_secretary_and_adviser_complete_the_same_call_in_their_own_screens(desk):
    playwright = pytest.importorskip("playwright.sync_api")
    operator, _, owner, day = desk
    with playwright.sync_playwright() as engine:
        try:
            browser = engine.chromium.launch()
        except playwright.Error as exc:
            pytest.skip(f"Chromium indisponible : {exc}")
        try:
            def tab(client, width):
                page = browser.new_page(viewport={"width": width, "height": 890}, locale="fr-FR")
                def serve(route):
                    req, url = route.request, urlsplit(route.request.url)
                    if url.netloc != "desk.test":
                        return route.abort()
                    response = client.open(url.path, query_string=url.query, method=req.method,
                        json=req.post_data_json if req.method in {"POST", "PATCH"} else None,
                        headers={"X-CSRF-Token": req.headers.get("x-csrf-token", "")})
                    route.fulfill(status=response.status_code, content_type=response.content_type, body=response.data)
                page.route("**/*", serve)
                return page
            reception = tab(operator, 390)
            reception.goto("https://desk.test/secretariat")
            reception.click("#agendaToggle")
            reception.fill("#agendaDay", day)
            reception.locator('#agendaList .meeting-host [data-advisor="cassandre"]').wait_for()
            row = reception.locator("#agendaList .meeting-owner").first
            face, button = (row.locator(s).bounding_box() for s in (".team-portrait", ".waiting-action"))
            assert abs((face["y"] + face["height"]/2) - (button["y"] + button["height"]/2)) <= 1
            reception.get_by_role("button", name="Signaler l’attente de Camille", exact=False).click()
            reception.wait_for_selector(".waiting-sent")
            assert "Cassandre MENARD" in reception.locator(".waiting-sent").inner_text()
            assert reception.locator("#agendaList .waiting-action").count() == 0
            assert reception.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
            reception.click("#agendaToggle")

            adviser = tab(owner, 1440)
            adviser.goto("https://desk.test/crm/notifications")
            adviser.locator("#waitingCallNotice a").click()
            adviser.get_by_role("button", name="Prendre en charge").click()
            adviser.wait_for_selector(".callback-request-status:has-text('Pris en charge')")

            def verify_caller():
                reception.click('[data-request="suivi"]')
                reception.fill("#telephone", IDENTITY["telephone"])
                reception.wait_for_selector("#useCallerMatch")
                reception.fill("#prenom", "Camille")
                reception.fill("#nom", "Test")
                reception.click("#useCallerMatch")
                reception.wait_for_selector(".desk-message")
            verify_caller()
            assert "Pris en charge · Cassandre MENARD" in reception.locator("#deskMessages").inner_text()
            adviser.locator(".callback-results summary").click()
            adviser.get_by_role("button", name="Appel sans réponse").click()
            adviser.wait_for_selector(".callback-request-status:has-text('Appel sans réponse')")
            reception.click("#refreshDeskMessages")
            reception.wait_for_selector("#deskMessages:has-text('Appel tenté, sans réponse')")
            reception.get_by_role("button", name="Compléter ce message").click()
            reception.fill("#supplementNote", "Appeler après 16 h.")
            reception.click("#sendSupplement")
            reception.wait_for_selector('#callReceipt:has-text("Appeler après 16 h.")')
            assert len(app.load_data()["secretariat_demandes"]) == 1
            adviser.reload()
            adviser.wait_for_selector('.callback-request-note:has-text("Appeler après 16 h.")')
            reception.reload()
            verify_caller()
            reception.get_by_role("button", name="Compléter ce message").click()
            reception.fill("#supplementNote", "Question sur la convocation.")
            adviser.locator(".callback-results summary").click()
            adviser.get_by_role("button", name="Personne rappelée et jointe").click()
            adviser.get_by_role("button", name="Traitées").click()
            adviser.wait_for_selector(".callback-request-status:has-text('Personne rappelée et jointe')")
            reception.click("#sendSupplement")
            reception.wait_for_selector("#supplementAppointment")
            assert not reception.locator("#sendSupplement").is_visible()
            assert reception.locator("#supplementNote").input_value() == "Question sur la convocation."
            agenda_url = app.SECRETARIAT_FORMATIONS["BTS_MOS"]["calendly"]
            assert reception.locator("#supplementAppointment").get_attribute("href") == agenda_url
            reception.click("#closeSupplement")
            assert "Personne rappelée et jointe" in reception.locator("#deskMessages").inner_text()
            assert reception.get_by_role("button", name="Demander un nouveau rappel").count() == 0
            assert reception.locator(".desk-message button").count() == 0
            reception.context.route("https://calendly.com/**", lambda route: route.fulfill(body="Agenda de test"))
            with reception.expect_popup() as opened:
                reception.get_by_role("link", name="Réserver un rendez-vous téléphonique").click()
            opened.value.wait_for_url(agenda_url)
            assert reception.locator("#verifiedDossier").is_visible()
            entries = app.load_data()["secretariat_demandes"]
            assert len(entries) == 1 and entries[0]["callback_status"] == "processed"
            assert sum(e["kind"] == "supplement" for e in entries[0]["callback_events"]) == 1
        finally:
            browser.close()
