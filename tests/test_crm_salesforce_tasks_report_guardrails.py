import importlib.util
import uuid
from pathlib import Path

import crm_salesforce_tasks_import as tasks_template
from crm_salesforce_tasks_report_guardrails import (
    CATEGORY_AMBIGUOUS,
    CATEGORY_EXCLUDED,
    CATEGORY_MISSING_CONTACT,
    CATEGORY_NAME_WARNING,
    CATEGORY_NOT_LINKED,
    install_salesforce_tasks_report_guardrails,
)


def _fresh_module():
    module_name = f"crm_salesforce_tasks_report_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(tasks_template.__file__),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    install_salesforce_tasks_report_guardrails(module)
    return module


def _contact(identifier, *, email, phone, **extra):
    contact = {
        "id": identifier,
        "prenom": "Lina",
        "nom": "Martin",
        "mail": email,
        "telephone": phone,
        "formation": "A3P",
        "statut": "A relancer",
        "salesforce_id": f"00Q-{identifier}",
        "salesforce_ids": [f"00Q-{identifier}"],
        "relances": [],
        "activities": [],
        "updated_at": "2026-08-21T10:00:00+02:00",
    }
    contact.update(extra)
    return contact


def _task(identifier, *, name, email, phone, date="2026-09-08"):
    return {
        "ActivityId": identifier,
        "IsTask": "1",
        "DueDate": date,
        "Subject": "Appel",
        "OwnerName": "CLEMENT VAILLANT",
        "Priority": "Normal",
        "Status": "Open",
        "LeadName": name,
        "Email": email,
        "Phone": phone,
        "Comments": "Rappeler le candidat",
        "CreatedDate": "2026-08-20",
    }


def test_full_report_separates_every_manual_review_category():
    tasks = _fresh_module()
    contacts = [
        _contact(
            "unlinked",
            email="unlinked@example.com",
            phone="0611111111",
            salesforce_id="",
            salesforce_ids=[],
        ),
        _contact(
            "ambiguous-one",
            email="ambiguous@example.com",
            phone="0622222221",
        ),
        _contact(
            "ambiguous-two",
            email="ambiguous@example.com",
            phone="0622222222",
        ),
        _contact(
            "excluded",
            email="excluded@example.com",
            phone="0633333333",
            statut="Disqualifié",
        ),
        _contact(
            "warning",
            email="warning@example.com",
            phone="0644444444",
        ),
        _contact(
            "good",
            email="good@example.com",
            phone="0655555555",
        ),
    ]
    rows = [
        _task(
            "00Tmissing",
            name="Personne Absente",
            email="missing@example.com",
            phone="0600000000",
        ),
        _task(
            "00Tunlinked",
            name="Lina Martin",
            email="unlinked@example.com",
            phone="0611111111",
        ),
        _task(
            "00Tambiguous",
            name="Lina Martin",
            email="ambiguous@example.com",
            phone="",
        ),
        _task(
            "00Texcluded",
            name="Lina Martin",
            email="excluded@example.com",
            phone="0633333333",
        ),
        _task(
            "00Twarning",
            name="Nom Salesforce Différent",
            email="warning@example.com",
            phone="0644444444",
        ),
        _task(
            "00Tgood",
            name="Lina Martin",
            email="good@example.com",
            phone="0655555555",
        ),
    ]

    result = tasks.import_salesforce_task_rows(
        contacts,
        rows,
        dry_run=True,
    )

    assert result["manual_review_total"] == 5
    assert result["manual_review_counts"] == {
        CATEGORY_MISSING_CONTACT: 1,
        CATEGORY_NOT_LINKED: 1,
        CATEGORY_AMBIGUOUS: 1,
        CATEGORY_EXCLUDED: 1,
        CATEGORY_NAME_WARNING: 1,
    }
    assert len(result["missing_contact_rows"]) == 1
    assert len(result["not_salesforce_linked_rows"]) == 1
    assert len(result["ambiguous_full"]) == 1
    assert len(result["excluded_full"]) == 1
    assert len(result["name_warnings_full"]) == 1
    assert result["created"] == 2


def test_full_report_is_not_limited_to_thirty_examples():
    tasks = _fresh_module()
    rows = [
        _task(
            f"00Tmissing-{index}",
            name=f"Personne Absente {index}",
            email=f"missing-{index}@example.com",
            phone=f"06{index:08d}",
        )
        for index in range(40)
    ]

    result = tasks.import_salesforce_task_rows(
        [],
        rows,
        dry_run=True,
    )

    assert result["unmatched"] == 40
    assert result["manual_review_total"] == 40
    assert len(result["manual_review_rows"]) == 40
    assert len(result["missing_contact_rows"]) == 40
    assert result["manual_review_counts"] == {
        CATEGORY_MISSING_CONTACT: 40,
    }
