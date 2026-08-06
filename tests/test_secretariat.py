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


def test_secretariat_includes_a_training_search(client, monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))
    response = client.get("/secretariat")

    assert response.status_code == 200
    assert b'id="formationSearch"' in response.data
    assert b'placeholder="Rechercher par nom ou sigle' in response.data
    assert b'id="clearFormationSearch"' in response.data
    assert b'id="formationEmpty" role="status"' in response.data
    assert b"function filterFormations()" in response.data


def test_secretariat_displays_training_details_before_caller_form(client, monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))
    response = client.get("/secretariat")

    assert response.status_code == 200
    assert b"La fiche pratique de la formation" in response.data
    assert b"Financements possibles" in response.data
    assert b"Pr\xc3\xa9requis \xc3\xa0 v\xc3\xa9rifier" in response.data
    assert b"Ce que l\xe2\x80\x99appelant va apprendre" in response.data
    assert b"Prochaines dates" in response.data
    assert b"protection rapproch" in response.data
    assert b"Demander \xc3\xa0 l\xe2\x80\x99assistant IA" in response.data


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
    crm_calls = []
    monkeypatch.setattr(application, "creer_piste_salesforce", crm_calls.append)

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
    assert crm_calls[0]["prenom"] == "Camille"
    assert crm_calls[0]["nom"] == "Martin"
    assert crm_calls[0]["formation"] == "APS"
    assert crm_calls[0]["source_formulaire"] == "assistant-secretariat"


def test_secretariat_api_rejects_unknown_request_type(client):
    response = client.post("/api/secretariat/demandes", json={"type": "inconnu"})

    assert response.status_code == 400


def test_secretariat_ai_uses_selected_training_context(client, monkeypatch):
    calls = []

    def fake_ai(system, user, max_tokens):
        calls.append((system, user, max_tokens))
        return "La formation dure 175 heures. Faites confirmer les prérequis par l'équipe."

    monkeypatch.setattr(application, "_crm_ai", fake_ai)
    response = client.post("/api/secretariat/assistant", json={
        "formation": "APS",
        "message": "Combien de temps dure la formation ?",
    })

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert "175 h" in calls[0][1]
    assert "N'invente jamais" in calls[0][0]


def test_secretariat_ai_rejects_unknown_training(client):
    response = client.post("/api/secretariat/assistant", json={
        "formation": "INCONNUE",
        "message": "Quel est le tarif ?",
    })

    assert response.status_code == 400


def test_secretariat_exposes_both_ai_features(client, monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))
    response = client.get("/secretariat")

    assert response.status_code == 200
    assert b"Poser une question \xc3\xa0 l\xe2\x80\x99IA sur cette formation" in response.data
    assert b"G\xc3\xa9n\xc3\xa9rer les informations cl\xc3\xa9s avec l\xe2\x80\x99IA" in response.data
    assert b"Recherche en cours\xe2\x80\xa6" in response.data
    assert b"Copier la r\xc3\xa9ponse" in response.data
    assert b"R\xc3\xa9g\xc3\xa9n\xc3\xa9rer" in response.data


def test_secretariat_question_route_uses_a3p_data_and_conversation(client, monkeypatch):
    calls = []
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))
    monkeypatch.setattr(application, "_crm_ai",
                        lambda system, user, max_tokens: calls.append((system, user)) or "Réponse fiable")

    response = client.post("/api/secretariat/formations/A3P/ai/question", json={
        "question": "Faut-il une autorisation du CNAPS pour entrer en formation ?",
        "conversation": [{"question": "Quel tarif ?", "answer": "4 200 € TTC"}],
    })

    assert response.status_code == 200
    assert response.get_json()["reply"] == "Réponse fiable"
    assert "4 200 € TTC" in calls[0][1]
    assert "328 h" in calls[0][1]
    assert "Présentiel" in calls[0][1]
    assert "CNAPS" in calls[0][1]
    assert "Cette information n’est pas disponible" in calls[0][0]


def test_secretariat_key_information_route_uses_server_context(client, monkeypatch):
    calls = []
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))
    monkeypatch.setattr(application, "_crm_ai",
                        lambda system, user, max_tokens: calls.append(user) or "Synthèse A3P")

    response = client.post("/api/secretariat/formations/A3P/ai/key-information", json={
        "price": "1 €", "duration": "1 h",
    })

    assert response.status_code == 200
    assert response.get_json()["summary"] == "Synthèse A3P"
    assert "4 200 € TTC" in calls[0]
    assert '"price": "1 €"' not in calls[0]


def test_secretariat_question_route_validates_input(client):
    too_long = client.post("/api/secretariat/formations/A3P/ai/question",
                           json={"question": "x" * 501})
    unknown = client.post("/api/secretariat/formations/UNKNOWN/ai/question",
                          json={"question": "Tarif ?"})

    assert too_long.status_code == 400
    assert unknown.status_code == 404
