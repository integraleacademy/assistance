import app as application


ENDPOINT = "/api/integrations/meta/zapier-leads"
SECRET = "test-webhook-secret"


def setup_client(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    monkeypatch.setenv("ZAPIER_META_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(application, "creer_piste_salesforce", lambda payload: None)
    monkeypatch.setattr(application, "send_email_html", lambda *args: True)
    monkeypatch.setattr(application, "send_sms", lambda *args: True)
    application.app.config.update(TESTING=True)
    return application.app.test_client()


def post(client, payload, secret=SECRET):
    headers = {"X-Zapier-Secret": secret} if secret is not None else {}
    return client.post(ENDPOINT, json=payload, headers=headers)


def lead(**values):
    payload = {
        "leadgen_id": "meta-1", "created_time": "2026-08-10T12:00:00+0000",
        "Full Name": "Lina Martin", "Email": "LINA@Example.FR",
        "Phone Number": "+33 6 12 34 56 78", "Form Name": "Formation APS",
        "Campaign Name": "Rentrée", "Ad Name": "Vidéo 1",
    }
    payload.update(values)
    return payload


def test_secret_is_required_and_compared(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    assert post(client, lead(), secret=None).status_code == 403
    wrong = post(client, lead(), secret="incorrect")
    assert wrong.status_code == 403
    assert wrong.get_json() == {"success": False, "error": "unauthorized"}
    assert post(client, lead()).status_code == 201


def test_new_lead_creates_contact_submission_notification_and_custom_answers(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    payload = lead(field_data=[
        {"name": "formation", "values": ["APS"]},
        {"name": "Avez-vous une carte professionnelle ?", "values": ["Oui"]},
    ])
    response = post(client, payload)
    assert response.status_code == 201 and response.get_json()["result"] == "created"
    data = application.load_data()
    contact = data["crm_contacts"][0]
    assert contact["statut"] == "Nouveaux" and contact["origine"] == "META"
    assert contact["source_detail"] == "Facebook / Instagram Lead Ads"
    assert contact["formation"] == "A3P"
    assert contact["lieu"] == "Côte d’Azur"
    assert contact["meta_answers"] == [
        {"question": "Avez-vous une carte professionnelle ?", "answer": "Oui",
         "received_at": contact["received_at"]},
        {"question": "Formation souhaitée", "answer": "APS",
         "received_at": contact["received_at"]},
    ]
    assert len(data["crm_notifications"]) == 1
    submission = data["crm_meta_lead_submissions"][0]
    assert submission["raw_payload"] == payload
    assert submission["custom_answers"]["Avez-vous une carte professionnelle ?"] == ["Oui"]
    assert submission["mapped_fields"]["formation"] == "A3P"
    assert submission["mapped_fields"]["lieu"] == "Côte d’Azur"
    assert submission["mapped_fields"]["carte_pro"] == "OUI"
    meta_activity = next(
        activity for activity in contact["activities"]
        if activity["kind"] == "meta_lead"
    )
    assert "Formation APS" in meta_activity["detail"]


def test_new_meta_lead_creates_exactly_one_salesforce_prospect(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    salesforce_payloads = []
    monkeypatch.setattr(application, "creer_piste_salesforce", salesforce_payloads.append)
    payload = lead(**{
        "leadgen_id": "meta-salesforce-1",
        "Quelle formation souhaitez-vous ?": "Agent de Prévention et de Sécurité (APS)",
        "Dans quel centre souhaitez-vous suivre la formation ?": "Puget-sur-Argens (Côte d'Azur)",
        "Quelles dates de formation souhaitez-vous ?": "7 septembre au 9 octobre 2026",
        "Avez-vous consulté votre compte CPF ?": "Oui",
        "Quel montant avez-vous sur votre CPF ?": "1 250 €",
        "Souhaitez-vous un financement France Travail ?": "Oui",
        "Avez-vous une carte professionnelle CNAPS ?": "Non",
    })

    first = post(client, payload)
    duplicate = post(client, payload)

    assert first.status_code == 201 and first.get_json()["result"] == "created"
    assert duplicate.status_code == 200 and duplicate.get_json()["result"] == "already_processed"
    assert len(salesforce_payloads) == 1
    salesforce = salesforce_payloads[0]
    assert salesforce["source_formulaire"] == "meta-zapier-leads"
    assert salesforce["meta_lead_id"] == "meta-salesforce-1"
    assert salesforce["origine"] == "META"
    assert salesforce["formation"] == "A3P"
    assert salesforce["centre"] == "cote_azur"
    assert salesforce["cpf_consulte"] == "OUI"
    assert salesforce["cpf_montant"] == "1250.00"
    assert salesforce["france_travail"] == "OUI"
    assert salesforce["cnaps_ok"] == "NON"
    assert "Identifiant Meta : meta-salesforce-1" in salesforce["infos_complementaires"]
    assert "Réponses au formulaire" in salesforce["infos_complementaires"]


def test_meta_salesforce_payload_maps_all_supported_crm_training_labels():
    cases = (
        ({"formation": "APS"}, "APS"),
        ({"formation": "A3P"}, "A3P"),
        ({"formation": "SSIAP 1"}, "SSIAP"),
        ({"formation": "Chauffeur VTC"}, "VTC"),
        ({"formation": "DESP", "desp_type": "INITIAL"}, "DESP_INIT"),
        ({"formation": "DESP", "desp_type": "VAE"}, "DESP_VAE"),
    )
    for crm_values, expected in cases:
        salesforce = application._meta_salesforce_payload(
            "meta-mapping", {}, crm_values, [],
        )
        assert salesforce["formation"] == expected
        assert salesforce["nom"] == "Sans nom"


def test_new_a3p_meta_lead_receives_public_form_email_and_sms_without_quote(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    emails, sms = [], []
    monkeypatch.setattr(
        application, "send_email_html",
        lambda *args: emails.append(args) or True,
    )
    monkeypatch.setattr(
        application, "send_sms",
        lambda *args: sms.append(args) or True,
    )
    payload = lead(**{
        "leadgen_id": "meta-a3p",
        "Quelle formation souhaitez-vous ?": "A3P – Bodyguard",
        "Dans quel centre souhaitez-vous suivre la formation ?": "Paris",
        "Quelles dates de formation souhaitez-vous ?": "Septembre 2026",
    })

    response = post(client, payload)

    assert response.status_code == 201
    data = application.load_data()
    contact = data["crm_contacts"][0]
    submission = data["crm_meta_lead_submissions"][0]
    assert submission["automatic_delivery"] == {"email": True, "sms": True}
    assert data.get("demandes", []) == []
    assert not contact.get("source_devis_id")
    assert not contact.get("devis_url")
    assert emails[0][0] == "LINA@Example.FR"
    expected = application._a3p_information_email_content(
        "Lina", contact["dates_formation"], "cote_azur", "",
    )
    assert emails[0][1:] == expected
    assert "Télécharger mon devis détaillé" not in emails[0][3]
    assert "/plan/" not in emails[0][3]
    assert sms == [("+33 6 12 34 56 78", application.build_training_information_sms_text("A3P"))]
    titles = [activity["title"] for activity in contact["activities"]]
    assert "Devis détaillé créé" not in titles
    assert "E-mail automatique envoyé" in titles
    assert "SMS automatique envoyé" in titles


def test_unmapped_meta_lead_still_receives_a3p_email_and_sms(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    emails, sms = [], []
    monkeypatch.setattr(
        application, "send_email_html",
        lambda *args: emails.append(args) or True,
    )
    monkeypatch.setattr(
        application, "send_sms",
        lambda *args: sms.append(args) or True,
    )
    payload = lead(**{
        "leadgen_id": "meta-a3p-unmapped",
        "Form Name": "Formulaire instantané",
        "Campaign Name": "Campagne recrutement",
        "Ad Name": "Vidéo recrutement",
    })

    response = post(client, payload)

    assert response.status_code == 201
    data = application.load_data()
    contact = data["crm_contacts"][0]
    submission = data["crm_meta_lead_submissions"][0]
    assert contact["formation"] == "A3P"
    assert contact["lieu"] == "Côte d’Azur"
    assert submission["automatic_delivery"] == {"email": True, "sms": True}
    assert emails and "A3P" in emails[0][1]
    assert sms == [("+33 6 12 34 56 78", application.build_training_information_sms_text("A3P"))]
    assert {activity["title"] for activity in contact["activities"]} >= {
        "E-mail automatique envoyé", "SMS automatique envoyé",
    }


def test_failed_meta_a3p_deliveries_remain_visible_in_activity_journal(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    monkeypatch.setattr(application, "send_email_html", lambda *args: False)
    monkeypatch.setattr(application, "send_sms", lambda *args: False)
    payload = lead(**{
        "leadgen_id": "meta-a3p-failed-delivery",
        "Quelle formation souhaitez-vous ?": "A3P – Bodyguard",
    })

    response = post(client, payload)

    assert response.status_code == 201
    data = application.load_data()
    contact = data["crm_contacts"][0]
    submission = data["crm_meta_lead_submissions"][0]
    assert submission["automatic_delivery"] == {"email": False, "sms": False}
    failures = [
        activity for activity in contact["activities"]
        if activity["title"].startswith("Échec")
    ]
    assert len(failures) == 2
    assert {activity["kind"] for activity in failures} == {"erreur"}
    javascript = open(
        application.app.root_path + "/static/crm.js", encoding="utf-8",
    ).read()
    assert "'calendly','erreur'" in javascript


def test_attached_a3p_meta_lead_does_not_resend_automatic_messages(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    data = application.load_data()
    data["crm_contacts"] = [{
        "id": "existing", "prenom": "Lina", "nom": "MARTIN",
        "mail": "lina@example.fr", "telephone": "+33612345678",
        "formation": "A3P", "activities": [],
    }]
    application.save_data(data)
    monkeypatch.setattr(application, "send_email_html", lambda *args: (_ for _ in ()).throw(AssertionError("email envoyé")))
    monkeypatch.setattr(application, "send_sms", lambda *args: (_ for _ in ()).throw(AssertionError("SMS envoyé")))

    response = post(client, lead())

    assert response.status_code == 200
    assert response.get_json()["result"] == "attached"
    assert application.load_data()["crm_meta_lead_submissions"][0].get("automatic_delivery") is None


def test_meta_questions_fill_training_session_funding_and_regulatory_fields(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    payload = lead(**{
        "Quelle formation souhaitez-vous ?": "Agent de Prévention et de Sécurité (APS)",
        "Dans quel centre souhaitez-vous suivre la formation ?": "Puget-sur-Argens (Côte d'Azur)",
        "Quelles dates de formation souhaitez-vous ?": "7 septembre au 9 octobre 2026",
        "Avez-vous consulté votre compte CPF ?": "Oui",
        "Quel montant avez-vous sur votre CPF ?": "1 250 €",
        "Avez-vous créé votre identité numérique La Poste ?": "Pas encore",
        "Souhaitez-vous un financement France Travail ?": "Oui",
        "Êtes-vous inscrit à France Travail ?": "Oui, je suis déjà inscrit",
        "Avez-vous une carte professionnelle CNAPS ?": "Non",
    })

    response = post(client, payload)

    assert response.status_code == 201
    contact = application.load_data()["crm_contacts"][0]
    assert contact["formation"] == "A3P"
    assert contact["lieu"] == "Côte d’Azur"
    assert contact["dates_formation"] == "7 septembre au 9 octobre 2026"
    assert contact["cpf"] == "OUI"
    assert contact["cpf_montant"] == "1250.00"
    assert contact["identite_creation"] == "NON"
    assert contact["financement_ft"] == "OUI"
    assert contact["inscrit_ft"] == "OUI"
    assert contact["carte_pro"] == "NON"
    assert len(contact["meta_answers"]) >= 9


def test_nested_zapier_answers_and_form_name_fallback_are_supported(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    payload = {
        "data": {
            "leadgen_id": "meta-nested",
            "Full Name": "Lina Martin",
            "Email": "lina@example.fr",
            "Form Name": "Demande formation SSIAP 1 - Côte d'Azur",
            "questions_and_answers": {
                "cpf_consulte": {"answer": "Oui"},
                "identite_numerique": {"value": "Non"},
                "mode de financement": {"values": ["CPF", "France Travail"]},
            },
        },
    }

    response = post(client, payload)

    assert response.status_code == 201
    contact = application.load_data()["crm_contacts"][0]
    assert contact["formation"] == "A3P"
    assert contact["lieu"] == "Côte d’Azur"
    assert contact["cpf"] == "OUI"
    assert contact["identite_creation"] == "NON"
    assert contact["financement_ft"] == "OUI"


def test_existing_meta_submissions_are_backfilled_without_overwriting_manual_values(tmp_path, monkeypatch):
    setup_client(tmp_path, monkeypatch)
    payload = lead(**{
        "Quelle formation souhaitez-vous ?": "APS",
        "Lieu de formation": "Puget-sur-Argens",
        "Avez-vous consulté votre compte CPF ?": "Oui",
    })
    data = application.load_data()
    data["crm_contacts"] = [{
        "id": "legacy-meta", "prenom": "Franck", "nom": "DENIOT",
        "mail": "franck@example.fr", "telephone": "0600000000",
        "formation": "A3P", "lieu": "", "cpf": "", "activities": [],
        "statut": "Nouveaux", "source": "META",
    }]
    data["crm_meta_lead_submissions"] = [{
        "meta_lead_id": "meta-legacy", "contact_id": "legacy-meta",
        "received_at": "2026-08-14T15:49:00+02:00", "raw_payload": payload,
        "custom_answers": {},
    }]

    changed, _ = application._crm_prepare_contacts(data)

    contact = data["crm_contacts"][0]
    assert changed is True
    assert contact["formation"] == "A3P"
    assert contact["lieu"] == "Côte d’Azur"
    assert contact["cpf"] == "OUI"
    assert any(row["question"] == "Avez-vous consulté votre compte CPF ?"
               for row in contact["meta_answers"])
    assert application._crm_prepare_contacts(data)[0] is False


def test_meta_backfill_keeps_the_latest_source_context_and_is_idempotent():
    data = {
        "crm_contacts": [{"id": "contact-1", "formation": "", "activities": []}],
        "crm_meta_lead_submissions": [
            {
                "contact_id": "contact-1", "received_at": "2026-08-14T15:00:00+02:00",
                "raw_payload": {"leadgen_id": "new", "Form Name": "APS - formulaire récent"},
            },
            {
                "contact_id": "contact-1", "received_at": "2026-08-01T10:00:00+02:00",
                "raw_payload": {"leadgen_id": "old", "Form Name": "A3P - ancien formulaire"},
            },
        ],
    }

    assert application._crm_backfill_meta_submissions(data) is True
    assert data["crm_contacts"][0]["formation"] == "A3P"
    assert data["crm_contacts"][0]["lieu"] == "Côte d’Azur"
    assert data["crm_contacts"][0]["origine"] == "META"
    assert data["crm_contacts"][0]["meta_source"]["form_name"] == "APS - formulaire récent"
    assert application._crm_backfill_meta_submissions(data) is False


def test_meta_origin_repairs_existing_contacts_and_survives_manual_edits(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    data = application.load_data()
    data["crm_contacts"] = [
        {
            "id": "origin-meta", "prenom": "Lina", "nom": "MARTIN",
            "formation": "APS", "lieu": "Paris", "origine": "meta",
            "source": "", "statut": "Nouveaux", "activities": [],
        },
        {
            "id": "source-meta", "prenom": "Yanis", "nom": "DURAND",
            "formation": "SSIAP 1", "lieu": "Auvergne", "origine": "Google",
            "source": "META", "statut": "Nouveaux", "activities": [],
        },
        {
            "id": "not-meta", "prenom": "Emma", "nom": "ROBERT",
            "formation": "APS", "lieu": "Paris", "origine": "Google",
            "source": "", "statut": "Nouveaux", "activities": [],
        },
    ]
    application.save_data(data)

    contacts = {row["id"]: row for row in client.get("/api/crm/contacts").get_json()}
    for contact_id in ("origin-meta", "source-meta"):
        assert contacts[contact_id]["origine"] == "META"
        assert contacts[contact_id]["formation"] == "A3P"
        assert contacts[contact_id]["lieu"] == "Côte d’Azur"
    assert contacts["not-meta"]["origine"] == "Google"
    assert contacts["not-meta"]["formation"] == "APS"
    assert contacts["not-meta"]["lieu"] == "Paris"

    updated = client.patch("/api/crm/contacts/origin-meta", json={
        "prenom": "Lina-Marie", "origine": "Autre",
        "formation": "DESP", "lieu": "Paris",
    }).get_json()
    assert updated["prenom"] == "Lina-Marie"
    assert updated["origine"] == "META"
    assert updated["formation"] == "A3P"
    assert updated["lieu"] == "Côte d’Azur"

    reloaded = client.get("/api/crm/contacts/origin-meta").get_json()
    assert reloaded["origine"] == "META"
    assert reloaded["formation"] == "A3P"
    assert reloaded["lieu"] == "Côte d’Azur"


def test_crm_ui_displays_every_original_meta_answer():
    javascript = open(application.app.root_path + "/static/crm.js", encoding="utf-8").read()
    stylesheet = open(application.app.root_path + "/static/crm.css", encoding="utf-8").read()

    assert "const isMetaLead=" in javascript
    assert "['META','Google','Site internet'" in javascript
    assert "Origine verrouillée pour conserver la provenance META" in javascript
    assert "Formation A3P et lieu Côte d’Azur définis automatiquement" in javascript
    assert "function metaAnswersSection(c)" in javascript
    assert "Réponses du formulaire META" in javascript
    assert "rows.map(row=>" in javascript
    assert ".meta-answer-list" in stylesheet


def test_email_match_is_case_insensitive_and_only_fills_empty_fields(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    salesforce_payloads = []
    monkeypatch.setattr(application, "creer_piste_salesforce", salesforce_payloads.append)
    data = application.load_data()
    data["crm_contacts"] = [{
        "id": "existing", "prenom": "Lina", "nom": "MARTIN",
        "mail": "lina@example.fr", "telephone": "", "formation": "APS",
        "statut": "Converti", "origine": "Téléphone", "activities": [],
    }]
    application.save_data(data)
    response = post(client, lead())
    assert response.status_code == 200
    assert response.get_json() == {"success": True, "result": "attached", "contact_id": "existing"}
    contact = application.load_data()["crm_contacts"][0]
    assert contact["telephone"] == "+33 6 12 34 56 78"
    assert contact["statut"] == "Converti" and contact["origine"] == "Téléphone"
    assert salesforce_payloads == []


def test_phone_match_accepts_french_formats_without_name(tmp_path, monkeypatch):
    for index, incoming in enumerate(("06 12 34 56 78", "+33612345678")):
        client = setup_client(tmp_path / str(index), monkeypatch)
        data = application.load_data()
        data["crm_contacts"] = [{
            "id": "phone-contact", "prenom": "", "nom": "", "mail": "",
            "telephone": "+33 6 12 34 56 78", "activities": [],
        }]
        application.save_data(data)
        payload = {"lead_id": f"phone-{index}", "telephone": incoming}
        response = post(client, payload)
        assert response.status_code == 200
        assert response.get_json()["contact_id"] == "phone-contact"
        assert len(application.load_data()["crm_contacts"]) == 1


def test_identical_calls_are_idempotent(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    first = post(client, lead())
    second = post(client, lead())
    assert first.status_code == 201 and second.status_code == 200
    assert second.get_json()["result"] == "already_processed"
    data = application.load_data()
    assert len(data["crm_contacts"]) == len(data["crm_meta_lead_submissions"]) == 1


def test_invalid_payload_returns_explicit_400(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    missing_id = post(client, {"email": "test@example.fr"})
    assert missing_id.status_code == 400 and "Identifiant Meta manquant" in missing_id.get_json()["error"]
    missing_contact = post(client, {"leadgen_id": "meta-empty"})
    assert missing_contact.status_code == 400 and "téléphone" in missing_contact.get_json()["error"]
