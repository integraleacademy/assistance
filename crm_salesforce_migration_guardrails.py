"""Garde-fous complémentaires pour la migration Salesforce.

Le module de migration reste lisible et isolé. Ces contrôles sont installés au
point d'entrée CRM, sur le même modèle que la compatibilité CSV Salesforce :
ils refusent les rapprochements d'identités incompatibles et les lignes qui ne
pourraient pas être retrouvées lors d'un second import.
"""

from __future__ import annotations

import hashlib
from typing import Any


def install_salesforce_migration_guardrails(migration_module) -> None:
    """Installe des protections idempotentes autour des fonctions de migration."""
    if getattr(migration_module, "_guardrails_installed", False):
        return

    original_formation = migration_module._normalized_formation
    original_map_row = migration_module._map_row
    original_prepare = migration_module._prepare_complete_rows
    original_match = migration_module._match
    original_signature = migration_module._contacts_signature

    def guarded_formation(value: Any) -> str:
        folded = migration_module._fold(value)
        aliases = {
            "agent de protection physique des personnes": "A3P",
            "agent de prevention et de securite": "APS",
        }
        return aliases.get(folded) or original_formation(value)

    def guarded_map_row(row: dict[str, Any]) -> dict[str, Any]:
        mapped = original_map_row(row)
        owner = migration_module._row_value(
            row,
            "OwnerName", "Owner Name", "Owner.Name", "Owner: Full Name",
            "Lead Owner", "Lead Owner: Full Name",
            "Propriétaire de la piste", "Nom complet du propriétaire",
        )
        if owner:
            mapped["commercial"] = owner
            mapped["salesforce_owner"] = owner
        owner_id = migration_module._row_value(
            row, "OwnerId", "Owner ID", "ID du propriétaire",
        )
        if owner_id:
            mapped["salesforce_owner_id"] = owner_id

        status = migration_module._normalized_status(row)
        mapped["statut"] = status
        converted = (
            migration_module._truthy(migration_module._row_value(
                row, "IsConverted", "Converti", "Est converti",
            ))
            or status == "Converti"
        )
        mapped["salesforce_is_converted"] = converted
        if converted:
            converted_at = migration_module._iso(migration_module._row_value(
                row, "ConvertedDate", "Date de conversion",
            ))
            if converted_at:
                mapped["converted_at"] = converted_at
        if not mapped.get("received_at"):
            mapped["received_at"] = mapped.get("created_at")
        return mapped

    def guarded_prepare(*args, **kwargs):
        prepared, stats = original_prepare(*args, **kwargs)
        valid = []
        skipped = 0
        for row in prepared:
            has_name = bool(
                migration_module._text(row.get("nom"))
                or migration_module._text(row.get("prenom"))
            )
            has_stable_key = bool(
                migration_module._text(row.get("salesforce_id"))
                or migration_module._email(row.get("mail"))
                or migration_module._phone(row.get("telephone"))
            )
            if has_name and has_stable_key:
                valid.append(row)
            else:
                skipped += 1
        if skipped:
            stats = dict(stats)
            stats["skipped_invalid"] = int(stats.get("skipped_invalid") or 0) + skipped
        return valid, stats

    def guarded_match(row, by_sf, by_email, by_phone, *, deduplicate: bool):
        contact, method, reason = original_match(
            row, by_sf, by_email, by_phone, deduplicate=deduplicate,
        )
        if (
            contact
            and method in {"email", "phone", "email+phone"}
            and not migration_module._compatible_names(contact, row)
        ):
            label = {
                "email": "L’e-mail",
                "phone": "Le téléphone",
                "email+phone": "Les coordonnées",
            }[method]
            return None, "", f"{label} correspond, mais l’identité est différente."
        return contact, method, reason

    def guarded_signature(contacts: list[dict[str, Any]]) -> str:
        base = original_signature(contacts)
        identity = [
            (
                migration_module._text(contact.get("id")),
                migration_module._email(contact.get("mail")),
                migration_module._phone(contact.get("telephone")),
            )
            for contact in sorted(
                contacts,
                key=lambda item: migration_module._text(item.get("id")),
            )
        ]
        return hashlib.sha256(f"{base}|{identity!r}".encode()).hexdigest()

    migration_module._normalized_formation = guarded_formation
    migration_module._map_row = guarded_map_row
    migration_module._prepare_complete_rows = guarded_prepare
    migration_module._match = guarded_match
    migration_module._contacts_signature = guarded_signature
    migration_module._guardrails_installed = True
