"""Normalisation des statuts Salesforce dans les deux niveaux du CRM."""

from __future__ import annotations

from typing import Any


PRIMARY_STATUSES = {
    "Nouveaux",
    "Blocage",
    "RDV programmé",
    "En cours",
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
    "fin ft en cours": "Financement FT en cours",
    "financement ft en cours": "Financement FT en cours",
    "financement ft refuse": "Financement FT refusé",
    "c2p": "C2P en cours",
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

    def folded_phrase(value: Any) -> str:
        folded = migration_module._fold(value)
        folded = migration_module.re.sub(r"['’]+", " ", folded)
        return " ".join(folded.split())

    def normalize_funding_status(value: Any) -> str:
        """Produit les mêmes codes internes que la synchronisation WEDOF."""
        raw = folded_phrase(value)
        if not raw:
            return ""
        if "annul" in raw or "abandon" in raw or "cancel" in raw:
            return "annulee"
        if any(term in raw for term in ("accepte", "accorde", "valide", "approved")):
            return "acceptee"
        if "refus" in raw or "reject" in raw:
            return "refusee"
        if any(term in raw for term in ("instruction", "en cours", "en attente", "pending")):
            return "en_cours_instruction"
        if any(term in raw for term in ("transmis", "envoye", "depose", "submitted")):
            return "transmise"
        if any(term in raw for term in ("a preparer", "pas encore depose", "to prepare")):
            return "a_preparer"
        if any(term in raw for term in ("aucune demande", "pas de demande", "no request", "none")):
            return "aucune_demande"
        return ""

    def source_status(row: dict[str, Any]) -> str:
        return folded_phrase(migration_module._row_value(
            row, "Status", "Statut",
        ))

    def source_is_converted(row: dict[str, Any]) -> bool:
        return migration_module._truthy(migration_module._row_value(
            row, "IsConverted", "Converti", "Est converti",
        ))

    def normalized_primary_status(row: dict[str, Any]) -> str:
        if source_is_converted(row):
            return "Converti"
        raw = source_status(row)
        if raw in SECONDARY_STATUS_ALIASES:
            # POEI, C2P, Marché FT, Def MOB, etc. décrivent l'avancement
            # métier. Sans tâche de relance importée, le dossier est « En cours ».
            return "En cours"
        status = original_status(row)
        return status if status in PRIMARY_STATUSES else "Nouveaux"

    def mapped_row(row: dict[str, Any]) -> dict[str, Any]:
        mapped = original_map_row(row)
        raw = source_status(row)
        secondary = SECONDARY_STATUS_ALIASES.get(raw)
        primary = migration_module._normalized_status(row)
        mapped["statut"] = primary

        normalized_funding_status = normalize_funding_status(
            mapped.get("statut_demande_financement_ft")
        )
        if normalized_funding_status:
            mapped["statut_demande_financement_ft"] = normalized_funding_status
            mapped["statut_demande_financement_ft_source"] = (
                "salesforce_migration"
            )

        # Une piste convertie reste avant tout une inscription. Son ancienne
        # étape Salesforce ne doit pas la faire réapparaître parmi les pistes.
        if secondary and primary != "Converti":
            mapped["statut_secondaire"] = secondary
            mapped["statut_secondaire_source"] = "salesforce_migration"
            funding_status = FUNDING_STATUS_BY_SECONDARY.get(secondary)
            if funding_status:
                mapped["statut_demande_financement_ft"] = funding_status
                mapped["statut_demande_financement_ft_source"] = (
                    "salesforce_migration"
                )
        return mapped

    migration_module._normalized_status = normalized_primary_status
    migration_module._map_row = mapped_row
    migration_module._status_guardrails_installed = True
