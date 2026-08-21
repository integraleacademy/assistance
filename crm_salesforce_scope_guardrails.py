"""Périmètre métier définitif de la migration Salesforce.

La migration destinée à la sortie de Salesforce ne reprend que les pistes dont
la date de création appartient à l'année civile 2026 en France. Les formations
BTS et CAP sont exclues, quel que soit leur libellé précis.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from functools import wraps
from typing import Any, Iterable

import pytz


MIGRATION_YEAR = 2026
EXCLUDED_FORMATION_FAMILIES = ("BTS", "CAP")
_PARIS_TZ = pytz.timezone("Europe/Paris")
_FORMATION_FIELDS = (
    "Type_de_formation__c",
    "Type de formation",
    "Formation",
    "Company",
    "Société",
)
_BTS_SHORT_LABELS = ("mos", "mco", "ndrc", "ci", "pi", "cg")
_CAP_SHORT_LABELS = (
    "aepe",
    "boulangerie",
    "coiffure",
    "cuisine",
    "patisserie",
)


def _source_formation(migration_module, row: dict[str, Any]) -> str:
    return migration_module._row_value(row, *_FORMATION_FIELDS)


def _starts_with_training_label(value: str, labels: tuple[str, ...]) -> bool:
    return any(value == label or value.startswith(f"{label} ") for label in labels)


def _is_excluded_formation(migration_module, value: Any) -> bool:
    """Reconnaît les familles BTS/CAP, y compris leurs libellés abrégés."""
    folded = migration_module._fold(value)
    if re.search(r"(?:^|\s)(?:bts|cap)(?:\s|$)", folded):
        return True

    # Les anciennes listes Salesforce utilisent parfois seulement « MOS »,
    # « MCO », « AEPE » ou « Pâtisserie », sans préfixe BTS/CAP.
    without_year = re.sub(r"(?:^|\s)20\d{2}(?:\s|$)", " ", folded)
    without_year = re.sub(r"\s+", " ", without_year).strip()
    return (
        _starts_with_training_label(without_year, _BTS_SHORT_LABELS)
        or _starts_with_training_label(without_year, _CAP_SHORT_LABELS)
    )


def _created_in_migration_year(migration_module, row: dict[str, Any]) -> bool:
    raw = migration_module._row_value(
        row,
        "CreatedDate",
        "Date de création",
    )
    parsed = migration_module._parse_datetime(raw)
    if parsed is None:
        return False
    if parsed.tzinfo is None:
        parsed = pytz.UTC.localize(parsed)
    return parsed.astimezone(_PARIS_TZ).year == MIGRATION_YEAR


def filter_salesforce_scope(
    migration_module,
    rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Filtre le périmètre avant tout rapprochement avec les fiches du CRM."""
    retained: list[dict[str, Any]] = []
    stats: Counter[str] = Counter()

    for row in rows:
        # Laisser le moteur principal comptabiliser correctement les suppressions.
        if migration_module._truthy(migration_module._row_value(
            row,
            "IsDeleted",
            "Supprimé",
        )):
            retained.append(row)
            continue

        if not _created_in_migration_year(migration_module, row):
            stats["skipped_other_year"] += 1
            continue

        if _is_excluded_formation(
            migration_module,
            _source_formation(migration_module, row),
        ):
            stats["skipped_formation"] += 1
            continue

        retained.append(row)

    return retained, dict(stats)


def install_salesforce_scope_guardrails(migration_module) -> None:
    """Applique le périmètre 2026 hors BTS/CAP au moteur de migration complet."""
    if getattr(migration_module, "_scope_guardrails_installed", False):
        return

    original_prepare = migration_module._prepare_complete_rows
    original_import = migration_module.import_complete_rows

    def scoped_prepare(rows, *args, **kwargs):
        scoped_rows, scope_stats = filter_salesforce_scope(
            migration_module,
            rows,
        )
        prepared, stats = original_prepare(scoped_rows, *args, **kwargs)
        stats = dict(stats)
        for key, value in scope_stats.items():
            stats[key] = int(stats.get(key) or 0) + int(value or 0)
        return prepared, stats

    def scoped_import(*args, **kwargs):
        result = original_import(*args, **kwargs)
        result["scope_year"] = MIGRATION_YEAR
        result["excluded_formation_families"] = list(
            EXCLUDED_FORMATION_FAMILIES
        )
        return result

    migration_module._prepare_complete_rows = scoped_prepare
    migration_module.import_complete_rows = scoped_import
    migration_module._scope_guardrails_installed = True


def enforce_salesforce_scope_route(
    app,
    *,
    request: Any,
    jsonify_fn,
    endpoint: str = "crm_migrate_salesforce",
) -> None:
    """Interdit l'ancien mode 2025 sur la nouvelle route de migration."""
    marker = f"_{endpoint}_scope_guardrail_installed"
    if getattr(app, marker, False):
        return

    view = app.view_functions.get(endpoint)
    if view is None:
        raise RuntimeError(
            "La route Salesforce doit être enregistrée avant son verrouillage."
        )

    @wraps(view)
    def scoped_view(*args, **kwargs):
        mode = str(request.form.get("mode", "complete") or "complete").strip()
        if mode != "complete":
            return jsonify_fn({
                "error": (
                    "Cette migration est limitée aux pistes créées en 2026, "
                    "hors formations BTS et CAP."
                )
            }), 400

        for field in ("created_from", "created_to"):
            raw = str(request.form.get(field, "") or "").strip()
            if not raw:
                continue
            try:
                parsed = dt.date.fromisoformat(raw[:10])
            except ValueError:
                return jsonify_fn({"error": f"Date de filtre invalide : {raw}"}), 400
            if parsed.year != MIGRATION_YEAR:
                return jsonify_fn({
                    "error": "Les éventuels filtres de dates doivent rester en 2026."
                }), 400

        return view(*args, **kwargs)

    app.view_functions[endpoint] = scoped_view
    setattr(app, marker, True)


def disable_legacy_salesforce_import(
    app,
    *,
    jsonify_fn,
    endpoint: str = "crm_import_salesforce",
) -> None:
    """Bloque l'ancienne route 2025 afin qu'elle ne contourne pas le périmètre."""
    marker = f"_{endpoint}_disabled_for_2026_migration"
    if getattr(app, marker, False):
        return

    view = app.view_functions.get(endpoint)
    if view is None:
        raise RuntimeError(
            "L'ancienne route Salesforce doit être enregistrée avant sa désactivation."
        )

    @wraps(view)
    def disabled_view(*args, **kwargs):
        return jsonify_fn({
            "error": (
                "L'ancien import Salesforce 2025 est désactivé. Utilisez "
                "« Importer Salesforce 2026 » : seules les pistes 2026 hors "
                "BTS et CAP sont autorisées."
            )
        }), 410

    app.view_functions[endpoint] = disabled_view
    setattr(app, marker, True)
