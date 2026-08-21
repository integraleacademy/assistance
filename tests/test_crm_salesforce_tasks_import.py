import copy

from crm_salesforce_tasks_import import (
    import_salesforce_task_rows,
    parse_salesforce_tasks_csv,
)


def _csv(*rows):
    header = (
        '"Date";"Société/Compte";"Opportunité";"Contact";"Piste";'
        '"Objet";"Attribué";"Priorité";"Statut";"Tâche";"Début";'
        '"Échéance (heures)";"Date/heure de réalisation";"Fin";'
        '"Type d\'activité";"Type d\'appel";"ID du compte";'
        '"ID du compte principal";"Commentaires";"Téléphone";'
        '"Téléphone mobile";"Adresse e-mail";"ID de l\'activité";'
        '"Date de création";"Origine de la piste de l\'opportunité"'
    )
    return (header + "\n" + "\n".join(rows) + "\n").encode("utf-8")


def _task(
    *,
    name="Lina Martin",
    email="lina@example.com",
    phone="0611223344",
    due="21/08/2026",
    activity_id="00Ttask",
    status="Open",
    is_task="1",
    completed_at="",
    subject="Appel",
):
    values = [
        due,
        "APR",
        "",
        "",
        name,
        subject,
        "CLEMENT VAILLANT",
        "Normal",
        status,
        is_task,
        due,
        f"{due} 00:00",
        completed_at,
        due,
        "",
        "",
        "",
        "",
        "Rappeler le candidat",
        phone,
        "",
        email,
        activity_id,
        "20/05/2026",
        "",
    ]
    escaped = [f'"{str(value).replace(chr(34), chr(34) * 2)}"' for value in values]
    return ";".join(escaped)


def _contact(**overrides):
    contact = {
        "id": "crm-1",
        "prenom": "Lina",
        "nom": "Martin",
        "mail": "lina@example.com",
        "telephone": "0611223344",
        "formation": "A3P",
        "statut": "Nouveaux",
        "salesforce_id": "00Qlead",
        "salesforce_ids": ["00Qlead"],
        "relances": [],
        "activities": [],
        "updated_at": "2026-08-20T12:00:00+02:00",
    }
    contact.update(overrides)
    return contact


def test_parser_maps_the_exact_french_task_report_headers():
    rows = parse_salesforce_tasks_csv(_csv(_task()))

    assert rows == [{
        "DueDate": "21/08/2026",
        "Company": "APR",
        "OpportunityName": "",
        "ContactName": "",
        "LeadName": "Lina Martin",
        "Subject": "Appel",
        "OwnerName": "CLEMENT VAILLANT",
        "Priority": "Normal",
        "Status": "Open",
        "IsTask": "1",
        "StartDateTime": "21/08/2026",
        "DueDateTime": "21/08/2026 00:00",
        "CompletedAt": "",
        "EndDateTime": "21/08/2026",
        "ActivityType": "",
        "CallType": "",
        "AccountId": "",
        "ParentAccountId": "",
        "Comments": "Rappeler le candidat",
        "Phone": "0611223344",
        "MobilePhone": "",
        "Email": "lina@example.com",
        "ActivityId": "00Ttask",
        "CreatedDate": "20/05/2026",
        "OpportunityLeadSource": "",
    }]


def test_open_task_creates_a_relance_and_promotes_a_new_lead():
    contacts = [_contact()]
    rows = parse_salesforce_tasks_csv(_csv(_task()))

    result = import_salesforce_task_rows(contacts, rows)

    assert result["created"] == 1
    assert result["matched_contacts"] == 1
    assert result["promoted_to_followup"] == 1
    assert result["match_method_counts"] == {"email+phone": 1}
    contact = contacts[0]
    assert contact["statut"] == "A relancer"
    assert contact["relance_date"] == "2026-08-21"
    assert contact["relances"][0]["scheduled_date"] == "2026-08-21"
    assert contact["relances"][0]["salesforce_task_id"] == "00Ttask"
    assert contact["relances"][0]["salesforce_subject"] == "Appel"
    assert contact["relances"][0]["salesforce_owner"] == "CLEMENT VAILLANT"


def test_event_and_closed_task_are_ignored():
    contacts = [_contact()]
    rows = parse_salesforce_tasks_csv(_csv(
        _task(activity_id="00Uevent", is_task="0"),
        _task(activity_id="00Tclosed", status="Completed", completed_at="21/08/2026 10:00"),
    ))

    result = import_salesforce_task_rows(contacts, rows)

    assert result["task_rows"] == 1
    assert result["skipped_events"] == 1
    assert result["skipped_closed"] == 1
    assert result["created"] == 0


def test_import_is_idempotent_and_updates_a_changed_due_date():
    contacts = [_contact()]
    first_rows = parse_salesforce_tasks_csv(_csv(_task()))
    second_rows = parse_salesforce_tasks_csv(_csv(_task(due="22/08/2026")))

    first = import_salesforce_task_rows(contacts, first_rows)
    identical = import_salesforce_task_rows(contacts, first_rows)
    changed = import_salesforce_task_rows(contacts, second_rows)

    assert first["created"] == 1
    assert identical["created"] == 0
    assert identical["updated"] == 0
    assert identical["unchanged"] == 1
    assert len(contacts[0]["relances"]) == 1
    assert len(contacts[0]["activities"]) == 2
    assert changed["updated"] == 1
    assert contacts[0]["relances"][0]["scheduled_date"] == "2026-08-22"
    assert contacts[0]["relance_date"] == "2026-08-22"


def test_completed_crm_relance_is_never_reopened_by_reimport():
    contacts = [_contact()]
    rows = parse_salesforce_tasks_csv(_csv(_task()))
    import_salesforce_task_rows(contacts, rows)
    contacts[0]["relances"][0]["status"] = "answered"
    changed_rows = parse_salesforce_tasks_csv(_csv(_task(due="23/08/2026")))

    result = import_salesforce_task_rows(contacts, changed_rows)

    assert result["preserved_completed"] == 1
    assert result["updated"] == 0
    assert contacts[0]["relances"][0]["status"] == "answered"
    assert contacts[0]["relances"][0]["scheduled_date"] == "2026-08-21"


def test_two_open_tasks_for_the_same_contact_are_both_preserved():
    contacts = [_contact()]
    rows = parse_salesforce_tasks_csv(_csv(
        _task(activity_id="00Tone", due="21/09/2026"),
        _task(activity_id="00Ttwo", due="07/12/2026"),
    ))

    result = import_salesforce_task_rows(contacts, rows)

    assert result["created"] == 2
    assert result["matched_contacts"] == 1
    assert len(contacts[0]["relances"]) == 2
    assert contacts[0]["relance_date"] == "2026-09-21"


def test_email_and_phone_pointing_to_different_contacts_are_blocked():
    contacts = [
        _contact(id="email-contact", telephone="0600000000"),
        _contact(id="phone-contact", mail="other@example.com"),
    ]
    rows = parse_salesforce_tasks_csv(_csv(_task()))

    result = import_salesforce_task_rows(contacts, rows)

    assert result["ambiguous"] == 1
    assert result["created"] == 0
    assert "deux fiches CRM différentes" in result["ambiguous_samples"][0]["reason"]


def test_contact_must_be_linked_to_salesforce_before_task_import():
    contacts = [_contact(salesforce_id="", salesforce_ids=[])]
    rows = parse_salesforce_tasks_csv(_csv(_task()))

    result = import_salesforce_task_rows(contacts, rows)

    assert result["skipped_not_salesforce_linked"] == 1
    assert result["created"] == 0
    assert "Importez d’abord le fichier des pistes" in result["unmatched_samples"][0]["reason"]


def test_disqualified_bts_and_test_aps_contacts_are_never_updated():
    contacts = [
        _contact(id="disq", mail="disq@example.com", telephone="0600000001", statut="Disqualifié"),
        _contact(id="bts", mail="bts@example.com", telephone="0600000002", formation="BTS MOS"),
        _contact(id="test", mail="test@example.com", telephone="0600000003", salesforce_company="TEST APS"),
    ]
    rows = parse_salesforce_tasks_csv(_csv(
        _task(name="Dis Q", email="disq@example.com", phone="0600000001", activity_id="00Tdisq"),
        _task(name="Bts Test", email="bts@example.com", phone="0600000002", activity_id="00Tbts"),
        _task(name="Test Aps", email="test@example.com", phone="0600000003", activity_id="00Ttest"),
    ))

    result = import_salesforce_task_rows(contacts, rows)

    assert result["skipped_excluded_contact"] == 3
    assert result["created"] == 0


def test_dry_run_never_mutates_contacts():
    contacts = [_contact()]
    before = copy.deepcopy(contacts)
    rows = parse_salesforce_tasks_csv(_csv(_task()))

    result = import_salesforce_task_rows(contacts, rows, dry_run=True)

    assert result["created"] == 1
    assert contacts == before
