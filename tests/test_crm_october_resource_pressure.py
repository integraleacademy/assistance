"""Regressions for leaked SQLite handles and concurrent CRM sheet reads."""
import copy
import gc
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

import app as application
from test_crm_wedof import authenticated_client, folder


def seed_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("WEDOF_DB_PATH", str(tmp_path / "wedof.sqlite3"))
    with application._wedof_connect() as db:
        for identifier, name in (("one", "Martin"), ("other", "Other")):
            payload = folder(identifier, first_name="Lina", last_name=name)
            db.execute("INSERT INTO wedof_resources VALUES (?, ?, ?, ?, ?)",
                       ("registrationFolder", identifier, json.dumps(payload),
                        "2026-10-01", "2026-10-01"))


def test_wedof_connection_closes_after_commit_and_rollback(tmp_path, monkeypatch):
    seed_cache(monkeypatch, tmp_path)
    with application._wedof_connect() as committed:
        committed.execute("INSERT INTO wedof_webhook_deliveries VALUES ('kept', 'now')")
    with pytest.raises(sqlite3.ProgrammingError):
        committed.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="abort"):
        with application._wedof_connect() as rolled_back:
            rolled_back.execute("INSERT INTO wedof_webhook_deliveries VALUES ('lost', 'now')")
            raise RuntimeError("abort")
    with pytest.raises(sqlite3.ProgrammingError):
        rolled_back.execute("SELECT 1")
    with application._wedof_connect() as check:
        assert [row[0] for row in check.execute(
            "SELECT delivery_id FROM wedof_webhook_deliveries")] == ["kept"]


def test_schema_initialization_failure_also_closes_connection(tmp_path, monkeypatch):
    seed_cache(monkeypatch, tmp_path)
    original_connect = sqlite3.connect
    connections = []

    class FailingSchema(sqlite3.Connection):
        def executescript(self, sql):
            raise sqlite3.OperationalError("schema unavailable")

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs, factory=FailingSchema)
        connections.append(connection)
        return connection

    monkeypatch.setattr(application.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="schema unavailable"):
        with application._wedof_connect():
            pytest.fail("initialization must fail before use")
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute("SELECT 1")


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="Linux resource probe")
def test_repeated_cache_reads_do_not_depend_on_garbage_collection(tmp_path, monkeypatch):
    seed_cache(monkeypatch, tmp_path)
    gc.collect()
    enabled = gc.isenabled()
    gc.disable()
    try:
        initial = len(os.listdir("/proc/self/fd"))
        for _ in range(60):
            with application._wedof_connect() as db:
                assert db.execute("SELECT COUNT(*) FROM wedof_resources").fetchone()[0] == 2
        assert len(os.listdir("/proc/self/fd")) <= initial + 2
    finally:
        if enabled:
            gc.enable()
        gc.collect()


def test_unlinked_contact_lookup_streams_rows_and_preserves_identity_matching(tmp_path, monkeypatch):
    seed_cache(monkeypatch, tmp_path)
    original_connect = application._wedof_connect

    class StreamingCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchone(self):
            return self.cursor.fetchone()

        def __iter__(self):
            return iter(self.cursor)

        def fetchall(self):
            pytest.fail("unrelated folder payloads must not accumulate in RAM")

    @contextmanager
    def streaming_connection():
        with original_connect() as db:
            class Connection:
                def execute(self, *args):
                    return StreamingCursor(db.execute(*args))
            yield Connection()

    monkeypatch.setattr(application, "_wedof_connect", streaming_connection)
    data = {"crm_contacts": [{"id": "a", "prenom": "Lina", "nom": "MARTIN"}]}
    before = copy.deepcopy(data)
    result = application._wedof_contact_resources("a", data)
    assert [row["stable_id"] for row in result] == ["one"]
    assert result[0]["match_method"] == "name" and result[0]["is_latest"]
    assert data == before


def test_sheet_read_helpers_do_not_copy_the_complete_database(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["crm_contacts"] = [{"id": "a", "prenom": "Lina", "nom": "Martin"}]
    application.save_data(data)
    monkeypatch.setattr(application, "load_data",
                        lambda: pytest.fail("read-only request copied the whole CRM"))
    monkeypatch.setattr(application, "_wedof_request",
                        lambda *a, **kw: pytest.fail("no remote folder exists"))
    assert client.get("/crm/contacts").status_code == 200
    assert client.get("/api/crm/contacts/a/ai-analysis").status_code == 200
    assert client.get("/api/crm/contacts/missing/ai-analysis").status_code == 404
    for operation in ("refresh", "refresh-on-open"):
        response = client.post(f"/api/crm/contacts/a/wedof/{operation}")
        assert response.status_code == 200
        assert response.get_json()["sync"]["reason"] == "no_known_folder"
    assert client.post("/api/secretariat/crm-contact", json={"email": "none@example.invalid"}).status_code == 200


def test_concurrent_wedof_projection_refresh_decodes_each_folder_once(tmp_path, monkeypatch):
    seed_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(application, "_WEDOF_FUNDING_CACHE_KEY", None)
    monkeypatch.setattr(application, "_WEDOF_FUNDING_CACHE_VALUE", None)
    original = application._wedof_france_travail_status
    entered, release = threading.Event(), threading.Event()
    calls = []

    def decode(payload, *args, **kwargs):
        calls.append(payload["externalId"])
        entered.set()
        assert release.wait(3)
        return original(payload, *args, **kwargs)

    monkeypatch.setattr(application, "_wedof_france_travail_status", decode)
    data = {"crm_contacts": [{"id": "a", "prenom": "Lina", "nom": "Martin"}]}
    with ThreadPoolExecutor(max_workers=6) as pool:
        first = pool.submit(application._wedof_funding_statuses_by_contact, data)
        assert entered.wait(3)
        others = [pool.submit(application._wedof_funding_statuses_by_contact, data) for _ in range(5)]
        release.set()
        results = [future.result(timeout=5) for future in [first, *others]]
    assert all(result == results[0] for result in results)
    assert sorted(calls) == ["one", "other"]


def test_funding_cache_ignores_empty_wal_but_detects_committed_writes(tmp_path, monkeypatch):
    seed_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(application, "_WEDOF_FUNDING_CACHE_KEY", None)
    monkeypatch.setattr(application, "_WEDOF_FUNDING_CACHE_VALUE", None)
    original = application._wedof_france_travail_status
    calls = []

    def decode(payload, *args, **kwargs):
        calls.append(payload["externalId"])
        return original(payload, *args, **kwargs)

    monkeypatch.setattr(application, "_wedof_france_travail_status", decode)
    data = {"crm_contacts": [{"id": "a", "prenom": "Lina", "nom": "Martin"}]}
    expected = application._wedof_funding_statuses_by_contact(data)
    signature = application._wedof_db_signature()
    with application._wedof_connect() as reader:
        reader.execute("SELECT COUNT(*) FROM wedof_resources").fetchone()
        assert application._wedof_db_signature() == signature
        assert application._wedof_funding_statuses_by_contact(data) == expected
    assert application._wedof_funding_statuses_by_contact(data) == expected
    assert sorted(calls) == ["one", "other"]

    # A committed change must still invalidate while another connection keeps
    # the non-empty WAL open, before its contents are checkpointed to the DB.
    with application._wedof_connect() as writer:
        payload = folder("new", first_name="Lina", last_name="Martin")
        writer.execute("INSERT INTO wedof_resources VALUES (?, ?, ?, ?, ?)",
                       ("registrationFolder", "new", json.dumps(payload),
                        "2026-10-02", "2026-10-02"))
        writer.commit()
        assert os.stat(f"{application._wedof_db_path()}-wal").st_size > 0
        assert application._wedof_db_signature() != signature
        calls.clear()
        application._wedof_funding_statuses_by_contact(data)
        assert sorted(calls) == ["new", "one", "other"]


def test_callback_backfill_indexes_agenda_once_and_keeps_existing_matching(monkeypatch):
    class CountedAgenda(list):
        visits = 0

        def __iter__(self):
            for appointment in super().__iter__():
                self.visits += 1
                yield appointment

    data = {"crm_contacts": [{"id": str(i), "mail": f"p{i}@example.invalid",
                              "telephone": f"060000{i:04d}"} for i in range(30)],
            "crm_calendly_appointments": [
                {"id": f"rdv{i}", "contact_id": str(i), "start_time": "2099-01-01T10:00:00Z",
                 "name": "Appel téléphonique", "status": "active"} for i in range(30)],
            "secretariat_demandes": [
                {"id": f"callback{i}", "type": "autre", "crm_contact_id": str(i),
                 "created_at": "2026-01-01T10:00:00Z"} for i in range(30)]}
    # Include alias-only matches, canceled/past/invalid rows and equal-time ties.
    for row in data["crm_calendly_appointments"][:6]:
        row["invitee_email"] = f"p{row['contact_id']}@example.invalid"
        row["contact_id"] = "removed"
    data["crm_calendly_appointments"][0]["status"] = "canceled"
    data["crm_calendly_appointments"][1]["start_time"] = "2000-01-01T10:00:00Z"
    data["crm_calendly_appointments"][2]["start_time"] = "invalid"
    data["crm_calendly_appointments"].append(dict(data["crm_calendly_appointments"][3], id="tie"))
    expected = copy.deepcopy(data)
    for entry in expected["secretariat_demandes"]:
        application._crm_prepare_callback_request(expected, entry)
    agenda = CountedAgenda(data["crm_calendly_appointments"])
    data["crm_calendly_appointments"] = agenda
    application._crm_backfill_callback_requests(data)
    assert data["secretariat_demandes"] == expected["secretariat_demandes"]
    # UUID activity IDs differ between the two equivalent backfills.
    for actual, wanted in zip(data["crm_contacts"], expected["crm_contacts"]):
        for row in actual["activities"] + wanted["activities"]:
            row.pop("id", None)
        assert actual == wanted
    assert agenda.visits == len(agenda)
