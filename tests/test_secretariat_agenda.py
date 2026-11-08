"""Reception desk views use shared CRM records and scoped, verified identities."""

import copy
from html.parser import HTMLParser

import pytest

import crm_cnaps_tracking
import crm_external_access as access
from crm_app import legacy_app as app


@pytest.fixture
def desk(monkeypatch, tmp_path):
    data = copy.deepcopy(app.DEFAULT_DATA)
    data["crm_contacts"] = [{"id": "test", "nom": "Du-Pré", "prenom": "Chloé", "mail": "chloe@example.test",
        "telephone": "0600000000", "formation": "APS", "statut": "Disqualifié",
        "disqualification_reason": "Projet reporté", "reactivation_date": "2027-01-10",
        "lieu": "Paris", "dates_formation": "Février 2027", "commercial": "Conseiller Test",
        "activities": [], "publications": [], "financement_ft": "OUI", "prochaine_action_manuelle": "Faire le point en janvier"}]
    data["crm_cnaps_scoring_snapshots"] = {"test": {"raw_status": "TRANSMIS", "last_checked_at": "2026-11-03T11:20:00Z"}}
    data["secretariat_demandes"] = []
    data["crm_calendly_appointments"] = []
    monkeypatch.setattr(app, "DATA_FILE", str(tmp_path / "desk.json"))
    monkeypatch.setattr(app, "_load_data_snapshot", lambda: data)
    monkeypatch.setattr(app, "load_data", lambda: data)
    monkeypatch.setattr(app, "save_data", lambda *_: None)
    monkeypatch.setattr(app, "_CRM_REGLEMENTAIRE_CACHE", {})
    app.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    return app.app.test_client(), data


def sign_in(client):
    with client.session_transaction() as s:
        s["user_email"] = "clement@integraleacademy.com"


def test_home_action_order_and_directory(desk):
    class Menu(HTMLParser):
        def __init__(self):
            super().__init__()
            self.keys = []
            self.links = []

        def handle_starttag(self, tag, attrs):
            fields = dict(attrs)
            if tag == "button" and ("data-request" in fields or fields.get("id") == "browseCourses"):
                self.keys.append(fields.get("data-request") or fields["id"])
            if tag == "a":
                self.links.append(fields.get("href"))

    client, _ = desk
    body = client.get("/secretariat").get_data(as_text=True)
    menu = Menu()
    menu.feed(body)
    assert menu.keys == ["formation", "suivi", "autre", "browseCourses"]
    assert all(label in body for label in ["Nouveau prospect", "Suivre un dossier", "Transmettre un message", "Les formations en détails"])
    assert set(["tel:+33487830615", "tel:+33769390457", "tel:+33487830616", "tel:+33743582264",
                "mailto:aurelie@integraleacademy.com", "mailto:cassandre@integraleacademy.com",
                "mailto:ecole@integraleacademy.com"]) <= set(menu.links)
    assert "54 chemin du Carreou" in body and "83480 PUGET SUR ARGENS" in body


def test_laptop_step_navigation_stays_usable_beside_the_agenda(desk):
    playwright = pytest.importorskip("playwright.sync_api")
    client, _ = desk
    with playwright.sync_playwright() as engine:
        try:
            browser = engine.chromium.launch()
        except playwright.Error as error:
            pytest.skip(f"Chromium indisponible : {error}")
        try:
            page = browser.new_page(viewport={"width": 1024, "height": 800})
            def serve(route):
                from urllib.parse import urlsplit
                url = urlsplit(route.request.url)
                if url.netloc != "desk.test":
                    return route.abort()
                response = client.get(url.path)
                route.fulfill(status=response.status_code, body=response.data, content_type=response.content_type)

            page.route("**/*", serve)
            page.goto("https://desk.test/secretariat")
            page.click('[data-request="suivi"]')
            link = page.locator('[data-go-step="1"]')
            assert link.is_visible() and link.is_enabled()
            assert page.locator(".desk-agenda").bounding_box()["x"] > page.locator(".card").bounding_box()["x"]
            link.click()
            assert page.locator('[data-step="1"]').is_visible()
        finally:
            browser.close()


def test_day_matches_the_crm_active_slots_across_types_reschedules_midnight_and_ties(desk):
    client, data = desk
    def meeting(start, **fields):
        return {"name": "Rendez-vous téléphonique", "start_time": start, "host_name": "Conseiller Test",
                "contact_id": "test", "invitee_email": "private@example.test", "cancel_url": "https://private.invalid", **fields}
    data["crm_calendly_appointments"] = [
        meeting("2026-03-29T13:00:00Z", status="canceled", rescheduled=True),
        meeting("2026-03-28T23:30:00Z", response_status="answered", invitee_name="Ancien nom"),
        meeting("2026-03-29T10:00:00Z", name="Dossier", response_status="no_answer"),
        meeting("2026-03-29T12:00:00Z", name="Visio", location={"kind": "zoom"}),
        meeting("2026-03-29T14:00:00Z", contact_id="", invitee_name="Zoé", status="active", rescheduled=True),
        meeting("2026-03-29T14:00:00Z", contact_id="", invitee_name="Alex"),
        meeting("2026-03-29T15:00:00Z", status="cancelled"),
        meeting("2026-03-29T22:00:00Z"), meeting("bad"),
    ]
    assert client.get("/api/secretariat/agenda?date=2026-03-29").status_code == 401
    sign_in(client)
    reference = client.get("/api/crm/calendly/appointments").json["appointments"]
    assert len(reference) == 9  # the full CRM history retains the old and cancelled events
    response = client.get("/api/secretariat/agenda?date=2026-03-29")
    assert response.status_code == 200 and "no-store" in response.headers["Cache-Control"]
    rows = response.json["appointments"]
    assert [(r["time"], r["state"]) for r in rows] == [
        ("00:30", "repondu"), ("12:00", "sans_reponse"), ("14:00", "sans_resultat"),
        ("16:00", "sans_resultat"), ("16:00", "sans_resultat")]
    assert [r["person"] for r in rows] == ["Chloé DU-PRÉ"] * 3 + ["Zoé", "Alex"]
    slots = sorted((a for a in reference if (a["calendar_slot"] or {}).get("day") == "2026-03-29"),
                   key=lambda a: a["calendar_slot"]["start"])
    assert [(r["time"], r["person"], r["host"], r["subject"]) for r in rows] == [
        (a["calendar_slot"]["time"], a["calendar_slot"]["person"], a["host_name"], a["name"]) for a in slots]
    assert "private" not in response.get_data(as_text=True)
    assert all(set(r) == {"time", "person", "host", "subject", "state"} for r in rows)
    assert client.get("/api/secretariat/agenda?date=2026-03-30").json["appointments"][0]["time"] == "00:00"
    assert client.get("/api/secretariat/agenda?date=2026-02-30").status_code == 400


def test_confirmed_dossier_and_live_cnaps_are_separate_from_declared_card(desk, monkeypatch):
    client, data = desk
    person = data["crm_contacts"][0]
    person["carte_pro"] = "OUI"
    lookup = {"telephone": person["telephone"], "confirmed_contact_id": "test"}
    assert "summary" not in client.post("/api/secretariat/crm-contact", json=lookup).json
    assert client.post("/api/secretariat/suivi-cnaps", json=lookup).status_code == 401
    sign_in(client)
    summary = client.post("/api/secretariat/crm-contact", json=lookup).json["summary"]
    assert (summary["statut"], summary["motif_disqualification"], summary["reactivation"]) == ("Disqualifié", "Projet reporté", "10/01/2027")
    assert (summary["centre"], summary["session"], summary["conseiller"]) == ("Paris", "Février 2027", "Conseiller Test")
    assert summary["cnaps"]["status"] == "Transmis au CNAPS" and summary["cnaps"]["card"] == "Oui"
    assert "03/11/2026" in summary["cnaps"]["checked"]
    calls = []
    def live(ident, *, refresh_requested):
        calls.append((ident, refresh_requested))
        return app.jsonify({"cnaps": {"cnaps_status": "EN INSTRUCTION", "nub": "private",
            "last_checked_at": "2026-11-07T12:30:00Z", "titles": [{"type": "AP SH", "status": "ACTIF", "expires_at": "2027-01-01"}]}})
    monkeypatch.setattr(app, "_crm_read_reglementaire", live)
    response = client.post("/api/secretariat/suivi-cnaps", json={**lookup, "refresh": True})
    assert calls == [("test", True)] and response.json["cnaps"]["status"] == "En instruction"
    assert "01/01/2027" in response.json["cnaps"]["titles"][0] and "private" not in response.get_data(as_text=True)
    assert client.post("/api/secretariat/suivi-cnaps", json={**lookup, "confirmed_contact_id": "other"}).status_code == 403
    monkeypatch.setattr(app, "_crm_read_reglementaire", lambda *a, **kw: (app.jsonify(error="remote credentials"), 502))
    assert client.post("/api/secretariat/suivi-cnaps", json=lookup).status_code == 503
    assert data["crm_cnaps_scoring_snapshots"]["test"]["raw_status"] == "TRANSMIS"
    person.update(statut="Converti", formation="BTS_MCO")
    assert client.post("/api/secretariat/crm-contact", json=lookup).json["summary"]["statut"] == "Converti"
    assert client.post("/api/secretariat/suivi-cnaps", json=lookup).status_code == 400


def test_alternance_uses_read_only_cnaps_lookup_and_handles_missing_file(desk, monkeypatch):
    client, data = desk
    data["crm_contacts"][0]["formation"] = "Parcours sécurité en alternance"
    sign_in(client)
    seen = []
    def lookup(_, contact, *, cnaps_only):
        seen.append((contact["id"], cnaps_only))
        return app.jsonify(reason="cnaps_not_found"), 404
    monkeypatch.setattr(crm_cnaps_tracking, "proxy_reglementaire", lookup)
    response = client.post("/api/secretariat/suivi-cnaps", json={"telephone": "0600000000", "confirmed_contact_id": "test"})
    assert response.status_code == 200 and response.json["cnaps"]["status"] == "Aucun dossier retrouvé"
    assert seen == [("test", True)]


def test_external_operator_requires_desk_permission_and_verified_identity(desk):
    admin, data = desk
    sign_in(admin)
    admin.get("/crm/acces-secretariat")
    with admin.session_transaction() as s:
        headers = {"X-CSRF-Token": s["external_access_csrf"]}
    login = {"email": "operator@example.test", "password": "fixture-password-1234"}
    assert admin.post("/api/crm/external-accounts", json={**login, "name": "Opérateur Test"}, headers=headers).status_code == 201
    operator = app.app.test_client()
    assert operator.post("/login?next=/secretariat", data=login).location == access.HOME
    assert operator.get("/api/secretariat/agenda").status_code == 403
    assert admin.patch("/api/crm/external-accounts", json={"email": login["email"], "desk_access": True}, headers=headers).json["desk_access"]
    assert operator.get("/api/secretariat/agenda").status_code == 401
    assert operator.post("/login?next=/secretariat", data=login).location == "/secretariat"
    operator.get("/secretariat")
    with operator.session_transaction() as s:
        scoped = {"X-CSRF-Token": s["external_access_csrf"]}
    assert operator.get("/api/secretariat/agenda").status_code == 200
    assert operator.get("/api/crm/calendly/appointments").status_code == 403
    url = "/api/secretariat/crm-contact"
    known = {"telephone": "0600000000", "nom": "Du Pré", "prenom": "Chloe", "verify_identity": True}
    assert operator.post(url, json=known).status_code == 403
    assert operator.post(url, json={"telephone": known["telephone"]}, headers=scoped).json == {"contact_id": None, "verify_identity": True}
    assert operator.post(url, json={**known, "nom": "Autre"}, headers=scoped).status_code == 404
    assert operator.post(url, json={**known, "email": "wrong@example.test"}, headers=scoped).status_code == 404
    limited = operator.post(url, json=known, headers=scoped).json
    assert limited["summary"]["statut"] == "Disqualifié" and "contact" not in limited
    assert not limited["summary"]["prochaine_action"]
    data["crm_contacts"].append({"id": "second", "telephone": known["telephone"]})
    assert operator.post(url, json=known, headers=scoped).status_code == 404


def test_busy_day_matches_the_rendered_crm_and_scrolls_inside_the_agenda(desk):
    from urllib.parse import urlsplit

    playwright = pytest.importorskip("playwright.sync_api")
    client, data = desk
    sign_in(client)
    day = "2026-11-09"
    records = []
    for sequence in range(24):
        hour, minute = divmod(8 * 60 + sequence * 15, 60)
        records.append({
            "id": f"meeting-{sequence}", "start_time": f"{day}T{hour:02}:{minute:02}:00Z",
            "status": "canceled" if sequence in (2, 4) else "active",
            "rescheduled": sequence in (2, 3), "name": "Entretien admissions" if sequence % 2 else "RDV téléphonique APS",
            "invitee_name": f"Invité {sequence}", "host_name": "Conseiller test",
            "contact_id": "test" if sequence == 0 else "", "response_status": "answered" if sequence == 1 else "",
        })
    data["crm_calendly_appointments"] = records
    with playwright.sync_playwright() as engine:
        try:
            browser = engine.chromium.launch()
        except playwright.Error as exc:
            pytest.skip(f"Chromium indisponible : {exc}")
        try:
            context = browser.new_context(viewport={"width": 1908, "height": 890}, locale="fr-FR", timezone_id="America/Los_Angeles")
            def serve(route):
                url = urlsplit(route.request.url)
                if url.netloc != "desk.test" or route.request.method != "GET":
                    return route.abort()
                response = client.get(url.path, query_string=url.query)
                route.fulfill(status=response.status_code, body=response.data, content_type=response.content_type)

            context.route("**/*", serve)
            page = context.new_page()
            page.goto("https://desk.test/secretariat")
            page.locator("#agendaDay").fill(day)
            page.wait_for_function("document.querySelectorAll('#agendaList li').length===22")
            desk_rows = page.locator("#agendaList li").evaluate_all("""items => items.map(el => [
                el.querySelector('time').textContent, el.querySelector('strong').textContent, el.querySelector('small').textContent])""")
            assert desk_rows[0][:2] == ["09:00", "Chloé DU-PRÉ"]
            assert not any(row[1] in {"Invité 2", "Invité 4"} for row in desk_rows)
            with page.expect_popup() as opened:
                page.locator("#agendaCrm").click()
            crm = opened.value
            crm.wait_for_selector("#calendarContent .calendar-event")
            assert crm.locator("#calendarDate").input_value() == day
            crm_rows = crm.locator("#calendarContent .calendar-event").evaluate_all("""items => items.map(el => [
                el.querySelector('time').textContent, el.querySelector('div > span').textContent.split(' · ')[0],
                el.querySelector('div > b').textContent])""")
            assert crm_rows == desk_rows
            crm.close()

            dimensions = page.evaluate("""() => {
              const box = s => document.querySelector(s).getBoundingClientRect(), list = document.querySelector('#agendaList');
              return {left:[box('.masthead-inner').left, box('.workspace').left, box('.directory-inner').left],
                right:[box('.masthead-inner').right, box('.workspace').right, box('.directory-inner').right],
                cardBottom:box('.card').bottom, agendaBottom:box('.agenda-card').bottom,
                footerTop:box('.desk-directory').top, listHeight:list.clientHeight, listContent:list.scrollHeight,
                scrollHeight:document.documentElement.scrollHeight, viewport:window.innerHeight};
            }""")
            for edges in (dimensions["left"], dimensions["right"]):
                assert max(edges) - min(edges) < 2
            assert 90 < dimensions["listHeight"] < dimensions["listContent"]
            assert dimensions["agendaBottom"] <= dimensions["cardBottom"] + 2
            assert 0 <= dimensions["footerTop"] - dimensions["cardBottom"] < 45
            assert dimensions["scrollHeight"] <= dimensions["viewport"] + 1
            control_y = page.locator("#agendaDay").bounding_box()["y"]
            page.locator("#agendaList").evaluate("el => el.scrollTop=el.scrollHeight")
            assert page.locator("#agendaList").evaluate("el => el.scrollTop") > 0
            assert page.locator("#agendaDay").bounding_box()["y"] == control_y
            assert page.locator("#agendaList li:last-child").inner_text().find("Invité 23") >= 0
            page.set_viewport_size({"width": 390, "height": 780})
            page.wait_for_function("document.querySelector('#agendaPanel').hidden")
            page.locator("#agendaToggle").click()
            page.wait_for_selector("#agendaList li")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.locator("#agendaSearch").fill("Invité 23")
            assert page.locator("#agendaList li").count() == 1
        finally:
            browser.close()
