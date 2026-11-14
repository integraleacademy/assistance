"""Server-to-server access to the academy's synchronized EDOF directory."""
import os
import time
from urllib.parse import urlsplit, urlunsplit

import requests

from crm_cnaps_tracking import gestion_stagiaires_api_url


class EdofAPIError(RuntimeError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def token():
    return os.getenv("GESTION_STAGIAIRES_API_TOKEN", "").strip()


def gestion_origin():
    base = urlsplit(gestion_stagiaires_api_url() or "https://gestionstagiaires-r5no.onrender.com")
    return urlunsplit((base.scheme, base.netloc, "", "", ""))


def read(path, *, params=None, operation=None, retry_budget=None):
    secret = token()
    if not secret:
        raise EdofAPIError("La connexion du CRM à Gestion Stagiaires n’est pas configurée.")
    url = gestion_origin() + "/api/integrations/crm/integrale-edof" + path
    retries = 1 if retry_budget is None else retry_budget
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, headers={"Authorization": f"Bearer {secret}", "Accept": "application/json"},
                                    params=params, timeout=(3, 15), allow_redirects=False)
        except requests.RequestException:
            if attempt == retries:
                raise EdofAPIError("Impossible de joindre Intégrale EDOF.") from None
        else:
            if response.status_code not in {502, 503, 504} or attempt == retries:
                break
        time.sleep(0.25)
    if response.status_code == 404 and path.startswith("/dossiers/"):
        raise EdofAPIError("Ce dossier est absent de la liste synchronisée par Intégrale EDOF.", 404)
    if not 200 <= response.status_code < 300:
        raise EdofAPIError(f"Intégrale EDOF a répondu avec le statut {response.status_code}.", response.status_code)
    try:
        return response.json(), response.headers
    except ValueError:
        raise EdofAPIError("Réponse Intégrale EDOF illisible.") from None
