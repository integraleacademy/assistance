import io

import app as application
from crm_salesforce_import import parse_salesforce_csv


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


def test_parse_salesforce_csv_accepts_excel_semicolon_separator():
    rows = parse_salesforce_csv(
        "Id;FirstName;LastName;Email\n00Q1;Léa;Martin;lea@example.com\n".encode("cp1252")
    )

    assert rows == [
        {
            "Id": "00Q1",
            "FirstName": "Léa",
            "LastName": "Martin",
            "Email": "lea@example.com",
        }
    ]


def test_parse_salesforce_csv_accepts_report_labels_and_tabs():
    rows = parse_salesforce_csv(
        b"Lead ID\tFirst Name\tLast Name\tEmail\n00Q2\tLina\tMartin\tlina@example.com\n"
    )

    assert rows[0]["Id"] == "00Q2"
    assert rows[0]["FirstName"] == "Lina"
    assert rows[0]["LastName"] == "Martin"
