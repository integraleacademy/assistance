from pathlib import Path
from types import SimpleNamespace

import requests

import app as application
import crm_app as production
import crm_cnaps_tracking


def client(tmp_path, monkeypatch, flask_app=application.app):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    flask_app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    test_client = flask_app.test_client()
    with test_client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return test_client


def contact(client):
    return client.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin"}).get_json()


def response(status, payload):
    return SimpleNamespace(status_code=status, content=b"{}", json=lambda: payload)


def configure(monkeypatch):
    monkeypatch.setenv("GESTION_STAGIAIRES_API_URL", "https://gestion.example/api/preremplissage?x=1")
    monkeypatch.setenv("GESTION_STAGIAIRES_API_TOKEN", "top-secret")


def test_app_entrypoint_calls_stagiaires_by_permanent_crm_id(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    lead = contact(test_client)
    configure(monkeypatch)
    captured = {}
    payload = {
        "trainee": {"id": 7, "public_token": "remove"},
        "cnaps": {"status": "ACCEPTÉ", "authorization": "remove"},
        "card_pro": {"titles": [{"title": "AP SH", "trainee_token": "remove"}]},
        "vae": {"applicable": True, "nested": [{"api_token": "remove", "ok": True}]},
    }
    monkeypatch.setattr(application.requests, "get", lambda url, **kwargs: (captured.update(url=url, **kwargs) or response(200, payload)))

    result = test_client.get(f"/api/crm/contacts/{lead['id']}/reglementaire")

    assert result.status_code == 200
    assert captured["url"] == "https://gestion.example/api/integrations/crm/stagiaires"
    assert captured["params"] == {"crm_contact_id": lead["id"]}
    assert "nom" not in captured["params"] and "prenom" not in captured["params"]
    assert captured["headers"] == {"Authorization": "Bearer top-secret", "Accept": "application/json"}
    body = result.get_json()
    assert set(body) == {"trainee", "cnaps", "card_pro", "vae"}
    assert body["vae"]["nested"] == [{"ok": True}]
    assert all(secret not in result.data for secret in (b"top-secret", b"public_token", b"trainee_token", b"api_token", b"authorization"))


def test_production_entrypoint_installs_same_proxy(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch, production.app)
    lead = contact(test_client)
    configure(monkeypatch)
    captured = {}
    monkeypatch.setattr(crm_cnaps_tracking.requests, "get", lambda url, **kwargs: (captured.update(url=url, **kwargs) or response(200, {"vae": {"applicable": True}})))
    result = test_client.get(f"/api/crm/contacts/{lead['id']}/reglementaire")
    assert result.status_code == 200
    assert captured["url"].endswith("/api/integrations/crm/stagiaires")
    assert captured["params"] == {"crm_contact_id": lead["id"]}


def test_remote_business_statuses_are_preserved(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    lead = contact(test_client)
    configure(monkeypatch)
    for status in (400, 401, 404, 409):
        monkeypatch.setattr(crm_cnaps_tracking.requests, "get", lambda *args, _status=status, **kwargs: response(_status, {"error": "remote technical detail", "token": "leak"}))
        result = test_client.get(f"/api/crm/contacts/{lead['id']}/reglementaire")
        assert result.status_code == status
        assert b"remote technical detail" not in result.data
        assert b"leak" not in result.data


def test_timeout_and_remote_unavailability_are_safe(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    lead = contact(test_client)
    configure(monkeypatch)
    for error in (requests.Timeout(), requests.ConnectionError()):
        monkeypatch.setattr(crm_cnaps_tracking.requests, "get", lambda *args, _error=error, **kwargs: (_ for _ in ()).throw(_error))
        result = test_client.get(f"/api/crm/contacts/{lead['id']}/reglementaire")
        assert result.status_code == 502
        assert result.get_json() == {"error": "Gestion Stagiaires est momentanément indisponible"}


def test_frontend_has_shared_cnaps_vae_loading_and_safe_rendering():
    javascript = (Path(application.app.root_path) / "static/crm.js").read_text(encoding="utf-8")
    assert "const isDespVae=" in javascript
    assert "['APS','A3P']" in javascript
    assert "needsCnaps(c)&&!isDespVae(c)" in javascript
    assert javascript.count("/reglementaire`") == 1
    assert "renderReglementaire(c,data);renderVaeTracking(c,data.vae)" in javascript
    for text in ("Suivi du dossier VAE", "Récupération du suivi VAE…", "Aucun dossier VAE administratif", "Plusieurs dossiers VAE", "Des compléments sont demandés", "Diplôme obtenu"):
        assert text in javascript
    assert "vae.applicable===false" in javascript
    assert "target=\"_blank\" rel=\"noopener noreferrer\"" in javascript
    assert "dossier.admin_url:vae.trainee_admin_url" in javascript
    assert "GESTION_STAGIAIRES_API_TOKEN" not in javascript
    assert "loadVae" not in javascript
