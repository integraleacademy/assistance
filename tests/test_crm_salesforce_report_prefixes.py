import crm_salesforce_migration as migration
from crm_salesforce_migration_guardrails import install_salesforce_migration_guardrails


install_salesforce_migration_guardrails(migration)


def test_prefixed_salesforce_report_columns_are_mapped():
    rows = [
        {
            "Id": "00Qprefix",
            "FirstName": "Lina",
            "LastName": "Martin",
            "Email": "lina@example.com",
            "CreatedDate": "2026-08-20T10:00:00Z",
            "Piste : Owner Name": "Cassandre Menard",
            "Piste : Owner ID": "005owner",
            "Piste : Montant CPF": "991",
            "Piste : GCLID": "abc123",
            "Piste : Status": "Closed - Converted",
            "Piste : LeadSource": "Web",
        }
    ]

    result = migration.import_complete_rows([], rows, dry_run=True)
    mapped, _ = migration._prepare_complete_rows(
        rows, include_converted=True, deduplicate=True,
    )

    assert result["created"] == 1
    assert result["status_counts"] == {"Converti": 1}
    assert result["source_counts"] == {"Site internet": 1}
    assert mapped[0]["commercial"] == "Cassandre Menard"
    assert mapped[0]["salesforce_owner_id"] == "005owner"
    assert mapped[0]["cpf_montant"] == "991"
    assert mapped[0]["gclid"] == "abc123"
    assert mapped[0]["salesforce_is_converted"] is True
