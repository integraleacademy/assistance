import app as application
from types import SimpleNamespace
from unittest.mock import patch


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


def test_information_form_creates_complete_crm_contact_and_activity_log(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application.app.config.update(TESTING=True, SERVER_NAME="localhost")
    public_client = application.app.test_client()

    with (
        patch.object(application, "creer_piste_salesforce"),
        patch.object(application, "send_email_html", return_value=True),
        patch.object(application, "envoyer_sms_demande_infos_formation", return_value=True),
    ):
        response = public_client.post("/demande-informations-formations", data={
            "nom": "Martin", "prenom": "Lina", "mail": "lina@example.com",
            "telephone": "0612345678", "formation": "SSIAP", "centre": "cote_azur",
            "dates": "Du 12 au 27 octobre 2026", "cpf_consulte": "OUI",
            "cpf_montant": "1200", "france_travail": "NON",
            "financement_perso": "OUI", "identite_numerique": "OUI",
            "cnaps_ok": "OUI", "garde_vue": "NON", "titre_sejour": "OUI",
            "ssiap_secourisme_valide": "OUI", "souhaite_devis": "OUI",
        })

    assert response.status_code == 302
    contact = application.load_data()["crm_contacts"][0]
    assert contact["statut"] == "Nouveaux"
    assert contact["prenom"] == "Lina"
    assert contact["nom"] == "MARTIN"
    assert contact["mail"] == "lina@example.com"
    assert contact["telephone"] == "0612345678"
    assert contact["formation"] == "SSIAP 1"
    assert contact["lieu"] == "Côte d’Azur"
    assert contact["dates_formation"] == "Du 12 au 27 octobre 2026"
    assert contact["cpf"] == "OUI"
    assert contact["financement_ft"] == "NON"
    assert contact["formulaire"]["cpf_montant"] == "1200"
    assert contact["source_demande_id"]
    assert contact["source_devis_id"]
    activities = {activity["title"]: activity for activity in contact["activities"]}
    assert "Formulaire de demande d’informations complété" in activities
    assert "Devis détaillé créé" in activities
    assert "Ouvrir le devis" in activities["Devis détaillé créé"]["preview"]
    assert "E-mail automatique envoyé" in activities
    assert "SMS automatique envoyé" in activities
    assert activities["E-mail automatique envoyé"]["preview"]


def test_crm_pages_and_templates(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    page = c.get("/CRM/pistes")
    assert page.status_code == 200
    assert b"iaconnectcrm.png" in page.data
    assert b"favicon_32x32.png" in page.data
    response = c.post("/api/crm/templates", json={"type": "email", "nom": "Bienvenue", "sujet": "Bonjour", "contenu": "<p>Bienvenue</p>"})
    assert response.status_code == 201
    assert c.get("/api/crm/templates").get_json()["email"][0]["nom"] == "Bienvenue"


def test_relances_page_uses_a_daily_calendar_view(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)

    page = c.get("/CRM/relances")
    assert page.status_code == 200

    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()
    assert "function remindersPage()" in crm_js
    assert 'id="reminderDate" type="date"' in crm_js
    assert "c.relance_date===reminderSelectedDate" in crm_js
    assert "changeReminderDate(-1)" in crm_js
    assert "changeReminderDate(1)" in crm_js
    assert "Aucune relance ce jour-là" in crm_js


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
    assert "<h3>${crmIcon('book')}<span>Formation</span></h3>" in crm_js
    assert "<h3>${crmIcon('shield')}<span>Réglementaire</span></h3>" in crm_js
    assert "<h3>${crmIcon('wallet')}<span>Financement</span></h3>" in crm_js


def test_contact_follow_up_is_displayed_below_activity_log():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    activity_log = crm_js.index("<h2>Journal d’activité</h2>")
    follow_up = crm_js.index("<h3>${crmIcon('activity')}<span>Suivi</span></h3>")
    side_column_end = crm_js.index("</aside></form>", activity_log)

    assert activity_log < follow_up < side_column_end


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


def test_crm_ai_summary_and_message_generation(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={
        "prenom": "Lina", "nom": "Martin", "formation": "APS"
    }).get_json()
    prompts = []

    def fake_ai(system, user, max_tokens=500):
        prompts.append((system, user, max_tokens))
        return "Texte généré."

    monkeypatch.setattr(application, "_crm_ai", fake_ai)

    summary = c.post(f"/api/crm/contacts/{contact['id']}/synthese", json={})
    message = c.post(f"/api/crm/contacts/{contact['id']}/generer-message", json={
        "type": "sms", "instructions": "Confirmer le prochain rendez-vous"
    })

    assert summary.get_json() == {"texte": "Texte généré."}
    assert message.get_json() == {"texte": "Texte généré."}
    assert "6 à 10 phrases" in prompts[0][0]
    assert "prochain rendez-vous prévu" in prompts[0][0]
    assert "sérieux" in prompts[0][0]
    assert "320 caractères maximum" in prompts[1][0]
    assert "Confirmer le prochain rendez-vous" in prompts[1][1]


def test_crm_normalizes_contact_names_on_create_update_and_existing_data(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={
        "prenom": "  jEAN-pIERRE ", "nom": " de la tour "
    }).get_json()
    assert contact["prenom"] == "Jean-Pierre"
    assert contact["nom"] == "DE LA TOUR"

    updated = c.patch(f"/api/crm/contacts/{contact['id']}", json={
        "prenom": "mARIE cLAIRE", "nom": "d'angelo"
    }).get_json()
    assert updated["prenom"] == "Marie Claire"
    assert updated["nom"] == "D'ANGELO"

    data = application.load_data()
    data["crm_contacts"][0]["prenom"] = "lINA"
    data["crm_contacts"][0]["nom"] = "martin"
    application.save_data(data)
    existing = c.get("/api/crm/contacts").get_json()[0]
    assert existing["prenom"] == "Lina"
    assert existing["nom"] == "MARTIN"


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


def test_crm_conversion_prefills_remote_registration_before_changing_status(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin", "formation": "APS"}).get_json()
    c.patch(f"/api/crm/contacts/{contact['id']}", json={
        "mail": "lina@example.com", "telephone": "0600000000", "lieu": "Paris",
        "dates_formation": "Du 1 au 5 septembre 2026", "desp_type": "Initial",
        "commentaires": "Financement validé.",
    })
    monkeypatch.setenv("GESTION_STAGIAIRES_API_URL", "https://gestion.example/api/preremplissage")
    monkeypatch.setenv("GESTION_STAGIAIRES_API_TOKEN", "secret")
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return SimpleNamespace(status_code=201, content=b'{}', json=lambda: {
            "url": "https://gestion.example/inscriptions/nouveau?jeton=temporary",
        })

    monkeypatch.setattr(application.requests, "post", fake_post)
    response = c.post(f"/api/crm/contacts/{contact['id']}/convertir")

    assert response.status_code == 200
    result = response.get_json()
    converted = result["contact"]
    assert converted["statut"] == "Converti"
    assert result["url"] == "https://gestion.example/inscriptions/nouveau?jeton=temporary"
    assert converted["activities"][0]["kind"] == "conversion"
    assert converted["activities"][0]["title"] == "Dossier d’inscription ouvert dans Gestion stagiaires"
    assert "gestion_stagiaire_id" not in converted
    assert captured["json"]["email"] == "lina@example.com"
    assert captured["json"] == {
        "source": "integrale-connect-crm", "crm_contact_id": contact["id"],
        "prenom": "Lina", "nom": "MARTIN", "email": "lina@example.com",
        "telephone": "0600000000", "formation": "APS", "parcours": "Initial",
        "centre": "Paris", "session": "Du 1 au 5 septembre 2026",
        "commentaires": "Financement validé.",
    }
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert "Idempotency-Key" not in captured["headers"]


def test_crm_conversion_does_not_change_status_when_remote_rejects(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin"}).get_json()
    c.patch(f"/api/crm/contacts/{contact['id']}", json={
        "mail": "lina@example.com", "lieu": "Paris", "dates_formation": "Septembre 2026",
    })
    monkeypatch.setenv("GESTION_STAGIAIRES_API_URL", "https://gestion.example/api/preremplissage")
    monkeypatch.setenv("GESTION_STAGIAIRES_API_TOKEN", "secret")
    monkeypatch.setattr(application.requests, "post", lambda *args, **kwargs: SimpleNamespace(
        status_code=422, content=b'{}', json=lambda: {"error": "Session inconnue"}))

    response = c.post(f"/api/crm/contacts/{contact['id']}/convertir")

    assert response.status_code == 502
    assert response.get_json()["error"] == "Session inconnue"
    assert c.get(f"/api/crm/contacts/{contact['id']}").get_json()["statut"] == "Nouveaux"


def test_crm_conversion_javascript_opens_and_closes_registration_tab():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "function conversionModal" not in crm_js
    assert "Inscrire dans Gestion stagiaires" not in crm_js
    open_tab = "const registrationTab=window.open('','_blank')"
    backend_call = "await api(`/api/crm/contacts/${c.id}/convertir`"
    assert open_tab in crm_js
    assert crm_js.index(open_tab) < crm_js.index(backend_call)
    assert "registrationTab.location.href=result.url" in crm_js
    assert "catch(e){registrationTab.close();toast(e.message,true)}" in crm_js
