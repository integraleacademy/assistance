"""Remplacement isolé du proxy CNAPS du CRM."""

from __future__ import annotations

import os
from typing import Any, Callable, Dict
from urllib.parse import urlsplit, urlunsplit

import requests
from flask import jsonify


def _tracking_api_url() -> str:
    explicit = str(os.getenv("GESTION_STAGIAIRES_CNAPS_API_URL") or "").strip()
    if explicit:
        return explicit

    for candidate in (
        os.getenv("GESTION_STAGIAIRES_PUBLIC_URL"),
        os.getenv("GESTION_STAGIAIRES_API_URL"),
    ):
        value = str(candidate or "").strip()
        if not value:
            continue
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc,
                    "/api/integrations/crm/cnaps-tracking",
                    "",
                    "",
                )
            )
    return ""


def _public_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _public_payload(item)
            for key, item in value.items()
            if str(key).lower()
            not in {"token", "api_token", "authorization", "x-api-key"}
        }
    if isinstance(value, list):
        return [_public_payload(item) for item in value]
    return value


def register_cnaps_tracking_proxy(
    app,
    *,
    load_data: Callable[[], Dict[str, Any]],
    find_contact: Callable[[Dict[str, Any], str], Any],
    login_required: Callable,
    http_get: Callable = requests.get,
) -> None:
    """Fait rechercher le prospect par identité dans le suivi CNAPS distant."""

    @login_required
    def crm_contact_reglementaire(contact_id):
        contact = find_contact(load_data(), contact_id)
        if not contact:
            return jsonify({"error": "Contact introuvable"}), 404

        api_url = _tracking_api_url()
        api_token = str(os.getenv("GESTION_STAGIAIRES_API_TOKEN") or "").strip()
        if not api_url or not api_token:
            return jsonify({"error": "Connexion Gestion stagiaires non configurée"}), 503

        last_name = str(contact.get("nom") or "").strip()
        first_name = str(contact.get("prenom") or "").strip()
        if not last_name:
            return jsonify(
                {
                    "linked": False,
                    "message": "Le nom du prospect doit être renseigné pour rechercher son suivi CNAPS.",
                }
            )

        try:
            response = http_get(
                api_url,
                params={"nom": last_name, "prenom": first_name},
                headers={
                    "Authorization": f"Bearer {api_token}",
                    "X-API-Key": api_token,
                    "Accept": "application/json",
                },
                timeout=float(os.getenv("GESTION_STAGIAIRES_CNAPS_TIMEOUT", "30")),
            )
            remote = response.json() if getattr(response, "content", b"") else {}
        except (requests.RequestException, ValueError, TypeError) as exc:
            app.logger.warning("Erreur suivi CNAPS Gestion stagiaires: %s", exc)
            return jsonify(
                {"error": "Gestion stagiaires est momentanément indisponible"}
            ), 502

        if response.status_code == 404:
            message = remote.get("message") if isinstance(remote, dict) else None
            return jsonify(
                {
                    "linked": False,
                    "message": message
                    or "Aucune demande CNAPS trouvée pour ce prospect dans le suivi CNAPS.",
                }
            )
        if response.status_code != 200:
            message = (
                remote.get("detail") or remote.get("error")
                if isinstance(remote, dict)
                else None
            )
            return jsonify(
                {"error": message or "Gestion stagiaires est momentanément indisponible"}
            ), 502
        if not isinstance(remote, dict):
            return jsonify({"error": "Réponse invalide de Gestion stagiaires"}), 502
        return jsonify(_public_payload(remote))

    endpoint = "crm_contact_reglementaire"
    if endpoint in app.view_functions:
        app.view_functions[endpoint] = crm_contact_reglementaire
        return
    app.add_url_rule(
        "/api/crm/contacts/<contact_id>/reglementaire",
        endpoint=endpoint,
        view_func=crm_contact_reglementaire,
        methods=["GET"],
    )
