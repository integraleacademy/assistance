"""Rapport exhaustif des relances Salesforce non importées automatiquement.

L'importeur historique ne renvoyait que trente exemples dans ``unmatched_samples``.
Ce garde-fou conserve ce résumé pour l'interface, mais ajoute au résultat une
liste complète et structurée de toutes les tâches nécessitant une intervention
manuelle : fiche absente, fiche CRM non reliée à Salesforce, ambiguïté, fiche
exclue ou simple avertissement de nom.
"""

from __future__ import annotations

from collections import Counter
from functools import wraps
from typing import Any


CATEGORY_MISSING_CONTACT = "Aucune fiche CRM"
CATEGORY_NOT_LINKED = "Fiche CRM non reliée à Salesforce"
CATEGORY_AMBIGUOUS = "Correspondance ambiguë"
CATEGORY_EXCLUDED = "Fiche exclue"
CATEGORY_NAME_WARNING = "Avertissement de nom"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _contact_label(contact: dict[str, Any] | None) -> str:
    if not contact:
        return ""
    return " ".join(
        part
        for part in (
            _text(contact.get("prenom")),
            _text(contact.get("nom")),
        )
        if part
    )


def _review_row(
    task: dict[str, Any],
    *,
    category: str,
    reason: str,
    action: str,
    contact: dict[str, Any] | None = None,
    match_method: str = "",
    importable_after_check: bool = False,
) -> dict[str, Any]:
    phones = list(task.get("phones") or [])
    return {
        "category": category,
        "recommended_action": action,
        "importable_after_check": bool(importable_after_check),
        "person": _text(task.get("relation_name")) or "Sans nom",
        "email": _text(task.get("email")),
        "phone": phones[0] if phones else "",
        "all_phones": phones,
        "scheduled_date": _text(task.get("scheduled_date")),
        "subject": _text(task.get("subject")),
        "owner": _text(task.get("owner")),
        "priority": _text(task.get("priority")),
        "salesforce_status": _text(task.get("salesforce_status")),
        "comments": _text(task.get("comments")),
        "activity_id": _text(task.get("salesforce_task_id")),
        "relation_type": _text(task.get("relation_type")),
        "company": _text(task.get("company")),
        "reason": reason,
        "match_method": match_method,
        "crm_contact_id": _text((contact or {}).get("id")),
        "crm_contact_name": _contact_label(contact),
        "crm_contact_status": _text((contact or {}).get("statut")),
        "crm_contact_formation": _text((contact or {}).get("formation")),
    }


def build_manual_review_rows(
    tasks_module,
    contacts: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rejoue uniquement le rapprochement afin de produire la liste exhaustive."""
    prepared, _stats = tasks_module._prepare_task_rows(rows)
    by_email, by_phone, by_task_id = tasks_module._indexes(contacts)
    review_rows: list[dict[str, Any]] = []

    for task in prepared:
        source_conflict = _text(task.get("source_conflict"))
        if source_conflict:
            review_rows.append(_review_row(
                task,
                category=CATEGORY_AMBIGUOUS,
                reason=source_conflict,
                action="Vérifier les doublons de l'activité Salesforce avant toute création manuelle.",
            ))
            continue

        contact, existing_relance, method, reason, name_warning = (
            tasks_module._match_task(
                task,
                by_email,
                by_phone,
                by_task_id,
            )
        )

        if reason:
            if reason.startswith("Aucune fiche CRM"):
                category = CATEGORY_MISSING_CONTACT
                action = (
                    "Créer la personne dans le CRM, puis programmer la relance à la date indiquée."
                )
            else:
                category = CATEGORY_AMBIGUOUS
                action = (
                    "Comparer le nom, l'e-mail et le téléphone avant de rattacher ou créer la relance."
                )
            review_rows.append(_review_row(
                task,
                category=category,
                reason=reason,
                action=action,
                contact=contact,
                match_method=method,
            ))
            continue

        if contact is None:
            review_rows.append(_review_row(
                task,
                category=CATEGORY_AMBIGUOUS,
                reason="Le moteur de rapprochement n'a retourné aucune fiche exploitable.",
                action="Vérifier manuellement les coordonnées avant toute création.",
            ))
            continue

        if (
            not tasks_module._contact_is_salesforce_linked(contact)
            and existing_relance is None
        ):
            review_rows.append(_review_row(
                task,
                category=CATEGORY_NOT_LINKED,
                reason=(
                    "La fiche existe dans le CRM, mais elle ne possède pas encore "
                    "d'identifiant de piste Salesforce."
                ),
                action=(
                    "Vérifier qu'il s'agit de la bonne personne, renseigner son identifiant "
                    "Salesforce ou créer directement la relance sur cette fiche."
                ),
                contact=contact,
                match_method=method,
                importable_after_check=True,
            ))
            continue

        excluded_reason = tasks_module._contact_is_excluded(contact)
        if excluded_reason:
            review_rows.append(_review_row(
                task,
                category=CATEGORY_EXCLUDED,
                reason=excluded_reason,
                action=(
                    "Ne pas importer, sauf décision explicite de réactiver ou requalifier cette fiche."
                ),
                contact=contact,
                match_method=method,
            ))
            continue

        if name_warning:
            review_rows.append(_review_row(
                task,
                category=CATEGORY_NAME_WARNING,
                reason=(
                    "Les deux coordonnées concordent, mais le nom Salesforce diffère du nom CRM."
                ),
                action=(
                    "Contrôler l'identité. La relance reste importable automatiquement si la fiche est correcte."
                ),
                contact=contact,
                match_method=method,
                importable_after_check=True,
            ))

    return sorted(
        review_rows,
        key=lambda item: (
            item.get("category") or "",
            item.get("scheduled_date") or "",
            item.get("person") or "",
            item.get("activity_id") or "",
        ),
    )


def install_salesforce_tasks_report_guardrails(tasks_module) -> None:
    """Enrichit chaque aperçu/import avec toutes les anomalies, sans changer l'import."""
    if getattr(tasks_module, "_full_report_guardrails_installed", False):
        return

    original_import = tasks_module.import_salesforce_task_rows

    @wraps(original_import)
    def import_with_full_report(
        contacts: list[dict[str, Any]],
        rows: list[dict[str, Any]],
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        result = original_import(
            contacts,
            rows,
            dry_run=dry_run,
        )
        manual_rows = build_manual_review_rows(
            tasks_module,
            contacts,
            rows,
        )
        category_counts = Counter(
            _text(item.get("category")) or "Autre"
            for item in manual_rows
        )
        result["manual_review_rows"] = manual_rows
        result["manual_review_total"] = len(manual_rows)
        result["manual_review_counts"] = dict(category_counts)
        result["missing_contact_rows"] = [
            item for item in manual_rows
            if item.get("category") == CATEGORY_MISSING_CONTACT
        ]
        result["not_salesforce_linked_rows"] = [
            item for item in manual_rows
            if item.get("category") == CATEGORY_NOT_LINKED
        ]
        result["ambiguous_full"] = [
            item for item in manual_rows
            if item.get("category") == CATEGORY_AMBIGUOUS
        ]
        result["excluded_full"] = [
            item for item in manual_rows
            if item.get("category") == CATEGORY_EXCLUDED
        ]
        result["name_warnings_full"] = [
            item for item in manual_rows
            if item.get("category") == CATEGORY_NAME_WARNING
        ]
        return result

    tasks_module.import_salesforce_task_rows = import_with_full_report
    tasks_module._full_report_guardrails_installed = True
