"""Keep concurrent CRM reads cheap without hiding newly saved data."""
import copy
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import app as application


def test_waiting_reader_rechecks_the_revision_after_acquiring_the_lock(monkeypatch):
    revision = [1]
    cached = {"revision": 2}
    waiting = threading.Event()
    lock = threading.RLock()

    class ObservedLock:
        def __enter__(self):
            waiting.set()
            lock.acquire()

        def __exit__(self, *args):
            lock.release()

    def unexpected_rebuild():
        pytest.fail("The preceding reader already prepared the current revision")

    monkeypatch.setattr(application, "_CRM_READ_MODEL_LOCK", ObservedLock())
    monkeypatch.setattr(application, "_crm_read_model_key", lambda: revision[0])
    monkeypatch.setattr(application, "_CRM_READ_MODEL_KEY", 1)
    monkeypatch.setattr(application, "_CRM_READ_MODEL_VALUE", {"revision": 1})
    monkeypatch.setattr(application, "load_data", unexpected_rebuild)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with lock:
            result = pool.submit(application._crm_prepared_read_model)
            assert waiting.wait(2)
            # A write and another reader finish while this reader is queued.
            revision[0] = 2
            application._CRM_READ_MODEL_KEY = 2
            application._CRM_READ_MODEL_VALUE = cached
        assert result.result(timeout=2) is cached


def test_second_write_during_preparation_is_not_cached_as_a_newer_revision(monkeypatch):
    revision = [1]
    prepared = []

    def prepare(data):
        prepared.append(data["revision"])
        if len(prepared) <= 2:
            revision[0] += 1

    monkeypatch.setattr(application, "_crm_read_model_key", lambda: revision[0])
    monkeypatch.setattr(application, "load_data", lambda: {"revision": revision[0]})
    monkeypatch.setattr(application, "_crm_prepare_contacts", prepare)
    monkeypatch.setattr(application, "_crm_backfill_callback_requests", lambda data: None)
    monkeypatch.setattr(application, "_CRM_READ_MODEL_KEY", None)
    monkeypatch.setattr(application, "_CRM_READ_MODEL_VALUE", None)

    first = application._crm_prepared_read_model()
    assert first["revision"] == 2
    # The next request must see the write that arrived during the second pass.
    fresh = application._crm_prepared_read_model()
    assert fresh["revision"] == 3
    assert prepared == [1, 2, 3]
    assert application._crm_prepared_read_model() is fresh


def test_preparing_contacts_visits_the_agenda_once(monkeypatch):
    class CountedAgenda(list):
        visits = 0

        def __iter__(self):
            for appointment in super().__iter__():
                self.visits += 1
                yield appointment

    agenda = CountedAgenda([
        {"id": f"rdv-{i}", "contact_id": f"contact-{i}",
         "start_time": "2099-01-01T10:00:00Z", "status": "active"}
        for i in range(25)
    ])
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["crm_contacts"] = [
        {"id": f"contact-{i}", "prenom": "Test", "nom": "CONTACT",
         "formation": "APS", "statut": "Nouveaux", "activities": [], "relances": []}
        for i in range(100)
    ]
    data["crm_calendly_appointments"] = agenda
    monkeypatch.setattr(application, "_wedof_funding_statuses_by_contact", lambda data: {})

    application._crm_prepare_contacts(data)

    assert agenda.visits == len(agenda)
    assert all(c["statut"] == "RDV programmé" for c in data["crm_contacts"][:25])
    assert all(c["statut"] == "Nouveaux" for c in data["crm_contacts"][25:])


@pytest.mark.parametrize("status,relance_date", [
    ("Nouveaux", ""), ("RDV programmé", ""),
    ("A relancer", "2026-10-03"), ("En cours", "2026-10-03"),
    ("Converti", ""), ("Disqualifié", ""),
])
@pytest.mark.parametrize("appointment", [
    None,
    {"start_time": "2026-10-01T08:00:00Z", "status": "active"},
    {"start_time": "2026-10-02T08:00:00Z", "status": "active"},
    {"start_time": "2026-09-30T08:00:00Z", "status": "active"},
    {"start_time": "2026-10-02T08:00:00Z", "status": "canceled"},
    {"start_time": "2026-10-02T08:00:00Z", "response_status": "no_answer"},
    {"start_time": "2026-10-02T08:00:00Z", "response_status": "answered"},
    {"start_time": "invalid"},
])
def test_indexed_agenda_preserves_pipeline_rules(monkeypatch, status, relance_date, appointment):
    contact = {"id": "selected", "statut": status, "relance_date": relance_date}
    selected_appointments = [{**appointment, "contact_id": "selected"}] if appointment else []
    data = {"crm_calendly_appointments": [
        {"contact_id": "other", "start_time": "2026-10-02T08:00:00Z"},
        *selected_appointments,
    ]}
    now = datetime.datetime(2026, 10, 1, 12, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(application, "_crm_now", lambda: "2026-10-01T12:00:00+00:00")
    expected = copy.deepcopy(contact)
    actual = copy.deepcopy(contact)

    expected_changed = application._crm_sync_contact_calendly_status(data, expected, now)
    actual_changed = application._crm_sync_contact_calendly_status(
        data, actual, now, appointments=selected_appointments,
    )

    assert actual == expected
    assert actual_changed == expected_changed
