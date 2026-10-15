"""Bounded, process-local caches for read-only CRM projections."""
from collections import OrderedDict
import copy
import hashlib
import json
import threading


class LastProjection:
    """Keep one result per partition; callers pass immutable dependencies."""

    def __init__(self):
        self.lock = threading.RLock()
        self.values = {}

    def get(self, partition, dependencies, build, *, immutable=False):
        with self.lock:
            previous = self.values.get(partition)
            if previous is not None and previous[0] == dependencies:
                return previous[1]
            result = build()
            self.values[partition] = (
                dependencies if immutable else copy.deepcopy(dependencies), result,
            )
            return result


class UpdateSnapshots:
    """Return changed rows, with a complete fallback after eviction/restart.

    Cursors are bound to the authenticated user, section and selected contact.
    Keep at most 16 snapshots / 8 MiB; no persistent CRM data is introduced.
    """

    def __init__(self, capacity=16, max_bytes=8 * 1024 * 1024):
        self.capacity = capacity
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.snapshots = OrderedDict()
        self.size = 0

    def response(self, payload, cursor, scope):
        for field in ("contacts", "appointments"):
            identifiers = [str(row.get("id") or "") for row in payload[field]]
            if "" in identifiers or len(set(identifiers)) != len(identifiers):
                # Legacy unkeyed rows cannot be merged safely by the client.
                return dict(payload, cursor="", delta=False)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        token = hashlib.sha256(
            (json.dumps(scope) + "\n" + encoded).encode("utf-8")
        ).hexdigest()
        with self.lock:
            prior = self.snapshots.get(cursor)
            previous = prior[1] if prior and prior[0] == scope else None
            if token not in self.snapshots and len(encoded) <= self.max_bytes:
                self.snapshots[token] = (scope, json.loads(encoded), len(encoded))
                self.size += len(encoded)
                while len(self.snapshots) > self.capacity or self.size > self.max_bytes:
                    _, evicted = self.snapshots.popitem(last=False)
                    self.size -= evicted[2]
            elif token in self.snapshots:
                self.snapshots.move_to_end(token)
        if previous is None:
            return dict(payload, cursor=token, delta=False)
        if cursor == token:
            return {"cursor": token, "unchanged": True}
        result = {"cursor": token, "delta": True}
        for field in ("contacts", "appointments"):
            before = {str(row["id"]): row for row in previous[field]}
            after = {str(row["id"]): row for row in payload[field]}
            changed = [row for key, row in after.items() if before.get(key) != row]
            removed = [key for key in before if key not in after]
            if changed or removed:
                result[field] = changed
                result[f"removed_{field}"] = removed
        for field in payload.keys() - {"contacts", "appointments"}:
            if payload[field] != previous.get(field):
                result[field] = payload[field]
        return result
