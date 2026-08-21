import crm_salesforce_migration as migration
from crm_salesforce_migration_guardrails import install_salesforce_migration_guardrails
from crm_salesforce_status_guardrails import (
    PRIMARY_STATUSES,
    install_salesforce_status_guardrails,
)


install_salesforce_migration_guardrails(migration)
install_salesforce_status_guardrails(migration)


def _mapped(status):
    rows = [{
        "Id": f"00Q-{status}",
        "FirstName": "Lina",
        "LastName": "Martin",
        "Email": f"{status.replace(' ', '-')}@example.com",
        "Status": status,
        "CreatedDate": "2026-08-20T10:00:00Z",
    }]
    mapped, _ = migration._prepare_complete_rows(
        rows,
        include_converted=True,
        deduplicate=True,
    )
    return mapped[0]


def test_secondary_salesforce_statuses_use_secondary_field():
    expected = {
        "POEI": "POEI",
        "Session FT": "Marché FT",
        "Marché FT": "Marché FT",
        "Def MOB": "Def MOB",
        "Financement FT en cours": "Financement FT en cours",
        "Financement FT refusé": "Financement FT refusé",
        "C2P en cours": "C2P en cours",
    }
    for source, secondary in expected.items():
        contact = _mapped(source)
        assert contact["statut"] == "Nouveaux"
        assert contact["statut_secondaire"] == secondary
        assert contact["statut_secondaire_source"] == "salesforce_migration"


def test_funding_secondary_statuses_also_set_funding_code():
    in_progress = _mapped("Financement FT en cours")
    refused = _mapped("Financement FT refusé")

    assert in_progress["statut_demande_financement_ft"] == "en_cours_instruction"
    assert refused["statut_demande_financement_ft"] == "refusee"


def test_every_primary_status_is_valid_for_crm():
    statuses = [
        "New",
        "Blocage",
        "RDV programmé",
        "Prochain RDV inscription",
        "Working - Contacted",
        "Unqualified",
        "Closed - Converted",
    ]
    values = {_mapped(status)["statut"] for status in statuses}

    assert values <= PRIMARY_STATUSES
    assert values == {
        "Nouveaux",
        "Blocage",
        "RDV programmé",
        "Prochain RDV inscription",
        "A relancer",
        "Disqualifié",
        "Converti",
    }
