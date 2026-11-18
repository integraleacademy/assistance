"""The shared CRM shell, daily calendar and collaborative refresh use different payloads."""
import copy
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest

import app as crm


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(crm, "DATA_FILE", str(tmp_path / "crm.json"))
    data = copy.deepcopy(crm.DEFAULT_DATA)
    data["crm_contacts"] = [{"id": "lead", "prenom": "Ada", "nom": "Exemple", "formation": "APS",
                             "statut": "Nouveaux", "activities": [], "mail": "ada@example.test"}]
    data["crm_calendly_appointments"] = [
        {"id": key, "contact_id": "lead", "name": "Appel formation", "start_time": start,
         "host_name": "Conseillère test", "status": status, "invitee_uri": "internal/" + key}
        for key, start, status in [
            ("evening", "2026-11-17T23:10:00Z", "active"),
            ("local", "2026-11-18T14:00:00", "active"),
            ("tomorrow", "2026-11-18T23:15:00Z", "active"),
            ("cancelled", "2026-11-18T12:00:00Z", "cancelled"),
        ]
    ]
    crm.save_data(data)
    client = crm.app.test_client()
    with client.session_transaction() as session:
        session["user_email"] = next(iter(crm.USERS))
    return client


def test_calendar_returns_only_active_paris_day_and_preserves_original_api(client):
    path = "/api/crm/calendly/appointments"
    full = client.get(path).json["appointments"]
    assert len(full) == 4
    day = client.get(path + "?date=2026-11-18&navigation=1").json
    assert [a["id"] for a in day["appointments"]] == ["evening", "local"]
    for row in day["appointments"]:
        original = next(a for a in full if a["id"] == row["id"])
        assert row["calendar_slot"] == original["calendar_slot"]
        assert row["contact"] == original["contact"]
        assert row["host_name"] == original["host_name"]
        assert "invitee_uri" not in row
    assert "callback_pending_count" in day["navigation"]
    assert client.get(path + "?date=2026-11-20").json["appointments"] == []
    assert client.get(path + "?date=2026-02-30").status_code == 400
    assert crm.app.test_client().get(path + "?date=2026-11-18").status_code in {302, 401}


def test_workspace_reuses_summaries_and_does_not_prepare_templates_or_calendar_history(client, monkeypatch):
    calls = []
    summaries = crm._crm_contact_summaries_payload

    def counted(*args, **kwargs):
        calls.append(1)
        return summaries(*args, **kwargs)

    monkeypatch.setattr(crm, "_crm_contact_summaries_payload", counted)
    for helper in ("_crm_templates_payload", "_crm_calendly_appointments_payload"):
        monkeypatch.setattr(crm, helper, lambda *_: pytest.fail("Unrelated data on first render"))
    base = "/api/crm/bootstrap?view=workspace&section="
    for section in ("accueil", "contacts", "pistes", "inscrits", "disqualifies", "relances"):
        body = client.get(base + section).json
        assert body["contacts"][0]["_summary"] and "templates" not in body
        assert all("contact" not in row and "invitee_uri" not in row for row in body["appointments"])
    assert len(calls) == 1
    client.get(base + "%20Notifications%20")
    assert len(calls) == 2
    client.get(base + "fil-actu")
    assert len(calls) == 2


def test_revision_skips_unchanged_polls_and_is_bound_to_user_section_and_writes(client, monkeypatch):
    route = "/api/crm/contacts/updates?view=workspace&delta=1"
    first = client.get("/api/crm/bootstrap?view=workspace&section=pistes").json
    revision = first["revision"]
    with monkeypatch.context() as patch:
        patch.setattr(crm, "_crm_prepared_read_model", lambda: pytest.fail("Rebuilt unchanged snapshot"))
        assert client.get(route + "&section=pistes&revision=" + revision).json["unchanged"]
    assert not client.get(route + "&revision=" + revision).json.get("unchanged")
    with client.session_transaction() as session:
        session["user_email"] = list(crm.USERS)[1]
    assert not client.get(route + "&section=pistes&revision=" + revision).json.get("unchanged")
    initial = client.get(route).json
    data = crm.load_data()
    data["crm_calendly_appointments"][0]["response_status"] = "answered"
    crm.save_data(data)
    result = client.get(route + "&cursor=" + initial["cursor"] + "&revision=" + initial["revision"]).json
    assert [row["id"] for row in result["appointments"]] == ["evening"]
    assert result["appointments"][0]["response_status"] == "answered"


def test_notification_reads_do_not_wait_for_writes(client):
    with ThreadPoolExecutor(max_workers=1) as pool, crm._CRM_RECONCILIATION_LOCK:
        assert pool.submit(client.get, "/api/crm/notifications?kind=waiting_call").result(timeout=2).status_code == 200


def test_up_to_date_callback_reads_do_not_wait_for_writes(client):
    data = crm.load_data()
    data["secretariat_demandes"] = [{"type": "autre", "nom": "Exemple"}]
    crm.save_data(data)
    route = "/api/crm/callback-requests"
    result = client.get(route).json["callback_requests"]
    assert len(result) == 1 and result[0]["id"]
    crm.save_data(crm.load_data())  # A new disk revision, without any callback to repair.
    with ThreadPoolExecutor(max_workers=1) as pool, crm._SECRETARIAT_DELIVERY_LOCK, crm._CRM_RECONCILIATION_LOCK:
        refreshed = pool.submit(client.get, route).result(timeout=2).json
    assert refreshed["callback_requests"][0]["id"] == result[0]["id"]


def test_bulk_callback_lookup_keeps_ambiguous_and_intake_rules():
    people = [{"id": "a", "mail": "a@example.test", "telephone": "0611111111"},
              {"id": "b", "mail": "b@example.test", "telephone": "0611111111"}]
    data = {"crm_contacts": people}
    lookup = crm._crm_callback_contact_lookup(data)
    for entry in [{"email": "a@example.test"}, {"telephone": "+33611111111"},
                  {"email": "a@example.test", "telephone": "0611111111"},
                  {"crm_contact_id": "b", "email": "a@example.test"},
                  {"type": "formation", "email": "a@example.test"}, {}]:
        assert lookup(entry) is crm._crm_callback_request_contact(data, entry)


def test_real_crm_navigation_uses_daily_agenda_and_lazy_templates(client):
    pw = pytest.importorskip("playwright.sync_api")
    requests = []
    failures = []
    pending = []
    with pw.sync_playwright() as driver:
        try:
            browser = driver.chromium.launch()
        except pw.Error as exc:
            pytest.skip(str(exc))
        try:
            page = browser.new_page(timezone_id="Europe/Paris", viewport={"width": 1440, "height": 1200})
            page.clock.set_fixed_time("2026-11-18T09:00:00Z")
            page.on("pageerror", lambda exc: failures.append(str(exc)))

            def serve(route):
                url = urlsplit(route.request.url)
                if url.netloc != "crm.test":
                    return route.abort()
                if route.request.method == "PATCH" and url.path == "/api/crm/notifications":
                    response = client.patch(url.path, json=route.request.post_data_json)
                    return route.fulfill(status=response.status_code, json=response.json)
                if route.request.method == "POST" and url.path == "/api/crm/contacts/lead/appel":
                    assert route.request.post_data_json["appointment_id"] == "evening"
                    return route.fulfill(json={"contact": client.get("/api/crm/contacts/lead").json,
                                               "appointment": {"id": "evening", "response_status": "answered"},
                                               "delivery": {"email": True, "sms": True}})
                if route.request.method != "GET":
                    return route.abort()
                if url.path.startswith("/api/"):
                    requests.append((url.path, parse_qs(url.query)))
                if url.path.endswith("/cassandre-availability"):
                    pending.append(route)
                    return
                response = client.get(url.path, query_string=url.query)
                route.fulfill(status=response.status_code, body=response.data, content_type=response.content_type)

            page.context.route("**/*", serve)
            page.goto("https://crm.test/crm/calendrier?date=2026-11-18")
            rows = page.locator("#calendarContent .calendar-event")
            pw.expect(rows).to_have_count(2)
            assert not any(p in {"/api/crm/bootstrap", "/api/crm/contacts/updates"} for p, _ in requests)
            assert pending
            pending.pop().fulfill(json={"days": {f"2026-11-{d}": 1 for d in (18, 19, 20, 23, 24)}})
            pw.expect(page.locator(".cassandre-total strong")).to_have_text("5")
            page.locator("#calendarNext").click()
            pw.expect(rows).to_have_count(1)
            assert requests[-1][1].get("date") == ["2026-11-19"]
            page.locator("#calendarPrevious").click()
            pw.expect(rows).to_have_count(2)
            with page.expect_popup() as opened:
                rows.first.click(position={"x": 6, "y": 6})
            opened.value.wait_for_url("**/crm/contacts?fiche=lead")
            pw.expect(opened.value.locator("#contactForm")).to_be_visible()
            opened.value.close()
            rows.first.locator('[data-appointment-response]').select_option("answered")
            page.locator("#callNote").fill("Compte rendu de test")
            page.locator("#logCall").click()
            pw.expect(page.locator("#contactForm")).to_be_visible()
            page.goto("https://crm.test/crm/calendrier?date=2026-11-18")
            pw.expect(rows).to_have_count(2)
            page.locator('[data-nav="contacts"]').click()
            pw.expect(page.locator("#page")).to_contain_text("Ada")
            for section in ("accueil", "pistes", "inscrits", "disqualifies", "relances",
                            "notifications", "fil-actu", "demandes-rappel", "contacts"):
                page.locator(f'[data-nav="{section}"]').click()
                pw.expect(page.locator("body")).to_have_attribute("data-section", section)
                pw.expect(page.locator("#page h1").first).to_be_visible()
            assert not any(p == "/api/crm/templates" for p, _ in requests)
            page.locator('[data-nav="modeles"]').click()
            pw.expect(page.locator(".automatic-template-card").first).to_be_visible()
            assert sum(p == "/api/crm/templates" for p, _ in requests) == 1
            page.goto("https://crm.test/crm/calendrier?date=2026-11-18")
            page.locator("#globalSearch").fill("Ada")
            pw.expect(page.locator('[data-global-id="lead"]').first).to_be_visible()
            assert not failures
        finally:
            browser.close()
