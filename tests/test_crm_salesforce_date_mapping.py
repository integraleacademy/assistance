import datetime as dt

import crm_salesforce_migration as migration
from crm_salesforce_date_guardrails import install_salesforce_date_guardrails
from crm_salesforce_migration_guardrails import install_salesforce_migration_guardrails
from crm_salesforce_status_guardrails import install_salesforce_status_guardrails


install_salesforce_migration_guardrails(migration)
install_salesforce_status_guardrails(migration)
install_salesforce_date_guardrails(migration)


def _prepared(**fields):
    row = {
        "Id": "00Qdate",
        "FirstName": "Lina",
        "LastName": "Martin",
        "Email": "date@example.com",
        **fields,
    }
    mapped, _ = migration._prepare_complete_rows(
        [row],
        include_converted=True,
        deduplicate=True,
    )
    return mapped[0]


def test_french_report_date_is_stored_as_paris_iso():
    contact = _prepared(CreatedDate="20/08/2026 23:45")

    assert contact["created_at"] == "2026-08-20T23:45:00+02:00"
    assert contact["received_at"] == contact["created_at"]
    assert contact["salesforce_created_at_raw"] == "20/08/2026 23:45"


def test_utc_salesforce_date_is_converted_to_paris():
    contact = _prepared(CreatedDate="2026-08-20T22:30:00Z")

    assert contact["created_at"] == "2026-08-21T00:30:00+02:00"
    assert contact["salesforce_created_at"] == contact["created_at"]


def test_date_filter_uses_the_paris_calendar_day():
    rows = [
        {
            "Id": "00Qin",
            "FirstName": "Lina",
            "LastName": "In",
            "Email": "in@example.com",
            "CreatedDate": "2026-08-20T22:30:00Z",
        },
        {
            "Id": "00Qout",
            "FirstName": "Lina",
            "LastName": "Out",
            "Email": "out@example.com",
            "CreatedDate": "2026-08-20T21:30:00Z",
        },
    ]

    result = migration.import_complete_rows(
        [],
        rows,
        dry_run=True,
        created_from="2026-08-21",
        created_to="2026-08-21",
    )

    assert result["prepared_rows"] == 1
    assert result["created"] == 1
    assert result["skipped_outside_date_range"] == 1


def test_converted_date_is_normalized_for_a_converted_lead():
    contact = _prepared(
        CreatedDate="20/08/2026 10:00",
        IsConverted="1",
        ConvertedDate="21/08/2026 09:15",
    )

    assert contact["statut"] == "Converti"
    assert contact["converted_at"] == "2026-08-21T09:15:00+02:00"
    assert contact["salesforce_converted_at_raw"] == "21/08/2026 09:15"


def test_invalid_created_date_keeps_a_valid_crm_timestamp_and_raw_audit():
    contact = _prepared(CreatedDate="date inconnue")
    parsed = dt.datetime.fromisoformat(contact["created_at"])

    assert parsed.tzinfo is not None
    assert contact["salesforce_created_at_raw"] == "date inconnue"
    assert contact["salesforce_created_at_invalid"] is True
