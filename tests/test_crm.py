import app as application


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    test_client = application.app.test_client()
    with test_client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return test_client


def test_crm_is_private(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application.app.config.update(TESTING=True)
    response = application.app.test_client().get("/CRM")
    assert response.status_code == 302
    assert "/login" in response.location


def test_contact_lifecycle_and_activity(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    created = c.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin", "formation": "APS"})
    assert created.status_code == 201
    contact = created.get_json()
    assert contact["statut"] == "Nouveaux"

    updated = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"statut": "A relancer", "relance_date": "2026-08-10", "carte_pro": "NON"},
    )
    assert updated.status_code == 200
    assert updated.get_json()["activities"][0]["title"] == "Statut : A relancer"

    call = c.post(f"/api/crm/contacts/{contact['id']}/appel", json={"commentaire": "Échange financement."})
    assert call.status_code == 200
    assert call.get_json()["activities"][0]["kind"] == "appel"


def test_crm_pages_and_templates(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    assert c.get("/CRM/pistes").status_code == 200
    response = c.post("/api/crm/templates", json={"type": "email", "nom": "Bienvenue", "sujet": "Bonjour", "contenu": "<p>Bienvenue</p>"})
    assert response.status_code == 201
    assert c.get("/api/crm/templates").get_json()["email"][0]["nom"] == "Bienvenue"
