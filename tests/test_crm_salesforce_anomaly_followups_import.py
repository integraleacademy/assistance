import copy

from crm_salesforce_anomaly_followups_import import (
    import_anomaly_followup_rows,
    parse_anomaly_followups_csv,
)


def _csv(*rows):
    header = (
        '"Catégorie";"Action recommandée";"Importable après vérification";'
        '"Personne";"E-mail";"Téléphone";"Date de relance";"Objet";'
        '"Attribué à";"Priorité";"Statut Salesforce";"Commentaires";'
        '"ID activité Salesforce";"Type de relation";"Société / Compte";'
        '"Motif";"Méthode de rapprochement";"ID fiche CRM";'
        '"Nom fiche CRM";"Statut fiche CRM";"Formation fiche CRM"'
    )
    return (header + "\n" + "\n".join(rows) + "\n").encode("utf-8")


def _row(
    *,
    person="Lina MARTIN",
    email="lina@example.com",
    phone="0611223344",
    date="2026-09-07",
    task_id="00Ttask",
    contact_id="crm-1",
    crm_name="Lina MARTIN",
    crm_status="Nouveaux",
    formation="A3P",
):
    values = [
        "Fiche CRM non reliée à Salesforce",
        "Créer directement la relance",
        "Oui",
        person,
        email,
        phone,
        date,
        "Appel",
        "CLEMENT VAILLANT",
        "Normal",
        "Open",
        "Rappeler le candidat",
        task_id,
        "lead",
        "Particulier",
        "Fiche non reliée",
        "email+phone",
        contact_id,
        crm_name,
        crm_status,
        formation,
    ]
    return ";".join(
        f'"{str(value).replace(chr(34), chr(34) * 2)}"'
        for value in values
    )


def _contact(**overrides):
    contact = {
        "id": "crm-1",
        "prenom": "Lina",
        "nom": "MARTIN",
        "mail": "lina@example.com",
        "telephone": "0611223344",
        "formation": "A3P",
        "statut": "Nouveaux",
        "relances": [],
        "activities": [],
        "updated_at": "2026-08-21T12:00:00+02:00",
    }
    contact.update(overrides)
    return contact


def test_parser_maps_the_exact_anomaly_export_headers():
    rows = parse_anomaly_followups_csv(_csv(_row()))
    assert rows[0]["contact_id"] == "crm-1"
    assert rows[0]["crm_formation"] == "A3P"
    assert rows[0]["salesforce_task_id"] == "00Ttask"
    assert rows[0]["scheduled_date"] == "2026-09-07"


def test_only_rows_with_a_crm_formation_are_selected():
    contacts = [_contact()]
    rows = parse_anomaly_followups_csv(_csv(
        _row(),
        _row(
            person="Sans FORMATION",
            email="empty@example.com",
            phone="0622222222",
            task_id="00Tempty",
            contact_id="crm-empty",
            crm_name="Sans FORMATION",
            formation="",
        ),
    ))
    result = import_anomaly_followup_rows(contacts, rows, dry_run=True)

    assert result["csv_rows"] == 2
    assert result["selected_with_formation"] == 1
    assert result["skipped_without_formation"] == 1
    assert result["ready"] == 1


def test_existing_contact_is_set_to_followup_and_relance_is_created():
    contacts = [_contact()]
    rows = parse_anomaly_followups_csv(_csv(_row()))

    result = import_anomaly_followup_rows(contacts, rows)

    assert result["ready"] == 1
    assert result["relances_created"] == 1
    assert result["statuses_changed"] == 1
    assert contacts[0]["statut"] == "A relancer"
    assert contacts[0]["relance_date"] == "2026-09-07"
    assert contacts[0]["relances"][0]["salesforce_task_id"] == "00Ttask"
    assert contacts[0]["activities"][0]["title"] == (
        "Anomalie de relance Salesforce régularisée"
    )


def test_disqualified_contact_is_explicitly_reactivated():
    contacts = [_contact(
        statut="Disqualifié",
        disqualification_reason="Pas de projet",
        archived_at="2026-06-01",
    )]
    rows = parse_anomaly_followups_csv(_csv(_row(crm_status="Disqualifié")))

    result = import_anomaly_followup_rows(contacts, rows)

    assert result["reactivated_disqualified"] == 1
    assert contacts[0]["statut"] == "A relancer"
    assert contacts[0]["disqualification_reason"] == ""
    assert contacts[0]["archived_at"] == ""
    assert contacts[0]["reactivation_date"]


def test_obvious_identity_mismatch_is_blocked():
    contacts = [_contact(
        prenom="Mahdi",
        nom="OUB",
        mail="brboxe@gmail.com",
        telephone="0611757796",
    )]
    rows = parse_anomaly_followups_csv(_csv(_row(
        person="Riad BOUCHERIT",
        email="brboxe@gmail.com",
        phone="0611757796",
        crm_name="Mahdi OUB",
    )))

    result = import_anomaly_followup_rows(contacts, rows)

    assert result["ready"] == 0
    assert result["blocked"] == 1
    assert result["identity_mismatch"] == 1
    assert contacts[0]["relances"] == []
    assert contacts[0]["statut"] == "Nouveaux"


def test_reversed_names_and_small_spelling_difference_are_accepted():
    contacts = [
        _contact(
            id="reverse",
            prenom="Benkhaoula",
            nom="KERBICHE",
            mail="reverse@example.com",
            telephone="0611111111",
        ),
        _contact(
            id="spelling",
            prenom="Jam",
            nom="AMAR",
            mail="jamel@example.com",
            telephone="0622222222",
        ),
    ]
    rows = parse_anomaly_followups_csv(_csv(
        _row(
            person="KERBICHE BENKHAOULA",
            email="reverse@example.com",
            phone="0611111111",
            task_id="00Treversed",
            contact_id="reverse",
            crm_name="Benkhaoula KERBICHE",
        ),
        _row(
            person="jamel ammar",
            email="jamel@example.com",
            phone="0622222222",
            task_id="00Tspelling",
            contact_id="spelling",
            crm_name="Jam AMAR",
        ),
    ))

    result = import_anomaly_followup_rows(contacts, rows)
    assert result["ready"] == 2
    assert result["blocked"] == 0


def test_import_is_idempotent_and_updates_the_due_date():
    contacts = [_contact()]
    first = parse_anomaly_followups_csv(_csv(_row()))
    changed = parse_anomaly_followups_csv(_csv(_row(date="2026-09-08")))

    initial = import_anomaly_followup_rows(contacts, first)
    identical = import_anomaly_followup_rows(contacts, first)
    updated = import_anomaly_followup_rows(contacts, changed)

    assert initial["relances_created"] == 1
    assert identical["relances_unchanged"] == 1
    assert len(contacts[0]["relances"]) == 1
    assert updated["relances_updated"] == 1
    assert contacts[0]["relances"][0]["scheduled_date"] == "2026-09-08"
    assert contacts[0]["relance_date"] == "2026-09-08"


def test_dry_run_does_not_mutate_contacts():
    contacts = [_contact()]
    before = copy.deepcopy(contacts)
    rows = parse_anomaly_followups_csv(_csv(_row()))

    result = import_anomaly_followup_rows(contacts, rows, dry_run=True)

    assert result["ready"] == 1
    assert contacts == before
