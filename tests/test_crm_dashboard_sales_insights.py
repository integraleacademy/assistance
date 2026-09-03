import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_PY = ROOT / "app.py"
CRM_JS = ROOT / "static" / "crm.js"
CRM_CSS = ROOT / "static" / "crm.css"
CRM_ORIGIN_COMPAT = ROOT / "static" / "crm_dashboard_origins.js"


def test_origin_dashboard_keeps_all_primary_rows_and_ignores_secondary_history():
    javascript = CRM_JS.read_text(encoding="utf-8")
    origin_helpers = javascript[
        javascript.index("const crmOriginFilterValues="):
        javascript.index("const dashboardHasContact=")
    ]
    dashboard_helpers = javascript[
        javascript.index("function dashboardOriginGroups"):
        javascript.index("function dashboardBarRows")
    ]
    script = f"""
{origin_helpers}
const dashboardHasContact=contact=>Boolean(contact.contacted);
const dashboardHasAppointment=contact=>Boolean(contact.appointment);
const dashboardRate=(a,b)=>b?Math.round(a/b*100):0;
const esc=value=>String(value);
{dashboard_helpers}
const assert=(condition,message)=>{{if(!condition)throw new Error(message)}};
const contacts=[
 {{origine:'Google Ads',contacted:true,appointment:true,statut:'Converti',source_history:[{{origin:'Secrétariat'}}]}},
 {{origine:'META',contacted:false,appointment:false,statut:'Nouveaux',source_history:[{{origin:'Google Ads'}}]}},
];
assert(JSON.stringify(crmOriginLabels(contacts[0]))===JSON.stringify(['Google Ads']),'only the primary origin is exposed');
assert(JSON.stringify(crmOriginLabels(contacts[1]))===JSON.stringify(['META']),'history never changes visible attribution');
const primary=dashboardOriginGroups(contacts);
assert(primary.length===8,'all canonical origins remain visible');
assert(primary.find(row=>row.label==='Secrétariat').count===0,'secretariat zero row visible');
assert(primary.find(row=>row.label==='Google Ads').count===1,'one primary Google Ads lead');
assert(primary.find(row=>row.label==='META').count===1,'one primary META lead');
console.log('CRM primary-origin dashboard aggregations: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "CRM primary-origin dashboard aggregations: OK" in completed.stdout
    assert "source_history" not in origin_helpers


def test_dashboard_renders_only_requested_primary_origin_and_sales_sections():
    javascript = CRM_JS.read_text(encoding="utf-8")

    for marker in (
        "Origines principales",
        "Une seule origine principale est utilisée pour chaque piste.",
        "Les huit origines CRM restent visibles, même à zéro.",
        "Priorités de vente",
        "Jamais contactées",
        "Sans prochaine action",
        "Données essentielles manquantes",
    ):
        assert marker in javascript
    for marker in (
        "Origines secondaires",
        "Acquisition et contribution Google Ads",
        "Performance par commercial",
        "Quelles publicités génèrent les meilleurs prospects ?",
    ):
        assert marker not in javascript
    assert "dashboardOriginTable(primaryOrigins,current.length)" in javascript
    assert "secondaryOrigins" not in javascript
    assert "dashboardGoogleAdsTable" not in javascript
    assert "dashboardCommercialTable" not in javascript
    assert "dashboardMetaTable" not in javascript


def test_compact_api_exposes_tracking_without_the_full_form():
    backend = APP_PY.read_text(encoding="utf-8")
    summary = backend[
        backend.index("def _crm_contact_summary_response"):
        backend.index("def _crm_contact_summaries_payload")
    ]

    assert "for key in CRM_GOOGLE_ADS_IDENTIFIER_KEYS" in summary
    assert "key: bool(contact.get(key) or form.get(key))" in summary
    assert "key not in CRM_GOOGLE_ADS_IDENTIFIER_KEYS" in summary
    assert 'summary["google_ads_tracking"] = google_ads_tracking' in summary
    assert 'summary["formulaire"]' not in summary
    assert 'CRM_ASSET_VERSION = "20260903-completeness-followup-1"' in backend


def test_sales_dashboard_panels_are_responsive_and_actionable():
    stylesheet = CRM_CSS.read_text(encoding="utf-8")

    for selector in (
        ".analytics-origin-table td:first-child{",
        ".sales-priority-grid{",
        ".sales-priority-grid a:hover",
    ):
        assert selector in stylesheet
    for selector in (
        ".analytics-secondary-origins{",
        ".google-ads-summary{",
        ".analytics-commercials .analytics-table",
    ):
        assert selector not in stylesheet
    assert "@media(max-width:900px)" in stylesheet
    assert "@media(max-width:650px)" in stylesheet
    assert 'href="/crm/relances"' in CRM_JS.read_text(encoding="utf-8")


def test_dashboard_origin_compatibility_asset_no_longer_monkey_patches_rendering():
    compatibility = CRM_ORIGIN_COMPAT.read_text(encoding="utf-8")

    assert "dashboard = function" not in compatibility
    assert "dashboardKpi = function" not in compatibility
    assert "intentionally kept for cached templates" in compatibility
