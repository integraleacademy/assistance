"""Contrôle des vraies pistes Salesforce au statut « Nouveau ».

Le CRM utilise « Nouveaux » comme valeur de repli lorsqu'un statut Salesforce
n'est pas reconnu. L'interface bloquait donc tout import contenant ce statut,
y compris lorsque Salesforce indiquait réellement « Nouveau ».

Ce garde-fou expose séparément :

- les vraies pistes dont le statut source est Nouveau/Nouveaux/New ;
- les lignes qui aboutissent à « Nouveaux » à cause d'un statut vide ou inconnu.

L'interface peut ainsi autoriser les premières tout en continuant à bloquer les
mappings suspects.
"""

from __future__ import annotations

from collections import Counter
from functools import wraps
from typing import Any


GENUINE_NEW_SOURCE_STATUSES = ("Nouveau", "Nouveaux", "New")
_GENUINE_NEW_FOLDED = {"nouveau", "nouveaux", "new"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def install_salesforce_genuine_new_guardrails(migration_module) -> None:
    """Ajoute au rapport d'import le détail des statuts « Nouveaux »."""
    if getattr(migration_module, "_genuine_new_guardrails_installed", False):
        return

    original_import = migration_module.import_complete_rows

    @wraps(original_import)
    def import_with_new_status_details(
        contacts,
        rows,
        *,
        include_converted: bool = True,
        deduplicate: bool = True,
        dry_run: bool = False,
        created_from: str = "",
        created_to: str = "",
        merge_policy: str = "safe",
    ):
        # Utiliser exactement la préparation finale installée sur le module :
        # périmètre 2026, exclusions, mapping des statuts et dédoublonnage.
        prepared, _ = migration_module._prepare_complete_rows(
            rows,
            include_converted=include_converted,
            deduplicate=deduplicate,
            created_from=created_from,
            created_to=created_to,
        )

        source_counts: Counter[str] = Counter()
        genuine_samples: list[dict[str, str]] = []
        unexpected_samples: list[dict[str, str]] = []
        genuine_count = 0
        unexpected_count = 0

        for incoming in prepared:
            if _text(incoming.get("statut")) != "Nouveaux":
                continue

            source_status = (
                _text(incoming.get("salesforce_status"))
                or "Non renseigné"
            )
            source_counts[source_status] += 1
            sample = {
                "salesforce_id": _text(incoming.get("salesforce_id")),
                "nom": " ".join(filter(None, (
                    _text(incoming.get("prenom")),
                    _text(incoming.get("nom")),
                ))),
                "formation": _text(incoming.get("formation")),
                "source_status": source_status,
            }

            if migration_module._fold(source_status) in _GENUINE_NEW_FOLDED:
                genuine_count += 1
                if len(genuine_samples) < 20:
                    genuine_samples.append(sample)
            else:
                unexpected_count += 1
                if len(unexpected_samples) < 20:
                    unexpected_samples.append(sample)

        result = original_import(
            contacts,
            rows,
            include_converted=include_converted,
            deduplicate=deduplicate,
            dry_run=dry_run,
            created_from=created_from,
            created_to=created_to,
            merge_policy=merge_policy,
        )
        result.update({
            "new_status_source_counts": dict(source_counts.most_common()),
            "genuine_new_count": genuine_count,
            "unexpected_new_count": unexpected_count,
            "genuine_new_samples": genuine_samples,
            "unexpected_new_samples": unexpected_samples,
            "genuine_new_source_statuses": list(
                GENUINE_NEW_SOURCE_STATUSES
            ),
            "genuine_new_allowed": unexpected_count == 0,
        })
        return result

    migration_module.import_complete_rows = import_with_new_status_details
    migration_module._genuine_new_guardrails_installed = True
