"""Proxy sécurisé des données Gestion Stagiaires utilisées par le CRM."""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Set, Tuple
from urllib.parse import urlsplit, urlunsplit

import requests
from flask import jsonify

from candidate_scoring import normalize_cnaps_tracking_status


REMOTE_PATH = "/api/integrations/crm/stagiaires"
CNAPS_TRACKING_PATH = "/api/integrations/crm/cnaps-tracking"
LINK_EXISTING_PATH = f"{REMOTE_PATH}/link-existing"
CNAPS_PUBLIC_ANNUAIRE_PAGE_URL = (
    "https://espace-consultation.cnaps.interieur.gouv.fr/annuaire/"
    "app/annuaire-public"
)
CNAPS_PUBLIC_ANNUAIRE_ENDPOINT = os.getenv(
    "CNAPS_PUBLIC_ANNUAIRE_ENDPOINT",
    "https://espace-consultation.cnaps.interieur.gouv.fr/annuaire/"
    "api/back/public/annuaire/search/personne-physique",
).strip()
CNAPS_PUBLIC_ANNUAIRE_PAGE_SIZE = 10
CNAPS_PUBLIC_ANNUAIRE_MAX_PAGES = 25
CNAPS_TITLE_ORDER = ["AP SH", "AP A3P", "CP SH", "CP A3P"]
_SECRET_KEYS = {
    "token", "public_token", "trainee_token", "api_token", "authorization",
    "x-api-key",
}


def normalized_name(value: Any) -> str:
    """Normalise un nom pour une recherche indépendante de sa présentation."""
    text = " ".join(str(value or "").strip().split()).casefold()
    return "".join(
        character for character in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(character)
    )


def normalize_cnaps_nub(value: Any) -> str:
    """Conserve uniquement les chiffres du numéro unique bénéficiaire."""
    return re.sub(r"\D+", "", str(value or ""))


def _cnaps_activity_code(value: Any) -> str:
    """Reprend les libellés courts utilisés dans Gestion Stagiaires."""
    label = normalized_name(value)
    prefix = ""
    if label.startswith("autorisation prealable"):
        prefix = "AP"
    elif label.startswith("carte professionnelle"):
        prefix = "CP"
    activity = ""
    if "surveillance humaine ou gardiennage" in label:
        activity = "SH"
    elif "protection physique des personnes" in label:
        activity = "A3P"
    return f"{prefix} {activity}".strip() or str(value or "").strip() or "Titre CNAPS"


def _cnaps_card_error(nub: str, *, http_status: Any = None) -> Dict[str, Any]:
    return {
        "check_status": "error",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "nub": normalize_cnaps_nub(nub),
        "titles": [],
        "active_titles": [],
        "message": "Vérification CNAPS impossible",
        "error": "cnaps_unavailable",
        "http_status": http_status,
    }


def fetch_cnaps_card_validity(
    last_name: str,
    nub: str,
    *,
    session: Any = None,
) -> Dict[str, Any]:
    """Interroge l'annuaire public CNAPS comme Gestion Stagiaires.

    La réponse est volontairement limitée aux titres, statuts et dates utiles à
    la fiche CRM. Les données d'autres personnes éventuellement renvoyées par
    l'annuaire sont écartées par le couple nom/NUB.
    """
    normalized_last_name = " ".join(str(last_name or "").strip().split()).upper()
    normalized_nub = normalize_cnaps_nub(nub)
    if (not CNAPS_PUBLIC_ANNUAIRE_ENDPOINT or not normalized_last_name
            or len(normalized_nub) != 7):
        return _cnaps_card_error(normalized_nub)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": CNAPS_PUBLIC_ANNUAIRE_PAGE_URL,
        "Origin": "https://espace-consultation.cnaps.interieur.gouv.fr",
        "User-Agent": (
            "Mozilla/5.0 (compatible; assistance-crm/1.0; "
            "+https://assistance-alw9.onrender.com)"
        ),
    }
    requester = session or requests.Session()
    rows: List[Dict[str, Any]] = []
    total_pages = 1
    page = 0
    last_http_status = None
    try:
        while page < min(total_pages, CNAPS_PUBLIC_ANNUAIRE_MAX_PAGES):
            payload = {
                "nom": normalized_last_name,
                "nub": normalized_nub,
                "page": page,
                "size": CNAPS_PUBLIC_ANNUAIRE_PAGE_SIZE,
                "sorts": [
                    {"field": "nom", "asc": True},
                    {"field": "dateFinValidite", "asc": True},
                ],
            }
            response = requester.post(
                CNAPS_PUBLIC_ANNUAIRE_ENDPOINT,
                json=payload,
                headers=headers,
                timeout=(3, 10),
                allow_redirects=True,
            )
            last_http_status = getattr(response, "status_code", None)
            if last_http_status != 200:
                return _cnaps_card_error(
                    normalized_nub, http_status=last_http_status,
                )
            content_type = str(
                (getattr(response, "headers", {}) or {}).get("Content-Type", "")
            ).lower()
            if content_type and "json" not in content_type:
                return _cnaps_card_error(
                    normalized_nub, http_status=last_http_status,
                )
            payload_response = response.json()
            if (not isinstance(payload_response, dict)
                    or not isinstance(payload_response.get("results"), list)):
                return _cnaps_card_error(
                    normalized_nub, http_status=last_http_status,
                )
            rows.extend(
                row for row in payload_response["results"]
                if isinstance(row, dict)
            )
            if page == 0:
                try:
                    total_pages = max(
                        1, int(payload_response.get("totalPages") or 1),
                    )
                except (TypeError, ValueError):
                    total_pages = 1
            page += 1
    except Exception:
        return _cnaps_card_error(normalized_nub, http_status=last_http_status)

    titles: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str, str, str]] = set()
    expected_name = normalized_name(normalized_last_name)
    for row in rows:
        row_nub = normalize_cnaps_nub(row.get("nub"))
        row_name = normalized_name(row.get("nom"))
        if row_nub != normalized_nub or row_name != expected_name:
            continue
        label = str(row.get("typeActivite") or "").strip()
        status = str(row.get("agrementStatutEs") or "").strip().upper()
        expires_at = str(row.get("dateFinValidite") or "").strip()
        code = _cnaps_activity_code(label)
        key = (code, label, status, expires_at)
        if key in seen:
            continue
        seen.add(key)
        titles.append({
            "code": code,
            "label": label or code,
            "status": status or "INCONNU",
            "display_status": " ".join(
                part for part in (code, status or "INCONNU") if part
            ),
            "expires_at": expires_at or None,
        })
    titles.sort(key=lambda title: (
        CNAPS_TITLE_ORDER.index(title["code"])
        if title["code"] in CNAPS_TITLE_ORDER else 999,
        title["label"],
        title.get("expires_at") or "",
    ))
    active_titles = [title for title in titles if title["status"] == "ACTIF"]
    return {
        "check_status": "success",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "nub": normalized_nub,
        "titles": titles,
        "active_titles": active_titles,
        "message": None if titles else "Aucun titre CNAPS trouvé",
        "error": None,
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


def gestion_stagiaires_link_existing_url() -> str:
    """Construit l'endpoint de rattachement depuis la même origine publique."""
    api_url = gestion_stagiaires_api_url()
    if not api_url:
        return ""
    parsed = urlsplit(api_url)
    return urlunsplit((parsed.scheme, parsed.netloc, LINK_EXISTING_PATH, "", ""))


def gestion_stagiaires_cnaps_tracking_api_url() -> str:
    """Construit l'endpoint du suivi CNAPS depuis l'origine configurée."""
    api_url = gestion_stagiaires_api_url()
    if not api_url:
        return ""
    parsed = urlsplit(api_url)
    return urlunsplit((parsed.scheme, parsed.netloc, CNAPS_TRACKING_PATH, "", ""))


def crm_contact_identity(contact: Dict[str, Any]) -> Dict[str, str]:
    """Normalise les champs d'identité partagés avec la conversion CRM."""
    def clean(key: str) -> str:
        value = contact.get(key)
        if value is None or str(value).strip().lower() in {"null", "undefined"}:
            return ""
        return str(value).strip()

    return {"prenom": clean("prenom"), "nom": clean("nom"),
            "email": clean("mail"), "telephone": clean("telephone")}


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


def _remote_titles(remote):
    titles = []
    for owner in (remote.get("card_pro"), remote.get("cnaps")):
        if isinstance(owner, dict) and isinstance(owner.get("titles"), list):
            titles.extend(item for item in owner["titles"] if isinstance(item, dict))
    return titles


def extract_cnaps_raw_status(remote):
    """Extrait uniquement le statut principal, sans confondre un statut annexe."""
    remote = remote if isinstance(remote, dict) else {}
    cnaps = remote.get("cnaps") if isinstance(remote.get("cnaps"), dict) else {}
    for owner in (cnaps, remote):
        for key in ("cnaps_status", "statut_cnaps", "status", "statut"):
            if owner.get(key) not in (None, ""):
                return owner[key]
    return ""


def scoring_snapshot_from_remote(remote, http_status=200, now=None):
    """Projette une réponse distante vers le cache minimal autorisé du score."""
    remote = remote if isinstance(remote, dict) else {}
    raw = extract_cnaps_raw_status(remote)
    status = normalize_cnaps_tracking_status({"raw_status": raw, "found": remote.get("found"),
                                              "http_status": http_status, "reason": remote.get("reason")})
    current = now or datetime.now(timezone.utc)
    if isinstance(current, str):
        try: current = datetime.fromisoformat(current.replace("Z", "+00:00"))
        except ValueError: current = datetime.now(timezone.utc)
    active, expired, active_expiry = False, False, None
    for title in _remote_titles(remote):
        state = normalized_name(title.get("state") or title.get("status") or title.get("etat"))
        kind = normalized_name(title.get("type") or title.get("title") or title.get("titre") or title.get("name"))
        expiry = title.get("expires_at") or title.get("expiration_date") or title.get("date_expiration") or title.get("valid_until")
        expiry_date = None
        if expiry:
            try:
                expiry_date = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
                if expiry_date.tzinfo is None:
                    expiry_date = expiry_date.replace(tzinfo=timezone.utc)
            except ValueError: pass
        is_professional = not kind or any(word in kind for word in ("profession", "carte"))
        is_expired = "expire" in state or (expiry_date is not None and expiry_date <= current)
        if is_professional and is_expired: expired = True
        if is_professional and not is_expired and state not in {"refuse", "rejete", "inactif", "inactive"}:
            active = True
            active_expiry = str(expiry) if expiry else None
    return {"found": False if status == "no_result" else remote.get("found", True),
            "normalized_status": status, "raw_status": str(raw)[:80],
            "has_active_professional_title": active,
            "has_expired_professional_title": expired,
            "active_title_expires_at": active_expiry,
            "last_checked_at": remote.get("last_checked_at"),
            "synced_at": current.isoformat()}


def proxy_reglementaire(app, contact: Dict[str, Any], http_get=None, http_post=None):
    """Recherche le stagiaire puis tente au besoin de rattacher le suivi demandé."""
    api_url = gestion_stagiaires_api_url()
    api_token = str(os.getenv("GESTION_STAGIAIRES_API_TOKEN") or "").strip()
    if not api_url or not api_token:
        return jsonify({"error": "Connexion Gestion Stagiaires non configurée"}), 503

    formation = str(contact.get("formation") or "").strip().upper()
    is_cnaps_training = formation in {"APS", "A3P"}
    identity = crm_contact_identity(contact)
    if is_cnaps_training:
        api_url = gestion_stagiaires_cnaps_tracking_api_url()
        params = {
            "nom": normalized_name(identity["nom"]),
            "prenom": normalized_name(identity["prenom"]),
        }
        if not params["nom"] or not params["prenom"]:
            return jsonify({"error": "Le suivi CNAPS nécessite le nom et le prénom du contact."}), 422
    else:
        params = {"crm_contact_id": contact["id"]}

    try:
        response = (http_get or requests.get)(
            api_url,
            params=params,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
            },
            timeout=float(os.getenv("GESTION_STAGIAIRES_CNAPS_TIMEOUT", "8")),
        )
        remote = response.json() if getattr(response, "content", b"") else {}
    except (requests.RequestException, ValueError, TypeError) as exc:
        # Ne journaliser ni URL signée, ni en-têtes, ni corps distant.
        app.logger.warning("Gestion Stagiaires indisponible (%s)", type(exc).__name__)
        return jsonify({"error": "Gestion Stagiaires est momentanément indisponible"}), 502

    if not isinstance(remote, dict):
        return jsonify({"error": "Réponse invalide de Gestion Stagiaires"}), 502
    if response.status_code == 200:
        return jsonify(public_payload(remote))
    if is_cnaps_training and response.status_code == 404:
        return jsonify({
            "error": "Aucun dossier CNAPS correspondant à ce nom et ce prénom n’a été trouvé dans le suivi CNAPS.",
            "reason": "cnaps_not_found",
        }), 404
    needs_tracking = (formation == "DESP"
                      and str(contact.get("desp_type") or "").strip().upper() == "VAE")
    if response.status_code == 404 and needs_tracking:
        identity = crm_contact_identity(contact)
        if not identity["prenom"] or not identity["nom"] or not (identity["email"] or identity["telephone"]):
            return jsonify({"error": "Le rattachement automatique nécessite le nom, le prénom et au moins une adresse e-mail ou un téléphone.",
                            "reason": "insufficient_identity"}), 422
        payload = {"crm_contact_id": str(contact["id"]), **identity, "source": "integrale_connect"}
        try:
            linked_response = (http_post or requests.post)(
                gestion_stagiaires_link_existing_url(), json=payload,
                headers={"Authorization": f"Bearer {api_token}", "Accept": "application/json",
                         "Content-Type": "application/json"},
                timeout=float(os.getenv("GESTION_STAGIAIRES_CNAPS_TIMEOUT", "8")))
            linked = linked_response.json() if getattr(linked_response, "content", b"") else {}
        except (requests.RequestException, ValueError, TypeError) as exc:
            app.logger.warning("Gestion Stagiaires indisponible (%s)", type(exc).__name__)
            return jsonify({"error": "Gestion Stagiaires est momentanément indisponible"}), 502
        if not isinstance(linked, dict):
            return jsonify({"error": "Réponse invalide de Gestion Stagiaires"}), 502
        if linked_response.status_code == 200:
            return jsonify(public_payload(linked))
        if linked_response.status_code == 401:
            return jsonify({"error": "L’intégration Gestion Stagiaires n’est pas correctement configurée."}), 401
        reason = linked.get("reason")
        allowed_reasons = {"trainee_not_found", "conflicting_matches", "ambiguous_match",
                           "identity_mismatch", "crm_contact_id_already_used", "trainee_already_linked"}
        safe = {"error": "Le rattachement automatique du stagiaire a échoué."}
        if reason in allowed_reasons:
            safe["reason"] = reason
        status = linked_response.status_code if linked_response.status_code in {400, 404, 409, 422} else 502
        return jsonify(safe), status
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

    # L'application principale possède la route enrichie (snapshot, cache et score).
    # Ne jamais la remplacer par le proxy minimal au chargement de l'entrypoint Gunicorn.
    endpoint = "crm_contact_reglementaire"
    if endpoint in app.view_functions:
        return

    @login_required
    def crm_contact_reglementaire(contact_id):
        contact = find_contact(load_data(), contact_id)
        if not contact:
            return jsonify({"error": "Contact introuvable"}), 404
        return proxy_reglementaire(app, contact, http_get=http_get)

    app.add_url_rule(
        "/api/crm/contacts/<contact_id>/reglementaire",
        endpoint=endpoint,
        view_func=crm_contact_reglementaire,
        methods=["GET"],
    )
