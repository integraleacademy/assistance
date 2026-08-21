import importlib.util
import uuid
from pathlib import Path

import crm_salesforce_migration as migration_template
from crm_salesforce_date_guardrails import install_salesforce_date_guardrails
from crm_salesforce_migration_guardrails import install_salesforce_migration_guardrails
from crm_salesforce_report_guardrails import install_salesforce_report_guardrails
from crm_salesforce_scope_guardrails import install_salesforce_scope_guardrails
from crm_salesforce_status_guardrails import install_salesforce_status_guardrails


MAX_BYTES = 20 * 1024 * 1024


def _fresh_migration():
    module_name = f"crm_salesforce_report_test_{uuid.uuid4().hex}"
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
    return module


def _report(*lines):
    header = (
        '"Prénom";"Nom";"Société/Compte";"Adresse e-mail";'
        '"Origine de la piste";"Propriétaire de la piste";'
        '"Type de formation";"Montant CPF";'
        '"Inscrit France Travail ?";"Identité numérique fonctionnelle ?";'
        '"Dates souhaitées ?";"Statut de la piste";"Converti";'
        '"Date de création";"Dernière modification";"ID de piste";'
        '"Téléphone";"Téléphone mobile"'
    )
    return (header + "\n" + "\n".join(lines) + "\n").encode("utf-8")


def test_exact_french_report_headers_are_mapped_to_canonical_fields():
    migration = _fresh_migration()
    raw = _report(
        '"Lina";"Martin";"APR";"lina@example.com";"Calendly";'
        '"CLEMENT VAILLANT";"A3P";"1200";"Oui";"OUI";'
        '"Septembre";"A relancer";"0";"02/01/2026";'
        '"19/08/2026";"00Qreport";"0611223344";""'
    )

    rows = migration.parse_compatible_csv(raw, max_csv_bytes=MAX_BYTES)
    row = rows[0]

    assert row["Id"] == "00Qreport"
    assert row["Status"] == "A relancer"
    assert row["Company"] == "APR"
    assert row["LeadSource"] == "Calendly"
    assert row["OwnerName"] == "CLEMENT VAILLANT"
    assert row["LastModifiedDate"] == "19/08/2026"
    assert row["Montant_CPF__c"] == "1200"
    assert row["Inscrit_France_Travail__c"] == "Oui"
    assert row["Identit_num_rique_fonctionnelle__c"] == "OUI"
    assert row["Dates_souhait_es__c"] == "Septembre"

    mapped, _ = migration._prepare_complete_rows(
        rows,
        include_converted=True,
        deduplicate=True,
    )
    contact = mapped[0]
    assert contact["salesforce_id"] == "00Qreport"
    assert contact["statut"] == "A relancer"
    assert contact["formation"] == "A3P"
    assert contact["origine"] == "Calendly"
    assert contact["commercial"] == "CLEMENT VAILLANT"
    assert contact["cpf_montant"] == "1200"
    assert contact["inscrit_ft"] == "OUI"
    assert contact["identite_ok"] == "OUI"
    assert contact["dates_formation"] == "Septembre"


def test_disqualified_bts_and_test_aps_rows_are_excluded_before_import():
    migration = _fresh_migration()
    raw = _report(
        '"Nicolas";"Mille";"APR";"nicolas@example.com";"";'
        '"CLEMENT VAILLANT";"A3P";"";"";"";"";'
        '"Disqualifié";"0";"02/01/2026";"28/05/2026";'
        '"00Qdisqualified";"0673161184";""',
        '"Lou";"Bts";"bts mos";"lou@example.com";"";'
        '"CLEMENT VAILLANT";"";"";"";"";"";'
        '"Nouveau";"0";"03/01/2026";"20/08/2026";'
        '"00Qbts";"0611111111";""',
        '"Cassandre";"MENARD";"TEST APS";"cassandre@integraleacademy.com";"";'
        '"CLEMENT VAILLANT";"";"";"";"";"";'
        '"Qualifié";"1";"13/07/2026";"13/07/2026";'
        '"00Qtestaps";"0743582264";""',
        '"Léa";"Active";"APR";"lea@example.com";"Calendly";'
        '"CLEMENT VAILLANT";"A3P";"";"Oui";"OUI";"Septembre";'
        '"A relancer";"0";"04/01/2026";"20/08/2026";'
        '"00Qactive";"0622222222";""'
    )

    rows = migration.parse_compatible_csv(raw, max_csv_bytes=MAX_BYTES)
    result = migration.import_complete_rows([], rows, dry_run=True)

    assert result["csv_rows"] == 4
    assert result["prepared_rows"] == 1
    assert result["created"] == 1
    assert result["skipped_disqualified"] == 1
    assert result["skipped_formation"] == 1
    assert result["skipped_test"] == 1
    assert result["excluded_test_labels"] == ["TEST APS"]
    assert result["status_counts"] == {"A relancer": 1}
    assert result["formation_counts"] == {"A3P": 1}


def test_a_real_aps_formation_is_not_excluded_as_test_data():
    migration = _fresh_migration()
    raw = _report(
        '"Lina";"APS";"Particulier";"aps@example.com";"";'
        '"CLEMENT VAILLANT";"APS";"";"";"";"";'
        '"Nouveau";"0";"05/01/2026";"20/08/2026";'
        '"00Qrealaps";"0633333333";""'
    )

    rows = migration.parse_compatible_csv(raw, max_csv_bytes=MAX_BYTES)
    result = migration.import_complete_rows([], rows, dry_run=True)

    assert result["created"] == 1
    assert result["skipped_test"] == 0
    assert result["formation_counts"] == {"APS": 1}


def test_abbreviated_france_travail_status_is_mapped_as_secondary_followup():
    migration = _fresh_migration()
    raw = _report(
        '"Lina";"FT";"APR";"ft@example.com";"";'
        '"CLEMENT VAILLANT";"A3P";"";"Oui";"OUI";"";'
        '"Fin. FT en cours";"0";"05/01/2026";"20/08/2026";'
        '"00Qft";"0633333333";""'
    )

    rows = migration.parse_compatible_csv(raw, max_csv_bytes=MAX_BYTES)
    mapped, _ = migration._prepare_complete_rows(
        rows,
        include_converted=True,
        deduplicate=True,
    )
    contact = mapped[0]

    assert contact["statut"] == "A relancer"
    assert contact["statut_secondaire"] == "Financement FT en cours"
    assert contact["statut_demande_financement_ft"] == "en_cours_instruction"
