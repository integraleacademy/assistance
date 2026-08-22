import importlib.util
import uuid
from pathlib import Path

import crm_salesforce_migration as migration_template
from crm_salesforce_date_guardrails import install_salesforce_date_guardrails
from crm_salesforce_genuine_new_guardrails import (
    install_salesforce_genuine_new_guardrails,
)
from crm_salesforce_migration_guardrails import install_salesforce_migration_guardrails
from crm_salesforce_report_guardrails import install_salesforce_report_guardrails
from crm_salesforce_scope_guardrails import install_salesforce_scope_guardrails
from crm_salesforce_status_guardrails import install_salesforce_status_guardrails


def _fresh_migration():
    module_name = f"crm_salesforce_genuine_new_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(migration_template.__file__),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    install_salesforce_migration_guardrails(module)
    install_salesforce_status_guardrails(module)
    install_salesforce_date_guardrails(module)
    install_salesforce_report_guardrails(module)
    install_salesforce_scope_guardrails(module)
    install_salesforce_genuine_new_guardrails(module)
    return module


def _row(identifier, status, email=None, formation="A3P", **extra):
    phone_suffix = sum(ord(character) for character in identifier) % 10000
    return {
        "Id": identifier,
        "FirstName": "Lina",
        "LastName": identifier,
        "Email": email or f"{identifier}@example.com",
        "Phone": f"060000{phone_suffix:04d}",
        "Status": status,
        "CreatedDate": "2026-08-20T10:00:00Z",
        "Type_de_formation__c": formation,
        **extra,
    }


def test_genuine_french_and_english_new_statuses_are_allowed():
    migration = _fresh_migration()
    rows = [
        _row("00Qnewfr", "Nouveau"),
        _row("00Qnewen", "New"),
    ]

    result = migration.import_complete_rows([], rows, dry_run=True)

    assert result["created"] == 2
    assert result["status_counts"] == {"Nouveaux": 2}
    assert result["new_status_source_counts"] == {
        "Nouveau": 1,
        "New": 1,
    }
    assert result["genuine_new_count"] == 2
    assert result["unexpected_new_count"] == 0
    assert result["genuine_new_allowed"] is True
    assert {item["salesforce_id"] for item in result["genuine_new_samples"]} == {
        "00Qnewfr",
        "00Qnewen",
    }


def test_unknown_status_still_blocks_as_unexpected_new():
    migration = _fresh_migration()
    rows = [_row("00Qunknown", "À qualifier manuellement")]

    result = migration.import_complete_rows([], rows, dry_run=True)

    assert result["status_counts"] == {"Nouveaux": 1}
    assert result["genuine_new_count"] == 0
    assert result["unexpected_new_count"] == 1
    assert result["genuine_new_allowed"] is False
    assert result["unexpected_new_samples"][0]["source_status"] == (
        "À qualifier manuellement"
    )


def test_open_not_contacted_with_a_formation_is_not_silently_allowed():
    migration = _fresh_migration()
    rows = [_row("00Qopen", "Open - Not Contacted", formation="APS")]

    result = migration.import_complete_rows([], rows, dry_run=True)

    assert result["status_counts"] == {"Nouveaux": 1}
    assert result["genuine_new_count"] == 0
    assert result["unexpected_new_count"] == 1
    assert result["new_status_source_counts"] == {
        "Open - Not Contacted": 1,
    }


def test_genuine_new_count_is_calculated_after_safe_deduplication():
    migration = _fresh_migration()
    rows = [
        _row("00Qduplicate1", "Nouveau", email="same@example.com"),
        {
            **_row("00Qduplicate2", "Nouveau", email="same@example.com"),
            "LastName": "00Qduplicate1",
            "LastModifiedDate": "2026-08-21T10:00:00Z",
        },
    ]

    result = migration.import_complete_rows([], rows, dry_run=True)

    assert result["prepared_rows"] == 1
    assert result["duplicates_in_file"] == 1
    assert result["genuine_new_count"] == 1
    assert result["unexpected_new_count"] == 0


def test_non_new_rows_do_not_change_the_new_status_counters():
    migration = _fresh_migration()
    rows = [
        _row("00Qfollow", "A relancer"),
        _row("00Qconverted", "Qualifié", IsConverted="1"),
    ]

    result = migration.import_complete_rows([], rows, dry_run=True)

    assert result["new_status_source_counts"] == {}
    assert result["genuine_new_count"] == 0
    assert result["unexpected_new_count"] == 0
    assert result["genuine_new_allowed"] is True
