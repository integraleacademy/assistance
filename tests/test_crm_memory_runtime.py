"""Runtime contract checks in Flask, isolated from production data and APIs."""
import copy
import os
import runpy
import threading
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, jsonify, request

from crm_memory_optimizations import install_crm_memory_optimizations


ROOT = Path(__file__).resolve().parents[1]


def make_app():
    app = Flask(__name__)
    store = {"crm_contacts": [{"id": "one", "mail": "one@example.invalid"}],
             "crm_calendly": {}, "crm_calendly_appointments": []}
    def login_required(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if request.headers.get("X-Test-Login") != "yes":
                return jsonify({"error": "authentication required"}), 401
            return fn(*args, **kwargs)
        return wrapped
    def original(contact_id):
        return jsonify({"original_post": contact_id}), 201
    app.add_url_rule("/calendly/<contact_id>", "crm_contact_calendly_appointments",
                     original, methods=["GET", "POST"])
    app.add_url_rule("/wedof/<contact_id>", "crm_contact_wedof", original)
    def forbidden(*args, **kwargs):
        raise AssertionError("network/write must not run in this read-only test")
    legacy = SimpleNamespace(
        app=app, request=request, jsonify=jsonify, login_required=login_required,
        current_user=lambda: {"email": "test@example.invalid"},
        _load_data_snapshot=lambda: store, load_data=lambda: copy.deepcopy(store),
        save_data=forbidden, _CRM_RECONCILIATION_LOCK=threading.RLock(),
        _crm_contact=lambda data, key: next((c for c in data["crm_contacts"] if c["id"] == key), None),
        _calendly_token=lambda: "", _crm_normalize_email=lambda value: str(value or ""),
        _crm_calendly_fetch_contact_appointments=forbidden,
        _crm_calendly_relink_appointments=lambda *args: False,
        _crm_sync_contact_calendly_status=lambda *args: False,
        _crm_calendly_status_payload=lambda data: {"configured": False},
        _wedof_contact_resources=lambda key, data: [{"stable_id": "test-folder"}],
        _wedof_status_payload=lambda **kwargs: {"configured": True},
        CalendlyAPIError=RuntimeError,
    )
    install_crm_memory_optimizations(legacy)
    return app


def test_flask_json_response_and_status_are_compatible():
    app = make_app()
    client = app.test_client()
    response = client.get("/calendly/one", headers={"X-Test-Login": "yes"})
    assert response.status_code == 200
    assert response.get_json() == {
        "appointments": [], "integration": {"configured": False},
        "lookup": {"method": "local", "processed_events": 0, "matched_appointments": 0},
    }
    assert client.get("/calendly/missing", headers={"X-Test-Login": "yes"}).status_code == 404
    assert client.get("/wedof/one", headers={"X-Test-Login": "yes"}).get_json()["resources"]


def test_flask_authentication_and_booking_post_are_unchanged():
    client = make_app().test_client()
    assert client.get("/calendly/one").status_code == 401
    assert client.get("/wedof/one").status_code == 401
    response = client.post("/calendly/one", headers={"X-Test-Login": "yes"}, json={"test": True})
    assert response.status_code == 201
    assert response.get_json() == {"original_post": "one"}


def test_gunicorn_keeps_one_worker_and_bounded_concurrency():
    keys = ("GUNICORN_THREADS", "GUNICORN_TIMEOUT", "GUNICORN_GRACEFUL_TIMEOUT",
            "GUNICORN_MAX_REQUESTS", "GUNICORN_MAX_REQUESTS_JITTER")
    with patch.dict(os.environ, {k: v for k, v in os.environ.items() if k not in keys}, clear=True):
        config = runpy.run_path(str(ROOT / "gunicorn.conf.py"))
    assert config["workers"] == 1
    assert config["threads"] == 8
    assert config["max_requests"] == 1500
    assert config["max_requests_jitter"] == 150
    assert config["graceful_timeout"] >= config["timeout"]
    lines = []
    worker = SimpleNamespace(nr=1, log=SimpleNamespace(info=lambda *args: lines.append(args)))
    config["post_request"](worker, None, None, None)
    config["post_request"](worker, None, None, None)
    assert len(lines) <= 1


def test_memory_optimization_precedes_existing_business_guardrails():
    source = (ROOT / "crm_app.py").read_text()
    assert source.index("install_crm_memory_optimizations(legacy_app)") < source.index(
        "install_crm_location_normalization(legacy_app)")
    assert source.index("install_crm_memory_optimizations(legacy_app)") < source.index(
        "install_crm_pipeline_status_consistency(legacy_app)")
