"""Regression cases for wrong-funder refusals and repeated WEDOF deliveries."""
import copy
from concurrent.futures import ThreadPoolExecutor
import subprocess
from pathlib import Path

import pytest

import app as application
from crm_funding_safety import france_travail_decision


@pytest.fixture
def crm(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    monkeypatch.setenv("WEDOF_DB_PATH", str(tmp_path / "wedof.sqlite3"))
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    client = application.app.test_client()
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    contact = client.post("/api/crm/contacts", json={
        "prenom": "Test", "nom": "Financement", "force_create": True,
    }).get_json()
    client.patch(f"/api/crm/contacts/{contact['id']}", json={
        "mail": "funding@example.test", "telephone": "+33612345678",
    })
    deliveries = []
    monkeypatch.setattr(application, "send_sms", lambda *_: deliveries.append("sms") or True)
    monkeypatch.setattr(application, "send_email_html", lambda *_: deliveries.append("email") or True)
    for kind in ("sms", "email"):
        client.post("/api/crm/templates", json={
            "type": kind, "nom": "FT refusé", "sujet": "Décision FT",
            "contenu": "Votre demande FT a été refusée.",
        })
    return client, contact["id"], deliveries


def funding_folder(*, funder=9, status="refused", submitted="2026-10-01T09:00:00Z"):
    return {
        "externalId": "funding-test", "type": "cpf", "state": "validated",
        "createdAt": "2026-10-01T08:00:00Z", "lastUpdate": "2026-10-29T09:00:00Z",
        "attendee": {"email": "funding@example.test", "firstName": "Test", "lastName": "Financement"},
        "trainingActionInfo": {"solicitations": [{
            "fundingType": funder, "status": status, "submissionDate": submitted,
        }]},
    }


def sync(payload):
    application._wedof_store_page([copy.deepcopy(payload)], application.load_data(), 1)


def stored_contact(contact_id):
    return next(c for c in application.load_data()["crm_contacts"] if c["id"] == contact_id)


@pytest.mark.parametrize("state", [
    "refusedByAttendee", "refusedByOrganism", "rejected", "rejectedWithoutTitulaireSuite",
    "refusedByFinancer", "validated", "waitingAcceptation",
])
def test_generic_folder_states_never_identify_france_travail(state):
    assert application._wedof_france_travail_status({
        "state": state, "history": {"waitingAcceptationDate": "2026-10-01", "refusedByFinancerDate": "2026-10-02"},
    }, previous_status="en_cours_instruction") == ""


@pytest.mark.parametrize("funder", [None, 29, "C2P", "REGION", "TITULAIRE"])
def test_other_funders_never_send_france_travail_refusals(crm, funder):
    _, contact_id, deliveries = crm
    sync(funding_folder(funder=funder))
    assert deliveries == []
    assert stored_contact(contact_id).get("statut_demande_financement_ft", "") == ""
    assert not application.load_data().get("crm_notifications")


@pytest.mark.parametrize("secondary,manual", [("C2P en cours", False), ("C2P en cours", True), ("Transition pro", True), ("Def MOB", False)])
def test_explicit_non_ft_funding_blocks_even_a_stale_ft_solicitation(crm, secondary, manual):
    _, contact_id, deliveries = crm
    data = application.load_data()
    contact = next(c for c in data["crm_contacts"] if c["id"] == contact_id)
    contact["statut_secondaire"] = secondary
    if manual:
        contact["statut_secondaire_source"] = "manual"
    application.save_data(data)
    sync(funding_folder())
    result = stored_contact(contact_id)
    assert result["statut_secondaire"] == secondary
    assert result.get("statut_demande_financement_ft", "") == ""
    assert deliveries == []
    assert not result.get("relance_date")


def test_sparse_replays_and_older_webhooks_do_not_resend(crm):
    _, contact_id, deliveries = crm
    rich = funding_folder()
    sync(rich)
    assert deliveries == ["sms", "email"]
    sparse = {"externalId": rich["externalId"], "state": "validated", "history": [], "trainingActionInfo": {"solicitations": []}}
    sync(sparse)
    old = funding_folder(status="pending")
    old["lastUpdate"] = "2026-10-28T09:00:00Z"
    sync(old)
    sync(rich)
    assert deliveries == ["sms", "email"]
    assert stored_contact(contact_id)["statut_demande_financement_ft"] == "refusee"
    latest = application._wedof_latest_linked_resource(contact_id)
    assert france_travail_decision(latest["payload"])["status"] == "refusee"
    assert len(application.load_data()["crm_notifications"]) == len(application.USERS)


def test_distinct_submission_allows_a_new_refusal_without_rearming_old_one(crm):
    _, _, deliveries = crm
    sync(funding_folder())
    second = funding_folder(submitted="2026-10-15T09:00:00Z")
    second["lastUpdate"] = "2026-10-30T09:00:00Z"
    sync(second)
    sync(funding_folder())
    assert deliveries == ["sms", "email", "sms", "email"]


def test_a_new_refusal_notification_is_visible_after_cleanup_in_the_same_second(crm, monkeypatch):
    client, _, _ = crm
    monkeypatch.setattr(application, "_crm_now", lambda: "2026-10-30T10:00:00+01:00")
    sync(funding_folder())
    next_cycle = funding_folder(status="pending", submitted="2026-10-15T09:00:00Z")
    sync(next_cycle)
    next_cycle["trainingActionInfo"]["solicitations"][0]["status"] = "refused"
    sync(next_cycle)
    notifications = client.get("/api/crm/notifications").get_json()
    assert len(notifications) == 1
    assert notifications[0]["kind"] == "funding_refused"


def test_enriching_undated_decision_does_not_change_delivery_identity(crm):
    _, _, deliveries = crm
    sync(funding_folder(submitted=""))
    sync(funding_folder())
    assert deliveries == ["sms", "email"]


def test_persisted_claim_survives_lost_crm_history_and_sender_failure(crm, monkeypatch):
    _, contact_id, deliveries = crm
    before = application.load_data()
    attempts = []

    def ambiguous_email(*_):
        with application._wedof_connect() as db:
            assert db.execute("SELECT status FROM crm_ft_refusal_deliveries WHERE channel='email'").fetchone()[0] == "claimed"
        attempts.append("attempt")
        raise TimeoutError("Remote delivery outcome unknown")

    monkeypatch.setattr(application, "send_email_html", ambiguous_email)
    sync(funding_folder())
    # Simulate a restart after claims committed but before JSON history persisted.
    application.save_data(before)
    sync(funding_folder())
    assert attempts == ["attempt"]
    assert deliveries == ["sms"]
    with application._wedof_connect() as db:
        assert db.execute("SELECT status FROM crm_ft_refusal_deliveries WHERE contact_id=? AND channel='email'", (contact_id,)).fetchone()[0] == "failed_or_unknown"


def test_ledger_claim_is_atomic_between_concurrent_workers(crm):
    _, contact_id, _ = crm
    data = application.load_data()
    contact = stored_contact(contact_id)
    decision = france_travail_decision(funding_folder())
    with ThreadPoolExecutor(max_workers=6) as pool:
        claimed = list(pool.map(lambda _: application._crm_claim_ft_refusal(data, contact, "concurrent", decision, "email"), range(12)))
    assert claimed.count(True) == 1


def test_preexisting_sent_history_seeds_the_delivery_ledger(crm):
    _, contact_id, deliveries = crm
    data = application.load_data()
    contact = next(c for c in data["crm_contacts"] if c["id"] == contact_id)
    for kind, label in (("email", "E-mail"), ("sms", "SMS")):
        contact["activities"].append({"kind": kind, "title": f"{label} « FT refusé » envoyé", "date": "2026-10-29T09:00:00Z"})
    application.save_data(data)
    sync(funding_folder())
    assert deliveries == []


def test_only_latest_folder_can_send_a_refusal_regardless_of_page_order(crm):
    _, _, deliveries = crm
    old = funding_folder()
    new = funding_folder(funder="C2P")
    new.update(externalId="new-folder", createdAt="2026-10-20T10:00:00Z")
    application._wedof_store_page([old, new], application.load_data(), 1)
    sync(old)
    assert deliveries == []


def test_cleanup_is_reversible_idempotent_and_preserves_manual_followups(crm):
    client, contact_id, deliveries = crm
    data = application.load_data()
    contact = next(c for c in data["crm_contacts"] if c["id"] == contact_id)
    contact.update(statut_secondaire="C2P en cours", statut_secondaire_source="manual", statut_demande_financement_ft="refusee", relance_date="2026-11-02")
    auto = {"id": "auto", "status": "scheduled", "scheduled_date": "2026-11-02", "source": "wedof_ft_refusal"}
    manual = {"id": "manual", "status": "scheduled", "scheduled_date": "2026-11-03", "source": "manual"}
    contact["relances"] = [auto, manual]
    original_activities = copy.deepcopy(contact.get("activities", []))
    application._crm_add_funding_refusal_notifications(data, contact, "funding-test")
    application.save_data(data)
    application._crm_repair_ft_funding_at_startup()
    application._crm_repair_ft_funding_at_startup()
    result = stored_contact(contact_id)
    assert result["statut_secondaire"] == "C2P en cours"
    assert result["statut_demande_financement_ft"] == ""
    assert result["relances"] == [manual]
    assert result["relance_date"] == "2026-11-03"
    assert len(result["funding_safety_history"]) == 1
    assert result["funding_safety_history"][0]["cancelled_automatic_relances"] == [auto]
    assert result["activities"][1:] == original_activities
    assert deliveries == []
    assert client.get("/api/crm/notifications").get_json() == []
    assert len(application.load_data()["crm_notifications"]) == len(application.USERS)


def test_manual_c2p_selection_clears_old_ft_status_and_rejects_conflicting_patch(crm):
    client, contact_id, deliveries = crm
    sync(funding_folder())
    response = client.patch(f"/api/crm/contacts/{contact_id}", json={"statut_secondaire": "C2P en cours"})
    assert response.status_code == 200
    assert response.get_json()["statut_demande_financement_ft"] == ""
    rejected = client.patch(f"/api/crm/contacts/{contact_id}", json={"statut_demande_financement_ft": "refusee"})
    assert rejected.status_code == 400
    sync(funding_folder())
    assert deliveries == ["sms", "email"]


@pytest.mark.parametrize("kind", ["email", "sms"])
def test_manual_refusal_template_is_blocked_for_c2p(crm, kind):
    client, contact_id, deliveries = crm
    client.patch(f"/api/crm/contacts/{contact_id}", json={"statut_secondaire": "C2P en cours"})
    template = application._crm_named_template(application.load_data(), kind, "FT refusé")
    response = client.post(f"/api/crm/contacts/{contact_id}/message", json={
        "type": kind, "template_id": template["id"], "contenu": "Refus FT",
    })
    assert response.status_code == 409
    assert deliveries == []


def test_actual_ft_refusal_and_manual_status_survive_cleanup(crm):
    client, contact_id, _ = crm
    sync(funding_folder())
    before = stored_contact(contact_id)
    application._crm_repair_ft_funding_at_startup()
    assert stored_contact(contact_id) == before
    # An explicit manual FT decision also remains when the raw API has no evidence.
    second = funding_folder(funder="C2P")
    second["lastUpdate"] = "2026-10-30T10:00:00Z"
    sync(second)
    client.patch(f"/api/crm/contacts/{contact_id}", json={"statut_demande_financement_ft": "refusee"})
    manual = stored_contact(contact_id)
    application._crm_repair_ft_funding_at_startup()
    assert stored_contact(contact_id) == manual


@pytest.mark.parametrize("phase,expected", [("login", True), ("send", False), ("quit", True)])
def test_ft_smtp_fallback_only_before_submission(crm, monkeypatch, phase, expected):
    fallback = []

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_):
            if phase == "quit":
                raise application.smtplib.SMTPServerDisconnected()
        def login(self, *_):
            if phase == "login":
                raise application.smtplib.SMTPServerDisconnected()
        def send_message(self, *_):
            if phase == "send":
                raise application.smtplib.SMTPServerDisconnected()

    # crm mocks the high-level sender; test its original transport explicitly.
    monkeypatch.setattr(application.smtplib, "SMTP_SSL", SMTP)
    monkeypatch.setattr(application, "_send_email_brevo", lambda *a, **kw: fallback.append(True) or True)
    token = application._FT_REFUSAL_DELIVERY.set(True)
    try:
        result = ORIGINAL_EMAIL_SENDER("funding@example.test", "Refus", "Texte", "<p>Texte</p>")
    finally:
        application._FT_REFUSAL_DELIVERY.reset(token)
    assert result is expected
    assert fallback == ([True] if phase == "login" else [])


ORIGINAL_EMAIL_SENDER = application.send_email_html


def test_browser_mapping_has_same_funder_guards():
    subprocess.run(["node", "tests/wedof_status_smoke.js"], cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True, text=True)
