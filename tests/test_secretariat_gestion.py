"""Parcours de gestion d’un appel, du catalogue à la réservation effective chez Calendly."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from crm_app import legacy_app as app


NUMBER = "+33 (0)6 39 98 00 42"
MAIL = "chloe@example.test"
EVENT = "https://api.calendly.com/event_types/test-event"


@pytest.fixture
def desk(monkeypatch, tmp_path):
    data = deepcopy(app.DEFAULT_DATA)
    data.update(crm_contacts=[{"id": "one", "nom": "Du-Pré", "prenom": "Chloé", "mail": MAIL,
                               "telephone": NUMBER, "formation": "APS", "statut": "Nouveaux", "notes": "confidentiel"},
                              {"id": "two", "nom": "Dupré", "prenom": "Yves", "mail": "yves@example.test",
                               "telephone": "06 39 98 01 58"}], secretariat_demandes=[], demandes=[])
    monkeypatch.setattr(app, "DATA_FILE", str(tmp_path / "data.json"))
    monkeypatch.setattr(app, "_load_data_snapshot", lambda: data)
    monkeypatch.setattr(app, "load_data", lambda: data)
    monkeypatch.setattr(app, "save_data", lambda *_: None)
    monkeypatch.setattr(app, "_secretariat_refresh_calendly_appointments", lambda *_: None)
    monkeypatch.setattr(app, "_send_secretariat_information_messages", lambda *_: {})
    monkeypatch.setattr(app, "_ensure_secretariat_quote", lambda *_: None)
    monkeypatch.setattr(app, "creer_piste_salesforce", lambda *_: None)
    app.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    return app.app.test_client(), data


def signin(client):
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
        session["external_access_csrf"] = "test-desk-token"
    return {"X-CSRF-Token": "test-desk-token"}


def test_four_coordinate_search_and_scoped_projection(desk):
    client, data = desk
    url = "/api/secretariat/coordonnees"
    assert client.post(url, json={"telephone": NUMBER}).status_code == 401
    headers = signin(client)
    assert client.post(url, json={"nom": "dup"}).status_code == 403
    for query in [{"nom": "Du Pre", "prenom": "Chl"}, {"prenom": "chloe"}, {"email": "CHLOE@"},
                  {"telephone": "0639980042"}, {"telephone": "0033 6 39 98 00 42"}, {"telephone": "+33 (0)6 39 98 00 42"}]:
        response = client.post(url, json=query, headers=headers)
        person, = response.json["candidates"]
        assert person == {"id": "one", "prenom": "Chloé", "nom": "Du-Pré", "telephone": NUMBER, "email": MAIL}
        assert response.headers["Cache-Control"] == "private, no-store"
    assert len(client.post(url, json={"nom": "dup"}, headers=headers).json["candidates"]) == 2
    assert client.post(url, json={"nom": "d"}, headers=headers).json["candidates"] == []
    assert client.post(url, json={"email": MAIL, "telephone": "0639980158"}, headers=headers).json["candidates"] == []
    assert len(data["crm_contacts"]) == 2


def test_external_desk_login_and_exact_coordinate_completion(desk):
    admin, _ = desk
    headers = signin(admin)
    account = {"name": "Accueil test", "email": "accueil@example.test", "password": "Test-password-7820", "desk_access": True}
    assert admin.post("/api/crm/external-accounts", json=account, headers=headers).status_code == 201
    operator = app.app.test_client()
    login = operator.post("/login?next=/secretariat/gestion", data={"email": account["email"], "password": account["password"]})
    assert login.location == "/secretariat/gestion"
    for path in (login.location, "/secretariat/message", "/static/secretariat_message.css", "/static/secretariat_message.js"):
        assert operator.get(path).status_code == 200
    with operator.session_transaction() as session:
        scoped = {"X-CSRF-Token": session["external_access_csrf"]}
    url = "/api/secretariat/coordonnees"
    for partial in ({"nom": "Dup"}, {"prenom": "chl"}, {"telephone": "0639"}, {"email": "chlo"}):
        result = operator.post(url, json=partial, headers=scoped)
        assert result.json["candidates"] and all(set(c) == {"display_name", "hint"} for c in result.json["candidates"])
        assert MAIL not in result.get_data(as_text=True)
    for known in ({"telephone": "0639980042"}, {"email": MAIL}):
        person, = operator.post(url, json=known, headers=scoped).json["candidates"]
        assert (person["nom"], person["prenom"], person["email"], person["telephone"]) == ("Du-Pré", "Chloé", MAIL, NUMBER)
    assert operator.get("/api/crm/calendly/appointments").status_code == 403
    assert operator.post(url, json={"telephone": NUMBER}).status_code == 403


@pytest.fixture
def calendar(monkeypatch):
    start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(minute=15, second=0, microsecond=0)
    event = {"active": True, "scheduling_url": app.SECRETARIAT_FORMATIONS["A3P"]["calendly"],
             "uri": EVENT, "name": "RDV téléphonique A3P", "pooling_type": "round_robin",
             "locations": [{"kind": "custom", "location": "Appel téléphonique"}],
             "custom_questions": [{"enabled": True, "position": 1, "type": "phone_number", "name": "Numéro de téléphone", "required": True}]}
    bookings = []
    def provider(method, path, **kwargs):
        if path == "/event_types/test-event":
            return {"resource": event}
        if path == "/event_type_available_times":
            assert kwargs["params"]["event_type"] == EVENT
            return {"collection": [{"status": "available", "start_time": start.isoformat()}]}
        assert (method, path) == ("POST", "/invitees")
        bookings.append(kwargs["json_body"])
        return {"resource": {"event": "https://api.calendly.com/scheduled_events/evt",
                             "uri": "https://api.calendly.com/scheduled_events/evt/invitees/person"}}
    monkeypatch.setattr(app, "_calendly_event_types_for_context", lambda _: [event])
    monkeypatch.setattr(app, "_calendly_request", provider)
    return bookings, start, event


def booking_payload(start):
    return {"call_key": "ab"*16, "formation": "A3P", "event_type": EVENT, "start_time": start.isoformat(),
            "prenom": "Chloé", "nom": "Du-Pré", "telephone": NUMBER, "email": MAIL}


@pytest.mark.parametrize("sms", ["0639980042", ""])
def test_booking_uses_explicit_sms_choice_and_pins_exact_appointment(desk, calendar, sms):
    client, data = desk
    calls, start, _ = calendar
    headers = signin(client)
    assert client.get("/api/secretariat/calendly/creneaux?formation=A3P").json["slots"] == [start.isoformat()]
    body = {**booking_payload(start), "sms": sms}
    url = "/api/secretariat/calendly/reserver"
    assert client.post(url, json=body).status_code == 403
    assert client.post(url, json={**body, "formation": "APS"}, headers=headers).status_code == 409
    first = client.post(url, json=body, headers=headers)
    assert first.status_code == 201
    assert client.post(url, json=body, headers=headers).json == first.json
    assert len(calls) == 1
    outgoing, = calls
    assert outgoing["invitee"].get("text_reminder_number") == ("+33639980042" if sms else None)
    assert outgoing["questions_and_answers"] == [{"question": "Numéro de téléphone", "position": 1, "answer": "+33639980042"}]
    assert "location" not in outgoing
    assert client.post(url, json={**body, "start_time": (start + timedelta(hours=2)).isoformat()}, headers=headers).status_code == 409
    other_time = start - timedelta(days=1)
    data["crm_calendly_appointments"] = [{"contact_id": "one", "name": "Ancien RDV téléphonique", "status": "active",
                                         "start_time": other_time.isoformat(), "invitee_uri": "another"}]
    receipt = first.json["appointment"]
    intake = {**body, "type": "formation", "notes": "Renseignements", "nom_famille": "Du-Pré", "nom": "Chloé Du-Pré",
              "desk_calendly_booked": True, "formation_date_status": "a_definir", "rdv": "faux horaire"}
    assert client.post("/api/secretariat/demandes", json={**intake, "email": "different@example.test"}, headers=headers).status_code == 409
    response = client.post("/api/secretariat/demandes", json=intake, headers=headers)
    assert response.status_code == 201
    recorded, = data["secretariat_demandes"]
    assert (recorded["rdv_date"], recorded["rdv_time"], recorded["rdv_invitee_uri"]) == (receipt["date"], receipt["time"], receipt["invitee_uri"])


def test_location_compatibility_and_uncertain_booking_are_not_replayed(desk, calendar, monkeypatch):
    client, _ = desk
    _, start, event = calendar
    headers = signin(client)
    body = booking_payload(start)
    posts = []
    def provider(method, path, **kw):
        if method == "GET":
            return {"resource": event}
        posts.append(kw["json_body"])
        if len(posts) == 1:
            raise app.CalendlyAPIError(400, {"title": "Invalid Argument", "details": [{"parameter": "location", "code": "required"}]})
        raise app.CalendlyTransportError(booking_uncertain=True)
    monkeypatch.setattr(app, "_calendly_request", provider)
    url = "/api/secretariat/calendly/reserver"
    assert client.post(url, json=body, headers=headers).status_code == 504
    assert "location" not in posts[0] and posts[1]["location"] == event["locations"][0]
    assert client.post(url, json=body, headers=headers).status_code == 409
    assert len(posts) == 2


def test_formation_first_browser_flow(desk, calendar):
    browser_api = pytest.importorskip("playwright.sync_api")
    client, _ = desk
    calls, _, _ = calendar
    signin(client)
    errors = []
    with browser_api.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch()
        except browser_api.Error as exc:
            pytest.skip(str(exc))
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))
            def route(req):
                url = urlsplit(req.request.url)
                if url.netloc != "desk.test":
                    return req.abort()
                response = client.open(url.path + ("?" + url.query if url.query else ""), method=req.request.method,
                                       data=req.request.post_data, headers={k:v for k,v in req.request.headers.items() if k in {"content-type", "x-csrf-token"}})
                req.fulfill(status=response.status_code, content_type=response.content_type, body=response.data)
            page.route("**/*", route)
            page.goto("https://desk.test/secretariat/gestion")
            assert page.locator('[data-step="3"]').is_visible()
            for term in ["A3P", "garde du corps", "APR", "bodyguard"]:
                page.fill("#formationSearch", term)
                assert page.locator("[data-formation-code]:visible").first.get_attribute("data-formation-code") == "A3P"
            page.press("#formationSearch", "Enter")
            assert page.locator('[data-step="5"]').is_visible()
            assert "garde du corps" in page.locator("#trainingPurpose").inner_text()
            assert "ministère de l’Intérieur (CNAPS)" in page.locator("#trainingChecks").inner_text()
            page.click("#nextDates")
            assert page.locator("#callerFields input").evaluate_all("(fields) => fields.map(f => f.id)") == ["nom","prenom","email","telephone"]
            page.fill("#nom", "Dup")
            page.locator("#callerCandidates button").first.wait_for()
            assert page.locator("#callerCandidates button").count() == 2
            page.fill("#nom", "")
            page.fill("#telephone", "0639980042")
            page.locator("#callerCandidates button", has_text="Chloé").click()
            assert page.input_value("#prenom") == "Chloé" and page.input_value("#email") == MAIL
            page.click("#callerSubmit")
            assert page.locator("#qualificationForm").evaluate("f => getComputedStyle(f.firstElementChild).gridTemplateColumns.split(' ').length") == 1
            page.get_by_label("Aide sur l’Identité Numérique La Poste", exact=True).click()
            assert page.get_by_text("Elle permet de valider une inscription CPF", exact=False).is_visible()
            for key in ["cpf_consulte", "france_travail", "financement_perso", "identite_numerique", "cnaps_ok", "devis"]:
                page.check(f'[name="{key}"][value="OUI" i]' if key == "cnaps_ok" else f'[name="{key}"][value="NON"]')
            page.click("#nextQualification")
            page.check('[name="wants_rdv"][value="oui"]')
            page.locator("#deskSlot optgroup option").wait_for(state="attached")
            assert page.input_value("#deskSmsNumber") == "+33639980042"
            assert page.locator('#deskEventQuestions input[type=tel]').input_value() == "+33639980042"
            page.check("#deskSmsConsent")
            page.select_option("#deskSlot", index=1)
            page.click("#bookDeskSlot")
            page.get_by_text("Rendez-vous réservé le ", exact=False).wait_for()
            page.click("#openCallNotes")
            assert page.locator("#prepareCallback").is_hidden()
            page.click("#resumeCall")
            page.click("#reviewRequest")
            assert page.locator('[data-step="7"]').is_visible()
            assert len(calls) == 1 and calls[0]["invitee"]["text_reminder_number"] == "+33639980042"
            assert errors == []
        finally:
            browser.close()
