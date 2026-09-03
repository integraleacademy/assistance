from io import BytesIO
from pathlib import Path

import app as application


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    test_client = application.app.test_client()
    with test_client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return test_client


def create_email_template(test_client, filename="programme.pdf", content=b"PDF template"):
    return test_client.post(
        "/api/crm/templates",
        data={
            "type": "email",
            "nom": "Programme APS",
            "sujet": "Votre programme",
            "contenu": "<p>Bonjour {{ prenom }}</p>",
            "attachment": (BytesIO(content), filename),
        },
        content_type="multipart/form-data",
    )


def test_template_attachment_is_stored_privately_and_downloadable(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)

    response = create_email_template(test_client)

    assert response.status_code == 201
    template = response.get_json()
    metadata = template["piece_jointe"]
    assert metadata["nom"] == "programme.pdf"
    assert metadata["taille"] == len(b"PDF template")
    stored_path = application._crm_email_attachment_path(metadata)
    assert stored_path
    assert Path(stored_path).read_bytes() == b"PDF template"
    assert str(tmp_path) in stored_path

    downloaded = test_client.get(
        f"/api/crm/templates/{template['id']}/attachment",
    )
    assert downloaded.status_code == 200
    assert downloaded.data == b"PDF template"
    assert "programme.pdf" in downloaded.headers["Content-Disposition"]


def test_template_attachment_can_be_replaced_removed_and_deleted(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    template = create_email_template(test_client).get_json()
    original_path = application._crm_email_attachment_path(template["piece_jointe"])

    replaced = test_client.patch(
        f"/api/crm/templates/{template['id']}",
        data={"attachment": (BytesIO(b"new file"), "nouveau.docx")},
        content_type="multipart/form-data",
    )

    assert replaced.status_code == 200
    replaced_template = replaced.get_json()
    replacement_path = application._crm_email_attachment_path(
        replaced_template["piece_jointe"],
    )
    assert replaced_template["piece_jointe"]["nom"] == "nouveau.docx"
    assert not Path(original_path).exists()
    assert Path(replacement_path).read_bytes() == b"new file"

    removed = test_client.patch(
        f"/api/crm/templates/{template['id']}",
        data={"remove_attachment": "true"},
        content_type="multipart/form-data",
    )
    assert removed.status_code == 200
    assert "piece_jointe" not in removed.get_json()
    assert not Path(replacement_path).exists()

    restored = test_client.patch(
        f"/api/crm/templates/{template['id']}",
        data={"attachment": (BytesIO(b"last file"), "dernier.pdf")},
        content_type="multipart/form-data",
    ).get_json()
    restored_path = application._crm_email_attachment_path(restored["piece_jointe"])
    assert test_client.delete(f"/api/crm/templates/{template['id']}").status_code == 204
    assert not Path(restored_path).exists()


def test_template_attachment_is_sent_and_manual_file_overrides_it(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    template = create_email_template(test_client).get_json()
    contact = test_client.post(
        "/api/crm/contacts", json={"prenom": "Lina"},
    ).get_json()
    contact = test_client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"mail": "lina@example.com"},
    ).get_json()
    deliveries = []

    def fake_send(to, subject, plain, html, attachments_paths=None):
        paths = list(attachments_paths or [])
        deliveries.append({
            "to": to,
            "names": [Path(path).name for path in paths],
            "contents": [Path(path).read_bytes() for path in paths],
        })
        return True

    monkeypatch.setattr(application, "send_email_html", fake_send)
    endpoint = f"/api/crm/contacts/{contact['id']}/message"
    base_payload = {
        "type": "email",
        "template_id": template["id"],
        "sujet": template["sujet"],
        "contenu": template["contenu"],
    }

    template_send = test_client.post(endpoint, json=base_payload)
    manual_send = test_client.post(
        endpoint,
        data={
            **base_payload,
            "attachment": (BytesIO(b"manual file"), "devis.pdf"),
        },
        content_type="multipart/form-data",
    )
    without_attachment = test_client.post(
        endpoint,
        data={**base_payload, "include_template_attachment": "false"},
        content_type="multipart/form-data",
    )
    test_email = test_client.post("/api/crm/test-email", json={
        "destinataire": "test@example.com",
        "sujet": template["sujet"],
        "contenu": template["contenu"],
        "template_id": template["id"],
    })

    assert template_send.status_code == 200
    assert manual_send.status_code == 200
    assert without_attachment.status_code == 200
    assert test_email.status_code == 200
    assert deliveries[0]["names"] == ["programme.pdf"]
    assert deliveries[0]["contents"] == [b"PDF template"]
    assert deliveries[1]["names"] == ["devis.pdf"]
    assert deliveries[1]["contents"] == [b"manual file"]
    assert deliveries[2]["names"] == []
    assert deliveries[3]["names"] == ["programme.pdf"]


def test_multiple_manual_attachments_are_all_sent_and_keep_duplicate_names(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    contact = test_client.post(
        "/api/crm/contacts", json={"prenom": "Lina"},
    ).get_json()
    contact = test_client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"mail": "lina@example.com"},
    ).get_json()
    delivered = {}

    def fake_send(to, subject, plain, html, attachments_paths=None):
        paths = list(attachments_paths or [])
        delivered["names"] = [Path(path).name for path in paths]
        delivered["contents"] = [Path(path).read_bytes() for path in paths]
        return True

    monkeypatch.setattr(application, "send_email_html", fake_send)
    response = test_client.post(
        f"/api/crm/contacts/{contact['id']}/message",
        data={
            "type": "email", "sujet": "Documents", "contenu": "Bonjour",
            "attachment": [
                (BytesIO(b"first"), "document.pdf"),
                (BytesIO(b"second"), "document.pdf"),
            ],
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert delivered["names"] == ["document.pdf", "document.pdf"]
    assert delivered["contents"] == [b"first", b"second"]


def test_multiple_manual_attachments_share_the_total_size_limit(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    contact = test_client.post(
        "/api/crm/contacts", json={"prenom": "Lina"},
    ).get_json()
    monkeypatch.setattr(application, "CRM_EMAIL_ATTACHMENT_MAX_BYTES", 5)

    response = test_client.post(
        f"/api/crm/contacts/{contact['id']}/message",
        data={
            "type": "email", "sujet": "Documents", "contenu": "Bonjour",
            "attachment": [
                (BytesIO(b"123"), "un.pdf"),
                (BytesIO(b"456"), "deux.pdf"),
            ],
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 413
    assert "ensemble des pièces jointes" in response.get_json()["error"]


def test_saved_attachment_follows_an_automatic_template_send(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    template = create_email_template(test_client).get_json()
    data = application.load_data()
    stored_template = next(
        item for item in data["crm_email_templates"]
        if item["id"] == template["id"]
    )
    stored_template["nom"] = "FT refusé"
    contact = {
        "id": "contact-auto",
        "prenom": "Lina",
        "nom": "Martin",
        "mail": "lina@example.com",
        "formation": "APS",
        "activities": [],
    }
    delivered = {}

    def fake_send(to, subject, plain, html, attachments_paths=None):
        delivered["names"] = [Path(path).name for path in attachments_paths or []]
        return True

    monkeypatch.setattr(application, "send_email_html", fake_send)
    with application.app.test_request_context():
        result = application._crm_send_appointment_followup(
            data, contact, "FT refusé",
        )

    assert result["email"] is True
    assert delivered["names"] == ["programme.pdf"]


def test_email_attachment_validation_and_sms_rejection(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)

    invalid = create_email_template(test_client, "danger.exe", b"binary")
    empty = create_email_template(test_client, "vide.pdf", b"")
    monkeypatch.setattr(application, "CRM_EMAIL_ATTACHMENT_MAX_BYTES", 5)
    oversized = create_email_template(test_client, "gros.pdf", b"123456")
    sms = test_client.post(
        "/api/crm/templates",
        data={
            "type": "sms", "nom": "SMS", "contenu": "Bonjour",
            "attachment": (BytesIO(b"file"), "document.pdf"),
        },
        content_type="multipart/form-data",
    )

    assert invalid.status_code == 400
    assert "Format" in invalid.get_json()["error"]
    assert empty.status_code == 400
    assert "vide" in empty.get_json()["error"]
    assert oversized.status_code == 413
    assert "20 Mo" in oversized.get_json()["error"]
    assert sms.status_code == 400
    assert "réservées aux e-mails" in sms.get_json()["error"]


def test_crm_email_attachment_controls_are_exposed_in_both_interfaces():
    javascript = Path(application.app.root_path, "static", "crm.js").read_text(
        encoding="utf-8",
    )
    stylesheet = Path(application.app.root_path, "static", "crm.css").read_text(
        encoding="utf-8",
    )

    for marker in (
        'id="tplAttachment" type="file"',
        'id="messageAttachment" type="file" multiple',
        "manualAttachments.forEach(file=>formData.append('attachment'",
        "draftState.attachments=[...manualAttachments]",
        "data-remove-message-attachment",
        "include_template_attachment",
        "templateAttachmentBadge",
        "Pièce jointe du modèle",
        "Plusieurs fichiers acceptés, jusqu’à 20 Mo au total",
    ):
        assert marker in javascript
    for selector in (
        ".email-attachment-field",
        ".email-attachment-chip",
        ".template-attachment-badge",
    ):
        assert selector in stylesheet
