"""The external desk must never inherit the internal CRM's broad access."""

import copy
import json
from pathlib import Path

import pytest

import app as legacy
import crm_app  # Exercise the actual Render entry point and its extra routes.
import crm_external_access as access


EMAIL = "desk@example.test"
PASSWORD = "Test-password-2026"


@pytest.fixture
def clients(tmp_path, monkeypatch):
    monkeypatch.setattr(legacy, "DATA_FILE", str(tmp_path / "data.json"))
    legacy.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    legacy._LOGIN_ATTEMPTS.clear()
    base = {"prenom": "Marie", "nom": "TEST", "statut": "Nouveaux",
            "origine": "META", "formation": "A3P", "activities": [], "relances": [],
            "created_at": "2026-10-12T07:00:00", "updated_at": "2026-10-12T07:00:00"}
    contacts = [
        {**copy.deepcopy(base), "id": "new-meta", "mail": "prospect@example.test",
         "telephone": "0600000000", "cnaps_password": "NEVER EXPOSE THIS",
         "formulaire": {"unrelated": "PRIVATE"}, "cout_estime": "SECRET COST",
         "meta_answers": [{"question": "Formation ?", "answer": "A3P", "token": "PRIVATE"}]},
        {**copy.deepcopy(base), "id": "other-origin", "origine": "Site internet"},
        {**copy.deepcopy(base), "id": "old-meta", "statut": "En cours"},
        {**copy.deepcopy(base), "id": "archived-meta", "archived_at": "2026-10-12"},
        {**copy.deepcopy(base), "id": "converted-meta", "statut": "Converti"},
    ]
    data = copy.deepcopy(legacy.DEFAULT_DATA)
    data["crm_contacts"] = contacts
    data["crm_calendly_appointments"] = []
    legacy.save_data(data)
    admin = legacy.app.test_client()
    with admin.session_transaction() as state:
        state["user_email"] = "clement@integraleacademy.com"
    assert admin.get("/crm/acces-secretariat").status_code == 200
    with admin.session_transaction() as state:
        admin_headers = {"X-CSRF-Token": state["external_access_csrf"]}
    response = admin.post("/api/crm/external-accounts", json={
        "email": EMAIL, "name": "Secrétariat externe", "password": PASSWORD,
    }, headers=admin_headers)
    assert response.status_code == 201
    external = legacy.app.test_client()
    response = external.post("/login?next=/admin", data={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 302 and response.location == "/crm/pistes"
    assert external.get(response.location).status_code == 200
    with external.session_transaction() as state:
        external_headers = {"X-CSRF-Token": state["external_access_csrf"]}
    return admin, external, admin_headers, external_headers


def test_account_management_hashes_passwords_and_preserves_internal_accounts(clients):
    admin, _, headers, _ = clients
    stored = access.account_path(legacy.DATA_FILE).read_text()
    assert PASSWORD not in stored
    assert json.loads(stored)[EMAIL]["password_hash"].startswith("scrypt:")
    assert access.account_path(legacy.DATA_FILE).stat().st_mode & 0o777 == 0o600
    response = admin.get("/api/crm/external-accounts")
    assert response.status_code == 200
    assert "password" not in response.get_data(as_text=True)
    assert response.json[0]["role"] == access.ROLE
    conflict = admin.post("/api/crm/external-accounts", headers=headers, json={
        "email": "clement@integraleacademy.com", "name": "Desk", "password": PASSWORD,
    })
    assert conflict.status_code == 409
    assert legacy.USERS["clement@integraleacademy.com"]["role"] == "admin"


def test_no_account_creation_or_mutation_without_admin_and_csrf(clients):
    admin, external, _, _ = clients
    assert admin.post("/api/crm/external-accounts", json={}).status_code == 403
    assert external.get("/api/crm/external-accounts").status_code == 403
    assert external.post("/api/crm/external-accounts", json={}).status_code == 403
    ordinary = legacy.app.test_client()
    with ordinary.session_transaction() as state:
        state["user_email"] = "cassandre@integraleacademy.com"
    assert ordinary.get("/api/crm/external-accounts").status_code == 403
    assert ordinary.get("/crm/acces-secretariat").status_code == 403
    assert legacy.app.test_client().get("/api/crm/external-accounts").status_code == 401


@pytest.mark.parametrize("payload", [
    {"email": "invalid"}, {"password": "short"}, {"enabled": "false"},
    {"role": "admin"}, {"name": ""}, {"password": 123},
])
def test_invalid_account_changes_are_rejected(clients, payload):
    admin, _, headers, _ = clients
    before = access.read_accounts(legacy.DATA_FILE)
    response = admin.patch("/api/crm/external-accounts", json={"email": EMAIL, **payload}, headers=headers)
    assert response.status_code == 400
    assert access.read_accounts(legacy.DATA_FILE) == before


def test_scope_matches_new_meta_and_response_does_not_leak_secrets(clients):
    _, external, _, _ = clients
    response = external.get("/api/crm/external-meta?section=contacts&include_archived=1")
    assert response.status_code == 200
    assert [c["id"] for c in response.json["contacts"]] == ["new-meta"]
    assert "Converti" not in response.json["statuses"]
    detail = external.get("/api/crm/external-meta/new-meta")
    assert detail.json["meta_answers"] == [{"question": "Formation ?", "answer": "A3P"}]
    for result in (response, detail):
        assert "no-store" in result.headers["Cache-Control"]
        for forbidden in ("NEVER EXPOSE", "PRIVATE", "SECRET COST", "cnaps_password", "formulaire"):
            assert forbidden not in result.get_data(as_text=True)


@pytest.mark.parametrize("method,path", [
    ("get", "/api/crm/bootstrap?section=contacts"),
    ("get", "/api/crm/contacts"), ("post", "/api/crm/contacts"),
    ("get", "/api/crm/contacts/new-meta"), ("patch", "/api/crm/contacts/new-meta"),
    ("delete", "/api/crm/contacts/new-meta"), ("get", "/api/crm/contacts/updates"),
    ("get", "/api/crm/settings"), ("patch", "/api/crm/contacts/bulk"),
    ("post", "/api/crm/contacts/merge"), ("get", "/api/crm/calendly/appointments"),
    ("get", "/api/crm/notifications"), ("get", "/api/crm/exports/contacts"),
    ("post", "/api/crm/wedof/sync"), ("post", "/api/crm/test-email"),
    ("get", "/admin"), ("get", "/api/secretariat/crm-contact"),
    ("get", "/unknown/private/endpoint"),
])
def test_external_user_is_denied_all_legacy_and_unlisted_routes(clients, method, path):
    _, external, _, _ = clients
    before = legacy.load_data()
    response = getattr(external, method)(path)
    assert response.status_code == 403
    assert legacy.load_data() == before


@pytest.mark.parametrize("contact_id", ["other-origin", "old-meta", "archived-meta", "converted-meta", "unknown"])
def test_direct_read_and_update_of_out_of_scope_contacts_is_rejected(clients, contact_id):
    _, external, _, headers = clients
    before = legacy.load_data()
    assert external.get(f"/api/crm/external-meta/{contact_id}").status_code == 404
    assert external.patch(f"/api/crm/external-meta/{contact_id}", json={"commentaires": "unauthorized"}, headers=headers).status_code == 404
    assert legacy.load_data() == before


def test_only_restricted_page_and_assets_are_served(clients):
    _, external, _, _ = clients
    for path in ("/crm", "/CRM", "/crm/contacts", "/crm/exports", "/crm/modeles"):
        response = external.get(path, follow_redirects=True)
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Nouveaux META" in body
        assert 'id="adminToolsBtn"' not in body
        assert 'href="/crm/contacts"' not in body
        assert 'filename=' not in body
    assert external.get("/static/crm_external.js").status_code == 200
    assert external.get("/static/crm.js").status_code == 200
    assert external.get("/static/crm_salesforce_import.js").status_code == 403
    sheet = external.get("/crm/pistes?fiche=new-meta").get_data(as_text=True)
    assert '"external_meta": true' in sheet
    assert 'filename=' not in sheet
    assert 'crm_external_contact.js' in sheet
    assert 'id="adminToolsBtn"' not in sheet
    assert 'id="newContact"' not in sheet
    assert 'href="/crm/contacts"' not in sheet
    assert 'crm_salesforce_import.js' not in sheet


def test_native_sheet_projects_score_and_appointments_without_internal_data(clients):
    admin, external, _, _ = clients
    data = legacy.load_data()
    data["crm_ai_candidate_analyses"] = {"new-meta": {"result": {"summary": "PRIVATE INTERNAL ANALYSIS"}}}
    data["crm_calendly_appointments"] = [
        {"id": "own", "contact_id": "new-meta", "name": "Rendez-vous", "start_time": "2026-01-01T10:00:00Z", "status": "canceled", "secret": "PRIVATE", "cancel_url": "PRIVATE"},
        {"id": "other", "contact_id": "old-meta", "name": "PRIVATE OTHER PERSON"},
    ]
    legacy.save_data(data)
    response = external.get("/api/crm/external-meta/new-meta")
    assert response.status_code == 200
    assert response.json["integration_score"] == admin.get("/api/crm/contacts/new-meta").json["integration_score"]
    assert [row["id"] for row in response.json["appointments"]] == ["own"]
    assert "PRIVATE" not in response.get_data(as_text=True)
    assert "cnaps_password" not in response.json
    assert "cout_estime" not in response.json


def test_qualification_and_call_are_saved_with_author_then_status_removes_access(clients):
    _, external, _, headers = clients
    response = external.patch("/api/crm/external-meta/new-meta", headers=headers, json={
        "cpf": "OUI", "cpf_montant": "2000", "qualification_flag": "green",
        "commentaires": "Souhaite démarrer en novembre.", "call_note": "Appel effectué, projet confirmé.",
    })
    assert response.status_code == 200 and response.json["in_scope"]
    stored = legacy._crm_contact(legacy.load_data(), "new-meta")
    call = next(a for a in stored["activities"] if a["kind"] == "appel")
    assert call["author"] == "Secrétariat externe"
    assert call["detail"] == "Appel effectué, projet confirmé."
    assert stored["cpf_montant"] == "2000.00"
    response = external.patch("/api/crm/external-meta/new-meta", headers=headers, json={"statut": "En cours"})
    assert response.status_code == 200
    assert response.json == {"in_scope": False, "contact": None}
    assert external.get("/api/crm/external-meta").json["contacts"] == []
    assert external.get("/api/crm/external-meta/new-meta").status_code == 404
    assert external.patch("/api/crm/external-meta/new-meta", headers=headers, json={"statut": "Nouveaux"}).status_code == 404


@pytest.mark.parametrize("payload", [
    {"origine": "Site internet"}, {"commercial": "Clément"}, {"cnaps_password": "hidden"},
    {"id": "other-origin"}, {"archived_at": "2026-10-12"}, {"activities": []},
    {"statut": "Converti"}, {"statut": "RDV programmé"}, {"statut": "Disqualifié"},
    {"statut": "A relancer"}, {"cpf": "something else"}, {"commentaires": ["text"]},
    {"cpf_montant": "-20"}, {"qualification_flag": "invalid"},
])
def test_external_payload_cannot_escalate_scope_or_bypass_validation(clients, payload):
    _, external, _, headers = clients
    before = legacy.load_data()
    response = external.patch("/api/crm/external-meta/new-meta", headers=headers, json=payload)
    assert response.status_code == 400
    assert legacy.load_data() == before


def test_followup_and_disqualification_use_existing_crm_workflow(clients):
    _, external, _, headers = clients
    response = external.patch("/api/crm/external-meta/new-meta", headers=headers, json={
        "statut": "A relancer", "relance_date": "2026-11-02", "relance_motif": "À rappeler",
    })
    assert response.status_code == 200 and not response.json["in_scope"]
    stored = legacy._crm_contact(legacy.load_data(), "new-meta")
    assert stored["statut"] == "A relancer"
    assert stored["relances"][0]["scheduled_date"] == "2026-11-02"
    assert stored["relances"][0]["created_by"] == "Secrétariat externe"


def test_read_then_concurrent_status_change_is_rechecked_before_write(clients):
    _, external, _, headers = clients
    assert external.get("/api/crm/external-meta/new-meta").status_code == 200
    data = legacy.load_data()
    legacy._crm_contact(data, "new-meta")["statut"] = "En cours"
    legacy.save_data(data)
    response = external.patch("/api/crm/external-meta/new-meta", headers=headers, json={"commentaires": "stale change"})
    assert response.status_code == 404
    assert legacy.load_data() == data


@pytest.mark.parametrize("old_status", ["", "ancienne étape inconnue"])
def test_older_meta_lead_uses_same_repaired_status_for_read_and_write(clients, old_status):
    _, external, _, headers = clients
    data = legacy.load_data()
    legacy._crm_contact(data, "new-meta")["statut"] = old_status
    legacy.save_data(data)
    assert external.get("/api/crm/external-meta/new-meta").json["statut"] == "Nouveaux"
    response = external.patch("/api/crm/external-meta/new-meta", headers=headers, json={
        "call_note": "Appel consigné sur une ancienne fiche.",
    })
    assert response.status_code == 200
    assert response.json["in_scope"]
    assert legacy._crm_contact(legacy.load_data(), "new-meta")["statut"] == "Nouveaux"


def test_csrf_is_required_for_external_mutations(clients):
    _, external, _, _ = clients
    before = legacy.load_data()
    assert external.patch("/api/crm/external-meta/new-meta", json={"statut": "En cours"}).status_code == 403
    assert legacy.load_data() == before


@pytest.mark.parametrize("change", [{"enabled": False}, {"password": "Replacement-password-2026"}])
def test_revocation_invalidates_existing_sessions_and_previous_password(clients, change):
    admin, external, headers, _ = clients
    assert admin.patch("/api/crm/external-accounts", headers=headers, json={"email": EMAIL, **change}).status_code == 200
    assert external.get("/api/crm/external-meta").status_code == 401
    assert external.get("/api/crm/contacts").status_code == 302
    fresh = legacy.app.test_client()
    assert fresh.post("/login", data={"email": EMAIL, "password": PASSWORD}).status_code == 401
    if "password" in change:
        assert fresh.post("/login", data={"email": EMAIL, "password": change["password"]}).status_code == 302


def test_new_meta_origin_aliases_and_secondary_status_match_the_existing_bucket():
    for origin in ("META", "Facebook", "Instagram", "campagne META A3P"):
        assert access.is_new_meta({"origine": origin, "statut": "Nouveaux"})
    assert access.is_new_meta({"meta_source": {"form_id": "example"}, "statut": "Nouveaux"})
    assert access.is_new_meta({"origine": "META", "statut": "En cours", "statut_secondaire": "Nouveaux"})
    assert not access.is_new_meta({"origine": "Site internet", "source": "META", "statut": "Nouveaux"})
    assert not access.is_new_meta({"origine": "META", "statut": "Converti", "statut_secondaire": "Nouveaux"})


def test_admin_menu_has_account_management_but_commercial_menu_does_not(clients):
    admin, _, _, _ = clients
    assert b'href="/crm/acces-secretariat"' in admin.get("/crm").data
    commercial = legacy.app.test_client()
    with commercial.session_transaction() as state:
        state["user_email"] = "cassandre@integraleacademy.com"
    assert b'href="/crm/acces-secretariat"' not in commercial.get("/crm").data


def test_unknown_old_session_cannot_fall_through_legacy_api(clients):
    stale = legacy.app.test_client()
    with stale.session_transaction() as state:
        state["user_email"] = "removed@example.test"
    assert stale.get("/api/crm/contacts").status_code == 401


def test_javascript_is_valid():
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    root = Path(__file__).resolve().parents[1]
    for name in ("crm_external.js", "crm_external_accounts.js", "crm_external_contact.js", "crm.js"):
        subprocess.run([node, "--check", str(root / "static" / name)], check=True, capture_output=True)


@pytest.mark.parametrize("browser_name", ["chromium", "webkit"])
def test_browser_can_create_access_and_process_a_meta_request(clients, tmp_path, browser_name):
    browser_api = pytest.importorskip("playwright.sync_api")
    import threading
    from werkzeug.serving import make_server
    admin, _, _, _ = clients
    server = make_server("127.0.0.1", 0, legacy.app, threaded=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with browser_api.sync_playwright() as playwright:
            engine = getattr(playwright, browser_name)
            if not Path(engine.executable_path).exists():
                pytest.skip(f"Playwright {browser_name} unavailable")
            browser = engine.launch(**({"args": ["--no-sandbox"]} if browser_name == "chromium" else {}))
            admin_context = browser.new_context(viewport={"width": 1440, "height": 1050})
            # Native CSS imports Google Fonts; don't let a third-party font
            # outage block script execution in these local workflow tests.
            admin_context.route("https://fonts.googleapis.com/**", lambda route: route.fulfill(status=200, content_type="text/css", body=""))
            admin_context.add_cookies([{
                "name": legacy.app.config["SESSION_COOKIE_NAME"],
                "value": admin.get_cookie(legacy.app.config["SESSION_COOKIE_NAME"]).value,
                "url": base,
            }])
            page = admin_context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/crm/acces-secretariat")
            page.get_by_label("Nom du collaborateur ou du prestataire").fill("Secrétariat téléphone")
            page.get_by_label("Adresse e-mail de connexion").fill("phone@example.test")
            page.locator('#createAccount [name="password"]').fill(PASSWORD)
            page.get_by_role("button", name="Créer l’accès").click()
            browser_api.expect(page.locator('[data-email="phone@example.test"]')).to_be_visible()
            page.screenshot(path=str(tmp_path / "external-admin.png"), full_page=True)

            external_context = browser.new_context(viewport={"width": 1440, "height": 1050})
            external_context.route("https://fonts.googleapis.com/**", lambda route: route.fulfill(status=200, content_type="text/css", body=""))
            external = external_context.new_page()
            external.on("pageerror", lambda error: errors.append(str(error)))
            requests = []
            external.on("request", lambda request: requests.append(request.url))
            external.goto(base + "/login?next=/crm/contacts")
            external.locator('[name="email"]').fill("phone@example.test")
            external.locator('[name="password"]').fill(PASSWORD)
            external.get_by_role("button", name="Accéder au CRM").click()
            external.wait_for_url(base + "/crm/pistes")
            browser_api.expect(external.locator('#count')).to_have_text("1")
            external.screenshot(path=str(tmp_path / "external-list.png"), full_page=True)
            external.get_by_text("Marie TEST", exact=True).click()
            try:
                browser_api.expect(external.locator('#contactForm')).to_be_visible()
            except AssertionError as error:
                raise AssertionError(f"Native sheet failed to open: {errors}; {external.locator('#page').inner_text()}") from error
            browser_api.expect(external.locator('.contact-header-title')).to_have_text("Fiche de Marie TEST")
            assert '/crm/pistes?fiche=new-meta' in external.url
            assert external.locator('#sheet').count() == 0
            external.go_back()
            browser_api.expect(external.locator('#count')).to_have_text("1")
            external.get_by_text("Marie TEST", exact=True).click()
            browser_api.expect(external.locator('#contactForm')).to_be_visible()
            external.reload()
            browser_api.expect(external.locator('#contactForm')).to_be_visible()
            external.locator('#backList').click()
            external.locator('#leads td').nth(1).click()
            browser_api.expect(external.locator('#contactForm')).to_be_visible()
            external.locator('#backList').click()
            external.get_by_role("button", name="Ouvrir la fiche CRM de Marie TEST").focus()
            external.keyboard.press("Enter")
            browser_api.expect(external.locator('#contactForm')).to_be_visible()
            external.locator('#backList').click()
            external.set_viewport_size({"width": 390, "height": 844})
            external.locator('#leads td').nth(2).click()
            browser_api.expect(external.locator('#contactForm')).to_be_visible()
            external.screenshot(path=str(tmp_path / "external-native-mobile.png"), full_page=True)
            assert external.evaluate("document.documentElement.scrollWidth <= innerWidth")
            external.locator('#backList').click()
            external.set_viewport_size({"width": 1440, "height": 1050})
            external.get_by_role("button", name="Traiter la demande de Marie TEST").click()
            browser_api.expect(external.locator('#contactForm')).to_be_visible()
            browser_api.expect(external.locator('#integrationScoreCard')).to_contain_text("Score d’intégration")
            external.locator('.regulatory-section summary').click()
            external.locator('label:has(> input[name="carte_pro"][value="NON"])').click()
            external.locator('label:has(> input[name="compte_cnaps"][value="OUI"])').click()
            for name in ('cnaps_username', 'cnaps_password', 'cnaps_birth_year'):
                browser_api.expect(external.locator(f'[name="{name}"]')).not_to_be_visible()
                browser_api.expect(external.locator(f'[name="{name}"]')).to_be_disabled()
            browser_api.expect(external.locator('#saveState')).to_have_text("✓ Enregistré")
            external.locator('.regulatory-section summary').click()
            external.locator('.funding-section summary').click()
            external.locator('label:has(> input[name="cpf"][value="OUI"])').click()
            external.locator('[name="cpf_montant"]').fill("2000")
            browser_api.expect(external.locator('#saveState')).to_have_text("✓ Enregistré")
            external.locator('#callBtn').click()
            external.get_by_label("Compte-rendu", exact=True).fill("Intéressée par la prochaine session.")
            external.locator('#logCall').click()
            browser_api.expect(external.locator('.call-modal')).not_to_be_visible()
            external.locator('#contactActivityTab').click()
            browser_api.expect(external.locator('#activityFeed')).to_contain_text("Intéressée par la prochaine session.")
            external.locator('#contactInfoTab').click()
            external.screenshot(path=str(tmp_path / "external-native-sheet.png"), full_page=True)
            for selector in ('#mailBtn', '#calendarBtn', '#convertBtn', '#contactWedofTab', '[name="cnaps_password"]'):
                browser_api.expect(external.locator(selector)).not_to_be_visible()
            # Switching stage immediately must flush the final keystroke even
            # when the native form's 550 ms autosave has not fired yet.
            external.get_by_label("Commentaire de suivi").fill("Projet qualifié avant transmission.")
            external.locator('[data-primary-step="En cours"]').click()
            browser_api.expect(external.locator('#count')).to_have_text("0")
            stored = legacy._crm_contact(legacy.load_data(), "new-meta")
            assert stored["commentaires"] == "Projet qualifié avant transmission."
            assert stored["cpf_montant"] == "2000.00"
            assert stored["cnaps_password"] == "NEVER EXPOSE THIS"
            assert stored["cout_estime"] == "SECRET COST"
            assert all('/api/crm/external-' in url for url in requests if '/api/' in url)
            assert external.request.get(base + "/api/crm/contacts").status == 403
            external.set_viewport_size({"width": 390, "height": 844})
            external.screenshot(path=str(tmp_path / "external-mobile.png"), full_page=True)
            assert external.evaluate("document.documentElement.scrollWidth <= innerWidth")

            page.locator('[data-email="phone@example.test"]').get_by_role("button", name="Désactiver").click()
            browser_api.expect(page.locator('[data-email="phone@example.test"] .state')).to_have_text("Désactivé")
            external.goto(base + "/crm/pistes")
            browser_api.expect(external.locator('[name="email"]')).to_be_visible()
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        worker.join(timeout=5)
