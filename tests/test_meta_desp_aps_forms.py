import pytest

import app as application
from test_meta_zapier_webhook import lead, post, setup_client


@pytest.mark.parametrize(
    "form_id,form_name,expected_formation,parcours,location,expected_location",
    [
        ("2748123228939966", "APS_HOOK1_COTE_AZUR", "APS", "", "", "Côte d’Azur"),
        ("2007914916834027", "DESP_VIDEO_INITIALE_VAE", "DESP", "Formation initiale", "Paris", "Paris"),
        ("2007914916834027", "DESP_VIDEO_INITIALE_VAE", "DESP", "VAE", "Côte d’Azur", "Côte d’Azur"),
        ("1665286434987197", "APS_HOOK1_COTE_AZUR", "APS", "", "", "Côte d’Azur"),
        ("1124725393644713", "DESP_VIDEO_INITIALE_VAE", "DESP", "Formation initiale", "Paris", "Paris"),
        ("1124725393644713", "DESP_VIDEO_INITIALE_VAE", "DESP", "VAE", "Côte d’Azur", "Côte d’Azur"),
    ],
)
def test_known_forms_keep_training_answers_and_do_not_send_a3p_messages(
    tmp_path, monkeypatch, form_id, form_name, expected_formation,
    parcours, location, expected_location,
):
    client = setup_client(tmp_path, monkeypatch)
    emails, sms, salesforce = [], [], []
    monkeypatch.setattr(application, "send_email_html", lambda *args: emails.append(args) or True)
    monkeypatch.setattr(application, "send_sms", lambda *args: sms.append(args) or True)
    monkeypatch.setattr(application, "creer_piste_salesforce", salesforce.append)
    payload = lead(**{
        "form_id": form_id, "Form Name": form_name,
        # A stale copied Zap value must not override the verified form identity.
        "formation": "A3P", "lieu": location,
        "Quel parcours DESP souhaitez-vous ?": parcours,
        "Question complémentaire": "Réponse conservée",
    })

    first = post(client, payload)
    repeated = post(client, payload)

    assert first.status_code == 201
    assert repeated.status_code == 200
    assert repeated.get_json()["result"] == "already_processed"
    data = application.load_data()
    assert len(data["crm_contacts"]) == 1
    contact = data["crm_contacts"][0]
    submission = data["crm_meta_lead_submissions"][0]
    assert contact["formation"] == expected_formation
    assert contact["lieu"] == expected_location
    assert contact["meta_source"]["form_id"] == form_id
    application._crm_enforce_meta_defaults(contact)
    assert contact["lieu"] == expected_location
    assert submission["mapped_fields"]["formation"] == expected_formation
    assert submission["raw_payload"] == payload
    assert submission["custom_answers"]["Question complémentaire"] == "Réponse conservée"
    assert submission["automatic_delivery"] == {"email": True, "sms": True}
    assert len(emails) == len(sms) == 1
    assert emails[0][0] == payload["Email"] and sms[0][0] == payload["Phone Number"]
    assert expected_formation in emails[0][1]
    for message in (*emails[0][2:], sms[0][1]):
        assert expected_formation in message and "A3P" not in message
        assert "{{" not in message
    assert len(salesforce) == 1
    if expected_formation == "DESP":
        expected_type = "VAE" if parcours == "VAE" else "INITIAL"
        assert contact["desp_type"] == expected_type
        assert salesforce[0]["formation"] == ("DESP_VAE" if expected_type == "VAE" else "DESP_INIT")
    else:
        assert salesforce[0]["formation"] == "APS"


@pytest.mark.parametrize("form_id", ["2007914916834027", "1124725393644713"])
def test_shared_desp_form_does_not_guess_vae_or_a_location(form_id):
    fields, answers = application._parse_meta_lead_payload({
        "form_id": form_id,
        "form_name": "DESP_VIDEO_INITIALE_VAE",
    })
    crm, _ = application._meta_crm_payload(fields, answers)
    assert crm["formation"] == "DESP"
    assert not crm.get("desp_type")
    assert not crm.get("lieu")
    contact = {"origine": "META", "meta_source": application._meta_source_details(fields)}
    application._crm_enforce_meta_defaults(contact)
    assert not contact.get("lieu")


def test_legacy_forms_keep_existing_a3p_assignment():
    crm, _ = application._meta_crm_payload(
        {"form_id": "1031503583183780", "form_name": "FORM V3", "formation": "APS", "lieu": "Paris"},
        {},
    )
    assert crm["formation"] == "A3P"
    assert crm["lieu"] == "Côte d’Azur"


def desp_zapier_payload():
    # Même structure que le POST Zapier, avec une identité fictive.
    return {
        "id": "desp-french-fields", "leadgen_id": "desp-french-fields",
        "form_id": "2007914916834027", "form_name": "DESP_VIDEO_INITIALE_VAE",
        "e-mail": "lina@example.fr", "nom_complet": "Lina Martin",
        "numéro_de_téléphone": "+33612345678",
        "quel_parcours_souhaitez-vous_étudier_?": "j’hésite entre les deux et je souhaite être conseillé.",
        "comment_envisagez-vous_de_financer_votre_parcours_?": "je prévois un financement personnel.",
        "quelle_démarche_avez-vous_déjà_engagée_ou_souhaitez-vous_engager_en_premier_?": "Aucune démarche",
        "raw_numéro_de_téléphone": "+33612345678",
        "raw_nom_complet": "Lina Martin",
        "raw_comment_envisagez-vous_de_financer_votre_parcours_?": "je_prévois_un_financement_personnel.",
    }


def test_actual_french_zapier_shape_fills_phone_name_and_personal_funding(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    payload = desp_zapier_payload()

    response = post(client, payload)

    assert response.status_code == 201
    data = application.load_data()
    contact = data["crm_contacts"][0]
    assert contact["telephone"] == "+33612345678"
    assert contact["prenom"] == "Lina" and contact["nom"] == "MARTIN"
    assert contact["financement_perso_possible"] == "OUI"
    for field in ("desp_type", "lieu", "cpf", "cpf_montant", "financement_ft", "dates_formation"):
        assert not contact.get(field), field
    assert data["crm_meta_lead_submissions"][0]["raw_payload"] == payload
    assert any("hésite" in row["answer"] for row in contact["meta_answers"])


def test_backfill_recovers_missing_phone_and_funding_without_changing_manual_data(tmp_path, monkeypatch):
    setup_client(tmp_path, monkeypatch)
    for function in ("send_email_html", "send_sms", "creer_piste_salesforce"):
        monkeypatch.setattr(application, function, lambda *args: pytest.fail("Unexpected delivery"))
    data = {
        "crm_contacts": [{"id": "existing", "prenom": "Lina", "nom": "MARTIN",
                          "mail": "manual@example.fr", "telephone": "", "formation": "DESP",
                          "desp_type": "INITIAL", "lieu": "Paris", "activities": []}],
        "crm_meta_lead_submissions": [{"contact_id": "existing", "received_at": "2026-11-13",
                                       "raw_payload": desp_zapier_payload()}],
    }

    assert application._crm_backfill_meta_submissions(data) is True
    contact = data["crm_contacts"][0]
    assert contact["telephone"] == "+33612345678"
    assert contact["financement_perso_possible"] == "OUI"
    assert contact["mail"] == "manual@example.fr"
    assert contact["desp_type"] == "INITIAL" and contact["lieu"] == "Paris"
    assert contact["activities"] == []
    assert application._crm_backfill_meta_submissions(data) is False
    contact["telephone"] = "+33698765432"
    contact["financement_perso_possible"] = "NON"
    assert application._crm_backfill_meta_submissions(data) is False
    assert contact["telephone"] == "+33698765432"
    assert contact["financement_perso_possible"] == "NON"


@pytest.mark.parametrize("payload,expected", [
    ({"raw_numéro_de_téléphone": "+33612345678"}, "+33612345678"),
    ({"raw_numéro_de_téléphone": "+33611111111", "phone_number": "+33612345678"}, "+33612345678"),
    ({"phone_number": "+33612345678", "raw_numéro_de_téléphone": "+33611111111"}, "+33612345678"),
])
def test_phone_aliases_preserve_readable_field_priority(payload, expected):
    fields, _ = application._parse_meta_lead_payload(payload)
    assert fields["phone_number"] == expected


@pytest.mark.parametrize("parcours,location,expected_type,expected_location", [
    ("Formation initiale", "Paris", "INITIAL", "Paris"),
    ("VAE", "Puget-sur-Argens", "VAE", "Côte d’Azur"),
    ("J’hésite entre la formation initiale et la VAE", "Paris ou Puget-sur-Argens", "", ""),
])
def test_real_desp_questions_only_map_explicit_choices(parcours, location, expected_type, expected_location):
    fields, answers = application._parse_meta_lead_payload({
        "form_id": "2007914916834027",
        "quel_parcours_souhaitez-vous_étudier_?": parcours,
        "où_pourriez-vous_suivre_les_deux_semaines_en_présentiel_?": location,
    })
    crm, _ = application._meta_crm_payload(fields, answers)
    assert crm.get("desp_type", "") == expected_type
    assert crm.get("lieu", "") == expected_location


@pytest.mark.parametrize("funding,expected_ft,expected_personal", [
    ("CPF", "", ""), ("France Travail", "OUI", ""),
    ("Financement personnel", "", "OUI"),
    ("Je ne sais pas : CPF ou France Travail", "", ""),
    ("Non, pas de financement personnel", "", ""),
    ("Je ne peux pas financer personnellement la formation", "", ""),
])
def test_aps_funding_plan_and_identity_help_are_not_completed_administrative_steps(funding, expected_ft, expected_personal):
    fields, answers = application._parse_meta_lead_payload({
        "form_id": "2748123228939966",
        "Quelle solution de financement envisagez-vous en priorité ?": funding,
        "Pour le CPF, souhaitez-vous de l’aide pour créer votre identité numérique ?": "Oui",
    })
    crm, rows = application._meta_crm_payload(fields, answers)
    assert crm.get("financement_ft", "") == expected_ft
    assert crm.get("financement_perso_possible", "") == expected_personal
    assert not crm.get("cpf") and not crm.get("identite_creation") and not crm.get("identite_ok")
    assert len(rows) == 2


@pytest.mark.parametrize("answer,expected", [("Oui", "OUI"), ("Non", "NON"), ("Je ne sais pas", "")])
def test_aps_partial_or_refused_funding_answers_map_to_personal_capacity(answer, expected):
    fields, answers = application._parse_meta_lead_payload({
        "form_id": "2748123228939966",
        "Si France Travail refuse votre demande, pourriez-vous régler la part restant à votre charge ?": answer,
        "Si votre financement ne couvre pas toute la formation, pourriez-vous régler la différence ?": answer,
    })
    crm, _ = application._meta_crm_payload(fields, answers)
    for field in ("refus_ft_perso", "reste_a_charge_perso", "financement_perso_possible"):
        assert crm.get(field, "") == expected


@pytest.mark.parametrize("prefix", ["", "raw_"])
def test_new_aps_reused_technical_key_means_cpf_consulted_not_identity_created(prefix):
    fields, answers = application._parse_meta_lead_payload({
        "form_id": "1665286434987197",
        prefix + "pour_le_cpf,_souhaitez-vous_de_l’aide_pour_créer_votre_identité_numérique ?": "Oui",
    })
    crm, rows = application._meta_crm_payload(fields, answers)
    assert crm["cpf"] == "OUI"
    assert not crm.get("identite_creation") and not crm.get("identite_ok")
    assert rows == [{
        "question": "Avez-vous déjà consulté votre Compte Personnel de Formation (CPF) ?",
        "answer": "Oui",
    }]


@pytest.mark.parametrize("form_id,answers,expected", [
    ("1665286434987197", {
        "quelle_solution_de_financement_envisagez-vous_en_priorité ?": "France Travail",
        "pour_le_cpf,_souhaitez-vous_de_l’aide_pour_créer_votre_identité_numérique ?": "Oui",
        "savez-vous_de_quel_montant_vous_disposez_sur_votre_compte_cpf_?": "1 200 €",
        "avez-vous_déjà_créé_votre_identité_numérique_la_poste_?": "Non",
        "si_france_travail_refuse_votre_demande,_pourriez-vous_régler_la_part_restant_à_votre_charge ?": "Non",
        "si_votre_financement_ne_couvre_pas_toute_la_formation,_pourriez-vous_régler_la_différence ?": "Non",
        "pouvez-vous_suivre_les_3_semaines_en_présentiel_à_puget-sur-argens_et_y_passer_l’examen ?": "Oui",
    }, {
        "formation": "APS", "lieu": "Côte d’Azur", "cpf": "OUI", "cpf_montant": "1200.00",
        "identite_creation": "NON", "financement_ft": "OUI",
        "refus_ft_perso": "NON", "reste_a_charge_perso": "NON", "financement_perso_possible": "NON",
    }),
    ("1124725393644713", {
        "quel_parcours_souhaitez-vous_étudier_?": "Formation initiale",
        "où_souhaiteriez-vous_suivre_votre_formation_?": "Paris",
        "comment_envisagez-vous_de_financer_votre_parcours_?": "CPF",
        "si_la_prise_en_charge_est_partielle_ou_refusée,_que_souhaitez-vous_faire_?": "Je peux financer personnellement la différence",
        "avez-vous_déjà_consulté_votre_compte_personnel_de_formation_(cpf)_?": "Oui",
        "de_quel_montant_disposez-vous_sur_votre_compte_personnel_de_formation_(cpf)_?": "2 350 €",
        "avez-vous_déjà_créé_votre_identité_numérique_la_poste_?": "Non",
        "quelles_responsabilités_avez-vous_personnellement_exercées_?": "Encadrement d’équipe",
        "pourriez-vous_illustrer_ces_responsabilités_avec_des_justificatifs_?": "Oui",
    }, {
        "formation": "DESP", "desp_type": "INITIAL", "lieu": "Paris", "cpf": "OUI",
        "cpf_montant": "2350.00", "identite_creation": "NON", "financement_perso_possible": "OUI",
    }),
])
def test_new_form_webhooks_persist_business_fields_and_every_answer(tmp_path, monkeypatch, form_id, answers, expected):
    client = setup_client(tmp_path, monkeypatch)
    payload = {
        "id": "new-form-" + form_id, "form_id": form_id,
        "nom_complet": "Lina Martin", "e-mail": "lina@example.fr",
        "numéro_de_téléphone": "+33612345678", **answers,
        **{"raw_" + key: value for key, value in answers.items()},
    }
    response = post(client, payload)
    assert response.status_code == 201
    assert post(client, payload).get_json()["result"] == "already_processed"
    data = application.load_data()
    assert len(data["crm_contacts"]) == 1
    contact = data["crm_contacts"][0]
    for field, value in expected.items():
        assert contact.get(field) == value, field
    assert contact["telephone"] == "+33612345678"
    assert not contact.get("dates_formation") and not contact.get("identite_ok")
    stored = data["crm_meta_lead_submissions"][0]
    assert stored["automatic_delivery"] == {"email": True, "sms": True}
    assert stored["raw_payload"] == payload
    assert stored["custom_answers"].items() >= answers.items()
    assert set(answers.values()) <= {row["answer"] for row in contact["meta_answers"]}
    assert application._crm_backfill_meta_submissions(data) is False


@pytest.mark.parametrize("amount,expected_amount,expected_tier", [
    ("Entre 1 000 et 2 000 €", "", "Entre 1 000 et 2 000 €"),
    ("Je ne sais pas", "", ""),
    ("Oui", "", ""),
    ("0 €", "0.00", ""),
])
def test_new_cpf_question_preserves_ranges_and_unknown_amounts(amount, expected_amount, expected_tier):
    crm, _ = application._meta_crm_payload({"form_id": "1665286434987197"}, {
        "Savez-vous de quel montant vous disposez sur votre compte CPF ?": amount,
    })
    assert crm.get("cpf_montant", "") == expected_amount
    assert crm.get("cpf_palier", "") == expected_tier


def test_france_travail_branch_does_not_invent_a_cpf_balance():
    crm, _ = application._meta_crm_payload({"form_id": "1665286434987197"}, {
        "quelle_solution_de_financement_envisagez-vous_en_priorité ?": "france travail, seul ou en complément du cpf.",
        "si_france_travail_refuse_votre_demande,_pourriez-vous_régler_la_part_restant_à_votre_charge ?": "oui, je pourrais la régler.",
    })
    assert crm["financement_ft"] == crm["financement_perso_possible"] == "OUI"
    assert not crm.get("cpf") and not crm.get("cpf_montant") and not crm.get("inscrit_ft")


@pytest.mark.parametrize("formation,form_id,slug", [
    ("APS", "1665286434987197", "aps"), ("DESP", "1124725393644713", "dirigeant"),
])
@pytest.mark.parametrize("score", [29, 30])
def test_automatic_messages_use_the_visible_models_and_course_booking(
        tmp_path, monkeypatch, formation, form_id, slug, score):
    client = setup_client(tmp_path, monkeypatch)
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    monkeypatch.setattr(application, "calculate_candidate_integration_score", lambda *args: {"score": score})
    emails, sms = [], []
    monkeypatch.setattr(application, "send_email_html", lambda *args: emails.append(args) or True)
    monkeypatch.setattr(application, "send_sms", lambda *args: sms.append(args) or True)
    models = client.get("/api/crm/templates").get_json()
    editable = {t["id"]: t for t in models["automatic_meta"] if t.get("meta_information")}
    assert len(editable) == 4
    email_id = f"automatic-meta-{formation.lower()}-email"
    sms_id = f"automatic-meta-{formation.lower()}-sms"
    assert editable[email_id]["nom"] == editable[sms_id]["nom"] == f"{formation} META"
    url = "https://calendly.com/integraleacademy/" + slug
    for template_id in (email_id, sms_id):
        preview = client.get(f"/api/crm/templates/{template_id}/meta-preview").get_json()["contenu"]
        assert url in preview and "{{" not in preview
    assert client.patch(f"/api/crm/templates/{email_id}", json={
        "nom": "Modèle personnalisé", "sujet": "Infos {{ prenom }}",
        "contenu": "<p>Dossier {{ prenom }}</p>{{ invitation_rdv }}",
    }).status_code == 200
    assert client.patch(f"/api/crm/templates/{sms_id}", json={
        "contenu": "Projet {{ prenom }}. {{ invitation_rdv }}",
    }).status_code == 200
    models = client.get("/api/crm/templates").get_json()
    assert sum(t["id"] == email_id for t in models["automatic_meta"] + models["email"]) == 1
    assert post(client, lead(form_id=form_id)).status_code == 201
    assert len(emails) == len(sms) == 1
    assert emails[0][1] == "Infos Lina"
    assert "Dossier Lina" in emails[0][2] and "Dossier Lina" in emails[0][3]
    assert "Cassandre MENARD" in emails[0][3] and sms[0][1].startswith("Projet Lina.")
    for message in (*emails[0][2:], sms[0][1]):
        assert (url in message) == (score >= 30)
        assert "{{" not in message


def test_distinct_form_attached_to_contact_uses_only_its_own_coordinates_and_course(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    emails, sms = [], []
    monkeypatch.setattr(application, "send_email_html", lambda *args: emails.append(args) or True)
    monkeypatch.setattr(application, "send_sms", lambda *args: sms.append(args) or True)
    assert post(client, lead(form_id="1665286434987197")).status_code == 201
    another = lead(form_id="1124725393644713", leadgen_id="second-submission")
    another.pop("Phone Number")
    assert post(client, another).get_json()["result"] == "attached"
    assert post(client, another).get_json()["result"] == "already_processed"
    assert "APS" in emails[0][1] and "DESP" in emails[1][1]
    assert len(emails) == 2 and len(sms) == 1
    data = application.load_data()
    assert data["crm_contacts"][0]["formation"] == "APS"
    assert data["crm_meta_lead_submissions"][0]["automatic_delivery"] == {"email": True, "sms": False}


def test_email_failure_leaves_sms_delivery_independent_and_logged(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    def failed_email(*args):
        raise RuntimeError("messagerie indisponible")
    monkeypatch.setattr(application, "send_email_html", failed_email)
    assert post(client, lead(form_id="1665286434987197")).status_code == 201
    data = application.load_data()
    assert data["crm_meta_lead_submissions"][0]["automatic_delivery"] == {"email": False, "sms": True}
    titles = {row["title"] for row in data["crm_contacts"][0]["activities"]}
    assert {"Échec de l’e-mail automatique", "SMS automatique envoyé"} <= titles


def test_receipt_survives_retry_and_preserves_a_write_during_delivery(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    payload = lead(form_id="1665286434987197")
    calls = []

    def deliver(*args):
        calls.append(args)
        assert post(client, payload).get_json()["result"] == "already_processed"
        data = application.load_data()
        data["crm_contacts"][0]["commentaires"] = "Note saisie pendant l’envoi"
        application.save_data(data)
        return True

    monkeypatch.setattr(application, "send_email_html", deliver)
    assert post(client, payload).status_code == 201
    data = application.load_data()
    assert len(calls) == 1
    assert data["crm_contacts"][0]["commentaires"] == "Note saisie pendant l’envoi"
    assert data["crm_meta_lead_submissions"][0]["automatic_delivery"] == {"email": True, "sms": True}
