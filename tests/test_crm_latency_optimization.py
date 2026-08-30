from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_web_process_has_headroom_for_chat_and_http_requests():
    procfile = (ROOT / "Procfile").read_text(encoding="utf-8")
    config = (ROOT / "gunicorn.conf.py").read_text(encoding="utf-8")

    assert "--workers 1" in procfile
    assert "--threads ${GUNICORN_THREADS:-32}" in procfile
    assert 'os.getenv("GUNICORN_THREADS", "32")' in config


def test_background_tabs_do_not_open_realtime_chat_connections():
    source = (ROOT / "static" / "chat.js").read_text(encoding="utf-8")
    set_open = source[source.index("function setOpen"):
                      source.index("function connection")]
    startup = source[source.index(
        'if (localStorage.getItem("ic-chat-open") === "1")'
    ):]

    assert "activateRealtime();" in set_open
    assert "bootstrap();" in startup
    assert "bootstrap().finally(initSocket);" not in startup
    assert "const CHAT_HEARTBEAT_INTERVAL_MS = 45000" in source
    assert "if (!document.hidden) heartbeat();" in source


def test_collaborative_refresh_is_spaced_without_losing_visibility_refresh():
    source = (ROOT / "static" / "crm.js").read_text(encoding="utf-8")

    assert "CRM_REFRESH_INTERVAL_MS=180000" in source
    assert "document.hidden||crmRefreshInFlight" in source
    assert "document.addEventListener('visibilitychange'" in source
