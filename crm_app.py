"""Point d'entrée Gunicorn enrichi des extensions CRM isolées."""

import app as legacy_app
import crm_salesforce_migration as salesforce_migration

from crm_cnaps_tracking import register_cnaps_tracking_proxy
from crm_salesforce_import import register_salesforce_import
from crm_salesforce_migration_guardrails import install_salesforce_migration_guardrails
from secretariat_followup_patch import register_secretariat_followup_patch


app = legacy_app.app
register_secretariat_followup_patch(legacy_app)
register_salesforce_import(app)
install_salesforce_migration_guardrails(salesforce_migration)
salesforce_migration.register_salesforce_migration(
    app,
    current_user_fn=legacy_app.current_user,
    load_data_fn=legacy_app.load_data,
    login_required_fn=legacy_app.login_required,
    save_data_fn=legacy_app.save_data,
)
register_cnaps_tracking_proxy(
    app,
    load_data=legacy_app.load_data,
    find_contact=legacy_app._crm_contact,
    login_required=legacy_app.login_required,
)