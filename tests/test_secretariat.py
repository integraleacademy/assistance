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


def test_secretariat_flow_includes_bts_optional_quote_and_calendly(client, monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))
    response = client.get("/secretariat")
    assert response.status_code == 200
    assert b"BTS Management Op\xc3\xa9rationnel de la S\xc3\xa9curit\xc3\xa9" in response.data
    assert b'name="devis" value="OUI"' in response.data
    assert b'name="devis" value="OUI" checked' not in response.data
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


def test_secretariat_requires_asking_for_the_preferred_training_session(client, monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))
    response = client.get("/secretariat")

    assert response.status_code == 200
    assert "Quelles dates de formation souhaitez-vous".encode() in response.data
    assert b'name="formation_date_souhaitee"' in response.data
    assert b"if(!selectedSession)" in response.data
    assert b"formation_date_souhaitee:requestType" in response.data


def test_secretariat_only_exposes_sessions_whose_start_date_has_not_passed(client, monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))
    monkeypatch.setattr(
        application,
        "get_upcoming_formation_sessions",
        lambda store, get_sessions=application.get_upcoming_formation_sessions: get_sessions(
            store, today=application.datetime.date(2026, 5, 1)
        ),
    )

    response = client.get("/secretariat")

    assert response.status_code == 200
    assert b"Du 29 avril au 9 juin 2026" not in response.data
    assert b"Du 26 mai au 29 juin 2026" in response.data


def test_secretariat_form_collects_funding_and_regulatory_information(client, monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))
    response = client.get("/secretariat")

    assert response.status_code == 200
    for field in (
        b"cpf_consulte", b"cpf_montant", b"france_travail", b"ft_refus_ok",
        b"financement_perso", b"identite_numerique", b"cnaps_ok", b"garde_vue",
        b"titre_sejour",
    ):
        assert b'name="' + field + b'"' in response.data
    assert b"Tous les champs sont facultatifs" in response.data
    assert "laissez les deux boutons décochés".encode() not in response.data
    assert b"Avez-vous d\xc3\xa9j\xc3\xa0 consult\xc3\xa9 votre compte CPF" in response.data
    assert b'<select id="cpf_consulte"' not in response.data
    assert response.data.index(b'name="cpf_consulte"') < response.data.index(b'id="cpfMontantField"')
    assert b"function updateCallerQuestions()" in response.data
    assert b"['APS','A3P'].includes(formation.value)" in response.data
    assert b"isCnapsFormation&&!cnapsYes" in response.data
    assert b'data-step="6"' in response.data
    assert response.data.index(b"Proposer un rendez-vous") < response.data.index(
        b"Objet et pr\xc3\xa9cisions sur la demande"
    )


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
    data["crm_contacts"] = []
    monkeypatch.setattr(application, "load_data", lambda: data)
    monkeypatch.setattr(application, "save_data", lambda payload: None)
    crm_calls = []
    monkeypatch.setattr(application, "creer_piste_salesforce", crm_calls.append)

    response = client.post("/api/secretariat/demandes", json={
        "type": "formation",
        "formation": "APS",
        "formation_date_souhaitee": "Côte d’Azur — 8 juillet au 12 août 2026",
        "formation_centre": "cote_azur",
        "formation_session_label": "8 juillet au 12 août 2026",
        "nom": "Camille Martin",
        "telephone": "0600000000",
        "rdv": "06/08/2026 10:30",
        "statut": "Traité",
        "devis": "OUI",
        "calendly_url": "https://calendly.com/integraleacademy/aps",
        "cpf_consulte": "OUI", "cpf_montant": "1250",
        "france_travail": "OUI", "ft_refus_ok": "NON",
        "financement_perso": "OUI", "identite_numerique": "OUI",
        "cnaps_ok": "NON", "garde_vue": "NON", "titre_sejour": "OUI",
    })

    assert response.status_code == 201
    assert data["secretariat_demandes"][0]["formation"] == "APS"
    assert data["secretariat_demandes"][0]["formation_date_souhaitee"] == "Côte d’Azur — 8 juillet au 12 août 2026"
    assert data["secretariat_demandes"][0]["nom"] == "Camille Martin"
    assert data["secretariat_demandes"][0]["devis"] == "OUI"
    assert crm_calls[0]["prenom"] == "Camille"
    assert crm_calls[0]["nom"] == "Martin"
    assert crm_calls[0]["formation"] == "APS"
    assert crm_calls[0]["source_formulaire"] == "assistant-secretariat"
    contact = data["crm_contacts"][0]
    assert response.get_json()["crm_contact_id"] == contact["id"]
    assert contact["prenom"] == "Camille"
    assert contact["nom"] == "MARTIN"
    assert contact["telephone"] == "0600000000"
    assert contact["formation"] == "APS"
    assert contact["lieu"] == "Côte d’Azur"
    assert contact["dates_formation"] == "8 juillet au 12 août 2026"
    assert contact["statut"] == "Nouveaux"
    assert contact["origine"] == "Secrétariat"
    assert contact["cpf"] == "OUI"
    assert contact["cpf_montant"] == "1250.00"
    assert contact["financement_ft"] == "OUI"
    assert contact["refus_ft_perso"] == "NON"
    assert contact["reste_a_charge_perso"] == "OUI"
    assert contact["identite_creation"] == "OUI"
    assert contact["carte_pro"] == "NON"
    assert contact["garde_vue"] == "NON"
    assert contact["titre_sejour"] == "OUI"
    assert crm_calls[0]["origine"] == "Secrétariat"
    assert crm_calls[0]["cpf_montant"] == "1250"
    assert contact["source_secretariat_id"] == data["secretariat_demandes"][0]["id"]
    assert contact["activities"][0]["title"] == "Piste créée depuis le secrétariat"


def test_secretariat_crm_recovers_centre_and_date_from_legacy_session_value():
    entry = {
        "id": "secretariat-legacy", "formation": "A3P",
        "formation_date_souhaitee": (
            "Intégrale Academy Côte d’Azur — Du 9 novembre 2026 au 19 janvier 2027"
        ),
    }
    data = {"crm_contacts": []}

    contact = application._crm_create_contact_from_secretariat(
        data, entry, {"prenom": "Camille", "nom": "Martin"}
    )

    assert contact["lieu"] == "Côte d’Azur"
    assert contact["dates_formation"] == "Du 9 novembre 2026 au 19 janvier 2027"


def test_secretariat_api_does_not_duplicate_crm_contact_when_completing_request(client, monkeypatch):
    data = dict(application.DEFAULT_DATA)
    data["secretariat_demandes"] = [{
        "id": "secretariat-1", "type": "formation", "formation": "APS",
        "nom": "Camille Martin", "telephone": "0600000000", "email": "",
        "notes": "", "devis": "OUI", "rdv": "", "calendly_url": "",
        "statut": "RDV à prendre", "created_at": "2026-08-06T10:00:00+02:00",
        "date": "06/08/2026 10:00",
    }]
    data["crm_contacts"] = [{"id": "crm-1", "source_secretariat_id": "secretariat-1"}]
    monkeypatch.setattr(application, "load_data", lambda: data)
    monkeypatch.setattr(application, "save_data", lambda payload: None)
    crm_calls = []
    monkeypatch.setattr(application, "creer_piste_salesforce", crm_calls.append)

    response = client.post("/api/secretariat/demandes", json={
        "type": "formation", "formation": "APS", "nom": "Camille Martin",
        "telephone": "0600000000", "statut": "Traité", "rdv": "Calendly proposé",
    })

    assert response.status_code == 201
    assert response.get_json()["crm_contact_id"] is None
    assert len(data["crm_contacts"]) == 1
    assert crm_calls == []


def test_secretariat_sends_ai_call_summary_email_and_commercial_sms(client, monkeypatch):
    data = dict(application.DEFAULT_DATA)
    data["secretariat_demandes"] = []
    data["crm_contacts"] = []
    data["crm_email_templates"] = [
        {"id": "mail-aps", "nom": "Informations APS", "sujet": "Votre APS", "contenu": "<p>Contenu APS</p>"},
        {"id": "mail-vtc", "nom": "Informations VTC", "sujet": "Votre VTC", "contenu": "Autre"},
    ]
    data["crm_sms_templates"] = [
        {"id": "sms-aps", "nom": "informations aps", "sujet": "", "contenu": "SMS APS"},
    ]
    monkeypatch.setattr(application, "load_data", lambda: data)
    monkeypatch.setattr(application, "save_data", lambda payload: None)
    monkeypatch.setattr(application, "creer_piste_salesforce", lambda payload: None)
    ai_calls = []
    monkeypatch.setattr(application, "_crm_ai", lambda system, user, max_tokens: ai_calls.append(
        (system, user, max_tokens)) or '''{"summary_paragraphs":["Merci pour cet échange consacré à votre projet APS et à vos objectifs professionnels.","Nous avons identifié ensemble les vérifications utiles avant de confirmer votre entrée en formation."],"financing_message":"","cnaps_message":"Notre équipe vous accompagne pour l’autorisation préalable CNAPS.","next_steps":["Consulter le dossier.","Confirmer la session."]}''')
    emails, sms = [], []
    monkeypatch.setattr(application, "send_email_html", lambda *args, **kwargs: emails.append(args) or True)
    monkeypatch.setattr(application, "send_sms", lambda *args: sms.append(args) or True)

    response = client.post("/api/secretariat/demandes", json={
        "type": "formation", "formation": "APS", "prenom": "Camille",
        "nom": "Camille Martin", "email": "camille@example.com", "telephone": "0600000000",
    })

    assert response.status_code == 201
    assert response.get_json()["messages"] == {"email": "sent", "sms": "sent"}
    assert emails[0][0] == "camille@example.com"
    assert "Agent de Prévention et de Sécurité" in emails[0][1]
    assert "Merci pour cet échange consacré" in emails[0][2]
    assert "Le résumé de notre échange" in emails[0][3]
    assert "Télécharger le dossier de présentation" in emails[0][3]
    assert "N'invente aucune information" in ai_calls[0][0]
    assert '"formation": "Agent de Prévention et de Sécurité (APS)"' in ai_calls[0][1]
    assert "Je fais suite à notre échange téléphonique" in sms[0][1]
    assert "https://www.integralesecuriteformations.com/dossiersfc" in sms[0][1]
    assert "Cassandre MENARD" in sms[0][1]
    entry = data["secretariat_demandes"][0]
    assert entry["information_email_sent_at"]
    assert entry["information_sms_sent_at"]
    assert [activity["kind"] for activity in data["crm_contacts"][0]["activities"][:2]] == ["sms", "email"]


def test_secretariat_summary_template_is_safe_complete_and_has_no_unwanted_appointment(monkeypatch):
    monkeypatch.setattr(application, "_crm_ai", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    entry, contact = application._secretariat_preview_data(False)
    with application.app.test_request_context():
        subject, plain, rendered = application._build_secretariat_followup_email(entry, contact)

    assert subject == "Votre projet APS – le résumé de notre échange"
    assert rendered.count("Bonjour Clément") == 1
    assert "cid:integrale-academy-logo" in rendered
    assert "Faites le premier pas vers votre futur métier" in rendered
    assert "Votre rendez-vous" not in rendered
    assert "Prendre un RDV téléphonique" in rendered
    assert "Formation Formation" not in rendered
    assert "Nos prochaines dates" in rendered
    assert "Certification et objectif" in rendered
    assert rendered.count("5 semaines") == 1
    assert "exercer le métier d’agent de surveillance humaine" in rendered
    assert "carte professionnelle ou d&#39;une autorisation préalable" in rendered
    assert "Un projet de formation ? Notre équipe vous accompagne dans toutes vos démarches" in rendered
    assert "Cette démarche est réalisée par nos soins" in rendered
    assert "Il a été généré à partir de la formation" not in rendered
    assert "Ce lien est personnel et vous permet" not in rendered
    assert "financement en attente" not in rendered.lower()
    assert "Carte professionnelle CNAPS : Non valide" not in rendered
    assert "Télécharger le dossier de présentation" in rendered
    assert "SIREN 840 899 884" in rendered
    assert "None" not in rendered and "undefined" not in rendered
    assert len(application._secretariat_email_fallback(entry)["summary_paragraphs"]) >= 2


def test_secretariat_first_name_restores_clement_without_removing_accents():
    assert application._secretariat_display_first_name("Clement") == "Clément"
    assert application._secretariat_display_first_name("ÉLODIE") == "Élodie"


def test_secretariat_rejects_a_repeated_thank_you_from_ai():
    entry, _ = application._secretariat_preview_data(False)
    fallback = application._secretariat_email_fallback(entry)
    raw = {
        "summary_paragraphs": [
            "Merci pour le temps consacré à votre projet.",
            "Vous souhaitez confirmer votre session.",
        ],
        "financing_message": "", "cnaps_message": "", "next_steps": [],
    }
    assert application._validate_secretariat_ai_content(raw, fallback, entry) is fallback


def test_secretariat_quote_creation_is_idempotent():
    entry, contact = application._secretariat_preview_data(False)
    entry.update({"prenom": "Clement", "nom_famille": "Martin",
                  "formation_centre": "cote_azur",
                  "formation_session_label": "Du 7 septembre au 9 octobre 2026",
                  "formation_date_examen": "12 octobre 2026"})
    data = {"demandes": []}
    with application.app.test_request_context(base_url="https://example.test"):
        first = application._ensure_secretariat_quote(data, entry, contact)
        second = application._ensure_secretariat_quote(data, entry, contact)
    assert first is second
    assert len(data["demandes"]) == 1
    assert first["prenom"] == "Clément"
    assert first["statut_devis"] == "A envoyer"
    assert "/plan/" in entry["devis_url"]


def test_secretariat_quote_is_not_created_when_not_requested():
    entry, contact = application._secretariat_preview_data(False)
    entry["devis"] = "NON"
    data = {"demandes": []}
    assert application._ensure_secretariat_quote(data, entry, contact) is None
    assert data["demandes"] == []


def test_secretariat_scheduled_appointment_and_other_training(monkeypatch):
    monkeypatch.setattr(application, "_crm_ai", lambda *args, **kwargs: "invalid-json")
    entry, contact = application._secretariat_preview_data(True)
    entry["formation"] = "VTC"
    with application.app.test_request_context():
        subject, _, rendered = application._build_secretariat_followup_email(entry, contact)
    sms = application._build_secretariat_followup_sms("VTC")

    assert "VTC" in subject and application.SECRETARIAT_FORMATIONS["VTC"]["label"] in rendered
    assert "Votre rendez-vous" in rendered
    assert "15 septembre 2026" in rendered and "10:30" in rendered and "visioconférence" in rendered
    assert "Vous n'avez pas encore planifié" not in rendered
    assert application.SECRETARIAT_FORMATIONS["VTC"]["label"] in sms


def test_secretariat_france_travail_wish_is_not_described_as_pending():
    entry, _ = application._secretariat_preview_data(False)
    fallback = application._secretariat_email_fallback(entry)
    assert "souhaitez étudier avec notre équipe la possibilité" in fallback["financing_message"]
    assert "en attente" not in fallback["financing_message"]


def test_secretariat_does_not_resend_templates_when_request_is_completed_twice(client, monkeypatch):
    data = dict(application.DEFAULT_DATA)
    data["secretariat_demandes"] = [{
        "id": "secretariat-1", "type": "formation", "formation": "APS",
        "nom": "Camille Martin", "telephone": "0600000000", "email": "camille@example.com",
        "statut": "RDV à prendre", "created_at": "2026-08-06T10:00:00+02:00", "date": "06/08/2026 10:00",
        "information_email_template_id": "mail-aps", "information_sms_template_id": "sms-aps",
    }]
    data["crm_contacts"] = [{"id": "crm-1", "source_secretariat_id": "secretariat-1", "activities": []}]
    data["crm_email_templates"] = [{"id": "mail-aps", "nom": "Informations APS", "sujet": "APS", "contenu": "Mail"}]
    data["crm_sms_templates"] = [{"id": "sms-aps", "nom": "Informations APS", "contenu": "SMS"}]
    monkeypatch.setattr(application, "load_data", lambda: data)
    monkeypatch.setattr(application, "save_data", lambda payload: None)
    monkeypatch.setattr(application, "creer_piste_salesforce", lambda payload: None)
    monkeypatch.setattr(application, "send_email_html", lambda *args: pytest.fail("email resent"))
    monkeypatch.setattr(application, "send_sms", lambda *args: pytest.fail("SMS resent"))

    response = client.post("/api/secretariat/demandes", json={
        "type": "formation", "formation": "APS", "nom": "Camille Martin",
        "email": "camille@example.com", "telephone": "0600000000", "statut": "Traité",
    })

    assert response.status_code == 201
    assert response.get_json()["messages"] == {"email": "already_sent", "sms": "already_sent"}


@pytest.mark.parametrize(("formation", "template_name"), [
    ("APS", "Informations APS"),
    ("A3P", "Informations A3P"),
    ("DESP_INIT", "Informations DESP"),
    ("DESP_VAE", "Informations DESP"),
    ("SSIAP", "Informations SSIAP"),
    ("VTC", "Informations VTC"),
    ("BTS_MOS", "Informations BTS MOS"),
])
def test_secretariat_matches_information_template_for_each_training(formation, template_name):
    expected = {"id": formation, "nom": template_name, "contenu": "Informations"}
    data = {"crm_email_templates": [expected]}

    assert application._secretariat_information_template(data, "email", formation) == expected


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


def test_secretariat_request_summary_reformulates_details_with_training_context(client, monkeypatch):
    calls = []
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))
    monkeypatch.setattr(
        application, "_crm_ai",
        lambda system, user, max_tokens: calls.append((system, user, max_tokens)) or "Résumé CRM final",
    )

    response = client.post("/api/secretariat/ai/request-summary", json={
        "type": "formation", "formation": "A3P", "nom": "Camille Martin",
        "cpf_consulte": "OUI", "rdv": "Calendly proposé",
        "summary": "Premier résumé", "precision": "veut commencer vite",
        "unexpected": "ne doit pas être transmis",
    })

    assert response.status_code == 200
    assert response.get_json()["summary"] == "Résumé CRM final"
    assert "Camille Martin" in calls[0][1]
    assert "Premier résumé" in calls[0][1]
    assert "veut commencer vite" in calls[0][1]
    assert "4 200 € TTC" in calls[0][1]
    assert "ne doit pas être transmis" not in calls[0][1]
    assert "N'invente aucune information" in calls[0][0]
    assert calls[0][2] == 700


def test_secretariat_request_summary_accepts_other_request_without_training(client, monkeypatch):
    monkeypatch.setattr(application, "_crm_ai", lambda system, user, max_tokens: "Demande administrative")

    response = client.post("/api/secretariat/ai/request-summary", json={
        "type": "autre", "rdv": "Non souhaité", "precision": "duplicata de facture",
    })

    assert response.status_code == 200
    assert response.get_json()["summary"] == "Demande administrative"


def test_secretariat_question_route_validates_input(client):
    too_long = client.post("/api/secretariat/formations/A3P/ai/question",
                           json={"question": "x" * 501})
    unknown = client.post("/api/secretariat/formations/UNKNOWN/ai/question",
                          json={"question": "Tarif ?"})

    assert too_long.status_code == 400
    assert unknown.status_code == 404
