from types import SimpleNamespace

import requests

import app as application


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    test_client = application.app.test_client()
    with test_client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return test_client


def converted_contact(client):
    contact = client.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin"}).get_json()
    client.patch(f"/api/crm/contacts/{contact['id']}", json={"statut": "Converti"})
    return contact


def response(status, payload):
    return SimpleNamespace(status_code=status, content=b"{}", json=lambda: payload)


def test_reglementaire_forwards_bearer_and_returns_cnaps_data(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    contact = converted_contact(test_client)
    monkeypatch.setenv("GESTION_STAGIAIRES_API_URL", "https://gestion.example/api/integrations/crm")
    monkeypatch.setenv("GESTION_STAGIAIRES_API_TOKEN", "top-secret")
    captured = {}
    payload = {"linked": True, "statut_cnaps": "ACCEPTÉ", "titres": [{"type": "AP SH", "etat": "ACTIF"}], "token": "top-secret"}
    monkeypatch.setattr(application.requests, "get", lambda url, **kwargs: (captured.update(url=url, **kwargs) or response(200, payload)))

    result = test_client.get(f"/api/crm/contacts/{contact['id']}/reglementaire")

    assert result.status_code == 200
    assert result.get_json() == {"linked": True, "statut_cnaps": "ACCEPTÉ", "titres": [{"type": "AP SH", "etat": "ACTIF"}]}
    assert captured["params"] == {"crm_contact_id": contact["id"]}
    assert captured["headers"] == {"Authorization": "Bearer top-secret", "Accept": "application/json"}
    assert b"top-secret" not in result.data


def test_reglementaire_maps_unlinked_registration(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    contact = converted_contact(test_client)
    monkeypatch.setenv("GESTION_STAGIAIRES_API_URL", "https://gestion.example/api")
    monkeypatch.setenv("GESTION_STAGIAIRES_API_TOKEN", "secret")
    monkeypatch.setattr(application.requests, "get", lambda *args, **kwargs: response(404, {"error": "not found"}))
    result = test_client.get(f"/api/crm/contacts/{contact['id']}/reglementaire")
    assert result.status_code == 200
    assert result.get_json() == {"linked": False, "message": "L’inscription n’a pas encore été enregistrée dans Gestion stagiaires."}


def test_reglementaire_handles_configuration_and_remote_errors(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    contact = converted_contact(test_client)
    missing = test_client.get(f"/api/crm/contacts/{contact['id']}/reglementaire")
    assert missing.status_code == 503
    monkeypatch.setenv("GESTION_STAGIAIRES_API_URL", "https://gestion.example/api")
    monkeypatch.setenv("GESTION_STAGIAIRES_API_TOKEN", "secret")
    monkeypatch.setattr(application.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(requests.Timeout()))
    unavailable = test_client.get(f"/api/crm/contacts/{contact['id']}/reglementaire")
    assert unavailable.status_code == 502
    assert "indisponible" in unavailable.get_json()["error"]


def test_cnaps_frontend_is_read_only_and_loads_for_converted_contacts():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        javascript = source.read()
    assert "Suivi CNAPS — Gestion stagiaires" in javascript
    assert "Carte professionnelle déclarée par le prospect" in javascript
    assert "AP SH" not in javascript  # Le titre affiché vient bien de l'API.
    assert "title.expires_before_training===true" in javascript
    assert "<strong>${esc(name)}</strong>" in javascript
    assert "${esc(state)}" in javascript
    assert "if(c.statut==='Converti')loadReglementaire(c)" in javascript
    assert "/reglementaire`))" in javascript
    assert "GESTION_STAGIAIRES_API_TOKEN" not in javascript
