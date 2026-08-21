import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace

import crm_salesforce_migration as migration_template
from crm_location_normalization import (
    canonical_crm_location,
    canonicalize_crm_locations,
    install_crm_location_normalization,
    install_salesforce_location_guardrails,
)
from crm_salesforce_date_guardrails import install_salesforce_date_guardrails
from crm_salesforce_migration_guardrails import install_salesforce_migration_guardrails
from crm_salesforce_report_guardrails import install_salesforce_report_guardrails
from crm_salesforce_status_guardrails import install_salesforce_status_guardrails


MAX_BYTES = 20 * 1024 * 1024


def _fresh_migration():
    module_name = f"crm_salesforce_location_test_{uuid.uuid4().hex}"
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
    install_salesforce_location_guardrails(module)
    return module


def _salesforce_report():
    return (
        '"Prénom";"Nom";"Société/Compte";"Adresse e-mail";'
        '"Type de formation";"Lieu";"Dates souhaitées ?";'
        '"Statut de la piste";"Converti";"Date de création";'
        '"Dernière modification";"ID de piste";"Téléphone"\n'
        '"baptiste";"barniers";"APR";"baptistebarniers@icloud.com";'
        '"A3P";"Côte d\'Azur";"session de septembre 2027";'
        '"A relancer";"0";"19/08/2026";"19/08/2026";'
        '"00QSa00000hpwvd";"06 31 68 39 86"\n'
    ).encode("utf-8")


def test_location_aliases_use_the_exact_crm_labels():
    assert canonical_crm_location("Côte d'Azur") == "Côte d’Azur"
    assert canonical_crm_location("COTE D AZUR") == "Côte d’Azur"
    assert canonical_crm_location("Puget-sur-Argens") == "Côte d’Azur"
    assert canonical_crm_location("Aurillac") == "Auvergne"
    assert canonical_crm_location("Paris") == "Paris"


def test_existing_crm_locations_are_normalized_on_load_and_save():
    stored = {
        "crm_contacts": [
            {"id": "one", "lieu": "Côte d'Azur"},
            {"id": "two", "lieu": "Aurillac"},
        ]
    }
    saved = []
    app_module = SimpleNamespace(
        load_data=lambda: {
            "crm_contacts": [dict(contact) for contact in stored["crm_contacts"]]
        },
        save_data=lambda data: saved.append(data),
    )

    install_crm_location_normalization(app_module)
    loaded = app_module.load_data()
    assert loaded["crm_contacts"][0]["lieu"] == "Côte d’Azur"
    assert loaded["crm_contacts"][1]["lieu"] == "Auvergne"

    app_module.save_data({"crm_contacts": [{"lieu": "Côte d'Azur"}]})
    assert saved[0]["crm_contacts"][0]["lieu"] == "Côte d’Azur"


def test_salesforce_location_and_session_are_mapped_for_audit():
    migration = _fresh_migration()
    rows = migration.parse_compatible_csv(
        _salesforce_report(),
        max_csv_bytes=MAX_BYTES,
    )
    mapped, _ = migration._prepare_complete_rows(
        rows,
        include_converted=True,
        deduplicate=True,
    )

    contact = mapped[0]
    assert contact["lieu"] == "Côte d’Azur"
    assert contact["salesforce_lieu"] == "Côte d’Azur"
    assert contact["salesforce_lieu_raw"] == "Côte d'Azur"
    assert contact["dates_formation"] == "session de septembre 2027"
    assert contact["salesforce_dates_formation"] == "session de septembre 2027"


def test_salesforce_priority_replaces_wrong_location_and_meta_session():
    migration = _fresh_migration()
    rows = migration.parse_compatible_csv(
        _salesforce_report(),
        max_csv_bytes=MAX_BYTES,
    )
    contacts = [{
        "id": "crm-baptiste",
        "prenom": "Baptiste",
        "nom": "BARNIERS",
        "mail": "baptistebarniers@icloud.com",
        "telephone": "06 31 68 39 86",
        "formation": "A3P",
        "lieu": "Auvergne",
        "dates_formation": "session de septembre 2027 — réponse META",
        "statut": "A relancer",
        "salesforce_id": "00QSa00000hpwvd",
        "salesforce_ids": ["00QSa00000hpwvd"],
        "created_at": "2026-08-19T10:00:00+02:00",
        "updated_at": "2026-08-21T10:00:00+02:00",
        "activities": [],
        "relances": [],
    }]

    result = migration.import_complete_rows(
        contacts,
        rows,
        dry_run=False,
        merge_policy=migration.MERGE_POLICY_SALESFORCE,
    )

    assert result["updated"] == 1
    assert contacts[0]["lieu"] == "Côte d’Azur"
    assert contacts[0]["dates_formation"] == "session de septembre 2027"
    assert contacts[0]["salesforce_lieu_raw"] == "Côte d'Azur"


def test_safe_mode_keeps_crm_location_but_records_salesforce_value():
    migration = _fresh_migration()
    rows = migration.parse_compatible_csv(
        _salesforce_report(),
        max_csv_bytes=MAX_BYTES,
    )
    contacts = [{
        "id": "crm-baptiste",
        "prenom": "Baptiste",
        "nom": "BARNIERS",
        "mail": "baptistebarniers@icloud.com",
        "telephone": "06 31 68 39 86",
        "formation": "A3P",
        "lieu": "Auvergne",
        "dates_formation": "ancienne réponse CRM",
        "statut": "A relancer",
        "salesforce_id": "00QSa00000hpwvd",
        "salesforce_ids": ["00QSa00000hpwvd"],
        "created_at": "2026-08-19T10:00:00+02:00",
        "updated_at": "2026-08-21T10:00:00+02:00",
        "activities": [],
        "relances": [],
    }]

    migration.import_complete_rows(
        contacts,
        rows,
        dry_run=False,
        merge_policy=migration.MERGE_POLICY_SAFE,
    )

    assert contacts[0]["lieu"] == "Auvergne"
    assert contacts[0]["dates_formation"] == "ancienne réponse CRM"
    assert contacts[0]["salesforce_lieu"] == "Côte d’Azur"
    assert contacts[0]["salesforce_dates_formation"] == "session de septembre 2027"


def test_data_normalizer_returns_the_number_of_changed_values():
    data = {
        "crm_contacts": [
            {"lieu": "Côte d'Azur", "salesforce_lieu": "Côte d'Azur"},
            {"lieu": "Paris"},
        ]
    }
    assert canonicalize_crm_locations(data) == 2
    assert data["crm_contacts"][0]["lieu"] == "Côte d’Azur"
    assert data["crm_contacts"][0]["salesforce_lieu"] == "Côte d’Azur"
