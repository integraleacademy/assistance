"""Vrais tests DOM dans Chromium. Installer avec: pip install playwright && playwright install chromium."""
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).parents[1]


@pytest.fixture
def chat_page(tmp_path, browser):
    """Le réseau chat est simulé, mais le JS/CSS de production tourne dans Chromium."""
    template = (ROOT / "templates/_chat_widget.html").read_text()
    template = template.replace("{{ chat_asset_version }}", "browser-test")
    template = template.replace("{{ url_for('static', filename='chat.css', v=chat_asset_version) }}", (ROOT / "static/chat.css").as_uri())
    template = template.replace("{{ url_for('static', filename='vendor/socket.io.min.js', v=chat_asset_version) }}", (ROOT / "static/vendor/socket.io.min.js").as_uri())
    template = template.replace("{{ url_for('static', filename='chat.js', v=chat_asset_version) }}", (ROOT / "static/chat.js").as_uri())
    html = tmp_path / "chat.html"
    html.write_text(f"<!doctype html><title>CRM</title>{template}")
    context = browser.new_context()
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    colleagues = [{"id": "elsa@integraleacademy.com", "name": "Elsa DUQUESNE", "online": True},
                  {"id": "aurelie@integraleacademy.com", "name": "Aurélie CHAUSSEZ", "online": False}]
    conversation = {"id": 12, "type": "direct", "title": "Elsa DUQUESNE", "peer_user_id": colleagues[0]["id"]}

    def api(route):
        url = route.request.url
        if url.endswith("/bootstrap"):
            route.fulfill(json={"current_user_id": "clement@integraleacademy.com", "conversations": [], "colleagues": colleagues, "unread": {}, "online_users_count": 2})
        elif url.endswith("/presence"):
            route.fulfill(json={"ok": True, "online_users_count": 2})
        elif url.endswith("/direct"):
            route.fulfill(json={"ok": True, "conversation": conversation})
        elif "/messages" in url:
            route.fulfill(json={"messages": [], "has_more": False})
        else:
            route.fulfill(json={"ok": True})
    page.route("**/api/chat/**", api)
    page.goto(html.as_uri())
    page.wait_for_selector('[data-user="elsa@integraleacademy.com"]')
    yield page, errors
    context.close()


def test_launcher_close_button_escape_and_socket_failure(chat_page):
    page, errors = chat_page
    page.click(".ic-launcher")
    assert page.locator(".ic-panel").is_visible()
    assert page.locator(".ic-launcher").get_attribute("aria-expanded") == "true"
    page.click(".ic-launcher")
    assert not page.locator(".ic-panel").is_visible()
    page.click(".ic-launcher"); page.click(".ic-close")
    assert not page.locator(".ic-panel").is_visible()
    page.click(".ic-launcher"); page.keyboard.press("Escape")
    assert not page.locator(".ic-panel").is_visible()
    # Le WebSocket file:// échoue volontairement: les contrôles restent utilisables.
    page.click(".ic-launcher")
    assert "reconnexion" in page.locator(".ic-connection").inner_text().lower()
    assert errors == []


def test_real_new_discussion_search_and_both_direct_entry_points(chat_page):
    page, _ = chat_page
    page.click(".ic-launcher"); page.click(".ic-new")
    assert page.get_by_role("dialog", name="Nouvelle discussion").is_visible()
    page.fill(".ic-search", "Elsa")
    assert page.locator(".ic-picker [data-user]").count() == 1
    page.click('.ic-picker [data-user="elsa@integraleacademy.com"]')
    assert page.locator(".ic-conv-title").inner_text() == "Elsa DUQUESNE"
    page.click(".ic-back")
    page.click('.ic-colleagues [data-user="elsa@integraleacademy.com"]')
    assert page.locator(".ic-conv-title").inner_text() == "Elsa DUQUESNE"


def test_network_error_is_visible(chat_page):
    page, _ = chat_page
    page.route("**/api/chat/direct", lambda route: route.fulfill(status=503, json={"error": "service indisponible"}))
    page.click(".ic-launcher"); page.click('.ic-colleagues [data-user="elsa@integraleacademy.com"]')
    assert "Impossible d’ouvrir" in page.locator(".ic-notice").inner_text()
    assert page.locator(".ic-retry").is_visible()


def test_read_badges_are_cleared_by_http_when_realtime_is_unavailable(chat_page):
    page, errors = chat_page
    direct = {"id": 12, "type": "direct", "title": "Elsa DUQUESNE",
              "peer_user_id": "elsa@integraleacademy.com"}

    def unread_api(route):
        url = route.request.url
        if url.endswith("/bootstrap"):
            route.fulfill(json={"current_user_id": "clement@integraleacademy.com",
                "conversations": [direct], "colleagues": [], "unread": {"12": 2},
                "online_users_count": 1})
        elif url.endswith("/presence"):
            route.fulfill(json={"ok": True, "online_users_count": 1})
        elif url.endswith("/read"):
            route.fulfill(json={"ok": True, "conversation_id": 12, "message_id": 42,
                "unread": {"12": 0}, "total_unread": 0})
        elif "/messages" in url:
            route.fulfill(json={"messages": [{"id": 42, "conversation_id": 12,
                "sender_user_id": "elsa@integraleacademy.com", "sender_name": "Elsa DUQUESNE",
                "body": "Bonjour", "created_at": "2026-08-04T12:00:00+00:00"}], "has_more": False})
        else:
            route.fulfill(json={"ok": True})

    page.route("**/api/chat/**", unread_api)
    page.reload()
    page.wait_for_selector('[data-cid="12"]')
    assert page.locator(".ic-total").inner_text() == "2"
    assert page.title().startswith("(2)")
    page.click(".ic-launcher")
    page.click('[data-cid="12"]')
    page.wait_for_selector(".ic-total[hidden]")
    assert page.locator('[data-cid="12"] .ic-badge').is_hidden()
    assert page.title() == "CRM"
    assert errors == []


def test_connection_never_stays_connecting_forever(chat_page):
    page, _ = chat_page
    page.wait_for_timeout(10500)
    assert page.locator(".ic-connection").inner_text() == "Temps réel indisponible — le chat reste accessible"
