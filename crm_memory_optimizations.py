"""Bound CRM allocation peaks without changing stored data or booking behavior.

Installed before the existing CRM guardrails. Read-only WEDOF access never
copies the database. Calendly copies only lookup credentials/contact identity
while doing network I/O, then reloads one mutable database under the existing
transaction lock. Concurrent identical GETs share only their in-flight result;
there is deliberately no persistent response cache and POST is untouched.
"""
from __future__ import annotations

import copy
import os
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeout
from functools import wraps

VERSION = "20260912-memory-1"
_TRUE = {"1", "true", "yes", "oui"}


class LookupBusy(RuntimeError):
    """The bounded lookup capacity is occupied; local data remains available."""


class InFlightRequests:
    """Coalesce concurrent work and release every entry on success or failure."""

    def __init__(self, capacity=32, wait_seconds=110):
        self.capacity = capacity
        self.wait_seconds = wait_seconds
        self._lock = threading.Lock()
        self._pending = {}

    def run(self, key, callback):
        with self._lock:
            future = self._pending.get(key)
            leader = future is None
            if leader:
                if len(self._pending) >= self.capacity:
                    raise LookupBusy("Actualisations en cours. Réessayez dans quelques instants.")
                future = Future()
                self._pending[key] = future
        if not leader:
            try:
                return future.result(timeout=self.wait_seconds)
            except FutureTimeout as exc:
                raise LookupBusy("Actualisation encore en cours. Les données enregistrées restent disponibles.") from exc
        try:
            result = callback()
            future.set_result(result)
            return result
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                if self._pending.get(key) is future:
                    del self._pending[key]


def _lookup_identity(legacy, contact_id):
    """Never hold a complete database snapshot across a remote API request."""
    snapshot = legacy._load_data_snapshot()
    contact = legacy._crm_contact(snapshot, contact_id)
    if contact is None:
        return None, None
    identity = {key: contact.get(key) for key in ("id", "mail", "telephone")}
    # The remote lookup uses only this state, not contacts/history/templates.
    context = {"crm_calendly": copy.deepcopy(snapshot.get("crm_calendly") or {})}
    return context, identity


def _finish_calendly_lookup(legacy, contact_id, fetched, lookup, succeeded, warning):
    # Preserve reconciliation, cancelled appointments, follow-ups and statuses.
    # Load AFTER network I/O, under the same lock as existing CRM writes: never
    # overwrite a user's edit with the snapshot captured before a slow lookup.
    with legacy._CRM_RECONCILIATION_LOCK:
        data = legacy.load_data()
        contact = legacy._crm_contact(data, contact_id)
        if contact is None:
            return {"error": "Contact introuvable"}, 404
        changed = False
        for payload in fetched:
            legacy._crm_upsert_calendly_appointment(
                data, payload, source="targeted_lookup", contact_id=contact_id,
                record_activity=False,
            )
            changed = True
        if legacy._crm_calendly_relink_appointments(data, contact):
            changed = True
        if legacy._crm_sync_contact_calendly_status(data, contact):
            changed = True
        if succeeded:
            data.setdefault("crm_calendly", {})["last_sync_at"] = legacy._crm_now()
            changed = True
        if changed:
            legacy.save_data(data)
        appointments = [item for item in data.get("crm_calendly_appointments", [])
                        if item.get("contact_id") == contact_id]
        appointments.sort(key=lambda item: item.get("start_time") or "", reverse=True)
        integration = legacy._crm_calendly_status_payload(data)
        if warning:
            integration["lookup_warning"] = warning
        lookup = dict(lookup, matched_appointments=len(appointments))
        # These small response fields do not retain the full database object.
        return {"appointments": appointments, "integration": integration,
                "lookup": lookup}, 200


def calendly_contact_get(legacy, contact_id, refresh, remote_slots):
    context, contact = _lookup_identity(legacy, contact_id)
    if contact is None:
        return {"error": "Contact introuvable"}, 404
    fetched = []
    lookup = {"method": "local", "processed_events": 0}
    warning = ""
    succeeded = False
    state = context["crm_calendly"]
    can_lookup = bool(legacy._calendly_token() and state.get("user")
                      and state.get("organization")
                      and legacy._crm_normalize_email(contact.get("mail")))
    if refresh and can_lookup:
        acquired = remote_slots.acquire(timeout=10)
        if not acquired:
            warning = "Actualisations Calendly en cours. Les rendez-vous enregistrés restent affichés ; réessayez dans quelques instants."
        else:
            try:
                fetched, lookup = legacy._crm_calendly_fetch_contact_appointments(context, contact)
                succeeded = True
            except (legacy.CalendlyAPIError, RuntimeError) as exc:
                warning = str(exc)
            finally:
                remote_slots.release()
    return _finish_calendly_lookup(legacy, contact_id, fetched, lookup, succeeded, warning)


def install_crm_memory_optimizations(legacy):
    app = legacy.app
    if app.extensions.get("crm_memory_optimizations"):
        return
    if str(os.getenv("CRM_MEMORY_OPTIMIZATIONS", "true")).lower() in {"0", "false", "off"}:
        return
    flights = InFlightRequests()
    remote_slots = threading.BoundedSemaphore(2)
    original_calendly = app.view_functions["crm_contact_calendly_appointments"]

    @legacy.login_required
    @wraps(original_calendly)
    def contact_calendly(contact_id):
        if legacy.request.method != "GET":
            return original_calendly(contact_id)
        refresh = str(legacy.request.args.get("refresh") or "").strip().lower() in _TRUE
        # Authenticate every request BEFORE coalescing. Scope by logged-in user
        # as well as contact; never share a Flask Response/session across threads.
        user = legacy.current_user() or {}
        key = (str(user.get("email") or user.get("id") or user.get("name") or ""),
               str(contact_id), refresh)
        try:
            payload, status = flights.run(
                key, lambda: calendly_contact_get(legacy, contact_id, refresh, remote_slots),
            )
        except LookupBusy as exc:
            payload, status = calendly_contact_get(legacy, contact_id, False, remote_slots)
            if status == 200:
                payload["integration"]["lookup_warning"] = str(exc)
        return legacy.jsonify(payload), status

    original_wedof = app.view_functions["crm_contact_wedof"]

    @legacy.login_required
    @wraps(original_wedof)
    def contact_wedof(contact_id):
        # Both helpers are read-only. Keep all contacts for duplicate-name
        # detection; truncating this snapshot would change WEDOF matching.
        data = legacy._load_data_snapshot()
        if not legacy._crm_contact(data, contact_id):
            return legacy.jsonify({"error": "Contact introuvable"}), 404
        return legacy.jsonify({
            "resources": legacy._wedof_contact_resources(contact_id, data),
            "status": legacy._wedof_status_payload(test_connection=False),
        })

    app.view_functions["crm_contact_calendly_appointments"] = contact_calendly
    app.view_functions["crm_contact_wedof"] = contact_wedof
    legacy.crm_contact_calendly_appointments = contact_calendly
    legacy.crm_contact_wedof = contact_wedof
    app.extensions["crm_memory_optimizations"] = {"version": VERSION, "flights": flights}
    app.logger.info("crm_memory_optimizations enabled version=%s remote_concurrency=2", VERSION)
