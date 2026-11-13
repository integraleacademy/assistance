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
