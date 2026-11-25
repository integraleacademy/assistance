"""Parcours de gestion d’un appel, du catalogue à la réservation effective chez Calendly."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

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
    assert b'id="chooseSmsBooking"' in operator.get(login.location).data
    assert b'id="chooseSmsBooking"' not in app.app.test_client().get(login.location).data
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


@pytest.mark.parametrize("booking_mode", ["calendly", "sms", "return_to_calendly"])
def test_formation_first_browser_flow(desk, calendar, booking_mode):
    browser_api = pytest.importorskip("playwright.sync_api")
    client, _ = desk
    calls, _, event = calendar
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
            requested_paths = []
            def route(req):
                url = urlsplit(req.request.url)
                if url.netloc == "assets.calendly.com" and url.path.endswith("/widget.js"):
                    return req.fulfill(content_type="application/javascript", body="""
                        window.Calendly = {initInlineWidget({url, parentElement}) {
                          const iframe = document.createElement('iframe');
                          iframe.src = url;
                          parentElement.append(iframe);
                        }};
                    """)
                if url.netloc == "calendly.com":
                    return req.fulfill(content_type="text/html", body="""
                        <button onclick="parent.postMessage({event: 'calendly.event_scheduled'}, 'https://desk.test')">Confirmer</button>
                    """)
                if url.netloc != "desk.test":
                    return req.abort()
                requested_paths.append(url.path)
                if url.path.endswith("/prefill-schema"):
                    return req.fulfill(json=app._secretariat_booking_schema(event["scheduling_url"], [event]))
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
            assert app.SECURITY_AUTHORITY in page.locator("#trainingChecks").inner_text()
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
            assert page.get_by_text("Avant le rendez-vous, ouvrez l’application", exact=False).is_visible()
            questions = page.locator('#qualificationForm [id$="-question"]').all_text_contents()
            assert questions.index("Êtes-vous inscrit à France Travail ?") < questions.index("Souhaitez-vous demander un financement à France Travail ?")
            for key in ["cpf_consulte", "inscrit_ft", "france_travail", "financement_perso", "identite_numerique", "cnaps_ok", "devis"]:
                page.check(f'[name="{key}"][value="OUI" i]' if key == "cnaps_ok" else f'[name="{key}"][value="NON"]')
            page.click("#nextQualification")
            page.check('[name="wants_rdv"][value="oui"]')
            assert page.input_value("#deskSmsNumber") == "+33639980042"
            assert page.locator("#calendlyInline iframe").count() == 0
            if booking_mode == "calendly":
                page.click("#skipSmsBooking")
            else:
                page.click("#chooseSmsBooking")
                page.locator("#deskSlot optgroup option").wait_for(state="attached")
                assert page.locator("#calendlyInline iframe").count() == 0
                assert page.input_value("#deskSmsNumber") == "+33639980042"
                assert page.locator('#deskEventQuestions input[type=tel]').input_value() == "+33639980042"
                if booking_mode == "return_to_calendly":
                    page.fill("#deskSmsNumber", "+33601020304")
                    page.click("#hostedBooking")
                    assert page.locator("#hostedSmsHint").is_visible()
                    assert page.input_value("#deskSmsNumber") == "+33601020304"
            if booking_mode == "sms":
                page.select_option("#deskSlot", index=1)
                page.click("#bookDeskSlot")
                page.get_by_text("Rendez-vous réservé le ", exact=False).wait_for()
                assert len(calls) == 1 and calls[0]["invitee"]["text_reminder_number"] == "+33639980042"
            else:
                frame = page.locator("#calendlyInline iframe")
                frame.wait_for()
                assert page.locator("#directBooking").is_hidden() and page.locator("#chooseSmsBooking").is_hidden()
                url = urlsplit(frame.get_attribute("src"))
                fields = parse_qs(url.query)
                assert url.netloc == "calendly.com" and url.path == "/integraleacademy/apr"
                assert (fields["first_name"], fields["email"], fields["a2"]) == (["Chloé"], [MAIL], ["+33639980042"])
                confirm = page.frame_locator("#calendlyInline iframe").get_by_role("button", name="Confirmer", include_hidden=True)
                if booking_mode == "calendly":
                    assert "/api/secretariat/calendly/creneaux" not in requested_paths
                    # Calendly can confirm after the operator has already collapsed its iframe.
                    page.check('[name="wants_rdv"][value="non"]')
                    confirm.evaluate("button => button.click()")
                else:
                    confirm.click()
                page.get_by_text("Rendez-vous réservé. Vous pouvez continuer.", exact=True).wait_for()
                assert page.locator('[name="wants_rdv"][value="oui"]').is_checked()
                assert calls == []
            assert page.locator('[name="wants_rdv"][value="oui"]').is_disabled()
            page.click("#openCallNotes")
            assert page.locator("#prepareCallback").is_hidden()
            page.click("#resumeCall")
            page.click("#reviewRequest")
            assert page.locator('[data-step="7"]').is_visible()
            assert errors == []
        finally:
            browser.close()


@pytest.mark.parametrize(("known", "registered", "wants_funding"), [
    (False, "OUI", "NON"), (True, "NON", "OUI"), (True, "", "OUI"),
])
def test_desk_registration_reaches_the_crm_independently_of_funding(desk, known, registered, wants_funding):
    client, data = desk
    headers = signin(client)
    email = MAIL if known else "lea@example.test"
    payload = {"type": "formation", "formation": "APS", "formation_date_status": "a_definir",
               "nom": "Chloé Du-Pré" if known else "Léa Durand", "nom_famille": "Du-Pré" if known else "Durand",
               "prenom": "Chloé" if known else "Léa", "email": email,
               "telephone": NUMBER if known else "0601020304", "notes": "Demande de renseignements",
               "france_travail": wants_funding}
    if known and registered == "NON":
        existing = next(c for c in data["crm_contacts"] if c["mail"] == MAIL)
        existing["inscrit_ft"] = "OUI"
    if registered:
        payload["inscrit_ft"] = registered
    response = client.post("/api/secretariat/demandes", headers=headers, json=payload)
    assert response.status_code == 201
    entry, = data["secretariat_demandes"]
    contact = next(c for c in data["crm_contacts"] if c["id"] == entry["crm_contact_id"])
    assert (entry["inscrit_ft"], contact.get("inscrit_ft") or "") == (registered, registered)
    assert contact["financement_ft"] == wants_funding
    assert not contact.get("statut_demande_financement_ft")
    if registered:
        assert f"Inscription à France Travail déclarée lors de l’appel : {registered.lower()}" in contact["publications"][0]["texte"]


def test_aps_prerequisites_and_residence_example_for_the_paris_date(desk):
    from datetime import date
    from html import unescape
    from secretariat_catalogue import DESK_ADMISSION, authority_wording, residence_permit_help

    client, _ = desk
    page = unescape(client.get("/secretariat/gestion").get_data(as_text=True))
    guide = " ".join(detail for _, detail in DESK_ADMISSION["APS"])
    for point in ("le centre de formation prépare et dépose", "test de compréhension", "B1", "bulletin n° 2",
                  "cinq ans", "recto et du verso", "attestation signée de l’hébergeant"):
        assert point in guide
    assert "Êtes-vous inscrit à France Travail ?" in page
    assert app.FUNDING_REQUEST_HELP in page
    assert residence_permit_help(datetime.now(app.secretariat_calls.PARIS).date()) in page
    assert "25/11/2021 au plus tard" in residence_permit_help(date(2026, 11, 25))
    assert "28/02/2023 au plus tard" in residence_permit_help(date(2028, 2, 29))
    for name in ("CNAPS", "CNAPS (ministère de l’Intérieur)", "ministère de l’Intérieur (CNAPS)", app.SECURITY_AUTHORITY):
        assert authority_wording(f"Auprès du {name}.") == f"Auprès du {app.SECURITY_AUTHORITY}."


def test_desk_calendar_accepts_local_aps_periods_and_preserves_older_records(desk):
    client, data = desk
    signin(client)
    label = "Du 5 février au 11 mars 2099"
    data["formation_sessions"] = {"cote_azur": {"APS": [{"label": label, "date_examen": "2099-03-12"}]}}
    form = {"action": "update", "centre": "cote_azur", "formation": "APS", "idx": "0", "label": label,
            "remoteStartDate": "2099-02-05", "remoteEndDate": "2099-02-16",
            "inPersonStartDate": "2099-02-17", "inPersonEndDate": "2099-03-11"}
    assert client.post("/admin/formation-sessions", data=form).status_code == 302
    course = next(item for item in app._secretariat_catalogue(data) if item["code"] == "APS")
    session, = course["centres"][0]["sessions"]
    assert session["periods"] == "Distanciel du 05/02/2099 au 16/02/2099 · Présentiel du 17/02/2099 au 11/03/2099"
    assert session["date_examen"] == "2099-03-12"
    assert client.post("/admin/formation-sessions", data={**form, "inPersonEndDate": "2099-01-31"}).status_code == 400
    assert data["formation_sessions"]["cote_azur"]["APS"][0]["inPersonEndDate"] == "2099-03-11"
