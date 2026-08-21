"""Compatibilité avec les libellés exacts des rapports Salesforce français.

Le rapport exporté depuis Lightning utilise notamment ``ID de piste``,
``Statut de la piste`` et ``Société/Compte``. Ces libellés ne correspondent pas
tous aux noms API attendus par le moteur de migration ; ce garde-fou les copie
vers les champs canoniques avant le filtrage et le rapprochement CRM.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


REPORT_HEADER_ALIASES = {
    "id de piste": "Id",
    "societe compte": "Company",
    "origine de la piste": "LeadSource",
    "proprietaire de la piste": "OwnerName",
    "montant cpf": "Montant_CPF__c",
    "inscrit france travail": "Inscrit_France_Travail__c",
    "identite numerique fonctionnelle": (
        "Identit_num_rique_fonctionnelle__c"
    ),
    "dates souhaitees": "Dates_souhait_es__c",
    "statut de la piste": "Status",
    "derniere modification": "LastModifiedDate",
}

STATUS_VALUE_ALIASES = {
    "fin ft en cours": "Financement FT en cours",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _fold(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", _text(value))
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Mn"
    ).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", normalized)).strip()


def normalize_salesforce_report_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ajoute les clés canoniques sans supprimer les colonnes du rapport source."""
    normalized_rows: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        for source_key, value in source.items():
            target_key = REPORT_HEADER_ALIASES.get(_fold(source_key))
            if not target_key:
                continue
            if _text(value) and not _text(row.get(target_key)):
                row[target_key] = _text(value)
            else:
                row.setdefault(target_key, _text(value))

        status = _text(row.get("Status"))
        normalized_status = STATUS_VALUE_ALIASES.get(_fold(status))
        if normalized_status:
            row["Status"] = normalized_status
        normalized_rows.append(row)
    return normalized_rows


def install_salesforce_report_guardrails(migration_module) -> None:
    """Normalise les lignes juste après le décodage CSV du rapport Salesforce."""
    if getattr(migration_module, "_report_guardrails_installed", False):
        return

    original_parser = migration_module.parse_compatible_csv

    def compatible_parser(raw: bytes, *, max_csv_bytes: int):
        rows = original_parser(raw, max_csv_bytes=max_csv_bytes)
        return normalize_salesforce_report_rows(rows)

    migration_module.parse_compatible_csv = compatible_parser
    migration_module._report_guardrails_installed = True
