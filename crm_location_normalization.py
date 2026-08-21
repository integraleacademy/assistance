"""Normalisation commune des lieux du CRM et des imports Salesforce.

Le CRM utilise des libellés canoniques avec une apostrophe typographique,
notamment ``Côte d’Azur``. Salesforce exporte parfois la même valeur avec une
apostrophe droite (``Côte d'Azur``). Une comparaison stricte dans le formulaire
faisait alors sélectionner le premier centre disponible, souvent Auvergne.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


_CANONICAL_LOCATIONS = {
    "cote_azur": "Côte d’Azur",
    "auvergne": "Auvergne",
    "paris": "Paris",
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


def canonical_crm_location(value: Any) -> str:
    """Retourne le libellé exact attendu par les listes du CRM."""
    raw = _text(value)
    folded = _fold(raw)
    if not folded:
        return ""

    if any(term in folded for term in (
        "cote d azur",
        "cote azur",
        "puget sur argens",
        "saint raphael",
        "frejus",
        "paca",
    )):
        return _CANONICAL_LOCATIONS["cote_azur"]
    if any(term in folded for term in (
        "auvergne",
        "aurillac",
        "arpajon sur cere",
        "cantal",
        "terres d auvergne",
    )):
        return _CANONICAL_LOCATIONS["auvergne"]
    if any(term in folded for term in (
        "paris",
        "ile de france",
    )):
        return _CANONICAL_LOCATIONS["paris"]
    return raw


def canonicalize_crm_locations(data: dict[str, Any]) -> int:
    """Normalise les lieux connus dans une copie de la base CRM."""
    changed = 0
    for contact in data.get("crm_contacts", []) or []:
        if not isinstance(contact, dict):
            continue
        for field in ("lieu", "salesforce_lieu"):
            current = _text(contact.get(field))
            if not current:
                continue
            canonical = canonical_crm_location(current)
            if canonical and canonical != current:
                contact[field] = canonical
                changed += 1
    return changed


def install_crm_location_normalization(app_module) -> None:
    """Normalise les lieux à chaque lecture et avant chaque sauvegarde."""
    if getattr(app_module, "_crm_location_normalization_installed", False):
        return

    original_load = app_module.load_data
    original_save = app_module.save_data

    def normalized_load(*args, **kwargs):
        data = original_load(*args, **kwargs)
        canonicalize_crm_locations(data)
        return data

    def normalized_save(data, *args, **kwargs):
        canonicalize_crm_locations(data)
        return original_save(data, *args, **kwargs)

    app_module.load_data = normalized_load
    app_module.save_data = normalized_save
    app_module._crm_location_normalization_installed = True


def install_salesforce_location_guardrails(migration_module) -> None:
    """Canonicalise le lieu et conserve les valeurs Salesforce pour audit."""
    if getattr(migration_module, "_location_guardrails_installed", False):
        return

    original_map_row = migration_module._map_row

    def mapped_row(row: dict[str, Any]) -> dict[str, Any]:
        mapped = original_map_row(row)
        raw_location = migration_module._row_value(
            row,
            "Lieu__c",
            "Lieu",
            "Centre",
            "Campus",
            "Localisation",
        )
        if raw_location:
            canonical = canonical_crm_location(raw_location)
            mapped["lieu"] = canonical
            mapped["salesforce_lieu"] = canonical
            mapped["salesforce_lieu_raw"] = raw_location

        raw_dates = migration_module._row_value(
            row,
            "Dates_souhait_es__c",
            "Dates souhaitées",
            "Dates souhaitées ?",
            "Date souhaitée",
        )
        if raw_dates:
            mapped["dates_formation"] = raw_dates
            mapped["salesforce_dates_formation"] = raw_dates
        return mapped

    migration_module._map_row = mapped_row
    migration_module._location_guardrails_installed = True
