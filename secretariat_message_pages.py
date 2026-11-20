"""Pages distinctes pour les messages rapides et les autres demandes du secrétariat.

Le point d'enregistrement reste /api/secretariat/demandes : aucune seconde
file de messages ni nouvelle structure de données n'est créée.
"""

from flask import render_template


def register_secretariat_message_pages(legacy_app):
    @legacy_app.app.get("/secretariat/message", endpoint="secretariat_transmettre_message")
    def transmettre_message():
        role = legacy_app._secretariat_role()
        page = render_template(
            "secretariat_message.html",
            recipients=[member["name"] for member in legacy_app.USERS.values()],
            desk_role=role,
            desk_csrf=legacy_app.crm_external_access.csrf_token() if role else "",
        )
        return legacy_app._secretariat_private(page)

    @legacy_app.app.get("/secretariat/gestion", endpoint="secretariat_autres_demandes")
    def autres_demandes():
        # Même contrôleur et mêmes règles d'accès que le secrétariat historique.
        # Le modèle HTML masque seulement le raccourci « transmettre un message ».
        return legacy_app.secretariat()
