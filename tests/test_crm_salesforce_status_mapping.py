import crm_salesforce_migration as migration
from crm_salesforce_migration_guardrails import install_salesforce_migration_guardrails
from crm_salesforce_status_guardrails import (
    PRIMARY_STATUSES,
    install_salesforce_status_guardrails,
)


install_salesforce_migration_guardrails(migration)
install_salesforce_status_guardrails(migration)


def _mapped(status, **extra):
    rows = [{
        "Id": f"00Q-{status}",
        "FirstName": "Lina",
        "LastName": "Martin",
        "Email": f"{status.replace(' ', '-')}@example.com",
        "Status": status,
        "CreatedDate": "2026-08-20T10:00:00Z",
        **extra,
    }]
    mapped, _ = migration._prepare_complete_rows(
        rows,
        include_converted=True,
        deduplicate=True,
    )
    return mapped[0]


def test_secondary_salesforce_statuses_are_in_progress_without_a_task():
    expected = {
        "POEI": "POEI",
        "Session FT": "Marché FT",
        "Marché FT": "Marché FT",
        "Def MOB": "Def MOB",
        "Financement FT en cours": "Financement FT en cours",
        "Financement FT refusé": "Financement FT refusé",
        "C2P": "C2P en cours",
        "C2P en cours": "C2P en cours",
    }
    for source, secondary in expected.items():
        contact = _mapped(source)
        assert contact["statut"] == "En cours"
        assert contact["statut_secondaire"] == secondary
        assert contact["statut_secondaire_source"] == "salesforce_migration"


def test_funding_secondary_statuses_also_set_funding_code():
    in_progress = _mapped("Financement FT en cours")
    refused = _mapped("Financement FT refusé")

    assert in_progress["statut"] == "En cours"
    assert in_progress["statut_demande_financement_ft"] == "en_cours_instruction"
    assert refused["statut"] == "En cours"
    assert refused["statut_demande_financement_ft"] == "refusee"


def test_french_funding_field_is_normalized_even_without_secondary_status():
    contact = _mapped(
        "New",
        Statut_financement_FT__c="En cours d'instruction",
    )

    assert contact["statut"] == "Nouveaux"
    assert contact["statut_demande_financement_ft"] == "en_cours_instruction"
    assert contact["statut_demande_financement_ft_source"] == (
        "salesforce_migration"
    )


def test_all_funding_states_use_the_same_codes_as_the_crm():
    expected = {
        "Demande acceptée": "acceptee",
        "Dossier refusé": "refusee",
        "Instruction en cours": "en_cours_instruction",
        "Demande transmise": "transmise",
        "À préparer": "a_preparer",
        "Aucune demande": "aucune_demande",
        "Dossier annulé par le candidat": "annulee",
    }
    for source, code in expected.items():
        contact = _mapped("New", Statut_financement_FT__c=source)
        assert contact["statut_demande_financement_ft"] == code
        assert contact["statut_demande_financement_ft_source"] == (
            "salesforce_migration"
        )


def test_converted_flag_takes_priority_over_an_old_secondary_status():
    contact = _mapped("POEI", IsConverted="1")

    assert contact["statut"] == "Converti"
    assert not contact.get("statut_secondaire")
    assert contact["salesforce_is_converted"] is True


def test_every_primary_status_is_valid_for_crm():
    statuses = [
        "New",
        "Blocage",
        "RDV programmé",
        "En cours",
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
        "En cours",
        "Prochain RDV inscription",
        "A relancer",
        "Disqualifié",
        "Converti",
    }
