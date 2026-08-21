"""Périmètre métier définitif de la migration Salesforce.

La migration destinée à la sortie de Salesforce ne reprend que les pistes dont
la date de création appartient à l'année civile 2026 en France. Les pistes
disqualifiées, les pistes « Open - Not Contacted » sans formation exploitable,
les fiches internes ou de test ainsi que les formations BTS et CAP sont exclues
avant tout rapprochement avec les fiches du CRM.
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
EXCLUDED_STATUS_LABELS = ("Disqualifié",)
EXCLUDED_TEST_LABELS = ("TEST APS",)
EXCLUDED_INTERNAL_RECORDS = ("Cassandre MENARD",)
EXCLUDED_OPEN_WITHOUT_FORMATION_LABELS = (
    "Open - Not Contacted sans formation",
)
_PARIS_TZ = pytz.timezone("Europe/Paris")
_EXPLICIT_FORMATION_FIELDS = (
    "Type_de_formation__c",
    "Type de formation",
    "Formation",
)
_COMPANY_FIELDS = (
    "Company",
    "Société",
    "Société/Compte",
)
_FORMATION_FIELDS = (*_EXPLICIT_FORMATION_FIELDS, *_COMPANY_FIELDS)
_STATUS_FIELDS = (
    "Status",
    "Statut",
    "Statut de la piste",
)
_BTS_SHORT_LABELS = ("mos", "mco", "ndrc", "ci", "pi", "cg")
_CAP_SHORT_LABELS = (
    "aepe",
    "boulangerie",
    "coiffure",
    "cuisine",
    "patisserie",
)
_DISQUALIFIED_STATUS_ALIASES = {
    "disqualifie",
    "unqualified",
    "closed not converted",
    "closed lost",
}
_OPEN_NOT_CONTACTED_ALIASES = {
    "open not contacted",
}
_EXCLUDED_TEST_ALIASES = {"test aps"}
_EXCLUDED_INTERNAL_SALESFORCE_IDS = {
    # Fiche Calendly interne de Cassandre MENARD présente dans l'export réel.
    "00QSa00000ZsDiU",
}
_NO_FORMATION_PLACEHOLDERS = {
    "",
    "company placeholder",
    "particulier",
    "integrale academy",
    "integrale securite formations",
}


def _source_formation(migration_module, row: dict[str, Any]) -> str:
    return migration_module._row_value(row, *_FORMATION_FIELDS)


def _source_status(migration_module, row: dict[str, Any]) -> str:
    return migration_module._row_value(row, *_STATUS_FIELDS)


def _source_salesforce_id(migration_module, row: dict[str, Any]) -> str:
    return migration_module._row_value(
        row,
        "Id",
        "ID de piste",
        "Lead ID",
        "Salesforce ID",
    )


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


def _is_disqualified(migration_module, row: dict[str, Any]) -> bool:
    """Refuse les variantes françaises et anglaises d'une piste disqualifiée."""
    status = migration_module._fold(_source_status(migration_module, row))
    return status in _DISQUALIFIED_STATUS_ALIASES


def _is_excluded_test_record(migration_module, row: dict[str, Any]) -> bool:
    """Écarte uniquement le libellé de test explicite, sans viser les vrais APS."""
    return any(
        migration_module._fold(
            migration_module._row_value(row, field)
        ) in _EXCLUDED_TEST_ALIASES
        for field in _FORMATION_FIELDS
    )


def _is_excluded_internal_record(migration_module, row: dict[str, Any]) -> bool:
    """Écarte uniquement la fiche Salesforce interne validée par la direction."""
    return _source_salesforce_id(
        migration_module,
        row,
    ) in _EXCLUDED_INTERNAL_SALESFORCE_IDS


def _has_usable_formation(migration_module, row: dict[str, Any]) -> bool:
    """Distingue une vraie formation d'un libellé Salesforce générique."""
    explicit = migration_module._fold(migration_module._row_value(
        row,
        *_EXPLICIT_FORMATION_FIELDS,
    ))
    if explicit and explicit not in _NO_FORMATION_PLACEHOLDERS:
        return True

    company = migration_module._fold(migration_module._row_value(
        row,
        *_COMPANY_FIELDS,
    ))
    return bool(company and company not in _NO_FORMATION_PLACEHOLDERS)


def _is_open_without_formation(migration_module, row: dict[str, Any]) -> bool:
    """Écarte les anciennes pistes Calendly sans formation identifiable."""
    status = migration_module._fold(_source_status(migration_module, row))
    return (
        status in _OPEN_NOT_CONTACTED_ALIASES
        and not _has_usable_formation(migration_module, row)
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

        # Priorité au statut : une piste disqualifiée n'est jamais importée,
        # même lorsqu'elle appartient aussi à une formation BTS ou CAP.
        if _is_disqualified(migration_module, row):
            stats["skipped_disqualified"] += 1
            continue

        # La fiche TEST APS du fichier transmis est exclue explicitement.
        if _is_excluded_test_record(migration_module, row):
            stats["skipped_test"] += 1
            continue

        # La fiche Calendly interne de Cassandre est exclue par son identifiant
        # Salesforce exact afin de ne jamais viser une homonyme légitime.
        if _is_excluded_internal_record(migration_module, row):
            stats["skipped_internal"] += 1
            continue

        if _is_excluded_formation(
            migration_module,
            _source_formation(migration_module, row),
        ):
            stats["skipped_formation"] += 1
            continue

        # Les 37 anciennes pistes « Open - Not Contacted » du rapport réel ne
        # contiennent aucune formation exploitable : elles ne doivent pas créer
        # artificiellement des fiches « Nouveaux » dans le nouveau CRM.
        if _is_open_without_formation(migration_module, row):
            stats["skipped_open_without_formation"] += 1
            continue

        retained.append(row)

    return retained, dict(stats)


def install_salesforce_scope_guardrails(migration_module) -> None:
    """Applique le périmètre 2026 validé à la migration Salesforce."""
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
        result["excluded_statuses"] = list(EXCLUDED_STATUS_LABELS)
        result["excluded_test_labels"] = list(EXCLUDED_TEST_LABELS)
        result["excluded_internal_records"] = list(EXCLUDED_INTERNAL_RECORDS)
        result["excluded_open_without_formation"] = list(
            EXCLUDED_OPEN_WITHOUT_FORMATION_LABELS
        )
        result.setdefault("skipped_disqualified", 0)
        result.setdefault("skipped_test", 0)
        result.setdefault("skipped_internal", 0)
        result.setdefault("skipped_open_without_formation", 0)
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
                    "hors pistes disqualifiées, pistes sans formation, fiches "
                    "internes/de test et formations BTS/CAP."
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
                "« Importer Salesforce 2026 » : seules les pistes 2026 du "
                "périmètre validé sont autorisées."
            )
        }), 410

    app.view_functions[endpoint] = disabled_view
    setattr(app, marker, True)
