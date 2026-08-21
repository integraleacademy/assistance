import copy
import io

import crm_salesforce_migration as migration
from crm_salesforce_csv_compat import parse_salesforce_csv


def test_complete_migration_imports_all_years_and_formations():
    rows = [
        {
            "Id": "old-bts",
            "FirstName": "Lina",
            "LastName": "Martin",
            "CreatedDate": "2024-04-03T12:00:00Z",
            "Type_de_formation__c": "BTS MOS 2025",
        },
        {
            "Id": "recent-a3p",
            "FirstName": "Lou",
            "LastName": "Durand",
            "CreatedDate": "2026-08-10T12:00:00Z",
            "Type_de_formation__c": "A3P",
        },
    ]

    result = migration.import_complete_rows([], rows, dry_run=True)

    assert result["prepared_rows"] == 2
    assert result["created"] == 2
    assert result["skipped_other_year"] == 0
    assert result["skipped_formation"] == 0
    assert result["year_counts"] == {"2024": 1, "2026": 1}
    assert result["formation_counts"] == {"BTS MOS": 1, "A3P": 1}


def test_ambiguous_email_and_phone_match_is_blocked():
    contacts = [
        {
            "id": "crm-email",
            "mail": "shared@example.com",
            "telephone": "0611111111",
        },
        {
            "id": "crm-phone",
            "mail": "other@example.com",
            "telephone": "0622222222",
        },
    ]
    rows = [
        {
            "Id": "00Qambiguous",
            "FirstName": "Camille",
            "LastName": "Test",
            "Email": "shared@example.com",
            "MobilePhone": "0622222222",
            "CreatedDate": "2026-07-01T10:00:00Z",
        }
    ]

    result = migration.import_complete_rows(contacts, rows)

    assert result["ambiguous"] == 1
    assert result["created"] == 0
    assert result["updated"] == 0
    assert len(contacts) == 2
    assert result["ambiguous_samples"][0]["salesforce_id"] == "00Qambiguous"


def test_safe_merge_preserves_existing_crm_values_and_fills_blanks():
    contacts = [
        {
            "id": "crm-1",
            "prenom": "Jeanne",
            "nom": "Martin",
            "mail": "jeanne@example.com",
            "telephone": "0600000000",
            "formation": "",
            "statut": "A relancer",
            "commentaires": "Note CRM existante",
            "created_at": "2026-01-10T10:00:00+01:00",
            "updated_at": "2026-08-01T10:00:00+02:00",
            "activities": [],
        }
    ]
    rows = [
        {
            "Id": "00Qsafe",
            "FirstName": "Jeanne",
            "LastName": "Martin",
            "Email": "jeanne@example.com",
            "MobilePhone": "0600000000",
            "Status": "Qualified",
            "Type_de_formation__c": "A3P",
            "Description": "Commentaire Salesforce",
            "CreatedDate": "2024-04-01T10:00:00Z",
            "LastModifiedDate": "2026-08-20T10:00:00Z",
        }
    ]

    result = migration.import_complete_rows(
        contacts,
        rows,
        merge_policy=migration.MERGE_POLICY_SAFE,
    )

    assert result["updated"] == 1
    contact = contacts[0]
    assert contact["statut"] == "A relancer"
    assert contact["formation"] == "A3P"
    assert contact["salesforce_id"] == "00Qsafe"
    assert contact["created_at"].startswith("2024-04-01")
    assert "Note CRM existante" in contact["commentaires"]
    assert "Commentaire Salesforce" in contact["commentaires"]
    assert contact["activities"][0]["kind"] == "import"


def test_complete_migration_supports_optional_date_range():
    rows = [
        {"Id": "before", "LastName": "Avant", "CreatedDate": "2023-12-31T23:59:59Z"},
        {"Id": "inside", "LastName": "Dedans", "CreatedDate": "2024-06-01T08:00:00Z"},
        {"Id": "after", "LastName": "Apres", "CreatedDate": "2025-01-01T00:00:01Z"},
    ]

    result = migration.import_complete_rows(
        [],
        rows,
        dry_run=True,
        created_from="2024-01-01",
        created_to="2024-12-31",
    )

    assert result["prepared_rows"] == 1
    assert result["created"] == 1
    assert result["skipped_outside_date_range"] == 2


def test_parser_accepts_utf16_french_report_with_preamble():
    text = (
        "Rapport des pistes Salesforce\r\n"
        "Généré le 21/08/2026\r\n"
        "Identifiant de la piste\tPrénom de la piste\tNom de la piste\tAdresse e-mail\tDate de création\r\n"
        "00Qutf16\tLéa\tMartin\tlea@example.com\t20/08/2026 10:15\r\n"
    )

    rows = parse_salesforce_csv(
        text.encode("utf-16"),
        max_csv_bytes=20 * 1024 * 1024,
    )

    assert rows == [
        {
            "Id": "00Qutf16",
            "FirstName": "Léa",
            "LastName": "Martin",
            "Email": "lea@example.com",
            "CreatedDate": "20/08/2026 10:15",
        }
    ]


def _registered_app(initial_store=None):
    from flask import Flask

    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test")
    store = copy.deepcopy(initial_store or {"crm_contacts": []})
    saves = []

    def load_data():
        return store

    def save_data(data):
        saves.append(copy.deepcopy(data))

    def login_required(function):
        return function

    migration.register_salesforce_migration(
        app,
        current_user_fn=lambda: {"role": "admin"},
        load_data_fn=load_data,
        login_required_fn=login_required,
        save_data_fn=save_data,
    )
    return app, store, saves


def _csv_file():
    return (
        b"Id,FirstName,LastName,Email,CreatedDate,Type_de_formation__c\n"
        b"00Qroute,Lina,Martin,lina@example.com,2024-06-12T10:00:00Z,BTS MOS 2025\n"
    )


def test_complete_route_requires_matching_preview_token_before_write():
    app, store, saves = _registered_app()
    client = app.test_client()
    preview = client.post(
        "/api/crm/migrate-salesforce",
        data={
            "mode": migration.IMPORT_MODE_COMPLETE,
            "merge_policy": migration.MERGE_POLICY_SAFE,
            "dry_run": "1",
            "file": (io.BytesIO(_csv_file()), "Lead.csv"),
        },
        content_type="multipart/form-data",
    )

    assert preview.status_code == 200
    preview_payload = preview.get_json()
    assert preview_payload["created"] == 1
    assert preview_payload["preview_token"]
    assert store["crm_contacts"] == []
    assert saves == []

    rejected = client.post(
        "/api/crm/migrate-salesforce",
        data={
            "mode": migration.IMPORT_MODE_COMPLETE,
            "merge_policy": migration.MERGE_POLICY_SAFE,
            "file": (io.BytesIO(_csv_file()), "Lead.csv"),
        },
        content_type="multipart/form-data",
    )
    assert rejected.status_code == 409
    assert store["crm_contacts"] == []

    imported = client.post(
        "/api/crm/migrate-salesforce",
        data={
            "mode": migration.IMPORT_MODE_COMPLETE,
            "merge_policy": migration.MERGE_POLICY_SAFE,
            "preview_token": preview_payload["preview_token"],
            "file": (io.BytesIO(_csv_file()), "Lead.csv"),
        },
        content_type="multipart/form-data",
    )

    assert imported.status_code == 200
    assert imported.get_json()["created"] == 1
    assert len(store["crm_contacts"]) == 1
    assert store["crm_contacts"][0]["salesforce_id"] == "00Qroute"
    assert len(saves) == 1
    assert store["crm_salesforce_import_history"][0]["created"] == 1


def test_preview_token_becomes_stale_when_crm_changes():
    app, store, _ = _registered_app()
    client = app.test_client()
    preview = client.post(
        "/api/crm/migrate-salesforce",
        data={
            "mode": migration.IMPORT_MODE_COMPLETE,
            "merge_policy": migration.MERGE_POLICY_SAFE,
            "dry_run": "1",
            "file": (io.BytesIO(_csv_file()), "Lead.csv"),
        },
        content_type="multipart/form-data",
    ).get_json()

    store["crm_contacts"].append(
        {
            "id": "concurrent-change",
            "nom": "Ajout concurrent",
            "updated_at": "2026-08-21T12:00:00+02:00",
        }
    )

    response = client.post(
        "/api/crm/migrate-salesforce",
        data={
            "mode": migration.IMPORT_MODE_COMPLETE,
            "merge_policy": migration.MERGE_POLICY_SAFE,
            "preview_token": preview["preview_token"],
            "file": (io.BytesIO(_csv_file()), "Lead.csv"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 409
    assert len(store["crm_contacts"]) == 1
