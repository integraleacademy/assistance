import app as application
from types import SimpleNamespace


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


def test_crm_rephrase_uses_chat_completion(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    create = lambda **kwargs: SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content="Compte-rendu reformulé."),
    )])
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(application, "OpenAI", lambda **kwargs: fake_client)

    response = c.post("/api/crm/reformuler", json={"texte": "note brute"})

    assert response.status_code == 200
    assert response.get_json() == {"texte": "Compte-rendu reformulé."}


def test_crm_email_has_branding_and_legal_footer(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    created = c.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin"}).get_json()
    c.patch(f"/api/crm/contacts/{created['id']}", json={"mail": "lina@example.com"})
    captured = {}

    def fake_send(recipient, subject, plain_text, html_body):
        captured["html"] = html_body
        return True

    monkeypatch.setattr(application, "send_email_html", fake_send)
    response = c.post(
        f"/api/crm/contacts/{created['id']}/message",
        json={"type": "email", "sujet": "Bienvenue", "contenu": "<p>Message</p>"},
    )

    assert response.status_code == 200
    assert "Logo_Integrale_Academy_officielpdf" in captured["html"]
    assert "Faites le premier pas vers votre futur métier" in captured["html"]
    assert "SIREN 840 899 884" in captured["html"]
    assert "Votre avenir, notre engagement" not in captured["html"]
