import copy
import json
import os
import subprocess
import sys
import threading

import pytest

import app as application
from crm_secretariat_answers import REPAIR_KEY, VERSION
from secretariat_followup_patch import _sync_quote_contact_training


def submission(**overrides):
    return {
        "id": "call-1", "type": "formation", "formation": "APS",
        "nom": "Camille Martin", "prenom": "Camille", "nom_famille": "Martin",
        "email": "camille@example.test", "telephone": "+33601020304",
        "created_at": "2026-09-23T10:00:00+02:00", "notes": "Appel de qualification",
        "formation_centre": "cote_azur", "formation_session_label": "Session de novembre",
        "cpf_consulte": "OUI", "cpf_montant": "600,50", "france_travail": "OUI",
        "ft_refus_ok": "OUI", "financement_perso": "OUI", "identite_numerique": "OUI",
        "cnaps_ok": "NON", "garde_vue": "NON", "titre_sejour": "NON", **overrides,
    }


def contact(**overrides):
    return {
        "id": "contact-1", "prenom": "Camille", "nom": "MARTIN", "formation": "APS",
        "mail": "CAMILLE@example.test", "telephone": "06 01 02 03 04",
        "origine": "Calendly", "statut": "RDV pris", "commercial": "Conseiller",
        "created_at": "2026-09-23T09:59:00+02:00", "activities": [], **overrides,
    }


def create(data, entry):
    return application._crm_create_contact_from_secretariat(
        data, entry, {"prenom": "Camille", "nom": "Martin"},
    )


def historical_data(entry=None, **contact_fields):
    entry = entry or submission()
    return {
        "crm_contacts": [contact(**contact_fields)], "secretariat_demandes": [entry],
        "crm_inbound_requests": [{
            "source": "assistant-secretariat", "external_id": entry["id"],
            "contact_id": "contact-1", "status": "matched", "raw_payload": copy.deepcopy(entry),
        }],
    }


def test_calendly_contact_receives_all_answers_without_changing_ownership_or_status():
    original = contact(cpf="Non renseigné")
    data, entry = {"crm_contacts": [original]}, submission()
    protected = {key: original[key] for key in ("origine", "statut", "commercial", "created_at")}
    assert create(data, entry) is original
    assert len(data["crm_contacts"]) == 1
    assert {key: original[key] for key in protected} == protected
    assert {key: original[key] for key in (
        "cpf", "cpf_montant", "financement_ft", "financement_perso_possible",
        "refus_ft_perso", "reste_a_charge_perso", "identite_creation", "carte_pro",
        "garde_vue", "antecedents", "titre_sejour", "dates_formation",
    )} == {
        "cpf": "OUI", "cpf_montant": "600.50", "financement_ft": "OUI",
        "financement_perso_possible": "OUI", "refus_ft_perso": "OUI",
        "reste_a_charge_perso": "OUI", "identite_creation": "OUI", "carte_pro": "NON",
        "garde_vue": "NON", "antecedents": "NON", "titre_sejour": "NON",
        "dates_formation": "Session de novembre",
    }
    assert entry["crm_contact_id"] == original["id"]
    assert not original.get("statut_demande_financement_ft")
    assert not original.get("montant_accorde_ft")
    assert not original.get("inscrit_ft")


def test_retry_completes_late_answers_and_is_idempotent():
    data = {"crm_contacts": [contact()]}
    first = submission(cpf_consulte="", cpf_montant="", identite_numerique="")
    original = create(data, first)
    create(data, submission())
    assert original["cpf_montant"] == "600.50"
    assert original["identite_creation"] == "OUI"
    after = copy.deepcopy(data)
    create(data, submission())
    assert data == after
    assert len(data["crm_inbound_requests"]) == 1


def test_existing_values_and_conflicting_aliases_are_preserved_and_reported():
    original = contact(cpf="OUI", cpf_montant="1200.00", refus_ft_perso="NON",
                       antecedents="OUI", identite_creation="NON", dates_formation="Session validée")
    data = {"crm_contacts": [original]}
    create(data, submission())
    assert original["cpf_montant"] == "1200.00"
    assert original["identite_creation"] == "NON"
    assert original["dates_formation"] == "Session validée"
    assert not original.get("financement_perso_possible")
    assert not original.get("garde_vue")
    assert {"cpf_montant", "financement_perso_possible", "garde_vue", "identite_creation"} <= set(
        data["crm_inbound_requests"][0]["differences"])


@pytest.mark.parametrize("changed", [{"email": "other@example.test"}, {"prenom": "Autre", "nom_famille": "Personne"}])
def test_reused_request_id_with_changed_identity_cannot_fill_an_old_contact(changed):
    original = contact()
    data = {"crm_contacts": [original]}
    create(data, submission(cpf_consulte="", cpf_montant=""))
    incoming = submission(**changed)
    payload = {"prenom": incoming["prenom"], "nom": incoming["nom_famille"]}
    assert application._crm_create_contact_from_secretariat(data, incoming, payload) is None
    assert not original.get("cpf_montant")
    assert data["crm_inbound_requests"][0]["status"] == "pending_review"
    assert not incoming.get("crm_contact_id")
    assert application._crm_create_contact_from_secretariat(data, incoming, payload) is None
    assert not original.get("cpf_montant")


def test_source_dependencies_cannot_be_replaced_by_conflicting_stored_answers():
    original = contact(cpf="OUI", financement_ft="OUI", carte_pro="NON")
    create({"crm_contacts": [original]}, submission(cpf_consulte="NON", cpf_montant="0",
                                                   france_travail="NON", cnaps_ok="OUI"))
    for field in ("cpf_montant", "financement_perso_possible", "refus_ft_perso", "garde_vue", "titre_sejour"):
        assert not original.get(field)


@pytest.mark.parametrize("fields", [{"formation": "A3P"}, {"formation": "DESP", "desp_type": "VAE"}])
def test_answers_for_a_different_training_are_not_applied(fields):
    original = contact(**fields)
    create({"crm_contacts": [original]}, submission(formation="DESP_INIT"))
    assert not original.get("cpf")
    assert not original.get("dates_formation")


def test_ambiguous_contact_does_not_receive_answers():
    data = {"crm_contacts": [contact(), contact(id="contact-2")]}
    assert create(data, submission()) is None
    assert all(not row.get("cpf") for row in data["crm_contacts"])


def test_new_contact_still_receives_answers():
    original = create({"crm_contacts": []}, submission())
    assert original["cpf_montant"] == "600.50"
    assert original["financement_ft"] == "OUI"
    assert original["origine"] == "Secrétariat"


def test_recovery_dry_run_then_apply_without_replaying_deliveries(monkeypatch):
    def no_delivery(*args, **kwargs):
        pytest.fail("Historical recovery must not call delivery or external integrations")
    for name in ("_send_secretariat_information_messages", "_ensure_secretariat_quote",
                 "creer_piste_salesforce", "_secretariat_refresh_calendly_appointments"):
        monkeypatch.setattr(application, name, no_delivery)
    data = historical_data()
    before = copy.deepcopy(data)
    preview = application._crm_recover_secretariat_answers(data)
    assert preview["contacts"] == 1 and preview["fields"] == 12
    assert data == before
    result = application._crm_recover_secretariat_answers(data, dry_run=False)
    assert result == preview
    assert data["crm_contacts"][0]["cpf_montant"] == "600.50"
    assert data["secretariat_demandes"] == before["secretariat_demandes"]
    assert data["crm_inbound_requests"] == before["crm_inbound_requests"]
    activity = data["crm_contacts"][0]["activities"][0]
    assert activity["source_secretariat_id"] == "call-1"
    assert application._crm_recover_secretariat_answers(data, dry_run=False)["fields"] == 0
    assert len(data["crm_contacts"][0]["activities"]) == 1


@pytest.mark.parametrize("link_kind", ["entry", "contact", "publication", "inbound"])
def test_recovery_accepts_each_durable_link_with_matching_coordinates(link_kind):
    data = historical_data()
    if link_kind != "inbound":
        data["crm_inbound_requests"] = []
    if link_kind == "entry":
        data["secretariat_demandes"][0]["crm_contact_id"] = "contact-1"
    if link_kind == "contact":
        data["crm_contacts"][0]["source_secretariat_id"] = "call-1"
    if link_kind == "publication":
        data["crm_contacts"][0]["publications"] = [{
            "source": "assistant-secretariat", "source_secretariat_id": "call-1",
        }]
    assert application._crm_recover_secretariat_answers(data)["contacts"] == 1


@pytest.mark.parametrize("problem", ["missing_link", "conflicting_link", "pending_review", "changed_coordinates", "other_request"])
def test_recovery_rejects_uncertain_or_unrelated_requests(problem):
    data = historical_data()
    if problem == "missing_link":
        data["crm_inbound_requests"] = []
    if problem == "conflicting_link":
        data["crm_contacts"].append(contact(id="contact-2", source_secretariat_id="call-1"))
    if problem == "pending_review":
        data["crm_inbound_requests"][0]["status"] = "pending_review"
    if problem == "changed_coordinates":
        data["crm_contacts"][0]["mail"] = "different@example.test"
    if problem == "other_request":
        data["secretariat_demandes"][0]["type"] = "autre"
    before = copy.deepcopy(data)
    assert application._crm_recover_secretariat_answers(data, dry_run=False)["fields"] == 0
    assert data == before


def test_saved_request_takes_precedence_over_inbound_snapshot_including_blanks():
    data = historical_data()
    data["secretariat_demandes"][0].update(cpf_montant="", identite_numerique="NON")
    application._crm_recover_secretariat_answers(data, dry_run=False)
    assert not data["crm_contacts"][0].get("cpf_montant")
    assert data["crm_contacts"][0]["identite_creation"] == "NON"


def test_raw_inbound_answers_can_recover_when_journal_entry_is_unavailable():
    data = historical_data()
    data["secretariat_demandes"] = []
    assert application._crm_recover_secretariat_answers(data, dry_run=False)["contacts"] == 1


def test_invalid_balance_does_not_partially_apply_or_break_recovery():
    data = historical_data(submission(cpf_montant="inconnu"))
    before = copy.deepcopy(data)
    result = application._crm_recover_secretariat_answers(data, dry_run=False)
    assert result["skipped"][0]["reason"] == "invalid_answers"
    assert data == before


def test_unconsulted_cpf_does_not_recover_a_default_zero_or_inapplicable_answers():
    data = historical_data(submission(cpf_consulte="NON", cpf_montant="0",
                                     france_travail="NON", cnaps_ok="OUI"))
    application._crm_recover_secretariat_answers(data, dry_run=False)
    original = data["crm_contacts"][0]
    assert original["cpf"] == "NON"
    for field in ("cpf_montant", "refus_ft_perso", "financement_perso_possible", "garde_vue", "titre_sejour"):
        assert not original.get(field)


def test_startup_recovery_saves_once_and_does_not_refill_a_later_manual_clear(monkeypatch):
    data = historical_data()
    saves = []
    monkeypatch.setattr(application, "load_data", lambda: data)
    monkeypatch.setattr(application, "save_data", lambda value: saves.append(copy.deepcopy(value)))
    assert application._crm_restore_secretariat_answers_once()["contacts"] == 1
    assert data[REPAIR_KEY]["version"] == VERSION
    assert len(saves) == 1
    data["crm_contacts"][0]["cpf_montant"] = ""
    application._crm_restore_secretariat_answers_once()
    assert len(saves) == 1
    assert data["crm_contacts"][0]["cpf_montant"] == ""


def test_real_gunicorn_entrypoint_persists_recovery_and_keeps_it_on_restart(tmp_path):
    path = tmp_path / "data.json"
    path.write_text(json.dumps(historical_data()), encoding="utf-8")
    environment = {**os.environ, "DATA_FILE": str(path), "WEDOF_CRM_RECONCILIATION_ENABLED": "false"}
    for _ in range(2):
        result = subprocess.run([sys.executable, "-c", "import crm_app"], env=environment,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["crm_contacts"][0]["cpf_montant"] == "600.50"
        assert saved[REPAIR_KEY]["contacts"] == 1
        assert len(saved["crm_contacts"][0]["activities"]) == 1


def test_quote_followup_preserves_existing_training_and_completes_empty_fields():
    original = contact(lieu="Paris", dates_formation="Session validée")
    _sync_quote_contact_training(original, "quote", "cote_azur", "", "", "Autre session")
    assert original["lieu"] == "Paris" and original["dates_formation"] == "Session validée"
    blank = contact(lieu="Non renseigné")
    _sync_quote_contact_training(blank, "quote", "cote_azur", "", "", "Session choisie")
    assert blank["lieu"] == "Côte d’Azur" and blank["dates_formation"] == "Session choisie"


def test_public_submission_completes_a_calendly_contact_under_transaction_lock(monkeypatch):
    data = {"crm_contacts": [contact()], "secretariat_demandes": []}
    saves = []
    # A separate thread must not be able to acquire the CRM lock during load.
    def load():
        acquired = []
        def probe():
            held = application._CRM_RECONCILIATION_LOCK.acquire(blocking=False)
            acquired.append(held)
            if held:
                application._CRM_RECONCILIATION_LOCK.release()
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(timeout=2)
        assert acquired == [False]
        return data
    monkeypatch.setattr(application, "load_data", load)
    monkeypatch.setattr(application, "save_data", lambda value: saves.append(copy.deepcopy(value)))
    monkeypatch.setattr(application, "_secretariat_refresh_calendly_appointments", lambda *a: None)
    monkeypatch.setattr(application, "_secretariat_hydrate_appointment_from_crm", lambda *a: None)
    monkeypatch.setattr(application, "_ensure_secretariat_quote", lambda *a: None)
    monkeypatch.setattr(application, "_send_secretariat_information_messages", lambda *a: {})
    monkeypatch.setattr(application, "creer_piste_salesforce", lambda *a: pytest.fail("Existing lead"))
    client = application.app.test_client()
    response = client.post("/api/secretariat/demandes", json=submission())
    assert response.status_code == 201
    assert saves[0]["crm_contacts"][0]["cpf_montant"] == "600.50"
    assert saves[0]["secretariat_demandes"][0]["crm_contact_id"] == "contact-1"
    response = client.post("/api/secretariat/demandes", json=submission())
    assert response.status_code == 201
    assert len(data["crm_contacts"]) == len(data["secretariat_demandes"]) == 1
    assert len(data["crm_contacts"][0]["publications"]) == 1
