"""Proxy sécurisé des données Gestion Stagiaires utilisées par le CRM."""

from __future__ import annotations

import os
from typing import Any, Callable, Dict
from urllib.parse import urlsplit, urlunsplit

import requests
from flask import jsonify


REMOTE_PATH = "/api/integrations/crm/stagiaires"
_SECRET_KEYS = {
    "token", "public_token", "trainee_token", "api_token", "authorization",
    "x-api-key",
}


def gestion_stagiaires_api_url() -> str:
    """Construit l'endpoint stagiaires depuis l'origine publique configurée."""
    for name in (
        "GESTION_STAGIAIRES_PUBLIC_URL",
        "GESTION_STAGIAIRES_API_URL",
        "GESTION_STAGIAIRES_CNAPS_API_URL",
    ):
        value = str(os.getenv(name) or "").strip()
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return urlunsplit((parsed.scheme, parsed.netloc, REMOTE_PATH, "", ""))
    return ""


def public_payload(value: Any) -> Any:
    """Supprime récursivement les secrets éventuels d'une réponse distante."""
    if isinstance(value, dict):
        return {
            key: public_payload(item)
            for key, item in value.items()
            if str(key).lower() not in _SECRET_KEYS
        }
    if isinstance(value, list):
        return [public_payload(item) for item in value]
    return value


def proxy_reglementaire(app, contact: Dict[str, Any], http_get=None):
    """Effectue l'unique appel distant et relaie son payload métier complet."""
    api_url = gestion_stagiaires_api_url()
    api_token = str(os.getenv("GESTION_STAGIAIRES_API_TOKEN") or "").strip()
    if not api_url or not api_token:
        return jsonify({"error": "Connexion Gestion Stagiaires non configurée"}), 503

    try:
        response = (http_get or requests.get)(
            api_url,
            params={"crm_contact_id": contact["id"]},
            headers={
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
            },
            timeout=float(os.getenv("GESTION_STAGIAIRES_CNAPS_TIMEOUT", "30")),
        )
        remote = response.json() if getattr(response, "content", b"") else {}
    except (requests.RequestException, ValueError, TypeError) as exc:
        # Ne journaliser ni URL signée, ni en-têtes, ni corps distant.
        app.logger.warning("Gestion Stagiaires indisponible (%s)", type(exc).__name__)
        return jsonify({"error": "Gestion Stagiaires est momentanément indisponible"}), 502

    if not isinstance(remote, dict):
        return jsonify({"error": "Réponse invalide de Gestion Stagiaires"}), 502
    cleaned = public_payload(remote)
    if response.status_code == 200:
        return jsonify(cleaned)
    if response.status_code in {400, 401, 404, 409}:
        # Le statut suffit au frontend pour présenter un message sûr et stable.
        safe_messages = {
            400: "Requête Gestion Stagiaires invalide",
            401: "Intégration Gestion Stagiaires non configurée",
            404: "Aucun stagiaire lié",
            409: "Plusieurs stagiaires liés",
        }
        return jsonify({"error": safe_messages[response.status_code]}), response.status_code
    return jsonify({"error": "Gestion Stagiaires est momentanément indisponible"}), 502


def register_cnaps_tracking_proxy(
    app,
    *,
    load_data: Callable[[], Dict[str, Any]],
    find_contact: Callable[[Dict[str, Any], str], Any],
    login_required: Callable,
    http_get: Callable | None = None,
) -> None:
    """Installe le proxy partagé sur la route réglementaire historique."""

    @login_required
    def crm_contact_reglementaire(contact_id):
        contact = find_contact(load_data(), contact_id)
        if not contact:
            return jsonify({"error": "Contact introuvable"}), 404
        return proxy_reglementaire(app, contact, http_get=http_get)

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
