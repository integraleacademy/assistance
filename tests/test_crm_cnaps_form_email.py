import app as application


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application._DATA_CACHE_PAYLOAD = None
    application._DATA_CACHE_SIGNATURE = None
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    test_client = application.app.test_client()
    with test_client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return test_client


def contact_with_docs_aut(test_client):
    contact = test_client.post(
        "/api/crm/contacts",
        json={
            "prenom": "Lina",
            "nom": "Martin",
            "formation": "APS",
            "mail": "lina@example.com",
        },
    ).get_json()
    response = test_client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"carte_pro": "NON"},
    )
    assert response.status_code == 200
    data = application.load_data()
    data["crm_email_templates"] = [{
        "id": "docs-aut",
        "nom": "Docs AUT",
        "sujet": "Votre dossier {{ prenom }}",
        "contenu": "<p>Bonjour {{prenom}}, formation {{ formation }}</p>",
        "usage_count": 0,
    }]
    application.save_data(data)
    return response.get_json()


def test_cnaps_form_sends_named_template_and_persists_success(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    contact = contact_with_docs_aut(test_client)
    deliveries = []
    monkeypatch.setattr(
        application,
        "send_email_html",
        lambda *args: deliveries.append(args) or True,
    )
    timestamps = iter((
        "2026-08-23T17:19:59+00:00",
        "2026-08-23T17:20:00+00:00",
        "2026-08-23T18:29:59+00:00",
        "2026-08-23T18:30:00+00:00",
    ))
    monkeypatch.setattr(application, "_crm_now", lambda: next(timestamps))

    first = test_client.post(f"/api/crm/contacts/{contact['id']}/cnaps-form")
    second = test_client.post(f"/api/crm/contacts/{contact['id']}/cnaps-form")

    assert first.status_code == second.status_code == 200
    assert len(deliveries) == 2
    recipient, subject, plain, html_body = deliveries[0]
    assert recipient == "lina@example.com"
    assert subject == "Votre dossier Lina"
    assert "Bonjour Lina, formation APS" in plain
    assert "Bonjour Lina, formation APS" in html_body

    data = application.load_data()
    stored = next(item for item in data["crm_contacts"] if item["id"] == contact["id"])
    template = data["crm_email_templates"][0]
    assert stored["cnaps_form_sent_at"] == "2026-08-23T18:30:00+00:00"
    assert template["usage_count"] == 2
    assert template["last_used_at"] == stored["cnaps_form_sent_at"]
    assert [activity["titre"] for activity in stored["activities"]].count(
        "E-mail « Docs AUT » envoyé"
    ) == 2


def test_cnaps_form_failure_never_persists_a_false_success(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    contact = contact_with_docs_aut(test_client)
    monkeypatch.setattr(application, "send_email_html", lambda *args: False)

    response = test_client.post(f"/api/crm/contacts/{contact['id']}/cnaps-form")

    assert response.status_code == 502
    data = application.load_data()
    stored = next(item for item in data["crm_contacts"] if item["id"] == contact["id"])
    assert "cnaps_form_sent_at" not in stored
    assert not any(
        activity.get("titre") == "E-mail « Docs AUT » envoyé"
        for activity in stored.get("activities", [])
    )
    assert data["crm_email_templates"][0]["usage_count"] == 0


def test_cnaps_form_requires_no_card_email_and_docs_aut(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    contact = test_client.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "formation": "APS", "mail": "lina@example.com"},
    ).get_json()

    wrong_card = test_client.post(f"/api/crm/contacts/{contact['id']}/cnaps-form")
    assert wrong_card.status_code == 409

    test_client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"carte_pro": "NON"},
    )
    missing_template = test_client.post(f"/api/crm/contacts/{contact['id']}/cnaps-form")
    assert missing_template.status_code == 409
    assert "Docs AUT" in missing_template.get_json()["error"]

    data = application.load_data()
    stored = next(item for item in data["crm_contacts"] if item["id"] == contact["id"])
    stored["mail"] = ""
    data["crm_email_templates"] = [{"id": "docs-aut", "nom": "Docs AUT", "contenu": "Test"}]
    application.save_data(data)
    missing_email = test_client.post(f"/api/crm/contacts/{contact['id']}/cnaps-form")
    assert missing_email.status_code == 409
    assert "adresse e-mail" in missing_email.get_json()["error"]


def test_cnaps_form_frontend_contract():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        script = source.read()
    with open(application.app.root_path + "/static/crm.css", encoding="utf-8") as source:
        styles = source.read()

    assert 'id="sendCnapsForm"' in script
    assert 'data-show="without-card"' in script
    assert "/cnaps-form" in script
    assert "Formulaire envoyé le ${sentOn}" in script
    assert "day:'2-digit',month:'2-digit',year:'numeric'" in script
    assert "cnapsFormButton.disabled=false" in script
    assert ".cnaps-form-send.is-sent" in styles
    assert "background:var(--green)" in styles
