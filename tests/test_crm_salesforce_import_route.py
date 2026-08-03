import io

import app as application
from crm_salesforce_import import import_salesforce_rows, parse_salesforce_csv


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
    csv_file = (
        b"Id,FirstName,LastName,Email,CreatedDate\n"
        b"00Q1,Lina,Martin,lina@example.com,2025-06-12T10:00:00Z\n"
    )

    response = client.post(
        "/api/crm/import-salesforce",
        data={"file": (io.BytesIO(csv_file), "Lead.csv"), "dry_run": "1"},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.get_json()["created"] == 1


def test_salesforce_import_only_counts_2025_and_allowed_formations():
    rows = [
        {
            "Id": "allowed",
            "FirstName": "Lina",
            "LastName": "Martin",
            "CreatedDate": "2025-04-03T12:00:00Z",
            "Type_de_formation__c": "A3P",
        },
        {
            "Id": "old",
            "FirstName": "Lou",
            "LastName": "Martin",
            "CreatedDate": "2024-12-31T23:59:59Z",
            "Type_de_formation__c": "A3P",
        },
        {
            "Id": "excluded",
            "FirstName": "Sam",
            "LastName": "Martin",
            "CreatedDate": "03/04/2025 12:00",
            "Type_de_formation__c": "BTS MOS 2025",
        },
    ]

    result = import_salesforce_rows([], rows, dry_run=True)

    assert result["csv_rows"] == 3
    assert result["prepared_rows"] == 1
    assert result["created"] == 1
    assert result["formation_counts"] == {"A3P": 1}
    assert result["skipped_other_year"] == 1
    assert result["skipped_formation"] == 1


def test_salesforce_import_excludes_all_requested_formations():
    excluded = [
        "BTS",
        "BTS CI",
        "BTS MCO",
        "BTS MOS",
        "BTS MOS 2025",
        "BTS MOS 2026",
        "AFC",
        "APS + SSIAP",
        "BTS NDRC",
        "BTS PI",
        "BTS PI A DISTANCE 2026",
        "CAP AEPE",
        "CAP BOULANGERIE",
        "CAP COIFFURE",
        "CAP CUISINE",
        "CAP PATISSERIE",
    ]
    rows = [
        {
            "Id": str(index),
            "FirstName": "Test",
            "LastName": formation,
            "CreatedDate": "2025-01-02T08:00:00Z",
            "Type_de_formation__c": formation,
        }
        for index, formation in enumerate(excluded)
    ]

    result = import_salesforce_rows([], rows, dry_run=True)

    assert result["prepared_rows"] == 0
    assert result["created"] == 0
    assert result["formation_counts"] == {}
    assert result["status_counts"] == {}
    assert result["skipped_formation"] == len(excluded)


def test_salesforce_import_explains_which_source_statuses_become_new():
    rows = [
        {
            "Id": "blank-status",
            "LastName": "Sans statut",
            "CreatedDate": "2025-02-01T08:00:00Z",
            "Status": "",
        },
        {
            "Id": "known-new-status",
            "LastName": "Pas contacté",
            "CreatedDate": "2025-02-02T08:00:00Z",
            "Status": "Open - Not Contacted",
        },
        {
            "Id": "unexpected-status",
            "LastName": "Statut inattendu",
            "CreatedDate": "2025-02-03T08:00:00Z",
            "Status": "À qualifier dans Salesforce",
        },
        {
            "Id": "converted",
            "LastName": "Converti",
            "CreatedDate": "2025-02-04T08:00:00Z",
            "Status": "Qualified",
        },
    ]

    result = import_salesforce_rows([], rows, dry_run=True)

    assert result["status_counts"] == {"Nouveaux": 3, "Converti": 1}
    assert result["new_status_source_counts"] == {
        "Non renseigné": 1,
        "Open - Not Contacted": 1,
        "À qualifier dans Salesforce": 1,
    }


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
