"""Normalisation des statuts Salesforce dans les deux niveaux du CRM."""

from __future__ import annotations

from typing import Any


PRIMARY_STATUSES = {
    "Nouveaux",
    "Blocage",
    "RDV programmé",
    "Prochain RDV inscription",
    "A relancer",
    "Disqualifié",
    "Converti",
}

SECONDARY_STATUS_ALIASES = {
    "poei": "POEI",
    "session ft": "Marché FT",
    "marche ft": "Marché FT",
    "def mob": "Def MOB",
    "def mobilite": "Def MOB",
    "financement ft en cours": "Financement FT en cours",
    "financement ft refuse": "Financement FT refusé",
    "c2p en cours": "C2P en cours",
}

FUNDING_STATUS_BY_SECONDARY = {
    "Financement FT en cours": "en_cours_instruction",
    "Financement FT refusé": "refusee",
}


def install_salesforce_status_guardrails(migration_module) -> None:
    """Sépare les étapes principales des marqueurs secondaires du CRM."""
    if getattr(migration_module, "_status_guardrails_installed", False):
        return

    original_status = migration_module._normalized_status
    original_map_row = migration_module._map_row

    def source_status(row: dict[str, Any]) -> str:
        return migration_module._fold(migration_module._row_value(
            row, "Status", "Statut",
        ))

    def normalized_primary_status(row: dict[str, Any]) -> str:
        raw = source_status(row)
        if raw in SECONDARY_STATUS_ALIASES:
            return "Nouveaux"
        status = original_status(row)
        return status if status in PRIMARY_STATUSES else "Nouveaux"

    def mapped_row(row: dict[str, Any]) -> dict[str, Any]:
        mapped = original_map_row(row)
        raw = source_status(row)
        secondary = SECONDARY_STATUS_ALIASES.get(raw)
        mapped["statut"] = migration_module._normalized_status(row)
        if secondary:
            mapped["statut_secondaire"] = secondary
            mapped["statut_secondaire_source"] = "salesforce_migration"
            funding_status = FUNDING_STATUS_BY_SECONDARY.get(secondary)
            if funding_status and not migration_module._text(
                mapped.get("statut_demande_financement_ft")
            ):
                mapped["statut_demande_financement_ft"] = funding_status
                mapped["statut_demande_financement_ft_source"] = (
                    "salesforce_migration"
                )
        return mapped

    migration_module._normalized_status = normalized_primary_status
    migration_module._map_row = mapped_row
    migration_module._status_guardrails_installed = True
