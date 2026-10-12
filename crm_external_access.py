"""Accounts and server-side scope for the external META reception desk."""

import fcntl
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading

from flask import abort, jsonify, make_response, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash


ROLE = "external_secretariat"
HOME = "/crm/pistes"
VERSION = "20261012-external-meta-2"
_ACCOUNT_LOCK = threading.RLock()
EDIT_FIELDS = {
    "prenom", "nom", "telephone", "mail", "formation", "dates_formation",
    "cpf", "cpf_montant", "cpf_palier", "identite_creation", "identite_ok",
    "financement_ft", "inscrit_ft", "financement_perso_possible",
    "carte_pro", "titre_sejour", "titre_sejour_cnaps", "antecedents", "garde_vue",
    "compte_cnaps", "integration_dracar", "desp_type", "commentaires",
    "qualification_flag", "prochaine_action_manuelle", "statut",
    "relance_date", "relance_motif", "disqualification_reason",
    "disqualification_detail",
}
READ_FIELDS = EDIT_FIELDS | {"id", "lieu", "created_at", "updated_at"}
YES_NO_FIELDS = {
    "cpf", "identite_creation", "identite_ok", "financement_ft", "inscrit_ft",
    "financement_perso_possible", "carte_pro", "titre_sejour", "antecedents",
    "garde_vue", "compte_cnaps", "integration_dracar",
}


def account_path(data_file):
    return Path(data_file).parent / "crm_external_users.json"


def read_accounts(data_file):
    try:
        records = json.loads(account_path(data_file).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(records, dict):
        raise ValueError("Invalid external account store")
    return records


def find_user(email, data_file):
    record = read_accounts(data_file).get(email)
    if not record or not record.get("enabled"):
        return None
    return {**record, "email": email, "role": ROLE,
            "first_name": record["name"].split(maxsplit=1)[0]}


def password_matches(user, password):
    return bool(user and user.get("password_hash") and
                check_password_hash(user["password_hash"], password))


def csrf_token():
    if not session.get("external_access_csrf"):
        session["external_access_csrf"] = secrets.token_urlsafe(32)
    return session["external_access_csrf"]


def _require_csrf():
    expected = session.get("external_access_csrf", "")
    provided = request.headers.get("X-CSRF-Token", "")
    if not expected or not hmac.compare_digest(expected, provided):
        abort(403)


def _save_account(data_file, email, update):
    """Atomic writes and a shared file lock also work across Gunicorn workers."""
    path = account_path(data_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _ACCOUNT_LOCK, open(str(path) + ".lock", "a", encoding="utf-8") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = read_accounts(data_file)
        record = update(records.get(email))
        records[email] = record
        fd, temporary = tempfile.mkstemp(prefix=".crm-external-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(records, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return record


def public_account(email, record):
    return {"email": email, "name": record["name"],
            "enabled": bool(record.get("enabled")), "role": ROLE}


def is_new_meta(contact):
    """Match the existing Pistes > Nouveaux META bucket, including its aliases."""
    if contact.get("archived_at") or contact.get("statut") in {"Converti", "Disqualifié"}:
        return False
    if "Nouveaux" not in (contact.get("statut"), contact.get("statut_secondaire")):
        return False
    origin = str(contact.get("origine") or contact.get("source") or "").strip()
    return bool(re.search(r"(^|\s)(meta|facebook|instagram)(\s|$)", origin, re.I)
                if origin else contact.get("meta_source"))


def contact_payload(contact, *, detail=True):
    result = {key: contact.get(key, "") for key in READ_FIELDS}
    result["origine"] = "META"
    if detail:
        result["meta_answers"] = [
            {key: item.get(key, "") for key in ("question", "answer")}
            for item in contact.get("meta_answers", []) if isinstance(item, dict)
        ]
        result["activities"] = [
            {key: item.get(key, "") for key in ("date", "kind", "title", "detail", "author")}
            for item in contact.get("activities", [])
            if isinstance(item, dict) and item.get("kind") in {
                "appel", "suivi", "statut", "qualification", "relance",
            }
        ][:30]
    return result


def treatment_statuses(legacy, data):
    # Conversion and appointment booking remain with the internal team.
    permitted = {"Nouveaux", "En cours", "A relancer", "Blocage", "Disqualifié",
                 "RDV programmé sans rendez-vous"}
    return [value for value in legacy._crm_statuses(data) if value in permitted]


def install(legacy):
    app = legacy.app

    @app.before_request
    def restrict_external_account():
        email = session.get("user_email")
        # Internal accounts keep their existing routes. Unknown/stale identities
        # must never fall through to the legacy session-only decorators.
        if not email or email in legacy.USERS:
            return None
        user = legacy.current_user()
        if not user:
            next_path = HOME if session.get("user_role") == ROLE else request.path
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"error": "Votre accès a expiré. Reconnectez-vous."}), 401
            return redirect(legacy.url_for("login", next=next_path))
        if request.endpoint in {"login", "logout"}:
            return None
        if request.endpoint == "static" and request.method in {"GET", "HEAD"}:
            allowed_assets = {"logo.png", "iaconnectcrm.png", "favicon_32x32.png",
                              "crm_external.css", "crm_external.js"}
            if (request.view_args or {}).get("filename") in allowed_assets:
                return None
        if request.endpoint in {"crm", "crm_uppercase"} and request.method in {"GET", "HEAD"}:
            section = (request.view_args or {}).get("section")
            if request.path != HOME or section != "pistes":
                return redirect(HOME)
            return render_template("crm_external.html", user=user, version=VERSION,
                                   csrf=csrf_token())
        if request.endpoint in {"external_meta_list", "external_meta_contact"}:
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "Accès limité à Pistes > Nouveaux META."}), 403
        return "Accès limité à Pistes > Nouveaux META.", 403

    @app.after_request
    def private_external_responses(response):
        if (session.get("user_role") == ROLE or
                request.path.startswith("/api/crm/external-") or
                request.path == "/crm/acces-secretariat"):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.vary.add("Cookie")
        return response

    def require_role(role):
        user = legacy.current_user()
        if not user:
            abort(401)
        if user.get("role") != role:
            abort(403)
        return user

    @app.get("/crm/acces-secretariat")
    @legacy.login_required
    def external_accounts_page():
        user = require_role("admin")
        return render_template("crm_external_accounts.html", user=user,
                               csrf=csrf_token(), version=VERSION)

    @app.route("/api/crm/external-accounts", methods=["GET", "POST", "PATCH"])
    def external_accounts_api():
        user = require_role("admin")
        if request.method == "GET":
            return jsonify([public_account(email, record) for email, record in
                            sorted(read_accounts(legacy.DATA_FILE).items())])
        _require_csrf()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) - {"email", "name", "password", "enabled"}:
            return jsonify({"error": "Paramètres de compte invalides."}), 400
        email = str(payload.get("email") or "").strip().lower()
        if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            return jsonify({"error": "Renseignez une adresse e-mail valide."}), 400
        if email in legacy.USERS:
            return jsonify({"error": "Cette adresse appartient déjà à un compte interne."}), 409
        password = payload.get("password", "")
        if not isinstance(password, str) or (password and not 12 <= len(password) <= 256):
            return jsonify({"error": "Le mot de passe doit contenir entre 12 et 256 caractères."}), 400
        if "enabled" in payload and not isinstance(payload["enabled"], bool):
            return jsonify({"error": "Le statut du compte est invalide."}), 400
        name = str(payload.get("name") or "").strip()
        if "name" in payload and not 1 <= len(name) <= 120:
            return jsonify({"error": "Renseignez un nom (120 caractères maximum)."}), 400

        def update(previous):
            creating = request.method == "POST"
            if creating and previous:
                abort(409, description="Un accès existe déjà pour cette adresse.")
            if not creating and not previous:
                abort(404)
            if creating and (not name or not password):
                abort(400, description="Le nom et un mot de passe d’au moins 12 caractères sont requis.")
            record = dict(previous or {})
            if name:
                record["name"] = name
            record["enabled"] = payload.get("enabled", record.get("enabled", True))
            if password:
                record["password_hash"] = generate_password_hash(password)
            # Every admin edit invalidates existing sessions, including re-enabling.
            record["session_version"] = secrets.token_urlsafe(24)
            record["updated_at"] = legacy._crm_now()
            record["updated_by"] = user["email"]
            return record

        record = _save_account(legacy.DATA_FILE, email, update)
        return jsonify(public_account(email, record)), 201 if request.method == "POST" else 200

    @app.get("/api/crm/external-meta")
    def external_meta_list():
        require_role(ROLE)
        data = legacy._crm_prepared_read_model()
        contacts = [contact_payload(contact, detail=False)
                    for contact in data.get("crm_contacts", []) if is_new_meta(contact)]
        contacts.sort(key=lambda contact: str(contact.get("created_at") or ""), reverse=True)
        return jsonify({"contacts": contacts, "statuses": treatment_statuses(legacy, data)})

    @app.route("/api/crm/external-meta/<contact_id>", methods=["GET", "PATCH"])
    def external_meta_contact(contact_id):
        require_role(ROLE)
        if request.method == "GET":
            data = legacy._crm_prepared_read_model()
            contact = legacy._crm_contact(data, contact_id)
            if not contact or not is_new_meta(contact):
                return jsonify({"error": "Cette piste n’est plus disponible dans Nouveaux META."}), 404
            return jsonify(contact_payload(contact))
        _require_csrf()
        payload = request.get_json(silent=True)
        if (not isinstance(payload, dict) or not payload
                or set(payload) - EDIT_FIELDS - {"call_note"}
                or any(not isinstance(value, str) or len(value) > 5000 for value in payload.values())):
            return jsonify({"error": "Champs non autorisés ou invalides."}), 400
        for key in YES_NO_FIELDS.intersection(payload):
            if payload[key] not in {"", "OUI", "NON"}:
                return jsonify({"error": "Les réponses doivent être OUI ou NON."}), 400
        with legacy._CRM_RECONCILIATION_LOCK:
            data = legacy.load_data()
            contact = legacy._crm_contact(data, contact_id)
            if contact:
                # Use the same legacy repairs as the read model, restricted to
                # this record. In particular, older META leads may have an
                # empty status or answers awaiting the standard backfill.
                legacy._crm_prepare_contacts({**data, "crm_contacts": [contact]})
            # Recheck inside the write lock: a colleague may have processed it
            # since the external operator opened the sheet.
            if not contact or not is_new_meta(contact):
                return jsonify({"error": "Cette piste n’est plus disponible dans Nouveaux META."}), 404
            status = payload.get("statut", contact.get("statut"))
            if "statut" in payload and status not in treatment_statuses(legacy, data):
                return jsonify({"error": "Étape non autorisée."}), 400
            if status == "Disqualifié" and not payload.get("disqualification_reason", "").strip():
                return jsonify({"error": "Le motif de disqualification est obligatoire."}), 400
            if status == "A relancer" and not payload.get("relance_date", contact.get("relance_date")):
                return jsonify({"error": "Choisissez une date de relance."}), 400
            note = payload.pop("call_note", "").strip()
            if note:
                legacy._crm_activity(contact, "appel", "Appel consigné", note)
            response = make_response(legacy._crm_patch_contact_locked(data, contact, contact_id))
            if response.status_code >= 400:
                return response
            in_scope = is_new_meta(contact)
            return jsonify({"in_scope": in_scope,
                            "contact": contact_payload(contact) if in_scope else None})
