import copy
import importlib.util
import uuid
from pathlib import Path

import crm_salesforce_migration as migration_template
from crm_location_normalization import install_salesforce_location_guardrails
from crm_salesforce_date_guardrails import install_salesforce_date_guardrails
from crm_salesforce_existing_repair import repair_existing_salesforce_rows
from crm_salesforce_migration_guardrails import install_salesforce_migration_guardrails
from crm_salesforce_report_guardrails import install_salesforce_report_guardrails
from crm_salesforce_status_guardrails import install_salesforce_status_guardrails


def _fresh_migration():
    module_name = f"crm_salesforce_existing_repair_test_{uuid.uuid4().hex}"
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


def _salesforce_eric():
    return [{
        "Id": "00QSa00000hU7j1",
        "FirstName": "eric",
        "LastName": "MAHONEY",
        "Email": "mahoney43@hotmail.com",
        "Phone": "+33665535369",
        "Status": "C2P",
        "Type_de_formation__c": "DIRIGEANT",
        "Lieu__c": "Paris",
        "Dates_souhait_es__c": "26 octobre au 10 decembre",
        "CHOIX_DIRIGEANT_DESP__c": "DESP INITIAL",
        "LeadSource": "Google",
        "CreatedDate": "2025-11-10T10:00:00Z",
        "LastModifiedDate": "2026-08-21T10:00:00Z",
    }]


def _crm_eric(**overrides):
    contact = {
        "id": "23209479-f649-4d3d-8ddd-d9bfef97e513",
        "prenom": "Eric",
        "nom": "MAHONEY",
        "mail": "mahoney43@hotmail.com",
        "telephone": "0665535369",
        "formation": "DESP",
        "desp_type": "INITIAL",
        "lieu": "Auvergne",
        "dates_formation": "Du 26 octobre au 10 décembre 2026",
        "statut": "A relancer",
        "statut_secondaire": "",
        "origine": "Mon Compte Formation",
        "relances": [{"id": "followup", "status": "scheduled", "scheduled_date": "2026-09-02"}],
        "activities": [],
        "created_at": "2025-11-10T10:00:00+01:00",
        "updated_at": "2026-08-21T12:00:00+02:00",
    }
    contact.update(overrides)
    return contact


def test_historical_c2p_lead_corrects_existing_contact_without_creation():
    migration = _fresh_migration()
    contacts = [_crm_eric()]

    result = repair_existing_salesforce_rows(
        migration,
        contacts,
        _salesforce_eric(),
    )

    assert result["created"] == 0
    assert result["matched"] == 1
    assert result["updated"] == 1
    assert contacts[0]["lieu"] == "Paris"
    assert contacts[0]["statut"] == "A relancer"
    assert contacts[0]["statut_secondaire"] == "C2P en cours"
    assert contacts[0]["dates_formation"] == "26 octobre au 10 decembre"
    assert contacts[0]["origine"] == "Google Ads"
    assert contacts[0]["salesforce_id"] == "00QSa00000hU7j1"
    assert contacts[0]["relances"][0]["scheduled_date"] == "2026-09-02"


def test_unmatched_salesforce_lead_is_never_created():
    migration = _fresh_migration()
    contacts = []

    result = repair_existing_salesforce_rows(
        migration,
        contacts,
        _salesforce_eric(),
    )

    assert result["created"] == 0
    assert result["matched"] == 0
    assert result["not_found"] == 1
    assert contacts == []


def test_disqualified_crm_contact_is_not_reactivated():
    migration = _fresh_migration()
    contacts = [_crm_eric(statut="Disqualifié")]

    result = repair_existing_salesforce_rows(
        migration,
        contacts,
        _salesforce_eric(),
    )

    assert result["preserved_disqualified"] == 1
    assert result["updated"] == 0
    assert contacts[0]["statut"] == "Disqualifié"
    assert contacts[0]["lieu"] == "Auvergne"


def test_converted_status_is_preserved_while_other_fields_are_corrected():
    migration = _fresh_migration()
    contacts = [_crm_eric(statut="Converti")]

    result = repair_existing_salesforce_rows(
        migration,
        contacts,
        _salesforce_eric(),
    )

    assert result["preserved_converted_status"] == 1
    assert contacts[0]["statut"] == "Converti"
    assert contacts[0]["statut_secondaire"] == ""
    assert contacts[0]["lieu"] == "Paris"


def test_preview_does_not_mutate_contacts():
    migration = _fresh_migration()
    contacts = [_crm_eric()]
    before = copy.deepcopy(contacts)

    result = repair_existing_salesforce_rows(
        migration,
        contacts,
        _salesforce_eric(),
        dry_run=True,
    )

    assert result["updated"] == 1
    assert contacts == before
