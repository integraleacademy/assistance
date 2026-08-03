import pytest

import app as application


@pytest.fixture
def client():
    application.app.config.update(TESTING=True)
    return application.app.test_client()


def test_secretariat_page_explains_the_call_flow_and_has_one_start_button(client, monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))

    response = client.get("/secretariat")

    assert response.status_code == 200
    assert b"Assistant du secr\xc3\xa9tariat" in response.data
    assert b"Comprendre la demande" in response.data
    assert b"Choisir la bonne formation" in response.data
    assert b"Pr\xc3\xa9parer la suite" in response.data
    assert response.data.count(b'class="cta"') == 1
    assert b"/demande-informations-formations?secretariat=1" in response.data


def test_secretariat_training_form_adds_appointment_and_comments(client, monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))

    response = client.get("/demande-informations-formations?secretariat=1")

    assert response.status_code == 200
    assert b"Assistant de prise d'appel" in response.data
    assert b'name="rdv_telephonique"' in response.data
    assert b'name="commentaires_secretariat"' in response.data
    assert b"Enregistrer et terminer" in response.data


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
