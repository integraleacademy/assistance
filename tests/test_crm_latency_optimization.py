from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_web_process_has_headroom_for_http_requests():
    procfile = (ROOT / "Procfile").read_text(encoding="utf-8")
    config = (ROOT / "gunicorn.conf.py").read_text(encoding="utf-8")

    assert "--workers 1" in procfile
    assert "--threads ${GUNICORN_THREADS:-32}" in procfile
    assert 'os.getenv("GUNICORN_THREADS", "32")' in config
    assert "Socket.IO" not in config


def test_realtime_chat_runtime_is_removed_from_the_crm():
    application = (ROOT / "app.py").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert "from chat import init_chat" not in application
    assert "inject_authenticated_chat_widget" not in application
    assert "socketio.run(" not in application
    assert "Flask-SocketIO" not in requirements
    assert "simple-websocket" not in requirements
    assert "SQLAlchemy" not in requirements
    for path in (
        "chat.py",
        "static/chat.js",
        "static/chat.css",
        "static/vendor/socket.io.min.js",
        "templates/_chat_widget.html",
    ):
        assert not (ROOT / path).exists()


def test_collaborative_refresh_is_spaced_without_losing_visibility_refresh():
    source = (ROOT / "static" / "crm.js").read_text(encoding="utf-8")

    assert "CRM_REFRESH_INTERVAL_MS=180000" in source
    assert "document.hidden||crmRefreshInFlight" in source
    assert "document.addEventListener('visibilitychange'" in source


def test_sidebar_navigation_reuses_the_loaded_crm_snapshot():
    source = (ROOT / "static" / "crm.js").read_text(encoding="utf-8")
    router = source[source.index("async function refreshCrmSectionData"):
                    source.index("globalResults.addEventListener")]

    assert "history.pushState({crmSection:section}" in router
    assert "render();" in router
    assert "refreshCrmSectionData(section);" in router
    assert "api('/api/crm/contacts?section=fil-actu')" in router
    assert "api('/api/crm/callback-requests')" in router
    assert "event.preventDefault();" in router
    assert "location.reload()" not in router


def test_navigation_controls_remain_available_after_client_side_page_changes():
    template = (ROOT / "templates" / "crm.html").read_text(encoding="utf-8")

    assert '<button id="manageStatusesTop" {% if section != \'pistes\' %}hidden{% endif %}>' in template
    assert "navigation_version='20260830-fast-navigation-1'" in template
