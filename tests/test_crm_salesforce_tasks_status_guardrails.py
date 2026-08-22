import importlib.util
import uuid
from pathlib import Path

import crm_salesforce_tasks_import as tasks_template
from crm_salesforce_tasks_status_guardrails import (
    install_salesforce_tasks_status_guardrails,
)


def _fresh_tasks_module():
    module_name = f"crm_salesforce_tasks_status_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(tasks_template.__file__),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    install_salesforce_tasks_status_guardrails(module)
    return module


def _contact(**overrides):
    contact = {
        "id": "crm-poei",
        "prenom": "Maxence",
        "nom": "PIDOU",
        "mail": "maxencep7842@gmail.com",
        "telephone": "0675462457",
        "formation": "POEI",
        "statut": "En cours",
        "statut_secondaire": "POEI",
        "salesforce_id": "00QSa00000f9gel",
        "salesforce_ids": ["00QSa00000f9gel"],
        "relance_date": "",
        "relances": [],
        "activities": [],
    }
    contact.update(overrides)
    return contact


def _task(**overrides):
    task = {
        "ActivityId": "00Tpoei",
        "IsTask": "1",
        "DueDate": "04/09/2026",
        "DueDateTime": "04/09/2026 00:00",
        "StartDateTime": "04/09/2026",
        "CompletedAt": "",
        "Status": "Open",
        "Subject": "Appel",
        "OwnerName": "CLEMENT VAILLANT",
        "Priority": "Normal",
        "Comments": "",
        "LeadName": "Maxence PIDOU",
        "ContactName": "",
        "OpportunityName": "",
        "Company": "POEI",
        "Email": "maxencep7842@gmail.com",
        "Phone": "0675462457",
        "MobilePhone": "0675462457",
        "CreatedDate": "21/08/2026",
    }
    task.update(overrides)
    return task


def test_in_progress_secondary_case_is_promoted_only_when_task_is_created():
    tasks = _fresh_tasks_module()
    contacts = [_contact()]

    result = tasks.import_salesforce_task_rows(contacts, [_task()])

    assert result["created"] == 1
    assert result["promoted_to_followup"] == 1
    assert contacts[0]["statut"] == "A relancer"
    assert contacts[0]["statut_secondaire"] == "POEI"
    assert contacts[0]["relance_date"] == "2026-09-04"


def test_secondary_case_without_scheduled_task_returns_to_in_progress():
    tasks = _fresh_tasks_module()
    contacts = [_contact(
        statut="A relancer",
        relances=[{
            "id": "done",
            "scheduled_date": "2026-09-04",
            "status": "answered",
        }],
    )]

    result = tasks.import_salesforce_task_rows(
        contacts,
        [_task(
            ActivityId="00Tother",
            Email="other@example.com",
            Phone="0600000000",
            MobilePhone="",
            LeadName="Other Person",
        )],
    )

    assert result["unmatched"] == 1
    assert result["normalized_to_in_progress"] == 1
    assert contacts[0]["statut"] == "En cours"


def test_dry_run_applies_the_same_status_rules_without_mutation():
    tasks = _fresh_tasks_module()
    contacts = [_contact()]

    result = tasks.import_salesforce_task_rows(
        contacts,
        [_task()],
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["promoted_to_followup"] == 1
    assert contacts[0]["statut"] == "En cours"
    assert contacts[0]["relances"] == []
