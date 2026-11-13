import pytest

import app as application
from test_meta_zapier_webhook import lead, post, setup_client


@pytest.mark.parametrize(
    "form_id,form_name,expected_formation,parcours,location,expected_location",
    [
        ("2748123228939966", "APS_HOOK1_COTE_AZUR", "APS", "", "", "Côte d’Azur"),
        ("2007914916834027", "DESP_VIDEO_INITIALE_VAE", "DESP", "Formation initiale", "Paris", "Paris"),
        ("2007914916834027", "DESP_VIDEO_INITIALE_VAE", "DESP", "VAE", "Côte d’Azur", "Côte d’Azur"),
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
    assert "automatic_delivery" not in submission
    assert emails == [] and sms == []
    assert len(salesforce) == 1
    if expected_formation == "DESP":
        expected_type = "VAE" if parcours == "VAE" else "INITIAL"
        assert contact["desp_type"] == expected_type
        assert salesforce[0]["formation"] == ("DESP_VAE" if expected_type == "VAE" else "DESP_INIT")
    else:
        assert salesforce[0]["formation"] == "APS"


def test_shared_desp_form_does_not_guess_vae_or_a_location():
    fields, answers = application._parse_meta_lead_payload({
        "form_id": "2007914916834027",
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
