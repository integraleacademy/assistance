import pytest

import app as application


@pytest.fixture
def client():
    application.app.config.update(TESTING=True)
    return application.app.test_client()


def test_secretariat_page_starts_with_the_two_request_types(client, monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))
    response = client.get("/secretariat")
    assert response.status_code == 200
    assert b"Renseignements formation" in response.data
    assert b"Autre demande" in response.data
    assert b'data-request="formation"' in response.data
    assert b'data-request="autre"' in response.data


def test_secretariat_flow_includes_bts_default_quote_and_optional_calendly(client, monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))
    response = client.get("/secretariat")
    assert response.status_code == 200
    assert b"BTS Management Op\xc3\xa9rationnel de la S\xc3\xa9curit\xc3\xa9" in response.data
    assert b'name="devis" value="OUI" checked' in response.data
    assert b'id="calendlyLink"' in response.data
    assert b"ne souhaite pas de rendez-vous" in response.data


def test_secretariat_displays_formations_as_modern_buttons(client, monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))
    response = client.get("/secretariat")

    assert response.status_code == 200
    assert b'class="formation-choices"' in response.data
    assert b'class="formation-choice"' in response.data
    assert b'data-formation-code="APS"' in response.data
    assert b'<select id="formation"' not in response.data


def test_secretariat_displays_training_details_before_caller_form(client, monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))
    response = client.get("/secretariat")

    assert response.status_code == 200
    assert b"Les informations cl\xc3\xa9s de la formation" in response.data
    assert b"Financements possibles" in response.data
    assert b"Prochaines dates" in response.data
    assert b"protection rapproch" in response.data


def test_secretariat_uses_a_direct_calendly_link_instead_of_a_blocked_embed(client, monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))
    response = client.get("/secretariat")

    assert response.status_code == 200
    assert b'id="calendlyLink"' in response.data
    assert b'target="_blank"' in response.data
    assert b"Voir les cr\xc3\xa9neaux disponibles" in response.data
    assert b"Calendly ne peut pas \xc3\xaatre affich\xc3\xa9 ici" not in response.data

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
        "devis": "OUI",
        "calendly_url": "https://calendly.com/integraleacademy/aps",
    })

    assert response.status_code == 201
    assert data["secretariat_demandes"][0]["formation"] == "APS"
    assert data["secretariat_demandes"][0]["nom"] == "Camille Martin"
    assert data["secretariat_demandes"][0]["devis"] == "OUI"


def test_secretariat_api_rejects_unknown_request_type(client):
    response = client.post("/api/secretariat/demandes", json={"type": "inconnu"})

    assert response.status_code == 400
