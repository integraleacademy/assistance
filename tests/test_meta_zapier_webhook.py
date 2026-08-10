import app as application


ENDPOINT = "/api/integrations/meta/zapier-leads"
SECRET = "test-webhook-secret"


def setup_client(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    monkeypatch.setenv("ZAPIER_META_WEBHOOK_SECRET", SECRET)
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
    assert len(data["crm_notifications"]) == 1
    submission = data["crm_meta_lead_submissions"][0]
    assert submission["raw_payload"] == payload
    assert submission["custom_answers"]["Avez-vous une carte professionnelle ?"] == ["Oui"]
    assert "Formation APS" in contact["activities"][0]["detail"]


def test_email_match_is_case_insensitive_and_only_fills_empty_fields(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
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
