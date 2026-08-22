from types import SimpleNamespace

from crm_pipeline_status_consistency import (
    contact_has_scheduled_relance,
    install_crm_pipeline_status_consistency,
    normalize_contact_pipeline_status,
    normalize_crm_pipeline_statuses,
)


def _contact(**overrides):
    contact = {
        "id": "contact-1",
        "statut": "A relancer",
        "statut_secondaire": "POEI",
        "relance_date": "",
        "relances": [],
    }
    contact.update(overrides)
    return contact


def test_secondary_status_without_relance_becomes_in_progress():
    contact = _contact()

    assert normalize_contact_pipeline_status(contact) is True
    assert contact["statut"] == "En cours"
    assert contact["statut_secondaire"] == "POEI"


def test_scheduled_relance_keeps_or_sets_followup_status():
    contact = _contact(
        statut="En cours",
        relances=[{
            "scheduled_date": "2026-09-04",
            "status": "scheduled",
        }],
    )

    assert contact_has_scheduled_relance(contact) is True
    assert normalize_contact_pipeline_status(contact) is True
    assert contact["statut"] == "A relancer"


def test_completed_relance_returns_secondary_case_to_in_progress():
    contact = _contact(
        relance_date="2026-09-04",
        relances=[{
            "scheduled_date": "2026-09-04",
            "status": "answered",
        }],
    )

    assert contact_has_scheduled_relance(contact) is False
    assert normalize_contact_pipeline_status(contact) is True
    assert contact["statut"] == "En cours"
    assert contact["relance_date"] == ""


def test_legacy_relance_date_is_considered_a_real_followup():
    contact = _contact(
        statut="En cours",
        relance_date="2026-09-04",
        relances=[],
    )

    assert contact_has_scheduled_relance(contact) is True
    assert normalize_contact_pipeline_status(contact) is True
    assert contact["statut"] == "A relancer"


def test_new_secondary_case_becomes_in_progress():
    contact = _contact(statut="Nouveaux", statut_secondaire="C2P en cours")

    assert normalize_contact_pipeline_status(contact) is True
    assert contact["statut"] == "En cours"


def test_business_priority_statuses_are_never_overwritten():
    for status in (
        "Blocage",
        "RDV programmé",
        "Prochain RDV inscription",
        "Disqualifié",
        "Converti",
    ):
        contact = _contact(statut=status)
        assert normalize_contact_pipeline_status(contact) is False
        assert contact["statut"] == status


def test_contact_without_secondary_timeline_is_not_changed():
    contact = _contact(statut="A relancer", statut_secondaire="")

    assert normalize_contact_pipeline_status(contact) is False
    assert contact["statut"] == "A relancer"


def test_funding_secondary_code_is_recognized_without_explicit_second_status():
    contact = _contact(
        statut="A relancer",
        statut_secondaire="",
        statut_demande_financement_ft="en_cours_instruction",
    )

    assert normalize_contact_pipeline_status(contact) is True
    assert contact["statut"] == "En cours"


def test_whole_database_normalization_returns_the_number_of_changes():
    data = {
        "crm_contacts": [
            _contact(id="one"),
            _contact(id="two", statut="En cours"),
            _contact(id="three", statut_secondaire=""),
        ]
    }

    assert normalize_crm_pipeline_statuses(data) == 1
    assert data["crm_contacts"][0]["statut"] == "En cours"


def test_load_and_save_are_both_normalized():
    saved = []
    app_module = SimpleNamespace(
        load_data=lambda: {"crm_contacts": [_contact()]},
        save_data=lambda data: saved.append(data),
    )

    install_crm_pipeline_status_consistency(app_module)

    loaded = app_module.load_data()
    assert loaded["crm_contacts"][0]["statut"] == "En cours"

    app_module.save_data({"crm_contacts": [_contact()]})
    assert saved[0]["crm_contacts"][0]["statut"] == "En cours"
