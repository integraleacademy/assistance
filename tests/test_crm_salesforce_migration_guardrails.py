import crm_salesforce_migration as migration
from crm_salesforce_migration_guardrails import install_salesforce_migration_guardrails


install_salesforce_migration_guardrails(migration)


def test_same_email_with_different_identity_is_not_merged():
    contacts = [
        {
            "id": "crm-jeanne",
            "prenom": "Jeanne",
            "nom": "Martin",
            "mail": "famille@example.com",
            "telephone": "",
            "updated_at": "2026-08-01T10:00:00+02:00",
            "activities": [],
        }
    ]
    rows = [
        {
            "Id": "00Qpaul",
            "FirstName": "Paul",
            "LastName": "Durand",
            "Email": "famille@example.com",
            "CreatedDate": "2026-08-20T10:00:00Z",
        }
    ]

    result = migration.import_complete_rows(contacts, rows)

    assert result["ambiguous"] == 1
    assert result["created"] == 0
    assert result["updated"] == 0
    assert contacts[0]["prenom"] == "Jeanne"
    assert "identité est différente" in result["ambiguous_samples"][0]["raison"]


def test_shared_email_in_source_blocks_every_identity():
    rows = [
        {
            "Id": "00Qalice",
            "FirstName": "Alice",
            "LastName": "Martin",
            "Email": "partage@example.com",
            "CreatedDate": "2026-08-19T10:00:00Z",
        },
        {
            "Id": "00Qbob",
            "FirstName": "Bob",
            "LastName": "Durand",
            "Email": "partage@example.com",
            "CreatedDate": "2026-08-20T10:00:00Z",
        },
    ]

    result = migration.import_complete_rows([], rows, dry_run=True)

    assert result["prepared_rows"] == 2
    assert result["created"] == 0
    assert result["ambiguous"] == 2
    assert result["duplicate_conflicts_in_file"] >= 1
    assert all(
        "partagée par plusieurs identités" in item["raison"]
        for item in result["ambiguous_samples"]
    )


def test_same_identity_is_safely_deduplicated_inside_source():
    rows = [
        {
            "Id": "00Qfirst",
            "FirstName": "Alice",
            "LastName": "Martin",
            "Email": "alice@example.com",
            "CreatedDate": "2026-08-19T10:00:00Z",
        },
        {
            "Id": "00Qsecond",
            "FirstName": "Alice",
            "LastName": "Martin",
            "Email": "alice@example.com",
            "CreatedDate": "2026-08-20T10:00:00Z",
        },
    ]

    result = migration.import_complete_rows([], rows, dry_run=True)

    assert result["prepared_rows"] == 1
    assert result["created"] == 1
    assert result["duplicates_in_file"] == 1
    assert result["ambiguous"] == 0


def test_untraceable_row_is_skipped_instead_of_created_twice():
    rows = [
        {
            "Id": "",
            "FirstName": "Sans",
            "LastName": "Coordonnées",
            "Email": "",
            "Phone": "",
            "CreatedDate": "2026-08-20T10:00:00Z",
        }
    ]

    result = migration.import_complete_rows([], rows, dry_run=True)

    assert result["prepared_rows"] == 0
    assert result["created"] == 0
    assert result["skipped_invalid"] == 1


def test_french_phone_with_optional_zero_matches_local_number():
    contacts = [
        {
            "id": "crm-alice",
            "prenom": "Alice",
            "nom": "Martin",
            "mail": "",
            "telephone": "06 12 34 56 78",
            "activities": [],
        }
    ]
    rows = [
        {
            "Id": "00Qphone",
            "FirstName": "Alice",
            "LastName": "Martin",
            "Phone": "+33 (0)6 12 34 56 78",
            "CreatedDate": "2026-08-20T10:00:00Z",
        }
    ]

    result = migration.import_complete_rows(contacts, rows, dry_run=True)

    assert result["matched_phone"] == 1
    assert result["updated"] == 1
    assert result["created"] == 0


def test_owner_alias_and_long_formation_label_are_normalized():
    rows = [
        {
            "Id": "00Qowner",
            "FirstName": "Lina",
            "LastName": "Martin",
            "Email": "lina@example.com",
            "CreatedDate": "2026-08-20T10:00:00Z",
            "Owner.Name": "Cassandre Menard",
            "Owner ID": "005owner",
            "Type_de_formation__c": "Agent de protection physique des personnes",
        }
    ]

    result = migration.import_complete_rows([], rows, dry_run=True)

    assert result["created"] == 1
    assert result["formation_counts"] == {"A3P": 1}
    mapped, _ = migration._prepare_complete_rows(
        rows, include_converted=True, deduplicate=True,
    )
    assert mapped[0]["commercial"] == "Cassandre Menard"
    assert mapped[0]["salesforce_owner_id"] == "005owner"
