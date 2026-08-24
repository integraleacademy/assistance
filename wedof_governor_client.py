"""Client du compteur/verrou WEDOF central hébergé par Gestion Stagiaires."""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
import uuid
from typing import Any, Dict, Optional

import requests


DEFAULT_GOVERNOR_URL = (
    "https://gestionstagiaires-r5no.onrender.com/internal/wedof/governor"
)


class WedofGovernorError(RuntimeError):
    """Le compteur central n'a pas pu autoriser la requête."""


class WedofQuotaExceeded(WedofGovernorError):
    """Le plafond WEDOF configuré a été atteint."""


def governor_enabled() -> bool:
    value = os.getenv("WEDOF_GOVERNOR_ENABLED")
    if value is not None:
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID"))


def _governor_url() -> str:
    return (os.getenv("WEDOF_GOVERNOR_URL") or DEFAULT_GOVERNOR_URL).rstrip("/")


def _governor_secret() -> str:
    return (
        os.getenv("WEDOF_GOVERNOR_SECRET")
        or os.getenv("WEDOF_API_KEY")
        or ""
    ).strip()


def governor_auth_token() -> str:
    secret = _governor_secret()
    if not secret:
        raise WedofGovernorError("Le secret du compteur WEDOF central est absent.")
    return hmac.new(
        secret.encode("utf-8"),
        b"integrale-academy-wedof-governor-v1",
        hashlib.sha256,
    ).hexdigest()


def _headers() -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "IntegraleAcademy-CRM-Governor/1.0",
        "X-Wedof-Governor-Token": governor_auth_token(),
    }


def _post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        response = requests.post(
            f"{_governor_url()}/{path.lstrip('/')}",
            headers=_headers(),
            json=payload,
            timeout=(2, 5),
        )
    except requests.RequestException as exc:
        raise WedofGovernorError(
            "Le compteur WEDOF central est indisponible ; requête distante bloquée."
        ) from exc
    try:
        body = response.json()
    except (TypeError, ValueError):
        body = {}
    if response.status_code == 429:
        raise WedofQuotaExceeded(
            "Le plafond de requêtes WEDOF est atteint ; requête distante bloquée."
        )
    if not 200 <= response.status_code < 300 or not isinstance(body, dict):
        raise WedofGovernorError(
            "Le compteur WEDOF central a refusé la réservation ; requête distante bloquée."
        )
    return body


def reserve_wedof_request(*, operation: str, method: str, path: str) -> Dict[str, Any]:
    """Réserve une unité avant chaque tentative HTTP envoyée à WEDOF."""
    if not governor_enabled():
        return {"ok": True, "enabled": False}
    return _post("reserve", {
        "origin": "crm",
        "operation": str(operation or "wedof_request")[:80],
        "method": str(method or "GET").upper()[:10],
        "path": str(path or "")[:160],
    })


def acquire_wedof_lock(
    name: str, *, ttl_seconds: int = 3600, owner: Optional[str] = None,
) -> Dict[str, Any]:
    """Prend un verrou partagé entre le CRM et Gestion Stagiaires."""
    if not governor_enabled():
        return {"ok": True, "enabled": False, "acquired": True, "token": ""}
    lock_owner = owner or (
        f"crm:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    )
    return _post("locks/acquire", {
        "name": str(name or "")[:80],
        "owner": lock_owner[:160],
        "ttl_seconds": max(30, min(int(ttl_seconds), 86400)),
    })


def release_wedof_lock(name: str, token: str) -> None:
    if not governor_enabled() or not token:
        return
    try:
        _post("locks/release", {
            "name": str(name or "")[:80],
            "token": str(token)[:160],
        })
    except WedofGovernorError:
        # Le bail possède également une expiration. Une indisponibilité au moment
        # de la libération ne doit pas masquer le résultat métier de la synchro.
        return
