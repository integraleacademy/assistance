"""Accès indépendants au secrétariat, sans changer l'API de rappel."""
from crm_app import legacy_app as application


def test_message_link_displays_contact_fields_first_then_routing_fields():
    app = application.app
    app.config.update(TESTING=True)
    response = app.test_client().get("/secretariat/message")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    page = response.get_data(as_text=True)
    fields = ['id="firstName"', 'id="lastName"', 'id="email"',
              'id="telephone"', 'id="recipient"', 'id="callReason"',
              'id="messageText"']
    assert [page.index(field) for field in fields] == sorted(page.index(field) for field in fields)
    for field in fields:
        assert field in page
    assert 'id="messageForm"' in page
    assert 'id="messageSuccess"' in page
    assert 'href="/secretariat/gestion"' in page


def test_other_demands_page_keeps_full_flow_without_message_choice(monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))
    application.app.config.update(TESTING=True)
    response = application.app.test_client().get("/secretariat/gestion")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'data-request="formation"' in page
    assert 'data-request="suivi"' in page
    assert 'data-request="autre"' not in page
    assert 'id="browseCourses"' in page
    assert 'href="/secretariat/message"' in page


def test_historical_link_stays_usable_with_original_choices(monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))
    response = application.app.test_client().get("/secretariat")
    assert response.status_code == 200
    assert 'data-request="autre"' in response.get_data(as_text=True)


def test_quick_message_reuses_crm_callback_api_and_idempotency_key():
    script = application.app.test_client().get("/static/secretariat_message.js")
    assert script.status_code == 200
    content = script.get_data(as_text=True)
    assert "fetch('/api/secretariat/demandes'" in content
    assert "callback_motif: 'message'" in content
    assert "call_key: callKey" in content
    assert "type: 'autre'" in content
    assert "callback_recipient:" in content
    assert "Motif de l’appel :" in content
