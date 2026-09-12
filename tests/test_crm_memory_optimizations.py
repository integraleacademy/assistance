"""Memory/correctness regression coverage; no credentials or network needed."""
import ast
import copy
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from crm_memory_optimizations import (
    InFlightRequests, LookupBusy, calendly_contact_get, install_crm_memory_optimizations,
)


class Legacy:
    class CalendlyAPIError(Exception):
        pass

    def __init__(self):
        self.store = {
            "crm_contacts": [{"id": "one", "mail": "one@example.invalid",
                              "telephone": "0600000000", "statut": "Nouveaux",
                              "activities": [{"preview": "x" * 10000}]}],
            "crm_calendly": {"user": "u", "organization": "o"},
            "crm_calendly_appointments": [],
            "unrelated": [{"value": index} for index in range(2000)],
        }
        self.loads = 0
        self.saves = 0
        self.remote_calls = 0
        self.lock_depth = 0
        self._mutex = threading.RLock()
        self._CRM_RECONCILIATION_LOCK = self.transaction()
        self.request = SimpleNamespace(method="GET", args={})
        self.authenticated = True
        self.original_posts = 0
        self.app = SimpleNamespace(
            extensions={}, logger=SimpleNamespace(info=lambda *a: None),
            view_functions={"crm_contact_calendly_appointments": self.original,
                            "crm_contact_wedof": lambda contact_id: None},
        )

    @contextmanager
    def transaction(self):
        with self._mutex:
            self.lock_depth += 1
            try:
                yield
            finally:
                self.lock_depth -= 1

    def original(self, contact_id):
        self.original_posts += 1
        return {"booking": contact_id}, 201

    def login_required(self, fn):
        def authenticated(*args, **kwargs):
            if not self.authenticated:
                return {"error": "login"}, 401
            return fn(*args, **kwargs)
        return authenticated

    def current_user(self):
        return {"email": "operator@example.invalid"}

    def jsonify(self, payload):
        return copy.deepcopy(payload)

    def _load_data_snapshot(self):
        return self.store

    def load_data(self):
        self.loads += 1
        return copy.deepcopy(self.store)

    def save_data(self, data):
        assert self.lock_depth, "write must hold the transaction lock"
        self.saves += 1
        self.store = copy.deepcopy(data)

    def _crm_contact(self, data, contact_id):
        return next((c for c in data["crm_contacts"] if c["id"] == contact_id), None)

    def _calendly_token(self):
        return "fake-for-test"

    def _crm_normalize_email(self, value):
        return str(value or "").lower()

    def _crm_now(self):
        return "2026-09-12T08:00:00+02:00"

    def _crm_calendly_fetch_contact_appointments(self, data, contact):
        assert set(data) == {"crm_calendly"}, "lookup must not hold a full database"
        assert set(contact) == {"id", "mail", "telephone"}
        assert self.loads == 0, "no mutable database copy before network I/O"
        assert self.lock_depth == 0, "never hold transaction lock during network I/O"
        self.remote_calls += 1
        return [{"id": "booking", "start_time": "2026-09-15"}], {"method": "email", "processed_events": 1}

    def _crm_upsert_calendly_appointment(self, data, payload, **kwargs):
        assert self.lock_depth
        row = dict(payload, contact_id=kwargs["contact_id"])
        data["crm_calendly_appointments"] = [row]

    def _crm_calendly_relink_appointments(self, data, contact):
        return False

    def _crm_sync_contact_calendly_status(self, data, contact):
        if data["crm_calendly_appointments"] and contact["statut"] != "RDV programmé":
            contact["statut"] = "RDV programmé"
            return True
        return False

    def _crm_calendly_status_payload(self, data):
        return dict(data["crm_calendly"])

    def _wedof_contact_resources(self, contact_id, data):
        assert data is self.store, "read-only lookup should share the snapshot"
        return [{"stable_id": "folder", "is_latest": True}]

    def _wedof_status_payload(self, test_connection=True):
        assert test_connection is False
        return {"configured": True}


class MemoryTests(unittest.TestCase):
    def get(self, legacy, refresh=True):
        return calendly_contact_get(legacy, "one", refresh, threading.BoundedSemaphore(2))

    def test_remote_lookup_only_copies_database_once_and_keeps_payload(self):
        legacy = Legacy()
        before = copy.deepcopy(legacy.store["unrelated"])
        payload, status = self.get(legacy)
        self.assertEqual(status, 200)
        self.assertEqual((legacy.loads, legacy.saves, legacy.remote_calls), (1, 1, 1))
        self.assertEqual(payload["lookup"]["matched_appointments"], 1)
        self.assertEqual(legacy.store["crm_contacts"][0]["statut"], "RDV programmé")
        self.assertEqual(legacy.store["unrelated"], before)

    def test_local_read_does_not_call_remote_or_write_when_unchanged(self):
        legacy = Legacy()
        before = copy.deepcopy(legacy.store)
        self.assertEqual(self.get(legacy, False)[1], 200)
        self.assertEqual((legacy.loads, legacy.saves, legacy.remote_calls), (1, 0, 0))
        self.assertEqual(legacy.store, before)

    def test_missing_contact_does_not_copy_database(self):
        legacy = Legacy()
        payload, status = calendly_contact_get(legacy, "missing", True, threading.BoundedSemaphore(2))
        self.assertEqual(status, 404)
        self.assertEqual(legacy.loads, 0)

    def test_remote_error_keeps_local_appointments_and_releases_capacity(self):
        legacy = Legacy()
        legacy.store["crm_calendly_appointments"] = [{"id": "old", "contact_id": "one"}]
        def failure(*args):
            raise legacy.CalendlyAPIError("temporary failure")
        legacy._crm_calendly_fetch_contact_appointments = failure
        slots = threading.BoundedSemaphore(1)
        payload, status = calendly_contact_get(legacy, "one", True, slots)
        self.assertEqual(status, 200)
        self.assertEqual(payload["appointments"][0]["id"], "old")
        self.assertIn("temporary failure", payload["integration"]["lookup_warning"])
        self.assertTrue(slots.acquire(blocking=False))

    def test_capacity_exhausted_returns_cache_not_server_error(self):
        legacy = Legacy()
        slots = SimpleNamespace(acquire=lambda **kw: False, release=lambda: self.fail("not acquired"))
        payload, status = calendly_contact_get(legacy, "one", True, slots)
        self.assertEqual(status, 200)
        self.assertEqual(legacy.remote_calls, 0)
        self.assertIn("lookup_warning", payload["integration"])

    def test_edit_during_network_wait_is_not_overwritten(self):
        legacy = Legacy()
        original = legacy._crm_calendly_fetch_contact_appointments
        def remote(*args):
            result = original(*args)
            legacy.store["crm_contacts"][0]["commentaires"] = "edit made during lookup"
            return result
        legacy._crm_calendly_fetch_contact_appointments = remote
        self.get(legacy)
        self.assertEqual(legacy.store["crm_contacts"][0]["commentaires"], "edit made during lookup")

    def test_contact_deleted_during_lookup_is_not_recreated(self):
        legacy = Legacy()
        original = legacy._crm_calendly_fetch_contact_appointments
        def remote(*args):
            result = original(*args)
            legacy.store["crm_contacts"] = []
            return result
        legacy._crm_calendly_fetch_contact_appointments = remote
        self.assertEqual(self.get(legacy)[1], 404)
        self.assertEqual(legacy.saves, 0)

    def test_post_and_authentication_are_preserved(self):
        legacy = Legacy()
        install_crm_memory_optimizations(legacy)
        route = legacy.app.view_functions["crm_contact_calendly_appointments"]
        legacy.request.method = "POST"
        self.assertEqual(route("one"), ({"booking": "one"}, 201))
        legacy.authenticated = False
        self.assertEqual(route("one")[1], 401)
        self.assertEqual(legacy.original_posts, 1)

    def test_wedof_read_avoids_all_database_copies(self):
        legacy = Legacy()
        before = copy.deepcopy(legacy.store)
        install_crm_memory_optimizations(legacy)
        payload = legacy.app.view_functions["crm_contact_wedof"]("one")
        self.assertEqual(payload["resources"][0]["stable_id"], "folder")
        self.assertEqual(legacy.loads, 0)
        self.assertEqual(legacy.store, before)

    def test_install_is_idempotent(self):
        legacy = Legacy()
        install_crm_memory_optimizations(legacy)
        route = legacy.app.view_functions["crm_contact_calendly_appointments"]
        install_crm_memory_optimizations(legacy)
        self.assertIs(route, legacy.app.view_functions["crm_contact_calendly_appointments"])

    def test_duplicate_calls_share_one_work_item_and_no_result_cache(self):
        flights = InFlightRequests()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def work():
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(3))
            return {"ok": True}
        with ThreadPoolExecutor(max_workers=5) as pool:
            first = pool.submit(flights.run, "same", work)
            self.assertTrue(entered.wait(2))
            others = [pool.submit(flights.run, "same", work) for _ in range(4)]
            time.sleep(0.1)
            release.set()
            self.assertTrue(all(f.result()["ok"] for f in [first, *others]))
        self.assertEqual(len(calls), 1)
        self.assertEqual(flights._pending, {})
        flights.run("same", lambda: calls.append(1))
        self.assertEqual(len(calls), 2)

    def test_failed_work_does_not_leak_or_poison_next_lookup(self):
        flights = InFlightRequests()
        def failure():
            raise ValueError("test")
        with self.assertRaises(ValueError):
            flights.run("one", failure)
        self.assertEqual(flights._pending, {})
        self.assertEqual(flights.run("one", lambda: 42), 42)

    def test_capacity_is_bounded(self):
        flights = InFlightRequests(capacity=0)
        with self.assertRaises(LookupBusy):
            flights.run("one", lambda: 42)
        self.assertEqual(flights._pending, {})

    def test_real_legacy_get_response_parity(self):
        # In repository CI, execute the actual original GET branch with the
        # same deterministic helpers. No application import/network/secrets.
        path = Path(__file__).resolve().parents[1] / "app.py"
        if not path.exists():
            self.skipTest("full repository checked by GitHub CI")
        node = next(n for n in ast.parse(path.read_text()).body
                    if isinstance(n, ast.FunctionDef) and n.name == "crm_contact_calendly_appointments")
        node.decorator_list = []
        old = Legacy()
        old.request.args = {"refresh": "1"}
        # Legacy intentionally made the first full copy before the lookup.
        def old_remote(data, contact):
            return [{"id": "booking", "start_time": "2026-09-15"}], {"method": "email", "processed_events": 1}
        old._crm_calendly_fetch_contact_appointments = old_remote
        def old_save(data):
            old.store = copy.deepcopy(data)
        old.save_data = old_save
        old._crm_upsert_calendly_appointment = lambda data, payload, **kw: data.update(
            crm_calendly_appointments=[dict(payload, contact_id=kw["contact_id"])])
        namespace = {name: getattr(old, name) for name in dir(old) if not name.startswith("__")}
        tree = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
        exec(compile(tree, str(path), "exec"), namespace)
        expected = namespace["crm_contact_calendly_appointments"]("one")
        new = Legacy()
        actual, status = self.get(new)
        self.assertEqual(status, 200)
        self.assertEqual(actual, expected)
        self.assertEqual((old.loads, new.loads), (2, 1))


if __name__ == "__main__":
    unittest.main()
