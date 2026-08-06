"""Point d'entrée Gunicorn enrichi des extensions CRM isolées."""

import app as legacy_app

from crm_cnaps_tracking import register_cnaps_tracking_proxy
from crm_salesforce_import import register_salesforce_import
from secretariat_followup_patch import register_secretariat_followup_patch


app = legacy_app.app
register_secretariat_followup_patch(legacy_app)
register_salesforce_import(app)
register_cnaps_tracking_proxy(
    app,
    load_data=legacy_app.load_data,
    find_contact=legacy_app._crm_contact,
    login_required=legacy_app.login_required,
)
