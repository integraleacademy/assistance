import io

import app as application


def _admin_client(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    client = application.app.test_client()
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return client


def test_salesforce_import_route_is_registered_on_default_app_entrypoint():
    assert "crm_import_salesforce" in application.app.view_functions
    rule = next(
        rule
        for rule in application.app.url_map.iter_rules()
        if rule.endpoint == "crm_import_salesforce"
    )
    assert rule.rule == "/api/crm/import-salesforce"


def test_salesforce_import_dry_run_is_available_from_default_app(tmp_path, monkeypatch):
    client = _admin_client(tmp_path, monkeypatch)
    csv_file = b"Id,FirstName,LastName,Email\n00Q1,Lina,Martin,lina@example.com\n"

    response = client.post(
        "/api/crm/import-salesforce",
        data={"file": (io.BytesIO(csv_file), "Lead.csv"), "dry_run": "1"},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.get_json()["created"] == 1
