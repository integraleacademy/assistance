import json

import app as application


def quote_data():
    quote = {
        "id": "quote-1",
        "motif": "Demande de devis détaillé",
        "token_plan": "token-1",
        "nom": "Martin",
        "prenom": "Lina",
        "mail": "lina@example.com",
        "telephone": "06 12 34 56 78",
        "details": json.dumps({"formation": "VTC", "dates": "Octobre 2026"}),
        "statut_devis": "A envoyer",
    }
    contact = {
        "id": "contact-1",
        "nom": "Martin",
        "prenom": "Lina",
        "mail": "lina@example.com",
        "telephone": "+33 6 12 34 56 78",
        "source_devis_id": "quote-1",
        "activities": [],
    }
    return {"demandes": [quote], "crm_contacts": [contact]}, quote, contact


def client(tmp_path, monkeypatch, data):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application._DATA_CACHE_PAYLOAD = None
    application._DATA_CACHE_SIGNATURE = None
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    application.save_data(data)
    test_client = application.app.test_client()
    with test_client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return test_client


def test_sending_quote_adds_email_activity_to_linked_crm_contact(tmp_path, monkeypatch):
    data, _, _ = quote_data()
    test_client = client(tmp_path, monkeypatch, data)
    delivered = {}

    def send(to_emails, subject, plain_text, html_body):
        delivered.update(to=to_emails, subject=subject, html=html_body)
        return True

    monkeypatch.setattr(application, "send_email_html", send)
    response = test_client.post("/admin-devis/envoyer-plan/quote-1")
    assert response.status_code == 302

    stored = application.load_data()
    activity = stored["crm_contacts"][0]["activities"][0]
    assert activity["kind"] == "email"
    assert activity["title"] == "E-mail « Devis détaillé » envoyé"
    assert f"Objet : {delivered['subject']}" in activity["detail"]
    assert "Destinataire : lina@example.com" in activity["detail"]
    assert activity["preview"] == delivered["html"]
    assert activity["source_devis_id"] == "quote-1"
    assert activity["date"]
    assert stored["crm_contacts"][0]["updated_at"] == activity["date"]


def test_failed_quote_delivery_does_not_add_success_activity(tmp_path, monkeypatch):
    data, _, _ = quote_data()
    test_client = client(tmp_path, monkeypatch, data)
    monkeypatch.setattr(application, "send_email_html", lambda *args, **kwargs: False)
    response = test_client.post("/admin-devis/envoyer-plan/quote-1")
    assert response.status_code == 302
    assert application.load_data()["crm_contacts"][0]["activities"] == []


def test_legacy_quote_matches_only_one_identity_compatible_contact():
    data, quote, contact = quote_data()
    contact.pop("source_devis_id")
    assert application._crm_contact_for_quote_email(data, quote) is contact

    duplicate = dict(contact, id="contact-2", activities=[])
    data["crm_contacts"].append(duplicate)
    assert application._crm_contact_for_quote_email(data, quote) is None


def test_unmatched_quote_does_not_create_a_crm_contact():
    data, quote, _ = quote_data()
    data["crm_contacts"] = []
    assert application._crm_record_quote_email_sent(
        data, quote, "Objet test", "<p>Devis</p>",
    ) is False
    assert data["crm_contacts"] == []
