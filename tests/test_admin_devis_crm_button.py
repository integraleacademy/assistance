import json

import app as application


def test_admin_devis_exposes_crm_button(tmp_path, monkeypatch):
    data_file = tmp_path / "data.json"
    data_file.write_text(json.dumps({"demandes": []}), encoding="utf-8")
    monkeypatch.setattr(application, "DATA_FILE", str(data_file))

    client = application.app.test_client()
    admin_email = next(
        email for email, user in application.USERS.items() if user.get("role") == "admin"
    )
    with client.session_transaction() as session:
        session["user_email"] = admin_email

    response = client.get("/admin-devis")

    assert response.status_code == 200
    assert b'href="/crm"' in response.data
    assert "Accéder au CRM".encode("utf-8") in response.data
