import copy
import importlib.util
import uuid
from pathlib import Path

import crm_salesforce_migration as migration_template
import crm_salesforce_tasks_import as tasks
from crm_location_normalization import install_salesforce_location_guardrails
from crm_salesforce_date_guardrails import install_salesforce_date_guardrails
from crm_salesforce_migration_guardrails import install_salesforce_migration_guardrails
from crm_salesforce_old_followups_import import import_old_salesforce_followups
from crm_salesforce_report_guardrails import install_salesforce_report_guardrails
from crm_salesforce_status_guardrails import install_salesforce_status_guardrails


def _fresh_migration():
    module_name = f"crm_salesforce_old_followups_test_{uuid.uuid4().hex}"
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


def _lead(
    identifier,
    *,
    first_name="Lina",
    last_name="Martin",
    email=None,
    phone=None,
    status="A relancer",
    created="21/09/2025",
    formation="A3P",
    **extra,
):
    suffix = sum(ord(character) for character in identifier) % 10000
    return {
        "Id": identifier,
        "FirstName": first_name,
        "LastName": last_name,
        "Email": email or f"{identifier}@example.com",
        "Phone": phone or f"060000{suffix:04d}",
        "Status": status,
        "CreatedDate": created,
        "LastModifiedDate": "21/08/2026",
        "Type_de_formation__c": formation,
        **extra,
    }


def _task(
    identifier,
    *,
    name="Lina Martin",
    email="00Qold@example.com",
    phone="0600000001",
    date="02/09/2026",
    status="Open",
    **extra,
):
    return {
        "ActivityId": identifier,
        "IsTask": "1",
        "DueDate": date,
        "Status": status,
        "Subject": "Appel",
        "OwnerName": "CLEMENT VAILLANT",
        "LeadName": name,
        "Email": email,
        "Phone": phone,
        "CreatedDate": "07/05/2026",
        **extra,
    }


def test_only_old_active_leads_with_an_open_task_are_created():
    migration = _fresh_migration()
    leads = [
        _lead(
            "00Qold",
            email="old@example.com",
            phone="0611111111",
            created="21/09/2025",
        ),
        _lead(
            "00Qcurrent",
            email="current@example.com",
            phone="0622222222",
            created="21/09/2026",
        ),
        _lead(
            "00Qdisqualified",
            email="disqualified@example.com",
            phone="0633333333",
            status="Disqualifié",
        ),
        _lead(
            "00Qconverted",
            email="converted@example.com",
            phone="0644444444",
            status="Qualifié",
            IsConverted="1",
        ),
        _lead(
            "00Qbts",
            email="bts@example.com",
            phone="0655555555",
            formation="BTS MOS",
        ),
    ]
    task_rows = [
        _task(
            "00Told",
            email="old@example.com",
            phone="0611111111",
        ),
        _task(
            "00Tcurrent",
            name="Lina Martin",
            email="current@example.com",
            phone="0622222222",
        ),
        _task(
            "00Tdisqualified",
            email="disqualified@example.com",
            phone="0633333333",
        ),
        _task(
            "00Tconverted",
            email="converted@example.com",
            phone="0644444444",
        ),
        _task(
            "00Tbts",
            email="bts@example.com",
            phone="0655555555",
        ),
    ]
    contacts = []

    result = import_old_salesforce_followups(
        migration,
        tasks,
        contacts,
        leads,
        task_rows,
        dry_run=False,
    )

    assert result["old_leads_with_open_task"] == 1
    assert result["ready"] == 1
    assert result["created"] == 1
    assert result["relances_created"] == 1
    assert result["skipped_current_year_or_newer"] == 1
    assert result["skipped_disqualified"] == 1
    assert result["skipped_converted"] == 1
    assert result["skipped_formation"] == 1
    assert len(contacts) == 1
    assert contacts[0]["salesforce_id"] == "00Qold"
    assert contacts[0]["statut"] == "A relancer"
    assert contacts[0]["relance_date"] == "2026-09-02"
    assert contacts[0]["relances"][0]["salesforce_task_id"] == "00Told"


def test_c2p_is_preserved_as_a_secondary_timeline():
    migration = _fresh_migration()
    leads = [
        _lead(
            "00Qc2p",
            first_name="Kerbiche",
            last_name="Benkhaoula",
            email="kerbiche@example.com",
            phone="0625964956",
            status="C2P",
            formation="DIRIGEANT",
            CHOIX_DIRIGEANT_DESP__c="DESP VAE",
        )
    ]
    task_rows = [
        _task(
            "00Tc2p",
            name="KERBICHE BENKHAOULA",
            email="kerbiche@example.com",
            phone="0625964956",
            date="18/09/2026",
        )
    ]
    contacts = []

    result = import_old_salesforce_followups(
        migration,
        tasks,
        contacts,
        leads,
        task_rows,
        dry_run=False,
    )

    assert result["ready"] == 1
    assert contacts[0]["statut"] == "A relancer"
    assert contacts[0]["statut_secondaire"] == "C2P en cours"
    assert contacts[0]["desp_type"] == "VAE"


def test_existing_contact_is_linked_without_duplicate_and_import_is_idempotent():
    migration = _fresh_migration()
    leads = [
        _lead(
            "00Qmelvin",
            first_name="Melvin",
            last_name="Arviset",
            email="melvin@example.com",
            phone="0781128615",
            formation="CHAUFFEUR VTC",
        )
    ]
    task_rows = [
        _task(
            "00Tmelvin",
            name="melvin arviset",
            email="melvin@example.com",
            phone="0781128615",
        )
    ]
    contacts = [{
        "id": "crm-melvin",
        "prenom": "Melvin",
        "nom": "ARVISET",
        "mail": "melvin@example.com",
        "telephone": "07 81 12 86 15",
        "formation": "Chauffeur VTC",
        "statut": "Nouveaux",
        "created_at": "2025-09-21T10:00:00+02:00",
        "updated_at": "2026-08-21T10:00:00+02:00",
        "activities": [],
        "relances": [],
    }]

    first = import_old_salesforce_followups(
        migration,
        tasks,
        contacts,
        leads,
        task_rows,
        dry_run=False,
    )
    second = import_old_salesforce_followups(
        migration,
        tasks,
        contacts,
        leads,
        task_rows,
        dry_run=False,
    )

    assert first["created"] == 0
    assert first["updated"] == 1
    assert contacts[0]["salesforce_id"] == "00Qmelvin"
    assert contacts[0]["statut"] == "A relancer"
    assert len(contacts[0]["relances"]) == 1
    assert second["created"] == 0
    assert second["relances_created"] == 0
    assert len(contacts[0]["relances"]) == 1


def test_coordinate_match_with_a_different_name_is_blocked():
    migration = _fresh_migration()
    leads = [
        _lead(
            "00Qriad",
            first_name="Riad",
            last_name="Boucherit",
            email="shared@example.com",
            phone="0611757796",
        )
    ]
    task_rows = [
        _task(
            "00Triad",
            name="Riad BOUCHERIT",
            email="shared@example.com",
            phone="0611757796",
        )
    ]
    contacts = [{
        "id": "crm-mahdi",
        "prenom": "Mahdi",
        "nom": "OUB",
        "mail": "shared@example.com",
        "telephone": "0611757796",
        "formation": "A3P",
        "statut": "RDV programmé",
        "created_at": "2026-01-01T10:00:00+01:00",
        "updated_at": "2026-08-21T10:00:00+02:00",
        "activities": [],
        "relances": [],
    }]

    result = import_old_salesforce_followups(
        migration,
        tasks,
        contacts,
        leads,
        task_rows,
        dry_run=False,
    )

    assert result["ready"] == 0
    assert result["created"] == 0
    assert result["blocked_identity_mismatch"] == 1
    assert contacts[0]["salesforce_id"] if "salesforce_id" in contacts[0] else "" == ""
    assert contacts[0]["relances"] == []


def test_dry_run_does_not_modify_contacts():
    migration = _fresh_migration()
    leads = [
        _lead(
            "00Qpreview",
            email="preview@example.com",
            phone="0612345678",
        )
    ]
    task_rows = [
        _task(
            "00Tpreview",
            email="preview@example.com",
            phone="0612345678",
        )
    ]
    contacts = []
    before = copy.deepcopy(contacts)

    result = import_old_salesforce_followups(
        migration,
        tasks,
        contacts,
        leads,
        task_rows,
        dry_run=True,
    )

    assert result["ready"] == 1
    assert result["created"] == 1
    assert contacts == before
