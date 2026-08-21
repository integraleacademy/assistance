"""Point d'entrée Gunicorn enrichi des extensions CRM isolées."""

import app as legacy_app
import crm_salesforce_migration as salesforce_migration

from crm_cnaps_tracking import register_cnaps_tracking_proxy
from crm_google_ads import register_google_ads_offline_conversions
from crm_salesforce_date_guardrails import install_salesforce_date_guardrails
from crm_salesforce_import import register_salesforce_import
from crm_salesforce_migration_guardrails import install_salesforce_migration_guardrails
from crm_salesforce_report_guardrails import install_salesforce_report_guardrails
from crm_salesforce_scope_guardrails import (
    disable_legacy_salesforce_import,
    enforce_salesforce_scope_route,
    install_salesforce_scope_guardrails,
)
from crm_salesforce_status_guardrails import install_salesforce_status_guardrails
from crm_salesforce_transaction_guardrails import serialize_salesforce_writes
from secretariat_followup_patch import register_secretariat_followup_patch


app = legacy_app.app
register_secretariat_followup_patch(legacy_app)
register_salesforce_import(app)
install_salesforce_migration_guardrails(salesforce_migration)
install_salesforce_status_guardrails(salesforce_migration)
install_salesforce_date_guardrails(salesforce_migration)
install_salesforce_report_guardrails(salesforce_migration)
install_salesforce_scope_guardrails(salesforce_migration)
salesforce_migration.register_salesforce_migration(
    app,
    current_user_fn=legacy_app.current_user,
    load_data_fn=legacy_app.load_data,
    login_required_fn=legacy_app.login_required,
    save_data_fn=legacy_app.save_data,
)
serialize_salesforce_writes(
    app,
    request=legacy_app.request,
    transaction_lock=legacy_app._CRM_RECONCILIATION_LOCK,
)
enforce_salesforce_scope_route(
    app,
    request=legacy_app.request,
    jsonify_fn=legacy_app.jsonify,
)
disable_legacy_salesforce_import(
    app,
    jsonify_fn=legacy_app.jsonify,
)
register_cnaps_tracking_proxy(
    app,
    load_data=legacy_app.load_data,
    find_contact=legacy_app._crm_contact,
    login_required=legacy_app.login_required,
)
register_google_ads_offline_conversions(legacy_app)
