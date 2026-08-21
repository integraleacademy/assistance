import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace

import crm_salesforce_migration as migration_template
from crm_salesforce_date_guardrails import install_salesforce_date_guardrails
from crm_salesforce_migration_guardrails import install_salesforce_migration_guardrails
from crm_salesforce_scope_guardrails import (
    MIGRATION_YEAR,
    disable_legacy_salesforce_import,
    enforce_salesforce_scope_route,
    install_salesforce_scope_guardrails,
)
from crm_salesforce_status_guardrails import install_salesforce_status_guardrails


def _fresh_scoped_migration():
    """Charge le moteur sous un nom isolé pour ne pas modifier les autres tests."""
    module_name = f"crm_salesforce_migration_scope_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(migration_template.__file__),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    install_salesforce_migration_guardrails(module)
    install_salesforce_status_guardrails(module)
    install_salesforce_date_guardrails(module)
    install_salesforce_scope_guardrails(module)
    return module


def _row(identifier, created_at, formation="A3P", **extra):
    return {
        "Id": identifier,
        "FirstName": "Lina",
        "LastName": identifier,
        "Email": f"{identifier}@example.com",
        "CreatedDate": created_at,
        "Type_de_formation__c": formation,
        **extra,
    }


def test_only_the_2026_calendar_year_in_france_is_imported():
    migration = _fresh_scoped_migration()
    rows = [
        _row("before", "2025-12-31T22:30:00Z"),
        _row("first-hour", "2025-12-31T23:30:00Z"),
        _row("last-hour", "2026-12-31T22:30:00Z"),
        _row("after", "2026-12-31T23:30:00Z"),
    ]

    result = migration.import_complete_rows([], rows, dry_run=True)

    assert result["scope_year"] == MIGRATION_YEAR == 2026
    assert result["prepared_rows"] == 2
    assert result["created"] == 2
    assert result["skipped_other_year"] == 2
    assert result["year_counts"] == {"2026": 2}


def test_bts_and_cap_are_excluded_whatever_the_precise_label():
    migration = _fresh_scoped_migration()
    rows = [
        _row("a3p", "2026-05-10T10:00:00Z", "A3P"),
        _row("bts", "2026-05-10T10:00:00Z", "BTS MOS 2026"),
        _row("cap-aepe", "2026-05-10T10:00:00Z", "CAP AEPE"),
        _row("cap-company", "2026-05-10T10:00:00Z", "", Company="CAP Cuisine"),
        _row(
            "capacity",
            "2026-05-10T10:00:00Z",
            "Capacité professionnelle sécurité",
        ),
    ]

    result = migration.import_complete_rows([], rows, dry_run=True)

    assert result["prepared_rows"] == 2
    assert result["created"] == 2
    assert result["skipped_formation"] == 3
    assert result["formation_counts"] == {
        "A3P": 1,
        "Capacité professionnelle sécurité": 1,
    }
    assert result["excluded_formation_families"] == ["BTS", "CAP"]


def test_deleted_rows_remain_counted_as_deleted_before_scope_filtering():
    migration = _fresh_scoped_migration()
    rows = [
        _row(
            "deleted-old",
            "2024-01-01T10:00:00Z",
            "BTS MCO",
            IsDeleted="1",
        )
    ]

    result = migration.import_complete_rows([], rows, dry_run=True)

    assert result["prepared_rows"] == 0
    assert result["skipped_deleted"] == 1
    assert result["skipped_other_year"] == 0
    assert result["skipped_formation"] == 0


class _FakeApp:
    def __init__(self, endpoint, view):
        self.view_functions = {endpoint: view}


def _jsonify(payload):
    return payload


def test_route_rejects_the_old_2025_mode():
    app = _FakeApp("crm_migrate_salesforce", lambda: "ok")
    request = SimpleNamespace(form={"mode": "legacy_2025"})

    enforce_salesforce_scope_route(
        app,
        request=request,
        jsonify_fn=_jsonify,
    )

    payload, status = app.view_functions["crm_migrate_salesforce"]()
    assert status == 400
    assert "2026" in payload["error"]
    assert "BTS" in payload["error"]
    assert "CAP" in payload["error"]


def test_route_accepts_complete_mode_with_2026_date_bounds():
    app = _FakeApp("crm_migrate_salesforce", lambda: "ok")
    request = SimpleNamespace(form={
        "mode": "complete",
        "created_from": "2026-01-01",
        "created_to": "2026-12-31",
    })

    enforce_salesforce_scope_route(
        app,
        request=request,
        jsonify_fn=_jsonify,
    )

    assert app.view_functions["crm_migrate_salesforce"]() == "ok"


def test_route_rejects_a_date_filter_outside_2026():
    app = _FakeApp("crm_migrate_salesforce", lambda: "ok")
    request = SimpleNamespace(form={
        "mode": "complete",
        "created_from": "2025-01-01",
    })

    enforce_salesforce_scope_route(
        app,
        request=request,
        jsonify_fn=_jsonify,
    )

    payload, status = app.view_functions["crm_migrate_salesforce"]()
    assert status == 400
    assert "2026" in payload["error"]


def test_legacy_import_endpoint_is_disabled_in_the_production_entrypoint():
    app = _FakeApp("crm_import_salesforce", lambda: "legacy")

    disable_legacy_salesforce_import(
        app,
        jsonify_fn=_jsonify,
    )

    payload, status = app.view_functions["crm_import_salesforce"]()
    assert status == 410
    assert "désactivé" in payload["error"]
    assert "2026" in payload["error"]
    assert "BTS" in payload["error"]
    assert "CAP" in payload["error"]
