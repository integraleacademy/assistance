"""Cohérence entre le pipeline principal et les relances du CRM.

Les statuts de deuxième timeline (POEI, C2P, Marché FT, Def MOB, etc.)
décrivent l'avancement métier du dossier. Ils ne signifient pas qu'une relance
est programmée. Le statut principal doit donc être :

- ``En cours`` tant qu'aucune relance n'est planifiée ;
- ``A relancer`` uniquement lorsqu'une relance réellement programmée existe.

Cette normalisation s'applique également aux fiches déjà présentes dans la
base, lors des lectures et avant chaque sauvegarde.
"""

from __future__ import annotations

from typing import Any


EDITABLE_PRIMARY_STATUSES = {"", "Nouveaux", "En cours", "A relancer"}
FUNDING_SECONDARY_CODES = {"en_cours_instruction", "refusee"}
SCHEDULED_RELANCE_STATUSES = {"", "scheduled"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def contact_has_secondary_pipeline(contact: dict[str, Any]) -> bool:
    """Indique si la fiche possède une deuxième timeline métier."""
    if _text(contact.get("statut_secondaire")):
        return True
    return _text(contact.get("statut_demande_financement_ft")) in (
        FUNDING_SECONDARY_CODES
    )


def contact_has_scheduled_relance(contact: dict[str, Any]) -> bool:
    """Détecte une prochaine relance réelle, y compris les anciennes fiches."""
    for relance in contact.get("relances") or []:
        if not isinstance(relance, dict):
            continue
        status = _text(relance.get("status"))
        scheduled_date = _text(relance.get("scheduled_date"))
        if status in SCHEDULED_RELANCE_STATUSES and scheduled_date:
            return True

    # Compatibilité avec les fiches historiques qui possèdent uniquement le
    # champ synthétique ``relance_date`` sans entrée détaillée dans ``relances``.
    return bool(_text(contact.get("relance_date")))


def normalize_contact_pipeline_status(contact: dict[str, Any]) -> bool:
    """Aligne le statut principal d'une fiche avec sa prochaine relance."""
    if not isinstance(contact, dict) or not contact_has_secondary_pipeline(contact):
        return False

    current = _text(contact.get("statut"))
    if current not in EDITABLE_PRIMARY_STATUSES:
        # Les statuts métier prioritaires (RDV, blocage, converti, disqualifié)
        # ne doivent jamais être remplacés automatiquement.
        return False

    target = (
        "A relancer"
        if contact_has_scheduled_relance(contact)
        else "En cours"
    )
    if current == target:
        return False

    contact["statut"] = target
    return True


def normalize_crm_pipeline_statuses(data: dict[str, Any]) -> int:
    """Normalise toutes les fiches CRM et retourne le nombre de corrections."""
    changed = 0
    for contact in data.get("crm_contacts", []) or []:
        if normalize_contact_pipeline_status(contact):
            changed += 1
    return changed


def install_crm_pipeline_status_consistency(app_module) -> None:
    """Applique la règle à chaque lecture et avant chaque sauvegarde."""
    if getattr(app_module, "_crm_pipeline_status_consistency_installed", False):
        return

    original_load = app_module.load_data
    original_save = app_module.save_data

    def normalized_load(*args, **kwargs):
        data = original_load(*args, **kwargs)
        normalize_crm_pipeline_statuses(data)
        return data

    def normalized_save(data, *args, **kwargs):
        normalize_crm_pipeline_statuses(data)
        return original_save(data, *args, **kwargs)

    app_module.load_data = normalized_load
    app_module.save_data = normalized_save
    app_module._crm_pipeline_status_consistency_installed = True
