"""Sérialisation des écritures Salesforce avec les autres mutations du CRM."""

from __future__ import annotations

from functools import wraps
from typing import Any


def serialize_salesforce_writes(
    app,
    *,
    request: Any,
    transaction_lock,
    endpoint: str = "crm_migrate_salesforce",
) -> None:
    """Protège le chargement, la fusion et la sauvegarde dans une transaction.

    L’aperçu reste non bloquant et son jeton détecte les changements concurrents.
    L’import définitif partage en revanche le verrou utilisé par les autres
    écritures CRM, afin d’éviter toute perte de modification.
    """
    marker = f"_{endpoint}_transaction_guardrail_installed"
    if getattr(app, marker, False):
        return
    view = app.view_functions.get(endpoint)
    if view is None:
        raise RuntimeError(
            "La route Salesforce doit être enregistrée avant son verrouillage."
        )

    @wraps(view)
    def serialized_view(*args, **kwargs):
        dry_run = str(request.form.get("dry_run", "0") or "0").strip() == "1"
        if dry_run:
            return view(*args, **kwargs)
        with transaction_lock:
            return view(*args, **kwargs)

    app.view_functions[endpoint] = serialized_view
    setattr(app, marker, True)
