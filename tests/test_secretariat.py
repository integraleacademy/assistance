import pytest

import app as application


@pytest.fixture
def client():
    application.app.config.update(TESTING=True)
    return application.app.test_client()


def test_secretariat_page_lists_formations_and_all_sessions(client, monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))

    response = client.get("/secretariat")

    assert response.status_code == 200
    assert b"Portail secr" in response.data
    assert b"A3P" in response.data
    assert b"30 juin au 2 septembre 2026" in response.data
    assert b"9 novembre 2026 au 19 janvier 2027" in response.data


def test_secretariat_api_records_a_request(client, monkeypatch):
    data = dict(application.DEFAULT_DATA)
    data["secretariat_demandes"] = []
    monkeypatch.setattr(application, "load_data", lambda: data)
    monkeypatch.setattr(application, "save_data", lambda payload: None)

    response = client.post("/api/secretariat/demandes", json={
        "type": "formation",
        "formation": "APS",
        "nom": "Camille Martin",
        "telephone": "0600000000",
        "rdv": "06/08/2026 10:30",
        "statut": "Traité",
    })

    assert response.status_code == 201
    assert data["secretariat_demandes"][0]["formation"] == "APS"
    assert data["secretariat_demandes"][0]["nom"] == "Camille Martin"


def test_secretariat_api_rejects_unknown_request_type(client):
    response = client.post("/api/secretariat/demandes", json={"type": "inconnu"})

    assert response.status_code == 400
