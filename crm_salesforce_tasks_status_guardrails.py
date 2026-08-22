"""Garde-fous de statut autour de l'import des tâches Salesforce."""

from __future__ import annotations

import copy
from functools import wraps
from typing import Any

from crm_pipeline_status_consistency import normalize_contact_pipeline_status


def _text(value: Any) -> str:
    return str(value or "").strip()


def install_salesforce_tasks_status_guardrails(tasks_module) -> None:
    """Aligne le statut principal après l'import réel ou simulé des tâches.

    Le moteur historique ne promouvait que les fiches vides ou « Nouveaux ».
    Les dossiers « En cours » dotés d'une deuxième timeline doivent eux aussi
    passer à « A relancer » lorsqu'une tâche ouverte est effectivement créée.
    À l'inverse, une fiche avec deuxième timeline mais sans relance revient à
    « En cours ».
    """
    if getattr(tasks_module, "_status_guardrails_installed", False):
        return

    original_import = tasks_module.import_salesforce_task_rows

    @wraps(original_import)
    def guarded_import(contacts, rows, *, dry_run: bool = False):
        working_contacts = copy.deepcopy(contacts) if dry_run else contacts
        before = {
            _text(contact.get("id")): _text(contact.get("statut"))
            for contact in working_contacts
            if isinstance(contact, dict)
        }

        # Pour l'aperçu, exécuter le moteur sur une copie réelle afin de pouvoir
        # appliquer exactement les mêmes règles de cohérence sur le résultat.
        result = original_import(
            working_contacts,
            rows,
            dry_run=False,
        )

        normalized_to_in_progress = 0
        for contact in working_contacts:
            previous = _text(contact.get("statut"))
            if normalize_contact_pipeline_status(contact):
                if previous == "A relancer" and contact.get("statut") == "En cours":
                    normalized_to_in_progress += 1

        promoted_ids = {
            _text(contact.get("id"))
            for contact in working_contacts
            if isinstance(contact, dict)
            and _text(contact.get("statut")) == "A relancer"
            and before.get(_text(contact.get("id"))) != "A relancer"
        }

        result["dry_run"] = dry_run
        result["promoted_to_followup"] = len(promoted_ids)
        result["normalized_to_in_progress"] = normalized_to_in_progress
        return result

    tasks_module.import_salesforce_task_rows = guarded_import
    tasks_module._status_guardrails_installed = True
