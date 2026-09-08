import re
from io import BytesIO

import pytest

import app as application


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    test_client = application.app.test_client()
    with test_client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return test_client


def replace_field(html, key, value):
    return re.sub(
        rf"(<!-- EMAIL_{key}_START -->).*?(<!-- EMAIL_{key}_END -->)",
        lambda match: match[1] + value + match[2], html, flags=re.S,
    )


def test_free_email_starter_has_rendered_defaults_and_all_editable_text(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    starter = c.get("/api/crm/templates").get_json()["email_free_starter"]

    assert starter.startswith("<!doctype html>")
    assert "{%" not in starter
    assert "|default" not in starter
    assert "Votre projet : <strong>Formation {{ formation }}</strong>" in starter
    assert "Bonjour <strong>{{ prenom }}</strong>," in starter
    assert "<!-- EMAIL_CONTENT_START --><!-- EMAIL_CONTENT_END -->" in starter
    for key in (
        "HEADER_TITLE", "HEADER_SUBTITLE", "HEADER_TAGLINE", "GREETING",
        "SIGNATURE", "FOOTER_TAGLINE", "FOOTER_DETAILS", "FOOTER_NOTICE",
        "BRAND", "WEBSITE_LABEL",
    ):
        assert starter.count(f"<!-- EMAIL_{key}_START -->") == 1
        assert starter.count(f"<!-- EMAIL_{key}_END -->") == 1


@pytest.mark.parametrize("multipart", [False, True])
def test_custom_free_email_preview_matches_delivery_with_removed_sections(
        tmp_path, monkeypatch, multipart):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={
        "prenom": "Lina", "formation": "VTC", "mail": "lina@example.com",
    }).get_json()
    starter = c.get("/api/crm/templates").get_json()["email_free_starter"]
    content = replace_field(starter, "HEADER_TITLE", "Votre dossier {{ prenom }} &amp; documents")
    content = replace_field(content, "HEADER_SUBTITLE", "{{ formation }}")
    content = replace_field(content, "GREETING", "Bonjour {{ prenom }},")
    content = replace_field(content, "SIGNATURE", "L’équipe commerciale<br>À bientôt")
    for key in ("HEADER_TAGLINE", "FOOTER_TAGLINE", "FOOTER_NOTICE"):
        content = replace_field(content, key, "")
    content = replace_field(content, "CONTENT", "<p>Première ligne<br>Deuxième ligne : $&amp;</p>")
    payload = {"type": "email", "sujet": "Documents pour {{ prenom }}", "contenu": content}
    delivered = {}

    def fake_send(to, subject, plain, html, attachments_paths=None):
        delivered.update(html=html, subject=subject, attachments=len(attachments_paths or []))
        return True

    monkeypatch.setattr(application, "send_email_html", fake_send)
    endpoint = f"/api/crm/contacts/{contact['id']}"
    preview = c.post(endpoint + "/message-preview", json=payload)
    assert preview.status_code == 200
    assert application.load_data()["crm_contacts"][0]["activities"] == contact["activities"]
    if multipart:
        response = c.post(endpoint + "/message", data={
            **payload, "attachment": (BytesIO(b"test document"), "document.txt"),
        }, content_type="multipart/form-data")
    else:
        response = c.post(endpoint + "/message", json=payload)

    assert response.status_code == 200
    assert delivered["html"] == preview.get_json()["html"]
    assert delivered["subject"] == "Documents pour Lina"
    assert delivered["attachments"] == int(multipart)
    html = delivered["html"]
    assert html.count("<!doctype html>") == 1
    assert "Votre dossier Lina &amp; documents" in html
    assert "{{ formation }}" not in html
    assert "{{ prenom }}" not in html
    assert "Le résumé de notre échange" not in html
    assert "Faites le premier pas" not in html
    assert "Cassandre MENARD" not in html
    assert "L’équipe commerciale<br>À bientôt" in html
    assert "Deuxième ligne : $&amp;" in html
    assert any(activity.get("preview") == html for activity in response.get_json()["activities"])
