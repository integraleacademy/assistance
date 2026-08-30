"""Point d'entrée Gunicorn enrichi des extensions CRM isolées."""

import app as legacy_app
import crm_salesforce_migration as salesforce_migration
import crm_salesforce_tasks_import as salesforce_tasks_import

from crm_aircall_ai import register_aircall_ai_crm
from crm_aircall_call_capture_patch import install_aircall_call_capture_patch
from crm_aircall_caller_context import register_aircall_caller_context
from crm_aircall_dossier import register_aircall_dossier_actions
from crm_aircall_lead_capture import register_aircall_lead_capture
from crm_cnaps_tracking import register_cnaps_tracking_proxy
from crm_google_ads import register_google_ads_offline_conversions
from crm_location_normalization import (
    install_crm_location_normalization,
    install_salesforce_location_guardrails,
)
from crm_pipeline_status_consistency import (
    install_crm_pipeline_status_consistency,
)
from crm_salesforce_anomaly_followups_import import (
    register_salesforce_anomaly_followups_import,
)
from crm_salesforce_chatter_import import register_salesforce_chatter_import
from crm_salesforce_date_guardrails import install_salesforce_date_guardrails
from crm_salesforce_existing_repair import register_salesforce_existing_repair
from crm_salesforce_genuine_new_guardrails import (
    install_salesforce_genuine_new_guardrails,
)
from crm_salesforce_import import register_salesforce_import
from crm_salesforce_migration_guardrails import install_salesforce_migration_guardrails
from crm_salesforce_old_followups_import import (
    register_salesforce_old_followups_import,
)
from crm_salesforce_report_guardrails import install_salesforce_report_guardrails
from crm_salesforce_scope_guardrails import (
    disable_legacy_salesforce_import,
    enforce_salesforce_scope_route,
    install_salesforce_scope_guardrails,
)
from crm_salesforce_status_guardrails import install_salesforce_status_guardrails
from crm_salesforce_tasks_report_guardrails import (
    install_salesforce_tasks_report_guardrails,
)
from crm_salesforce_tasks_status_guardrails import (
    install_salesforce_tasks_status_guardrails,
)
from crm_salesforce_transaction_guardrails import serialize_salesforce_writes
from secretariat_followup_patch import register_secretariat_followup_patch


app = legacy_app.app
install_crm_location_normalization(legacy_app)
install_crm_pipeline_status_consistency(legacy_app)
register_secretariat_followup_patch(legacy_app)
register_salesforce_import(app)
install_salesforce_migration_guardrails(salesforce_migration)
install_salesforce_status_guardrails(salesforce_migration)
install_salesforce_date_guardrails(salesforce_migration)
install_salesforce_report_guardrails(salesforce_migration)
install_salesforce_location_guardrails(salesforce_migration)
install_salesforce_scope_guardrails(salesforce_migration)
install_salesforce_genuine_new_guardrails(salesforce_migration)
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
register_salesforce_existing_repair(
    app,
    migration_module=salesforce_migration,
    current_user_fn=legacy_app.current_user,
    load_data_fn=legacy_app.load_data,
    login_required_fn=legacy_app.login_required,
    save_data_fn=legacy_app.save_data,
    transaction_lock=legacy_app._CRM_RECONCILIATION_LOCK,
)
register_salesforce_chatter_import(
    app,
    current_user_fn=legacy_app.current_user,
    load_data_fn=legacy_app.load_data,
    login_required_fn=legacy_app.login_required,
    save_data_fn=legacy_app.save_data,
    transaction_lock=legacy_app._CRM_RECONCILIATION_LOCK,
)
install_salesforce_tasks_report_guardrails(salesforce_tasks_import)
install_salesforce_tasks_status_guardrails(salesforce_tasks_import)
register_salesforce_old_followups_import(
    app,
    migration_module=salesforce_migration,
    tasks_module=salesforce_tasks_import,
    current_user_fn=legacy_app.current_user,
    load_data_fn=legacy_app.load_data,
    login_required_fn=legacy_app.login_required,
    save_data_fn=legacy_app.save_data,
    transaction_lock=legacy_app._CRM_RECONCILIATION_LOCK,
)
salesforce_tasks_import.register_salesforce_tasks_import(
    app,
    current_user_fn=legacy_app.current_user,
    load_data_fn=legacy_app.load_data,
    login_required_fn=legacy_app.login_required,
    save_data_fn=legacy_app.save_data,
    transaction_lock=legacy_app._CRM_RECONCILIATION_LOCK,
)
register_salesforce_anomaly_followups_import(
    app,
    current_user_fn=legacy_app.current_user,
    load_data_fn=legacy_app.load_data,
    login_required_fn=legacy_app.login_required,
    save_data_fn=legacy_app.save_data,
    transaction_lock=legacy_app._CRM_RECONCILIATION_LOCK,
)
register_cnaps_tracking_proxy(
    app,
    load_data=legacy_app.load_data,
    find_contact=legacy_app._crm_contact,
    login_required=legacy_app.login_required,
)
install_aircall_call_capture_patch()
register_aircall_ai_crm(legacy_app)
register_aircall_dossier_actions(legacy_app)
register_aircall_caller_context(legacy_app)
register_aircall_lead_capture(legacy_app)
register_google_ads_offline_conversions(legacy_app)
