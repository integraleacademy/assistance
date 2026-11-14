import re

import html5lib
import pytest

import app as application
from crm_external_access import contact_payload
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
        ("2051871915516458", "DESP_VIDEO_INITIALE_VAE (v4)", "DESP", "VAE", "Paris", "Paris"),
        ("1313564204218877", "APS_HOOK1_COTE_AZUR (v3)", "APS", "", "", "Côte d’Azur"),
        ("3605918179579680", "DESP_VIDEO_INITIALE_VAE", "DESP", "Formation initiale", "Paris", "Paris"),
        ("1124945893774109", "APS_HOOK1_COTE_AZUR", "APS", "", "", "Côte d’Azur"),
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
    score = application.calculate_candidate_integration_score(contact)
    assert score["funding_solution_status"] == "full_personal_payment_planned"
    assert score["financial_breakdown"][0]["points"] == 80
    assert score["cpf_amount_eur"] is None
    context = application._build_candidate_ai_context(contact, data, score)
    assert "totalité des 4 300 €" in context["authoritative_facts"]["financing"]["fact"]
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
    assert application.calculate_candidate_integration_score(contact)["financial_breakdown"][0]["points"] == 80
    assert application._crm_backfill_meta_submissions(data) is False
    contact["telephone"] = "+33698765432"
    contact["financement_perso_possible"] = "NON"
    assert application._crm_backfill_meta_submissions(data) is False
    assert contact["telephone"] == "+33698765432"
    assert contact["financement_perso_possible"] == "NON"
    assert application.calculate_candidate_integration_score(contact)["financial_breakdown"][0]["points"] == 0


@pytest.mark.parametrize("form_id", ["2007914916834027", "1124725393644713"])
def test_only_primary_desp_personal_plan_covers_the_entire_price(form_id):
    question = "comment_envisagez-vous_de_financer_votre_parcours_?"
    fallback = "si_la_prise_en_charge_est_partielle_ou_refusée,_que_souhaitez-vous_faire_?"
    for key, value, full in [
        (question, "Je prévois un financement personnel.", True),
        ("raw_" + question, "je_prévois_un_financement_personnel.", True),
        (fallback, "Je prévois un financement personnel.", False),
        (question, "Je prévois un financement personnel partiel.", False),
        (question, "Je ne prévois pas de financement personnel.", False),
        (question, "CPF et financement personnel", False),
    ]:
        fields, answers = application._parse_meta_lead_payload({"form_id": form_id, key: value})
        mapped, rows = application._meta_crm_payload(fields, answers)
        contact = {"formation": "DESP", "desp_type": "VAE"}
        application._meta_apply_contact_answers(contact, mapped, rows, fields=fields)
        score = application.calculate_candidate_integration_score(contact)
        assert (score["funding_solution_status"] == "full_personal_payment_planned") is full
        assert score["training_price_eur"] == 3800
        assert score["financial_breakdown"][0]["points"] == (80 if full else 0)


def test_later_funding_plan_supersedes_the_older_full_price_declaration():
    question = "Comment envisagez-vous de financer votre parcours ?"
    submissions = [
        {"contact_id": "existing", "received_at": date,
         "raw_payload": {"form_id": "1124725393644713", question: answer}}
        for date, answer in [("2026-11-14", "France Travail"), ("2026-11-13", "Je prévois un financement personnel.")]
    ]
    contact = {"id": "existing", "formation": "DESP"}
    data = {"crm_contacts": [contact], "crm_meta_lead_submissions": submissions}
    assert application._crm_backfill_meta_submissions(data)
    score = application.calculate_candidate_integration_score(contact)
    assert score["financial_breakdown"][0]["points"] == 0
    assert not contact["meta_source"].get("financement_perso_integral_prevu")
    assert contact["meta_source"]["field_answers"]["financement_ft"] == "France Travail"
    assert not application._crm_backfill_meta_submissions(data)


def test_desp_undecided_answers_reach_the_sheet_and_existing_contacts(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    questions = {
        "quel_parcours_souhaitez-vous_étudier_?": "J’hésite entre les deux et je souhaite être conseillé.",
        "comment_envisagez-vous_de_financer_votre_parcours_?": "Je souhaite d’abord faire le point sur les possibilités.",
        "si_la_prise_en_charge_est_partielle_ou_refusée,_que_souhaitez-vous_faire_?": "Examiner le devis avant de me décider.",
    }
    route, funding, personal = questions.values()
    payload = lead(form_id="1124725393644713", **{"Campaign Name": "Paris", **questions})
    result = post(client, payload)
    assert result.status_code == 201
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    url = "/api/crm/contacts/" + result.json["contact_id"]
    contact = client.get(url).json
    answers = {"lieu": "", "desp_type": route, "financement_ft": funding, "financement_perso_possible": personal}
    assert contact["meta_source"]["field_answers"] == answers
    assert not any(contact.get(field) for field in answers)
    assert contact["integration_score"]["personal_funding_capacity"] is None
    assert not contact.get("statut_demande_financement_ft") and not contact.get("reste_a_charge_perso")
    assert contact_payload(contact)["meta_source"] == {"formation": "DESP", "field_answers": answers}
    assert "meta_source" not in contact_payload(contact, detail=False)

    data = application.load_data()
    stored = data["crm_contacts"][0]
    del stored["meta_source"]["field_answers"]
    history = list(stored["activities"])
    assert application._crm_backfill_meta_submissions(data)
    assert stored["meta_source"]["field_answers"] == answers
    assert stored["activities"] == history
    assert not application._crm_backfill_meta_submissions(data)

    assert not client.patch(url, json={"lieu": "À déterminer"}).json["lieu"]
    updated = client.patch(url, json={"lieu": "Paris", "desp_type": "VAE", "financement_ft": "NON"})
    assert updated.status_code == 200
    assert client.patch(url, json={"commentaires": "Projet précisé."}).json["lieu"] == "Paris"
    after = client.get(url).json
    assert (after["lieu"], after["desp_type"], after["financement_ft"]) == ("Paris", "VAE", "NON")
    assert after["meta_source"]["field_answers"] == answers


@pytest.mark.parametrize("answer,capacity", [
    ("Poursuivre mon projet et régler la part restante.", "OUI"),
    ("Attendre d’avoir obtenu un financement complet.", ""),
    ("Examiner le devis avant de me décider.", ""),
])
def test_desp_partial_payment_choices_are_distinct_from_a_confirmed_amount(answer, capacity):
    mapped, _ = application._meta_crm_payload({"form_id": "1124725393644713"}, {
        "où_souhaiteriez-vous_suivre_votre_formation_?": "Les deux lieux me conviennent, selon les dates.",
        "raw_si_la_prise_en_charge_est_partielle_ou_refusée,_que_souhaitez-vous_faire_?": answer.replace(" ", "_"),
    })
    assert not mapped.get("lieu")
    assert mapped["meta_field_answers"]["lieu"] == "Les deux lieux me conviennent, selon les dates."
    assert mapped["meta_field_answers"]["financement_perso_possible"] == answer
    assert mapped.get("financement_perso_possible", "") == capacity
    assert not mapped.get("reste_a_charge_perso") and not mapped.get("financement_perso_integral_prevu")


def test_desp_city_answer_replaces_a_legacy_placeholder_only():
    fields = {"form_id": "2007914916834027"}
    crm, rows = application._meta_crm_payload(fields, {"Où pourriez-vous suivre les deux semaines en présentiel ?": "À Paris."})
    for original, expected in [("À déterminer", "Paris"), ("Côte d’Azur", "Côte d’Azur")]:
        contact = {"lieu": original}
        application._meta_apply_contact_answers(contact, crm, rows, fields=fields)
        assert contact["lieu"] == expected


@pytest.mark.parametrize("form_id", ["1665286434987197", "1031503583183780"])
def test_other_meta_courses_display_their_financing_answers(form_id):
    crm, _ = application._meta_crm_payload({"form_id": form_id}, {
        "Souhaitez-vous un financement France Travail ?": "Je dois me renseigner.",
        "Pourriez-vous financer personnellement le reste à charge ?": "Selon le devis.",
    })
    assert crm["meta_field_answers"] == {
        "financement_ft": "Je dois me renseigner.", "financement_perso_possible": "Selon le devis.",
    }
    assert crm["lieu"] == "Côte d’Azur" and not crm.get("desp_type")
    assert not crm.get("financement_ft") and not crm.get("financement_perso_possible")


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


# Clés relevées dans les deux exemples du 14 novembre. Les réponses utilisées
# dans les scénarios sont fictives. Les transports de setup_client sont simulés.
PERSONAL_BALANCE = (
    "si_votre_financement_ne_couvre_pas_toute_la_formation,_par_exemple_si_votre_cpf_est_insuffisant_"
    "ou_si_france_travail_refuse_votre_demande,_pourriez-vous_régler_personnellement_la_différence_?"
)
FUNDING_PLAN = "quelle_solution_de_financement_envisagez-vous_en_priorité_?"
FT_REQUEST = "souhaitez-vous_demander_un_financement_à_france_travail_pour_cette_formation_?"
APS_CPF_KEY = "pour_le_cpf,_souhaitez-vous_de_l’aide_pour_créer_votre_identité_numérique_?"
APS_AMOUNT = "de_quelle_somme_disposez-vous_sur_votre_compte_cpf_?"
IDENTITY = "avez-vous_déjà_créé_votre_identité_numérique_la_poste_?"


@pytest.mark.parametrize("form_id,answers,expected", [
    ("1124945893774109", {
        FUNDING_PLAN: "France Travail, seul ou en complément du CPF.",
        APS_CPF_KEY: "Oui", APS_AMOUNT: "1 250 €", IDENTITY: "Non",
        FT_REQUEST: "Non", PERSONAL_BALANCE: "Oui, je pourrais régler la différence.",
    }, {
        "formation": "APS", "lieu": "Côte d’Azur", "cpf": "OUI", "cpf_montant": "1250.00",
        "identite_creation": "NON", "financement_ft": "NON", "financement_perso_possible": "OUI",
        "reste_a_charge_perso": "OUI",
    }),
    ("3605918179579680", {
        "quel_est_votre_projet_aujourd’hui_?": "Diriger mon entreprise",
        "quel_parcours_souhaitez-vous_étudier_?": "Formation initiale",
        "où_souhaiteriez-vous_suivre_votre_formation_?": "Paris",
        "quelles_responsabilités_avez-vous_personnellement_exercées_?": "Management",
        "pourriez-vous_illustrer_ces_responsabilités_avec_des_justificatifs_?": "Oui",
        FUNDING_PLAN: "Mon CPF", "avez-vous_déjà_consulté_votre_compte_personnel_de_formation_(cpf)_?": "Oui",
        "de_quel_montant_disposez-vous_sur_votre_compte_cpf_?": "Entre 1 000 et 2 000 €",
        IDENTITY: "Non", PERSONAL_BALANCE: "Non, il me faudrait un financement complet.",
    }, {
        "formation": "DESP", "lieu": "Paris", "desp_type": "INITIAL", "cpf": "OUI",
        "cpf_palier": "Entre 1 000 et 2 000 €", "identite_creation": "NON",
        "financement_perso_possible": "NON", "reste_a_charge_perso": "NON",
    }),
])
def test_november_forms_save_one_copy_of_each_answer_and_keep_funding_separate(
        tmp_path, monkeypatch, form_id, answers, expected):
    client = setup_client(tmp_path, monkeypatch)
    raw = {"raw_" + key: value.replace(" ", "_") for key, value in answers.items()}
    payload = lead(form_id=form_id, **raw, **answers)
    assert post(client, payload).status_code == 201
    assert post(client, payload).json["result"] == "already_processed"
    data = application.load_data()
    contact = data["crm_contacts"][0]
    assert {key: contact.get(key) for key in expected} == expected
    assert len(contact["meta_answers"]) == len(answers)
    assert not any(row["question"].startswith("raw_") for row in contact["meta_answers"])
    assert not contact.get("statut_demande_financement_ft") and not contact.get("refus_ft_perso")
    if form_id == "3605918179579680":
        assert not contact.get("financement_ft") and not contact.get("cpf_montant")
    else:
        assert contact["meta_source"]["field_answers"]["financement_ft"] == "Non"
    assert data["crm_meta_lead_submissions"][0]["raw_payload"] == payload
    assert not application._crm_backfill_meta_submissions(data)


@pytest.mark.parametrize("reverse", [True, False])
def test_explicit_undecided_answers_take_precedence_over_funding_plan(reverse):
    answers = [(FT_REQUEST, "Je dois me renseigner."), (PERSONAL_BALANCE, "Selon le devis."),
               (FUNDING_PLAN, "France Travail, seul ou en complément du CPF.")]
    crm, _ = application._meta_crm_payload(
        {"form_id": "1124945893774109"}, dict(reversed(answers) if reverse else answers),
    )
    assert not crm.get("cpf") and not crm.get("financement_ft") and not crm.get("financement_perso_possible")
    assert crm["meta_field_answers"]["financement_ft"] == "Je dois me renseigner."
    assert crm["meta_field_answers"]["financement_perso_possible"] == "Selon le devis."


@pytest.mark.parametrize("answer,amount,tier", [
    (0, "0.00", ""), ("Je ne sais pas", "", ""), ("Plus de 1 500 €", "", "Plus de 1 500 €"),
])
def test_aps_amount_distinguishes_zero_unknown_and_range(answer, amount, tier):
    crm, _ = application._meta_crm_payload({"form_id": "1124945893774109"}, {APS_AMOUNT: answer})
    assert crm.get("cpf_montant", "") == amount and crm.get("cpf_palier", "") == tier
    assert not crm.get("cpf")


def test_meta_generated_sample_questions_are_never_financial_answers():
    fields, custom = application._parse_meta_lead_payload({
        "form_id": "1124945893774109", "nom_complet": "<test lead: dummy data for nom complet>",
        "numéro_de_téléphone": "<test lead: dummy data for numéro de téléphone>",
        FT_REQUEST: "<test lead: dummy data for " + FT_REQUEST + ">",
        PERSONAL_BALANCE: "<test lead: dummy data for " + PERSONAL_BALANCE + ">",
        APS_AMOUNT: "<test lead: dummy data for montant à 1 700 €>",
    })
    crm, rows = application._meta_crm_payload(fields, custom)
    assert len(rows) == 3
    assert not any(crm.get(key) for key in (
        "prenom", "telephone", "cpf_montant", "financement_ft", "financement_perso_possible",
    ))
    assert not any(crm["meta_field_answers"].values())


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
        assert url in preview and "{{" not in preview.replace("{{ prenom }}", "")
        if template_id == email_id:
            assert "Bonjour {{ prenom }}," in preview
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
    assert 'class="cover-art"' in emails[0][3] and emails[0][3].count(url) == int(score >= 30)
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


@pytest.mark.parametrize("form_id,route,location,contents,absent", [
    ("1665286434987197", "", "", ["175 heures de formation", "Prochaine session", "1 700 € TTC", "PUGET"], "Livret 1"),
    ("1124725393644713", "Formation initiale", "Paris",
     ["245 heures", "175 heures de e-learning", "Paris", "4 350 € TTC"], "Livret 1"),
    ("1124725393644713", "VAE", "", ["Livret 1", "Livret 2", "3 850 € TTC", "1 155 €", "2 695 €"], "245 heures"),
    ("1124725393644713", "", "", ["245 heures", "Livret 1", "4 350 € TTC", "3 850 € TTC", "Paris", "Puget-sur-Argens"], "Centre de formation :"),
])
@pytest.mark.parametrize("score", [29, 30])
def test_full_course_email_and_a3p_booking_panel_use_the_answers_for_this_submission(
        tmp_path, monkeypatch, form_id, route, location, contents, absent, score):
    client = setup_client(tmp_path, monkeypatch)
    monkeypatch.setattr(application, "calculate_candidate_integration_score", lambda *args: {"score": score})
    emails = []
    monkeypatch.setattr(application, "send_email_html", lambda *args: emails.append(args) or True)
    response = post(client, lead(**{
        "form_id": form_id, "Quel parcours DESP souhaitez-vous ?": route, "lieu": location,
    }))
    assert response.status_code == 201 and len(emails) == 1
    plain, body = emails[0][2:]
    course = "APS" if form_id == "1665286434987197" else "DESP"
    booking = "https://calendly.com/integraleacademy/" + ("aps" if course == "APS" else "dirigeant")
    for text in (plain, body):
        assert "Bonjour Lina," in text and "Cassandre MENARD" in text
        assert all(value in text for value in contents)
        assert absent not in text
        assert "https://www.integraleacademy.com/dossiersfc" in text
        assert "Télécharger mon devis" not in text and "A3P" not in text
        assert text.count(booking) == int(score >= 30)
        assert "📞 Prochaine étape" not in text and "calendly.com/integraleacademy/apr" not in text
        assert not re.search(r"\{\{.*?\}\}|\{[a-z_]+\}", text)
    assert body.count("Memoji de Cassandre MENARD") == 1
    assert 'href=""' not in body
    assert body.count('class="cover-art"') == body.count('class="academy-wordmark"') == 1
    assert "<img" not in body.split("<h1", 1)[0]
    assert re.search(r'<h1\b[^>]*>.*?<em\b', body, re.S)
    assert body.count('class="fact" align="center"') == 3
    assert body.count('alt="L’Identité Numérique La Poste"') == 1
    assert body.count('alt="Mon Compte Formation"') == 1
    assert "4,8/5" in plain and body.count(application.TRAINING_GOOGLE_REVIEWS_URL) == 1
    assert "IA Intégrale Academy" not in body and "chatgpt.com/g/" not in body
    if score >= 30:
        with application.app.app_context():
            panel = application._meta_booking_content({"formation": course}, html=True)
        assert panel in body and body.index(panel) < body.index("Dossier de présentation")
        assert '<table role="presentation" align="center"' in panel
        assert all(topic in panel for topic in ("La formation", "Les dates", "Les tarifs", "Le financement"))
    assert application.load_data().get("demandes", []) == []


def test_saved_legacy_meta_email_uses_the_new_layout_without_losing_edits(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    data = application.load_data()
    with application.app.test_request_context():
        legacy = application.render_template(
            "crm_email_wrapper.html", prenom="{{ prenom }}", formation="APS",
            contenu="<!-- EMAIL_CONTENT_START --><p>Projet de {{ prenom }}.</p><!-- EMAIL_CONTENT_END -->"
                    '<p><a href="https://exemple.fr/brochure">Ma brochure</a></p>{{ invitation_rdv }}',
        )
    data["crm_email_templates"] = [{
        **application._meta_information_template(data, "APS", "email"),
        "contenu": legacy, "sujet": "Nos informations pour {{ prenom }}", "versions": [{"contenu": legacy}],
    }]
    application.save_data(data)
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    model = client.get("/api/crm/templates/automatic-meta-aps-email/meta-preview").get_json()
    assert model["sujet"] == "Nos informations pour {{ prenom }}"
    preview = model["contenu"]
    assert "Bonjour {{ prenom }}," in preview and "<p>Projet de {{ prenom }}.</p>" in preview
    assert 'href="https://exemple.fr/brochure"' in preview
    assert preview.count("<!doctype html>") == preview.count('class="academy-wordmark"') == 1
    assert preview.count("https://calendly.com/integraleacademy/aps") == 1
    assert preview.count("Memoji de Cassandre MENARD") == 1 and "/static/logo.png" not in preview
    assert application.load_data()["crm_email_templates"] == data["crm_email_templates"]


def test_meta_desp_preview_matches_delivery_and_keeps_individual_centres(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    data = application.load_data()
    data["formation_sessions"] = {
        "paris": {"DESP_INIT": [{"label": "Du 1 au 20 mars 2099"}]},
        "cote_azur": {"DESP_INIT": [{"label": "Du 3 au 24 mai 2099"}]},
    }
    application.save_data(data)
    monkeypatch.setattr(application, "calculate_candidate_integration_score", lambda *args: {"score": 30})
    emails = []
    monkeypatch.setattr(application, "send_email_html", lambda *args: emails.append(args) or True)
    preview = client.get("/api/crm/templates/automatic-meta-desp-email/meta-preview").get_json()["contenu"]
    assert post(client, lead(form_id="1124725393644713")).status_code == 201
    assert "Bonjour {{ prenom }}," in preview
    assert emails[0][3] == preview.replace("{{ prenom }}", "Lina")
    document = html5lib.parse(preview, namespaceHTMLElements=False)
    summary = document.find('.//table[@class="session-summary"]')
    places = summary.findall('.//table[@class="session-place"]')
    assert [place.find('.//h3').text for place in places] == ["Puget-sur-Argens", "Paris"]
    assert "mai 2099" in places[0].find('.//p').text
    assert "mars 2099" in places[1].find('.//p').text
    assert [price.find('strong').text for price in summary.findall('.//p[@class="session-price"]')] == [
        "4 350 € TTC", "3 850 € TTC",
    ]
    cards = document.findall('.//table[@class="desp-venue"]')
    assert [card.find(".//h3").text for card in cards] == ["Puget-sur-Argens", "Paris"]
    puget, paris = [" ".join(" ".join(card.itertext()).split()) for card in cards]
    assert "mai 2099" in puget and "mars 2099" not in puget and "54 chemin du Carreou" in puget
    assert "mars 2099" in paris and "mai 2099" not in paris and "34 rue Poissonnière, 75002 Paris" in paris
    assert "dans un autre secteur" in preview and "gérant ou gérante" in preview and "encadré une équipe" in preview
    assert post(client, lead(**{
        "form_id": "1124725393644713", "leadgen_id": "initial-paris",
        "lieu": "Paris", "Quel parcours DESP souhaitez-vous ?": "Formation initiale",
        "dates_formation": "Du 1 au 20 décembre 2026",
    })).status_code == 200
    assert "4 300 € TTC" in emails[1][3] and "mars 2099" in emails[1][3]
    assert "mai 2099" not in emails[1][3] and "4 350 € TTC" not in emails[1][3]
    assert emails[1][3].count('class="desp-venue"') == 1
    for text in emails[1][2:]:
        assert "34 rue Poissonnière, 75002 Paris" in text and "Rivoli" not in text



def test_desp_sessions_preserve_venue_selection_exam_and_unstructured_dates():
    period = "Du 4 janvier au 18 février 2099"
    data = {"formation_sessions": {
        "paris": {"DESP_INIT": [
            {"label": period + " (présentiel du 8/02 au 18/02)", "date_examen": "2099-02-19"},
            {"label": "Session <spéciale> & jury"},
        ]},
        "cote_azur": {"DESP_INIT": []},
    }}
    with application.app.test_request_context():
        single = application.build_desp_init_email_html("Alice", period, "paris", "", data)
        mixed = application._meta_course_email(
            {"formation": "DESP", "lieu": "Paris ou Puget-sur-Argens", "dates_formation": period}, data, "", False,
        )
        vae = application._meta_course_email({"formation": "DESP", "desp_type": "VAE", "lieu": "Paris"}, data, "", True)
        _, abandoned_plain, abandoned_html = application._abandoned_training_email_content("Alice", "DESP_VAE")
    assert single.count("19/02/2099") == single.count("Session demandée") == 1
    assert "Présentiel du 8/02 au 18/02" in single and "Session &lt;spéciale&gt; &amp; jury" in single
    assert mixed.count('class="desp-venue"') == 2
    assert mixed.count("Dates à venir prochainement.") == 1 and "Session demandée" not in mixed
    for message in (vae, abandoned_plain, abandoned_html):
        assert "gérant ou gérante" in message and "encadré une équipe dans un autre secteur" in message
        assert "recevabilité de votre demande" in message and "34 rue Poissonnière" not in message
        assert "expérience significative dans la sécurité privée" not in message


@pytest.mark.parametrize("course", ["APS", "DESP_INIT", "DESP_VAE"])
def test_public_course_emails_keep_their_quote_and_standard_booking(course):
    with application.app.test_request_context():
        quote = "https://example.test/devis/123"
        if course == "DESP_VAE":
            body = application.build_vae_desp_email_html("Camille", quote)
        elif course == "DESP_INIT":
            body = application.build_desp_init_email_html("Camille", "", "paris", quote, {})
        else:
            body = application.build_aps_email_html("Camille", "", "cote_azur", quote, {})
    assert body.count(quote) == body.count("calendly.com/") == 1
    assert "📞 Prochaine étape" in body and "VOTRE RENDEZ-VOUS TÉLÉPHONIQUE" not in body
    assert "{meta_introduction_html}" not in body and "{prominent_phone_booking_html}" not in body


def test_course_summary_uses_requested_or_earliest_session_and_handles_empty_dates():
    labels = ["Du 14 au 18 mai 2099", "Du 5 au 9 janvier 2099"]
    assert application._next_email_session(labels) == labels[1]
    assert application._next_email_session(labels, labels[0]) == labels[0]
    assert application._next_email_session([], "À convenir") == "À convenir"
    assert application._next_email_session([]) == "Dates à venir prochainement"


def test_aps_form_and_meta_name_the_actual_next_session_and_keep_the_requested_date(monkeypatch):
    first, second = "Du 5 au 9 janvier 2099", "Du 14 au 18 mai 2099"
    monkeypatch.setattr(application, "_upcoming_session_labels", lambda *args: [second, first, second])
    saved = {"crm_email_templates": [{"id": "automatic-meta-aps-email", "sujet":
             "Formation APS — vos prochaines sessions et votre financement", "contenu": "Bonjour"}]}
    with application.app.test_request_context():
        messages = [
            application.build_aps_email_html("Camille", second, "cote_azur", "", {}),
            application._meta_course_email({"formation": "APS", "dates_formation": second}, {}, "", False),
        ]
        assert application._meta_information_template(saved, "APS", "email")["sujet"] == "Formation APS — prochaine session et financement"
        saved["crm_email_templates"][0]["sujet"] = "Objet personnalisé"
        assert application._meta_information_template(saved, "APS", "email")["sujet"] == "Objet personnalisé"
    for html in messages:
        document = html5lib.parse(html, namespaceHTMLElements=False)
        assert document.find('.//table[@class="session-summary"]//p[@class="session-date"]').text == first
        assert document.find('.//div[@class="preheader"]').text.startswith("Prochaine session APS")
        assert document.find('.//h2[@aria-label="📅 Prochaine session"]') is not None
        assert second in html and "(date demandée)" in html and "Autres dates" in html
        assert "votre session" not in html.casefold() and "prochaines sessions" not in html.casefold()
