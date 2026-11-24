"""Pages distinctes pour les messages rapides et les autres demandes du secrétariat.

Le point d'enregistrement reste /api/secretariat/demandes : aucune seconde
file de messages ni nouvelle structure de données n'est créée.
"""

from itertools import islice
import re

from flask import jsonify, render_template, request

from secretariat_calls import name_key


RECIPIENT_FUNCTIONS = {
    "Cassandre": "Responsable commerciale",
    "Aurélie": "Responsable BTS et relation clients",
    "Elsa": "Comptabilité",
    "Clément": "Direction générale",
}


def phone_prefix(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = "33" + digits[1:]
    return "33" + digits[3:] if digits.startswith("330") else digits


def contact_suggestions(legacy, fields, role):
    normalizers = {"prenom": name_key, "nom": name_key, "email": legacy._crm_normalize_email, "telephone": phone_prefix}
    query = {key: normalize(str(fields.get(key) or "")[:254]) for key, normalize in normalizers.items()}
    if not (max(len(query["nom"]), len(query["prenom"])) >= 2 or len(query["email"]) >= 3
            or len(re.sub(r"\D", "", str(fields.get("telephone") or ""))) >= 4):
        return [], False
    contacts = legacy._load_data_snapshot().get("crm_contacts", [])

    def matching_rows():
        for contact in contacts:
            if not contact.get("id"):
                continue
            if all(not prefix or normalizers[key](contact.get("mail" if key == "email" else key)).startswith(prefix)
                   for key, prefix in query.items()):
                yield {"id": contact["id"], **legacy._secretariat_contact_fields(contact)}
    matches = list(islice(matching_rows(), 7))
    if role == "external" and matches:
        person = matches[0]
        exact = len(matches) == 1 and (
            (query["email"] and query["email"] == legacy._crm_normalize_email(person["email"]))
            or (len(query["telephone"]) >= 8 and query["telephone"] == phone_prefix(person["telephone"])))
        if not exact:
            # Coordinates also protect the private dossier. A name prefix must not disclose them.
            return [{"display_name": " ".join(part[:1] + "…" for part in (c["nom"], c["prenom"]) if part) or "Fiche CRM",
                     "hint": "Téléphone se terminant par " + phone_prefix(c["telephone"])[-2:] if c["telephone"] else "Précisez les coordonnées"}
                    for c in matches[:6]], len(matches) > 6
    return matches[:6], len(matches) > 6


def register_secretariat_message_pages(legacy_app):
    @legacy_app.app.post("/api/secretariat/coordonnees", endpoint="secretariat_coordonnees")
    def coordonnees():
        role = legacy_app._secretariat_role()
        if not role:
            return legacy_app._secretariat_private(jsonify(error="Connectez-vous au secrétariat."), 401)
        legacy_app.crm_external_access._require_csrf()
        fields = request.get_json(silent=True)
        if not isinstance(fields, dict):
            return legacy_app._secretariat_private(jsonify(error="Recherche invalide."), 400)
        matches, more = contact_suggestions(legacy_app, fields, role)
        return legacy_app._secretariat_private(jsonify(candidates=matches, more=more))

    @legacy_app.app.get("/secretariat/message", endpoint="secretariat_transmettre_message")
    def transmettre_message():
        role = legacy_app._secretariat_role()
        page = render_template(
            "secretariat_message.html",
            recipients=[(member["name"], function) for first_name, function in RECIPIENT_FUNCTIONS.items()
                        for member in legacy_app.USERS.values() if member["first_name"] == first_name],
            desk_role=role,
            desk_csrf=legacy_app.crm_external_access.csrf_token() if role else "",
        )
        return legacy_app._secretariat_private(page)

    @legacy_app.app.get("/secretariat/gestion", endpoint="secretariat_autres_demandes")
    def autres_demandes():
        return legacy_app.secretariat(formation_first=True)
