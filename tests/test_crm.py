from types import SimpleNamespace
from unittest.mock import patch

import pytest

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


def test_admin_can_read_live_brevo_sms_credits(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setenv("BREVO_API_KEY", "test-key")
    monkeypatch.setattr(application, "send_email_html", lambda *args: True)
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {"plan": [{"type": "sms", "credits": 125.5}]},
    )
    with patch.object(application.requests, "get", return_value=response) as get:
        result = c.get("/api/crm/brevo/sms-credits")

    assert result.status_code == 200
    assert result.get_json() == {"credits": 125.5}
    get.assert_called_once_with(
        "https://api.brevo.com/v3/account",
        headers={"accept": "application/json", "api-key": "test-key"},
        timeout=10,
    )


def test_low_brevo_sms_balance_alerts_both_recipients_only_once(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setattr(application, "_brevo_sms_credits", lambda: 220.5)
    deliveries = []
    monkeypatch.setattr(
        application,
        "send_email_html",
        lambda *args: deliveries.append(args) or True,
    )

    assert c.get("/api/crm/brevo/sms-credits").status_code == 200
    assert c.get("/api/crm/brevo/sms-credits").status_code == 200

    assert len(deliveries) == 1
    recipients, subject, plain_text, html_body = deliveries[0]
    assert recipients == (
        "clement@integraleacademy.com",
        "cassandre@integraleacademy.com",
    )
    assert "moins de 50 SMS" in subject
    assert "49 SMS restants" in plain_text
    assert "49 SMS restants" in html_body


def test_brevo_sms_balance_alert_rearms_after_top_up(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    balances = iter((220.5, 225, 220.5))
    monkeypatch.setattr(application, "_brevo_sms_credits", lambda: next(balances))
    deliveries = []
    monkeypatch.setattr(
        application,
        "send_email_html",
        lambda *args: deliveries.append(args) or True,
    )

    for _ in range(3):
        assert c.get("/api/crm/brevo/sms-credits").status_code == 200

    assert len(deliveries) == 2


def test_failed_brevo_sms_balance_alert_is_retried(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setattr(application, "_brevo_sms_credits", lambda: 0)
    attempts = []
    monkeypatch.setattr(
        application,
        "send_email_html",
        lambda *args: attempts.append(args) or len(attempts) > 1,
    )

    assert c.get("/api/crm/brevo/sms-credits").status_code == 200
    assert c.get("/api/crm/brevo/sms-credits").status_code == 200

    assert len(attempts) == 2


def test_brevo_sms_credits_are_admin_only(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    with c.session_transaction() as session:
        session["user_email"] = "cassandre@integraleacademy.com"

    assert c.get("/api/crm/brevo/sms-credits").status_code == 403


def test_administration_menu_displays_brevo_sms_balance():
    template = open(application.app.root_path + "/templates/crm.html", encoding="utf-8").read()
    crm_js = open(application.app.root_path + "/static/crm.js", encoding="utf-8").read()

    assert 'id="brevoSmsCredits"' in template
    assert "api('/api/crm/brevo/sms-credits')" in crm_js
    assert "if(opening)loadBrevoSmsCredits()" in crm_js
    assert "Estimation calculée sur une consommation moyenne de 4,5 crédits par SMS." in template
    assert "const brevoCreditsToSms=" in crm_js
    assert "Math.floor(numericCredits/4.5)" in crm_js
    assert "Solde Brevo : ${formattedCredits}" in crm_js


def test_global_search_closes_when_clicking_outside():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "document.addEventListener('click'" in crm_js
    assert "if(!searchBox.contains(e.target))globalResults.classList.remove('open')" in crm_js


def test_sidebar_lead_count_only_includes_new_leads():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "const count=contacts.filter(c=>c.statut==='Nouveaux').length" in crm_js
    assert "contacts.filter(isActiveLead).length;if(leadCount)leadCount.textContent=count" not in crm_js


def test_pipeline_financing_stages_follow_real_funding_request_status():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "if(fundingStatus==='en_cours_instruction')statuses.add('Financement FT en cours')" in crm_js
    assert "if(fundingStatus==='refusee')statuses.add('Financement FT refusé')" in crm_js
    assert "const contactHasPipelineStatus=(c,status)=>contactPipelineStatuses(c).includes(status)" in crm_js
    assert "contacts.filter(c=>contactHasPipelineStatus(c,s)).length" in crm_js
    assert "list.filter(c=>contactHasPipelineStatus(c,statusFilter))" in crm_js
    assert "!statusFilter||contactHasPipelineStatus(c,statusFilter)" in crm_js


def test_crm_collaborative_refresh_is_lightweight_and_never_overlaps():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "CRM_REFRESH_INTERVAL_MS=60000" in crm_js
    assert "crmRefreshInFlight" in crm_js
    assert "document.hidden||crmRefreshInFlight" in crm_js
    assert "api(`/api/crm/contacts/updates${suffix}`)" in crm_js
    assert "setInterval(async()=>{try{const fresh=await api('/api/crm/contacts')" not in crm_js


def test_wedof_remote_sync_is_manual_from_the_contact_sheet():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert 'id="wedofRefresh"' in crm_js
    assert "refresh.onclick=()=>refreshWedof(c,status)" in crm_js
    assert "cached.sync?.last_sync_at||status.last_sync_at});if(status.configured!==false)" not in crm_js
    assert "loadWedofTabCount(c,contactWedofTab);wedofLoaded=true;loadWedof(c)" not in crm_js


def test_gunicorn_recycles_the_single_worker():
    root = application.app.root_path
    procfile = open(root + "/Procfile", encoding="utf-8").read()
    config = open(root + "/gunicorn.conf.py", encoding="utf-8").read()

    assert "--max-requests ${GUNICORN_MAX_REQUESTS:-750}" in procfile
    assert "--max-requests-jitter ${GUNICORN_MAX_REQUESTS_JITTER:-75}" in procfile
    assert 'max_requests = int(os.getenv("GUNICORN_MAX_REQUESTS", "750"))' in config
    assert 'max_requests_jitter = int(os.getenv("GUNICORN_MAX_REQUESTS_JITTER", "75"))' in config
    assert "max_requests = 0" not in config


def test_funding_request_status_automatically_updates_secondary_timeline(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    created = c.post("/api/crm/contacts", json={"prenom": "Auto", "nom": "FT"}).get_json()

    in_progress = c.patch(
        f"/api/crm/contacts/{created['id']}",
        json={"statut_demande_financement_ft": "en_cours_instruction"},
    )
    assert in_progress.status_code == 200
    assert in_progress.get_json()["statut_secondaire"] == "Financement FT en cours"

    refused = c.patch(
        f"/api/crm/contacts/{created['id']}",
        json={"statut_demande_financement_ft": "refusee"},
    )
    assert refused.status_code == 200
    assert refused.get_json()["statut_secondaire"] == "Financement FT refusé"


def test_pipeline_table_displays_every_status_held_by_a_contact():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "contactPipelineStatuses(c).map(badge).join(' ')" in crm_js


def test_tracking_card_can_expand_and_displays_secretariat_origin():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert 'id="trackingExpand"' in crm_js
    assert "trackingCard.showPopover()" in crm_js
    assert ".tracking-card:popover-open" in open(
        application.app.root_path + "/static/crm.css", encoding="utf-8"
    ).read()
    assert "'Secrétariat','Ajout manuel','Autre'" in crm_js


def test_tracking_card_is_displayed_above_publications():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    main_column = crm_js.split('<div class="contact-main-column">', 1)[1].split(
        '<aside class="contact-side-column">', 1
    )[0]
    assert main_column.index('id="trackingCard"') < main_column.index(
        'class="card publications-card"'
    )


def test_contact_lifecycle_and_activity(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    created = c.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin", "formation": "APS"})
    assert created.status_code == 201
    contact = created.get_json()
    assert contact["statut"] == "Nouveaux"
    assert contact["origine"] == "Ajout manuel"

    updated = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"statut": "A relancer", "relance_date": "2026-08-10", "carte_pro": "NON"},
    )
    assert updated.status_code == 200
    assert updated.get_json()["activities"][0]["title"] == "Statut : A relancer"

    reloaded = c.get(f"/api/crm/contacts/{contact['id']}").get_json()
    assert reloaded["statut"] == "A relancer"
    assert reloaded["relance_date"] == "2026-08-10"

    call = c.post(f"/api/crm/contacts/{contact['id']}/appel", json={"commentaire": "Échange financement."})
    assert call.status_code == 200
    assert call.get_json()["activities"][0]["kind"] == "appel"


def test_vae_eligibility_simulation_creates_detailed_crm_lead(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    monkeypatch.setattr(application, "creer_piste_salesforce", lambda payload: None)
    application.app.config.update(TESTING=True)

    response = application.app.test_client().post(
        "/simulateur-eligibilite-vae-desp",
        json={
            "nom": "martin", "prenom": "lina", "mail": "lina@example.com",
            "telephone": "06 12 34 56 78",
            "reponses": {"q1": "oui", "q2": "oui", "q3": "non", "q4": "oui", "q5": "oui"},
        },
    )

    assert response.status_code == 200
    assert response.get_json()["score"] == 75
    contact = application.load_data()["crm_contacts"][0]
    assert response.get_json()["crm_contact_id"] == contact["id"]
    assert (contact["prenom"], contact["nom"]) == ("Lina", "MARTIN")
    assert (contact["formation"], contact["desp_type"], contact["statut"]) == ("DESP", "VAE", "Nouveaux")
    assert contact["source"] == "simulateur_vae_desp"
    assert contact["origine"] == "Simulateur VAE"
    assert contact["vae_eligibility"] == {
        "completed_at": contact["created_at"], "score": 75,
        "resultat": "Profil favorable",
        "reponses": {"q1": "oui", "q2": "oui", "q3": "non", "q4": "oui", "q5": "oui"},
    }
    assert contact["activities"][0]["title"] == "Test d’éligibilité VAE DESP complété"
    assert "Score : 75%" in contact["activities"][0]["detail"]


def test_crm_displays_vae_eligibility_score_and_clickable_details():
    crm_js = open(application.app.root_path + "/static/crm.js", encoding="utf-8").read()

    assert "vaeEligibilityCard(c)" in crm_js
    assert "VAE ${Number(eligibility.score)} %" in crm_js
    assert "Voir le détail des réponses" in crm_js
    assert "vaeEligibilityQuestions.map" in crm_js
    assert crm_js.index('id="calendlyCard"') < crm_js.index('${vaeEligibilityCard(c)}')
    assert "Simulateur VAE" in crm_js


def test_crm_templates_include_automatic_training_emails(tmp_path, monkeypatch):
    response = client(tmp_path, monkeypatch).get("/api/crm/templates")

    assert response.status_code == 200
    automatic = response.get_json()["automatic_email"]
    assert [template["formation"] for template in automatic] == [
        "DESP_VAE", "A3P", "APS", "SSIAP", "VTC", "DESP_INIT",
    ]
    aps = next(template for template in automatic if template["formation"] == "APS")
    assert aps["sujet"] == "👮‍♂️ Formation Agent de Sécurité Privée (APS)"
    assert "{{ prenom }}" in aps["contenu"]
    assert "1 650" in aps["contenu"]
    a3p = next(template for template in automatic if template["formation"] == "A3P")
    assert "youtube" not in a3p["contenu"].lower()
    assert "<iframe" not in a3p["contenu"].lower()


def test_crm_templates_page_displays_automatic_emails_as_read_only():
    crm_js = open(application.app.root_path + "/static/crm.js", encoding="utf-8").read()

    assert "E-mails automatiques du formulaire" in crm_js
    assert '<details class="card automatic-template-card">' in crm_js
    assert '<summary class="card-head">' in crm_js
    assert '</div>`+automaticSection' in crm_js
    assert "data-preview-automatic-template" in crm_js
    assert "Envoi automatique" in crm_js
    assert "templates.automatic_email||[]" in crm_js


def test_admin_can_reset_only_crm_prospect_data(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    c.post("/api/crm/contacts", json={"prenom": "Lina"})
    data = application.load_data()
    data.update({
        "demandes": [{"id": "outside-crm"}],
        "crm_calendly_appointments": [{"id": "rdv-1", "contact_id": "lead-1"}],
        "crm_notifications": [{"id": "notification-1", "contact_id": "lead-1"}],
        "crm_ai_candidate_analyses": {"lead-1": {"score": 80}},
        "crm_cnaps_scoring_snapshots": {"lead-1": {"score": 70}},
        "crm_email_templates": [{"id": "template-1"}],
    })
    application.save_data(data)

    response = c.delete("/api/crm/database")

    assert response.status_code == 200
    assert response.get_json()["deleted_count"] == 1
    saved = application.load_data()
    assert saved["crm_contacts"] == []
    assert saved["crm_calendly_appointments"] == []
    assert saved["crm_notifications"] == []
    assert saved["crm_ai_candidate_analyses"] == {}
    assert saved["crm_cnaps_scoring_snapshots"] == {}
    assert saved["crm_email_templates"] == [{"id": "template-1"}]
    assert saved["demandes"] == [{"id": "outside-crm"}]


def test_non_admin_cannot_reset_crm_database(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    with c.session_transaction() as session:
        session["user_email"] = "cassandre@integraleacademy.com"

    response = c.delete("/api/crm/database")

    assert response.status_code == 403
    assert application.load_data()["crm_contacts"][0]["id"] == contact["id"]


def test_database_reset_button_is_only_rendered_for_admin(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    assert b'id="deleteCrmDatabase"' in c.get("/crm").data
    with c.session_transaction() as session:
        session["user_email"] = "cassandre@integraleacademy.com"
    assert b'id="deleteCrmDatabase"' not in c.get("/crm").data


def test_contact_publication_is_signed_and_persisted(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin"}).get_json()

    response = c.post(
        f"/api/crm/contacts/{contact['id']}/publications",
        json={"texte": "Reviens vers nous prochainement"},
    )

    assert response.status_code == 201
    publication = response.get_json()["publication"]
    assert publication["texte"] == "Reviens vers nous prochainement"
    assert publication["author"] == "Clément VAILLANT"
    assert publication["date"]
    saved = c.get(f"/api/crm/contacts/{contact['id']}").get_json()
    assert saved["publications"] == [publication]


def test_empty_contact_publication_is_rejected(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    response = c.post(f"/api/crm/contacts/{contact['id']}/publications", json={"texte": "  "})
    assert response.status_code == 400


def test_publication_social_actions_and_owner_deletion(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    publication = c.post(
        f"/api/crm/contacts/{contact['id']}/publications", json={"texte": "Une actualité"}
    ).get_json()["publication"]

    liked = c.post(f"/api/crm/contacts/{contact['id']}/publications/{publication['id']}/like")
    assert liked.status_code == 200
    assert liked.get_json()["publication"]["likes"] == ["clement@integraleacademy.com"]

    commented = c.post(
        f"/api/crm/contacts/{contact['id']}/publications/{publication['id']}/comments",
        json={"texte": "Merci pour l'information"},
    )
    assert commented.status_code == 201
    comment = commented.get_json()["comment"]
    assert comment["author"] == "Clément VAILLANT"
    assert c.delete(
        f"/api/crm/contacts/{contact['id']}/publications/{publication['id']}/comments/{comment['id']}"
    ).status_code == 204
    assert c.delete(
        f"/api/crm/contacts/{contact['id']}/publications/{publication['id']}"
    ).status_code == 204
    assert c.get(f"/api/crm/contacts/{contact['id']}").get_json()["publications"] == []


def test_news_feed_page_is_available(tmp_path, monkeypatch):
    response = client(tmp_path, monkeypatch).get("/crm/fil-actu")
    assert response.status_code == 200
    assert b"Fil actu" in response.data
    assert b'"team"' in response.data
    assert b'Elsa DUQUESNE' in response.data


def test_news_feed_section_renders_publications_instead_of_contacts():
    crm_js = open(application.app.root_path + "/static/crm.js", encoding="utf-8").read()

    assert "else if(C.section==='fil-actu')return newsPage()" in crm_js
    assert "function newsPage()" in crm_js


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
            "commentaires_secretariat": "Ne doit pas être copié dans la fiche CRM.",
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
    assert contact["cpf_montant"] == "1200.00"
    assert contact["financement_ft"] == "NON"
    assert contact["garde_vue"] == "NON"
    assert contact["titre_sejour"] == "OUI"
    assert contact["antecedents"] == "NON"
    assert contact["origine"] == "Site internet"
    assert contact["commentaires"] == ""
    assert contact["formulaire"]["cpf_montant"] == "1200"
    assert application._crm_contact_response(contact)["integration_score"]["cpf_amount_eur"] == 1200
    assert contact["source_demande_id"]
    assert contact["source_devis_id"]
    activities = {activity["title"]: activity for activity in contact["activities"]}
    assert "Formulaire de demande d’informations complété" in activities
    assert "Devis détaillé créé" in activities
    assert "Ouvrir le devis" in activities["Devis détaillé créé"]["preview"]
    assert "E-mail automatique envoyé" in activities
    assert "SMS automatique envoyé" in activities
    assert activities["E-mail automatique envoyé"]["preview"]


def test_crm_backfills_regulatory_answers_from_older_information_form_contacts(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    data = application.load_data()
    data["crm_contacts"] = [{
        "id": "legacy-form-lead",
        "source": "demande_infos_formations",
        "prenom": "Lina",
        "nom": "MARTIN",
        "garde_vue": "",
        "titre_sejour": "",
        "formulaire": {"garde_vue": "NON", "titre_sejour": "OUI"},
    }]
    application.save_data(data)

    response = test_client.get("/api/crm/contacts/legacy-form-lead")

    assert response.status_code == 200
    assert response.get_json()["garde_vue"] == "NON"
    assert response.get_json()["titre_sejour"] == "OUI"


def test_crm_pages_and_templates(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    page = c.get("/CRM/pistes", follow_redirects=True)
    assert page.status_code == 200
    assert b"iaconnectcrm.png" in page.data
    assert b"favicon_32x32.png" in page.data
    assert b'id="manageStatusesTop"' in page.data
    assert b"20260814-wedof-ft-refusal" in page.data
    response = c.post("/api/crm/templates", json={"type": "email", "nom": "Bienvenue", "sujet": "Bonjour", "contenu": "<p>Bienvenue</p>"})
    assert response.status_code == 201
    assert c.get("/api/crm/templates").get_json()["email"][0]["nom"] == "Bienvenue"


def test_crm_email_starter_exposes_editable_complete_html(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)

    starter = c.get("/api/crm/templates").get_json()["email_starter"]

    assert starter.lower().startswith("<!doctype html>")
    assert "Faites le premier pas vers votre futur métier" in starter
    assert "Le résumé de notre échange" in starter
    assert "Écrivez ici le contenu de votre e-mail." in starter
    assert "<!-- EMAIL_CONTENT_START -->" in starter
    assert "<!-- EMAIL_CONTENT_END -->" in starter
    assert "Cassandre MENARD" in starter
    assert "cassandre@integraleacademy.com" in starter
    assert "SIREN 840 899 884" in starter
    assert "Autorisation d’exercice CNAPS" in starter
    assert "<!-- EMAIL_HEADER_TITLE_START -->" in starter
    assert "<!-- EMAIL_HEADER_TAGLINE_END -->" in starter
    assert "{{ contenu|safe }}" not in starter


def test_crm_template_library_lists_all_supported_variables():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()
    for variable in ("prenom", "nom", "email", "telephone", "formation", "lieu", "statut", "dates_formation", "prochaines_dates", "date_rdv_du_jour", "heure_rdv_du_jour", "date_heure_rdv_du_jour", "lien_rdv_calendly"):
        assert "{{ " + variable + " }}" in crm_js
    assert "Variables disponibles" in crm_js


def test_crm_unanswered_appointment_variables_use_its_paris_date_and_time(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    paris_now = application.datetime.datetime.now(
        application.pytz.timezone("Europe/Paris")
    )
    appointment_at = (paris_now - application.datetime.timedelta(days=7)).replace(
        hour=14, minute=30, second=0, microsecond=0
    )
    data = application.load_data()
    data["crm_calendly_appointments"] = [{
        "id": "rdv-today",
        "contact_id": contact["id"],
        "status": "active",
        "response_status": "no_answer",
        "response_status_updated_at": paris_now.isoformat(),
        "start_time": appointment_at.astimezone(application.pytz.UTC).isoformat(),
    }]
    application.save_data(data)

    response = c.post(
        f"/api/crm/contacts/{contact['id']}/message-preview",
        json={"contenu": "RDV le {{ date_rdv_du_jour }} à {{ heure_rdv_du_jour }} ({{ date_heure_rdv_du_jour }})"},
    )

    assert response.status_code == 200
    html = response.get_json()["html"]
    assert "à 14h30" in html
    assert f"{appointment_at.day} " in html
    assert "{{ date_rdv_du_jour }}" not in html
    assert "{{ heure_rdv_du_jour }}" not in html


def test_crm_formation_variable_uses_complete_customer_facing_name(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post(
        "/api/crm/contacts", json={"prenom": "Lina", "formation": "A3P"}
    ).get_json()

    response = c.post(
        f"/api/crm/contacts/{contact['id']}/message-preview",
        json={"contenu": "Votre formation {{ formation }}"},
    )

    assert response.status_code == 200
    html = response.get_json()["html"]
    assert "Agent de protection physique des personnes (A3P)" in html
    assert "Votre formation A3P<" not in html


def test_complete_crm_email_html_is_not_wrapped_twice(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    complete_html = "<!doctype html><html><body><h1>Bonjour {{ prenom }}</h1></body></html>"

    response = c.post(
        f"/api/crm/contacts/{contact['id']}/message-preview",
        json={"contenu": complete_html},
    )

    assert response.status_code == 200
    html = response.get_json()["html"]
    assert html == "<!doctype html><html><body><h1>Bonjour Lina</h1></body></html>"
    assert "Faites le premier pas" not in html


def test_crm_templates_can_be_edited_and_deleted(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    created = c.post("/api/crm/templates", json={"type": "sms", "nom": "Rappel", "contenu": "Ancien texte"}).get_json()
    updated = c.patch(f"/api/crm/templates/{created['id']}", json={"nom": "Rappel rendez-vous", "contenu": "Nouveau texte"})
    assert updated.status_code == 200
    assert updated.get_json()["nom"] == "Rappel rendez-vous"
    assert updated.get_json()["contenu"] == "Nouveau texte"
    assert updated.get_json()["updated_at"]
    assert c.patch(f"/api/crm/templates/{created['id']}", json={"nom": " "}).status_code == 400
    assert c.delete(f"/api/crm/templates/{created['id']}").status_code == 204
    assert c.get("/api/crm/templates").get_json()["sms"] == []
    assert c.delete(f"/api/crm/templates/{created['id']}").status_code == 404


def test_message_template_picker_prioritizes_the_contact_formation():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "messageTemplateOptions(list,c.formation)" in crm_js
    assert "includes(needle)" in crm_js
    assert 'value="__other_templates__">Autres modèles…' in crm_js
    assert 'label="Autres modèles"' in crm_js


def test_message_modal_does_not_rely_on_named_window_properties():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    message_modal = crm_js.split("function messageModal", 1)[1].split("function previewModal", 1)[0]
    for selector in ("#generateMessage", "#tpl", "#msg", "#subject", "#messagePreview", "#sendMessage"):
        assert f"document.querySelector('{selector}')" in message_modal
    assert "messagePreview.onclick" not in message_modal


def test_crm_email_preview_uses_the_sent_mail_wrapper(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()

    response = c.post(
        f"/api/crm/contacts/{contact['id']}/message-preview",
        json={"contenu": "<p>Mon texte libre</p>"},
    )

    assert response.status_code == 200
    html = response.get_json()["html"]
    assert "Bonjour Lina" in html
    assert "Mon texte libre" in html
    assert "Faites le premier pas vers votre futur métier" in html
    assert "integraleacademy.com" in html


def test_crm_email_dynamic_dates_come_from_upcoming_admin_sessions(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    data = application.load_data()
    data["formation_sessions"] = {"cote_azur": {"APS": [
        {"label": "Du 1 janvier au 2 janvier 2020", "badge": ""},
        {"label": "Du 3 septembre au 30 septembre 2099 - examen le 1 octobre 2099", "badge": ""},
    ]}}
    application.save_data(data)
    contact = c.post("/api/crm/contacts", json={
        "prenom": "Lina", "formation": "APS", "lieu": "Côte d’Azur",
    }).get_json()

    response = c.post(
        f"/api/crm/contacts/{contact['id']}/message-preview",
        json={"contenu": "<p>Nos prochaines dates :</p>{{prochaines_dates}}"},
    )

    assert response.status_code == 200
    html = response.get_json()["html"]
    assert "3 septembre au 30 septembre 2099" in html
    assert "1 janvier au 2 janvier 2020" not in html
    assert "{{prochaines_dates}}" not in html


@pytest.mark.parametrize(("formation", "expected_url"), [
    ("APS", "https://calendly.com/integraleacademy/aps"),
    ("A3P", "https://calendly.com/integraleacademy/apr"),
    ("SSIAP 1", "https://calendly.com/integraleacademy/ssiap1"),
    ("DESP", "https://calendly.com/integraleacademy/dirigeant"),
    ("Chauffeur VTC", "https://calendly.com/integraleacademy/chauffeurvtc"),
    ("Formation personnalisée", "https://calendly.com/integraleacademy/formation"),
])
def test_crm_calendly_variable_matches_contact_training(
    tmp_path, monkeypatch, formation, expected_url
):
    c = client(tmp_path, monkeypatch)
    contact = c.post(
        "/api/crm/contacts", json={"prenom": "Lina", "formation": formation}
    ).get_json()

    response = c.post(
        f"/api/crm/contacts/{contact['id']}/message-preview",
        json={"contenu": "<a href=\"{{ lien_rdv_calendly }}\">Prendre rendez-vous</a>"},
    )

    assert response.status_code == 200
    html = response.get_json()["html"]
    assert f'href="{expected_url}"' in html
    assert "{{ lien_rdv_calendly }}" not in html


def test_crm_can_send_a_template_test_email(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    sent = {}
    monkeypatch.setattr(application, "send_email_html", lambda to, subject, plain, html: sent.update(to=to, subject=subject, plain=plain, html=html) or True)

    response = c.post("/api/crm/test-email", json={
        "destinataire": "equipe@example.com",
        "sujet": "Aperçu formation",
        "contenu": "<h1>Bonjour</h1><p>Voici le programme.</p>",
    })

    assert response.status_code == 200
    assert sent == {"to": "equipe@example.com", "subject": "Aperçu formation", "plain": "Bonjour Voici le programme.", "html": "<h1>Bonjour</h1><p>Voici le programme.</p>"}
    assert c.post("/api/crm/test-email", json={"destinataire": "invalide", "contenu": "test"}).status_code == 400


def test_crm_can_send_a_template_test_sms(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    sent = {}
    monkeypatch.setattr(
        application,
        "send_sms",
        lambda to, body: sent.update(to=to, body=body) or True,
    )

    response = c.post("/api/crm/test-sms", json={
        "destinataire": "06 12 34 56 78",
        "contenu": "Bonjour, voici votre confirmation.",
    })

    assert response.status_code == 200
    assert sent == {"to": "06 12 34 56 78", "body": "Bonjour, voici votre confirmation."}
    assert c.post("/api/crm/test-sms", json={"destinataire": "123", "contenu": "test"}).status_code == 400
    assert c.post("/api/crm/test-sms", json={"destinataire": "0612345678", "contenu": "  "}).status_code == 400


def test_sms_templates_can_be_previewed_and_sent_as_tests_from_the_library():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert 'data-preview-type="${type}"' in crm_js
    assert "if(type==='sms')previewModal(smsPreviewHtml(t.contenu)" in crm_js
    assert 'id="sendTestSms"' in crm_js
    assert "api('/api/crm/test-sms'" in crm_js
    assert "À quel numéro envoyer ce SMS de test ?" in crm_js


def test_email_template_preview_does_not_render_the_subject_in_the_message_body():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "previewModal(t.contenu,true,{sujet:t.sujet,contenu:t.contenu})" in crm_js
    assert '<h3>${esc(t.sujet)}</h3>' not in crm_js


def test_starter_email_has_a_simple_content_editor():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert 'for="tplContent">Contenu du mail' in crm_js
    assert "la mise en page du modèle est conservée automatiquement" in crm_js
    assert "Modifier le code HTML (avancé)" in crm_js
    assert "emailContentToHtml(tplContent.value)" in crm_js
    assert "Header du mail" in crm_js
    assert 'for="tplHeaderTitle">Titre' in crm_js
    assert 'for="tplHeaderSubtitle">Sous-titre' in crm_js
    assert 'for="tplHeaderTagline">Accroche' in crm_js
    assert "EMAIL_HEADER_TITLE_START" in crm_js


def test_crm_pipeline_statuses_can_be_added_renamed_and_deleted(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()

    added = c.post("/api/crm/statuses", json={"label": "Dossier incomplet"})
    assert added.status_code == 201
    assert "Dossier incomplet" in added.get_json()

    renamed = c.patch("/api/crm/statuses/Nouveaux", json={"label": "À qualifier"})
    assert renamed.status_code == 200
    assert c.get(f"/api/crm/contacts/{contact['id']}").get_json()["statut"] == "À qualifier"

    deleted = c.delete("/api/crm/statuses/%C3%80%20qualifier")
    assert deleted.status_code == 200
    assert "À qualifier" not in deleted.get_json()["statuses"]
    assert c.get(f"/api/crm/contacts/{contact['id']}").get_json()["statut"] == "Blocage"

    assert c.patch("/api/crm/statuses/Converti", json={"label": "Gagné"}).status_code == 400


def test_relances_page_uses_a_daily_calendar_view(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)

    page = c.get("/CRM/relances", follow_redirects=True)
    assert page.status_code == 200

    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()
    assert "function remindersPage()" in crm_js
    assert 'id="reminderDate" type="date"' in crm_js
    assert "c.relance_date===reminderSelectedDate" in crm_js
    assert "changeReminderDate(-1)" in crm_js
    assert "changeReminderDate(1)" in crm_js
    assert "Aucune relance ce jour-là" in crm_js
    assert 'id="reminderShowAll"' in crm_js
    assert "Voir toutes les relances" in crm_js
    assert "all.reduce((dates,c)" in crm_js


def test_planning_a_reminder_waits_for_server_persistence_before_updating_contact(tmp_path, monkeypatch):
    client(tmp_path, monkeypatch)

    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    handler_start = crm_js.index("function relaunchModal")
    api_update = "api(`/api/crm/contacts/${id}`"
    confirmed_update = "mergeContactInStore(id,updated);closeModal();showContact(id,options.returnTab||'contactInfoTab')"
    optimistic_update = "Object.assign(c,next);mergeContactInStore(id,next);closeModal();showContact(id)"
    assert api_update in crm_js
    assert confirmed_update in crm_js
    assert crm_js.index(api_update, handler_start) < crm_js.index(confirmed_update, handler_start)
    assert optimistic_update not in crm_js
    assert "saveRelaunch.textContent='Enregistrement…'" in crm_js
    assert "saveRelaunch.disabled=false" in crm_js


def test_relance_no_answer_reprograms_and_sends_named_templates_only_once(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    deliveries = {"sms": [], "email": []}
    monkeypatch.setattr(
        application,
        "send_sms",
        lambda phone, body: deliveries["sms"].append((phone, body)) or True,
    )
    monkeypatch.setattr(
        application,
        "send_email_html",
        lambda mail, subject, plain, html: deliveries["email"].append(
            (mail, subject, plain, html)
        ) or True,
    )
    contact = c.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin", "formation": "APS"},
    ).get_json()
    c.post(
        "/api/crm/templates",
        json={
            "type": "sms",
            "nom": "Pas de réponse relance",
            "contenu": "Bonjour {{ prenom }}, nous avons tenté de vous joindre.",
        },
    )
    c.post(
        "/api/crm/templates",
        json={
            "type": "email",
            "nom": "Pas de réponse relance",
            "sujet": "Votre relance {{ formation }}",
            "contenu": "<p>Bonjour {{ prenom }}, nous avons tenté de vous joindre.</p>",
        },
    )
    planned = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={
            "telephone": "+33612345678",
            "mail": "lina@example.com",
            "statut": "A relancer",
            "relance_date": "2026-08-13",
        },
    ).get_json()
    first_relance = planned["relances"][0]
    assert first_relance["status"] == "scheduled"

    response = c.post(
        f"/api/crm/contacts/{contact['id']}/relances/{first_relance['id']}/sans-reponse",
        json={"next_date": "2026-08-15"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["delivery"] == {"email": True, "sms": True}
    assert payload["relance"]["status"] == "no_answer"
    assert payload["relance"]["message_template"] == "Pas de réponse relance"
    assert payload["next_relance"]["status"] == "scheduled"
    assert payload["contact"]["relance_date"] == "2026-08-15"
    assert len(deliveries["sms"]) == len(deliveries["email"]) == 1
    assert "Lina" in deliveries["sms"][0][1]
    assert "Agent de prévention et de sécurité" in deliveries["email"][0][1]

    duplicate = c.post(
        f"/api/crm/contacts/{contact['id']}/relances/{first_relance['id']}/sans-reponse",
        json={"next_date": "2026-08-16"},
    )
    assert duplicate.status_code == 200
    assert duplicate.get_json()["duplicate"] is True
    assert duplicate.get_json()["contact"]["relance_date"] == "2026-08-15"
    assert len(deliveries["sms"]) == len(deliveries["email"]) == 1


def test_answered_relance_requires_and_records_a_call_once(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin", "formation": "APS"},
    ).get_json()
    planned = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"statut": "A relancer", "relance_date": "2026-08-14"},
    ).get_json()
    relance = planned["relances"][0]

    missing_note = c.post(
        f"/api/crm/contacts/{contact['id']}/appel",
        json={"commentaire": "", "relance_id": relance["id"]},
    )
    assert missing_note.status_code == 400

    answered = c.post(
        f"/api/crm/contacts/{contact['id']}/appel",
        json={"commentaire": "La candidate a répondu et souhaite recevoir le devis.", "relance_id": relance["id"]},
    )
    assert answered.status_code == 200
    payload = answered.get_json()
    assert payload["relance"]["status"] == "answered"
    assert payload["contact"]["relance_date"] == ""
    assert any(
        activity["kind"] == "appel"
        and activity["detail"] == "La candidate a répondu et souhaite recevoir le devis."
        for activity in payload["contact"]["activities"]
    )

    activity_count = len(payload["contact"]["activities"])
    duplicate = c.post(
        f"/api/crm/contacts/{contact['id']}/appel",
        json={"commentaire": "Double clic", "relance_id": relance["id"]},
    ).get_json()
    assert duplicate["duplicate"] is True
    assert len(duplicate["contact"]["activities"]) == activity_count


def test_legacy_relance_date_is_migrated_without_being_duplicated(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    data = application.load_data()
    stored = next(item for item in data["crm_contacts"] if item["id"] == contact["id"])
    stored["relances"] = []
    stored["relance_date"] = "2026-08-20"
    application.save_data(data)

    first = c.get("/api/crm/contacts").get_json()[0]
    second = c.get("/api/crm/contacts").get_json()[0]

    assert first["relance_date"] == "2026-08-20"
    assert len(first["relances"]) == 1
    assert len(second["relances"]) == 1
    assert second["relances"][0]["source"] == "legacy"


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


def test_contact_activity_log_and_vae_tracking_are_displayed_in_tabs():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    tabs = crm_js.index('role="tablist" aria-label="Sections de la fiche"')
    information = crm_js.index('id="contactInfoTab"', tabs)
    wedof = crm_js.index('id="contactWedofTab"', tabs)
    vae = crm_js.index('id="contactVaeTab"', tabs)
    activity = crm_js.index('id="contactActivityTab"', tabs)
    relance = crm_js.index('id="contactRelanceTab"', tabs)

    assert information < wedof < vae < activity < relance
    assert 'id="contactVaePanel" role="tabpanel" aria-labelledby="contactVaeTab" hidden' in crm_js
    assert 'id="contactActivityPanel" role="tabpanel" aria-labelledby="contactActivityTab" hidden' in crm_js
    assert 'id="contactRelancePanel" role="tabpanel" aria-labelledby="contactRelanceTab" hidden' in crm_js
    assert crm_js.index('id="vaeTrackingPanel"') > crm_js.index('id="contactVaePanel"')
    assert crm_js.index('id="activityFeed"') > crm_js.index('id="contactActivityPanel"')
    assert crm_js.index('${relanceTracking(c)}') > crm_js.index('id="contactRelancePanel"')


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


def test_crm_persists_reglementaire_answers(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    contact = test_client.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin", "formation": "APS"}).get_json()
    answers = {
        "carte_pro": "NON",
        "antecedents": "NON",
        "titre_sejour": "OUI",
        "compte_cnaps": "OUI",
        "cnaps_username": "lina.cnaps",
        "cnaps_password": "secret",
        "integration_dracar": "OUI",
    }

    response = test_client.patch(f"/api/crm/contacts/{contact['id']}", json=answers)

    assert response.status_code == 200
    assert all(response.get_json()[key] == value for key, value in answers.items())


def test_crm_mentions_are_private_and_replyable(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post('/api/crm/contacts', json={'prenom': 'Lina'}).get_json()
    publication = c.post(f"/api/crm/contacts/{contact['id']}/publications", json={'texte': '@aurelie peux-tu vérifier ?'}).get_json()['publication']
    assert c.get('/api/crm/notifications').get_json() == []
    with c.session_transaction() as session:
        session['user_email'] = 'aurelie@integraleacademy.com'
    notifications = c.get('/api/crm/notifications').get_json()
    assert len(notifications) == 1
    assert notifications[0]['publication_id'] == publication['id']
    assert c.post(f"/api/crm/contacts/{contact['id']}/publications/{publication['id']}/comments", json={'texte': 'Oui, je regarde.'}).status_code == 201
    assert c.patch('/api/crm/notifications', json={'id': notifications[0]['id']}).get_json()[0]['read'] is True


def test_crm_uppercase_refresh_preserves_contact_query(tmp_path, monkeypatch):
    response = client(tmp_path, monkeypatch).get('/CRM/contacts?fiche=contact-123')
    assert response.status_code == 302
    assert response.location.endswith('/crm/contacts?fiche=contact-123')


def test_crm_regulatory_and_funding_dependencies_are_present():
    with open(application.app.root_path + '/static/crm.js', encoding='utf-8') as source:
        script = source.read()
    assert "selectHtml('garde_vue','Garde à vue ou prise d’empreintes ?'" in script
    assert 'data-show="cpf-yes"' in script
    assert 'data-show="identity-created"' in script
    assert 'data-show="ft-yes"' in script
    assert 'wedofLoaded=true;loadWedof(c)' in script


def test_leads_can_be_filtered_by_an_exact_training_session():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert 'id="sessionFilter"' in crm_js
    assert 'Toutes les sessions' in crm_js
    assert 'sessionFilterRows()' in crm_js
    assert "c.dates_formation===session[2]" in crm_js
    assert "c.formation===session[0]" in crm_js
    assert "(c.lieu||'')===session[1]" in crm_js
    assert 'id="filterResultCount"' in crm_js
