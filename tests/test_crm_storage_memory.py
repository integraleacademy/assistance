"""Saving a large CRM must not allocate a third copy of unchanged histories."""
import copy
import json
import logging
import runpy
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from crm_storage_snapshot import storage_snapshot
from crm_read_cache import LastProjection
import crm_memory_monitor as monitor


def test_save_snapshot_shares_only_old_unchanged_subtrees():
    old = {"contacts": [{"id": "a", "history": [{"note": "first"}]},
                        {"id": "b", "history": [{"note": "second"}]}],
           "settings": {"options": ["one"]}}
    incoming = copy.deepcopy(old)
    incoming["contacts"][0]["name"] = "Updated"
    incoming["contacts"].append({"id": "c", "history": [{"note": "new"}]})
    new = storage_snapshot(incoming, old)
    assert new == incoming
    assert new["contacts"] is not old["contacts"]
    assert new["contacts"][0] is not old["contacts"][0]
    assert new["contacts"][0]["history"] is old["contacts"][0]["history"]
    assert new["contacts"][1] is old["contacts"][1]
    assert new["settings"] is old["settings"]
    expected = copy.deepcopy(new)
    incoming["contacts"][0]["history"][0]["note"] = "Not saved"
    incoming["contacts"][1]["history"].clear()
    incoming["contacts"][2]["history"][0]["note"] = "Also not saved"
    incoming["settings"]["options"].clear()
    assert new == expected
    assert "name" not in old["contacts"][0]
    assert len(old["contacts"]) == 2


@pytest.mark.parametrize("before,after", [
    ({"nested": [True]}, {"nested": [1]}),
    ({"nested": [1]}, {"nested": [1.0]}),
    ({"nested": [0.0]}, {"nested": [-0.0]}),
    ({"a": 1, "b": 2}, {"b": 2, "a": 1}),
    ({"rows": [{"id": "a"}, {"id": "b"}]}, {"rows": [{"id": "b"}]}),
    ([{"id": "a"}, {"id": "b"}], [{"id": "b"}, {"id": "a"}]),
    ([{"id": "a"}], [{"id": "new"}, {"id": "a"}]),
    ({"deleted": [1]}, {}),
    ([], {"new": []}),
])
def test_snapshot_preserves_json_types_deletions_and_order(before, after):
    original = copy.deepcopy(before)
    result = storage_snapshot(after, before)
    assert json.dumps(result) == json.dumps(after)
    assert before == original


def test_history_size_does_not_determine_new_snapshot_allocations():
    old = {"contacts": [{"id": str(i), "history": [{"note": str(j)} for j in range(100)]}
                        for i in range(200)]}
    incoming = copy.deepcopy(old)
    incoming["contacts"][101]["name"] = "Changed"
    result = storage_snapshot(incoming, old)
    assert sum(a is b for a, b in zip(result["contacts"], old["contacts"])) == 199
    assert all(a["history"] is b["history"] for a, b in zip(result["contacts"], old["contacts"]))


def test_real_save_keeps_callers_old_readers_and_disk_isolated(tmp_path, monkeypatch):
    from crm_app import legacy_app as crm
    monkeypatch.setattr(crm, "DATA_FILE", str(tmp_path / "crm.json"))
    monkeypatch.setattr(crm, "_DATA_CACHE_PAYLOAD", None)
    monkeypatch.setattr(crm, "_DATA_CACHE_SIGNATURE", None)
    data = copy.deepcopy(crm.DEFAULT_DATA)
    data["crm_contacts"] = [{"id": "a", "activities": [{"detail": "Historical email"}]},
                            {"id": "b", "activities": [{"detail": "Historical call"}]}]
    crm.save_data(data)
    old = crm._load_data_snapshot()
    data = crm.load_data()
    data["crm_contacts"][0]["commentaires"] = "Saved"
    crm.save_data(data)
    new = crm._load_data_snapshot()
    assert new["crm_contacts"][1] is old["crm_contacts"][1]
    assert "commentaires" not in old["crm_contacts"][0]
    expected = copy.deepcopy(new)
    data["crm_contacts"][0]["activities"].clear()
    data["crm_contacts"][1]["activities"].clear()
    assert new == expected
    assert json.loads(Path(crm.DATA_FILE).read_text()) == expected
    mutable = crm.load_data()
    mutable["crm_contacts"].clear()
    assert crm._load_data_snapshot() == expected


def test_failed_disk_save_keeps_cached_revision(tmp_path, monkeypatch):
    import app as crm
    monkeypatch.setattr(crm, "DATA_FILE", str(tmp_path / "crm.json"))
    crm.save_data(copy.deepcopy(crm.DEFAULT_DATA))
    old = crm._load_data_snapshot()
    incoming = crm.load_data()
    incoming["compteur_traitees"] = 321

    def fail(*args):
        raise OSError("simulated disk error")

    monkeypatch.setattr(crm.os, "replace", fail)
    with pytest.raises(OSError):
        crm.save_data(incoming)
    assert crm._load_data_snapshot() is old
    assert crm.load_data()["compteur_traitees"] != 321


def test_obsolete_projection_is_released_before_rebuilding():
    cache = LastProjection()
    cache.get("contacts", 1, lambda: {"large": []})

    def rebuild():
        assert "contacts" not in cache.values
        return {"new": True}

    assert cache.get("contacts", 2, rebuild) == {"new": True}


def test_memory_samples_support_container_limits(monkeypatch):
    values = {"/proc/self/statm": "1000 256", "/sys/fs/cgroup/memory.current": str(512 * 2**20),
              "/sys/fs/cgroup/memory.max": str(2048 * 2**20)}
    monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: values[str(self)])
    monkeypatch.setattr(monitor.os, "sysconf", lambda _: 4096)
    sample = monitor.memory_sample()
    assert sample["rss_mib"] == 1.0
    assert sample["container_mib"] == 512
    assert sample["limit_mib"] == 2048


def test_periodic_memory_samples_run_without_http_requests():
    received = threading.Event()
    records = []

    def log(*args):
        records.append(args)
        if len(records) >= 2:
            received.set()

    stop = monitor.start_memory_monitor(SimpleNamespace(info=log), interval=0.01)
    try:
        assert received.wait(2)
        assert all(row[1] == "periodic" for row in records)
    finally:
        stop.set()


def test_memory_phase_logs_even_on_failure(caplog):
    with caplog.at_level(logging.INFO, logger="gunicorn.error"):
        with pytest.raises(RuntimeError):
            with monitor.memory_phase("edof_test"):
                raise RuntimeError("not swallowed")
    assert "phase=edof_test_start" in caplog.text
    assert "phase=edof_test_end" in caplog.text


def test_worker_hook_starts_poller_and_monitor_after_fork(monkeypatch):
    started = []
    fake_app = SimpleNamespace(_start_wedof_background_sync=lambda: started.append("edof"),
                               _start_calendly_link_review=lambda: started.append("calendly"),
                               run_hebergement_reminders=lambda: None)
    monkeypatch.setitem(sys.modules, "app", fake_app)
    monkeypatch.setattr(monitor, "start_memory_monitor", lambda logger: started.append("memory"))
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self.name))
    config = runpy.run_path(str(Path(__file__).resolve().parents[1] / "gunicorn.conf.py"))
    config["post_worker_init"](SimpleNamespace(log=None))
    assert started == ["edof", "calendly", "memory", "hebergement-rappels"]
