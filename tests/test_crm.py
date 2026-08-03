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
    page = c.get("/CRM/pistes")
    assert page.status_code == 200
    assert b"iaconnectcrm.png" in page.data
    assert b"favicon_32x32.png" in page.data
    response = c.post("/api/crm/templates", json={"type": "email", "nom": "Bienvenue", "sujet": "Bonjour", "contenu": "<p>Bienvenue</p>"})
    assert response.status_code == 201
    assert c.get("/api/crm/templates").get_json()["email"][0]["nom"] == "Bienvenue"


def test_crm_uses_admin_formation_sessions(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    data = application.load_data()
    data["formation_sessions"] = {
        "paris": {"APS": [{"label": "Du 1 au 5 septembre 2026", "badge": "", "date_examen": "2026-09-05"}]}
    }
    application.save_data(data)

    sessions = c.get("/api/formation-sessions").get_json()
    assert sessions["paris"]["APS"][0]["label"] == "Du 1 au 5 septembre 2026"

    javascript = (application.app.root_path + "/static/crm.js")
    with open(javascript, encoding="utf-8") as source:
        crm_js = source.read()
    assert "Programmer un rappel" in crm_js
    assert "api('/api/formation-sessions')" in crm_js
    assert '<h3>Formation</h3>' in crm_js
    assert '<h3>Réglementaire</h3>' in crm_js
    assert '<h3>Financement</h3>' in crm_js


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


def test_crm_conversion_creates_remote_trainee_before_changing_status(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin", "formation": "APS"}).get_json()
    c.patch(f"/api/crm/contacts/{contact['id']}", json={
        "mail": "lina@example.com", "telephone": "0600000000", "lieu": "Paris",
        "dates_formation": "Du 1 au 5 septembre 2026",
    })
    monkeypatch.setenv("GESTION_STAGIAIRES_API_URL", "https://gestion.example/api/stagiaires")
    monkeypatch.setenv("GESTION_STAGIAIRES_API_TOKEN", "secret")
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return SimpleNamespace(status_code=201, content=b'{}', json=lambda: {
            "id": "stagiaire-42", "url": "https://gestion.example/stagiaires/42",
        })

    monkeypatch.setattr(application.requests, "post", fake_post)
    response = c.post(f"/api/crm/contacts/{contact['id']}/convertir")

    assert response.status_code == 200
    converted = response.get_json()
    assert converted["statut"] == "Converti"
    assert converted["gestion_stagiaire_id"] == "stagiaire-42"
    assert converted["activities"][0]["kind"] == "conversion"
    assert captured["json"]["email"] == "lina@example.com"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["headers"]["Idempotency-Key"] == f"crm-contact-{contact['id']}"


def test_crm_conversion_does_not_change_status_when_remote_rejects(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin"}).get_json()
    c.patch(f"/api/crm/contacts/{contact['id']}", json={
        "mail": "lina@example.com", "lieu": "Paris", "dates_formation": "Septembre 2026",
    })
    monkeypatch.setenv("GESTION_STAGIAIRES_API_URL", "https://gestion.example/api/stagiaires")
    monkeypatch.setenv("GESTION_STAGIAIRES_API_TOKEN", "secret")
    monkeypatch.setattr(application.requests, "post", lambda *args, **kwargs: SimpleNamespace(
        status_code=422, content=b'{}', json=lambda: {"error": "Session inconnue"}))

    response = c.post(f"/api/crm/contacts/{contact['id']}/convertir")

    assert response.status_code == 502
    assert response.get_json()["error"] == "Session inconnue"
    assert c.get(f"/api/crm/contacts/{contact['id']}").get_json()["statut"] == "Nouveaux"
