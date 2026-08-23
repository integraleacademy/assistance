"""Lecture à la demande de l'historique Chatter d'une fiche CRM.

L'historique Salesforce peut contenir plusieurs milliers d'éléments au total.
Il n'est donc pas ajouté aux réponses compactes de la liste des contacts : la
fiche le charge uniquement lorsque nécessaire grâce à cette route dédiée.
"""

from __future__ import annotations

from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _contact_by_id(data: dict[str, Any], contact_id: str):
    wanted = _text(contact_id)
    return next(
        (
            contact
            for contact in (data.get("crm_contacts") or [])
            if isinstance(contact, dict)
            and _text(contact.get("id")) == wanted
        ),
        None,
    )


def register_salesforce_chatter_history(
    app,
    *,
    load_data_fn,
    login_required_fn,
) -> None:
    """Enregistre la route de lecture de l'historique Salesforce."""
    endpoint = "crm_salesforce_chatter_history"
    if endpoint in app.view_functions:
        return

    from flask import jsonify

    @app.get(
        "/api/crm/contacts/<contact_id>/salesforce-chatter",
        endpoint=endpoint,
    )
    @login_required_fn
    def crm_salesforce_chatter_history(contact_id: str):
        data = load_data_fn()
        contact = _contact_by_id(data, contact_id)
        if contact is None:
            return jsonify({"error": "Contact introuvable"}), 404

        items = [
            item
            for item in (contact.get("salesforce_chatter") or [])
            if isinstance(item, dict)
        ]
        items.sort(
            key=lambda item: (
                _text(item.get("date")),
                _text(item.get("id")),
            ),
            reverse=True,
        )
        comment_count = sum(
            len([
                comment
                for comment in (item.get("comments") or [])
                if isinstance(comment, dict)
            ])
            for item in items
        )
        return jsonify({
            "contact_id": _text(contact.get("id")),
            "items": items,
            "publication_count": len(items),
            "comment_count": comment_count,
            "last_imported_at": _text(
                contact.get("salesforce_chatter_imported_at")
            ),
        })
