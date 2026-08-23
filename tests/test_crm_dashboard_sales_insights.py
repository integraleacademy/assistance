import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_PY = ROOT / "app.py"
CRM_JS = ROOT / "static" / "crm.js"
CRM_CSS = ROOT / "static" / "crm.css"


def test_origin_dashboard_keeps_all_primary_rows_and_deduplicates_secondaries():
    javascript = CRM_JS.read_text(encoding="utf-8")
    helpers = javascript[
        javascript.index("function dashboardOriginGroups"):
        javascript.index("function dashboardBarRows")
    ]
    script = f"""
const crmOriginFilterValues=['META','Google Ads','Site internet','Bouche à oreilles','Mon Compte Formation','Secrétariat','Simulateur VAE','Autre'];
const labels=new Map();
const crmOriginLabels=contact=>contact.origins;
const dashboardOrigin=contact=>contact.origins[0];
const dashboardHasContact=contact=>Boolean(contact.contacted);
const dashboardHasAppointment=contact=>Boolean(contact.appointment);
const dashboardRate=(a,b)=>b?Math.round(a/b*100):0;
const canonicalCrmOriginValue=value=>value;
const esc=value=>String(value);
const dashboardGroup=(list,labelFor)=>[];
{helpers}
const assert=(condition,message)=>{{if(!condition)throw new Error(message)}};
const contacts=[
 {{origins:['Google Ads','Secrétariat','Secrétariat'],contacted:true,appointment:true,statut:'Converti',source_history:[]}},
 {{origins:['META','Google Ads'],contacted:false,appointment:false,statut:'Nouveaux',source_history:[],google_ads_tracking:{{wbraid:'w-1',utm_campaign:'Été',utm_source:'google',utm_medium:'cpc'}}}},
];
const primary=dashboardOriginGroups(contacts);
assert(primary.length===8,'all canonical origins remain visible');
assert(primary.find(row=>row.label==='Secrétariat').count===0,'secretariat zero row visible');
assert(primary.find(row=>row.label==='Google Ads').count===1,'one primary Google Ads lead');
const secondary=dashboardOriginGroups(contacts,true);
assert(secondary.find(row=>row.label==='Secrétariat').count===1,'duplicate secondary counted once');
assert(secondary.find(row=>row.label==='Google Ads').count===1,'assisted Google Ads counted');
const ads=dashboardGoogleAdsData(contacts);
assert(ads.contacts.length===2,'primary and assisted Google Ads included');
assert(ads.primary.length===1&&ads.assisted.length===1,'primary and assisted separated');
assert(ads.identifierTypes.WBRAID===1,'wbraid coverage counted');
assert(ads.campaigns[0].label==='Été','utm campaign exposed');
console.log('CRM sales dashboard aggregations: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "CRM sales dashboard aggregations: OK" in completed.stdout


def test_dashboard_renders_primary_secondary_google_and_sales_sections():
    javascript = CRM_JS.read_text(encoding="utf-8")

    for marker in (
        "Origines principales",
        "Les huit origines CRM restent visibles, même à zéro.",
        "Origines secondaires",
        "Une piste peut contribuer à plusieurs lignes.",
        "Acquisition et contribution Google Ads",
        "Identifiant de clic capté",
        "GCLID · WBRAID · GBRAID",
        "Les dépenses, impressions, clics, CPC, CPL et ROAS",
        "Priorités de vente",
        "Jamais contactées",
        "Sans prochaine action",
        "Données essentielles manquantes",
        "Performance par commercial",
    ):
        assert marker in javascript
    assert "dashboardOriginTable(primaryOrigins,current.length)" in javascript
    assert "dashboardOriginTable(secondaryOrigins,current.length,true)" in javascript
    assert "dashboardGoogleAdsTable(googleAds)" in javascript
    assert "dashboardCommercialTable(current)" in javascript
    assert "esc(row.label)" in javascript
    assert "esc(row.channel)" in javascript


def test_compact_api_exposes_tracking_without_the_full_form():
    backend = APP_PY.read_text(encoding="utf-8")
    summary = backend[
        backend.index("def _crm_contact_summary_response"):
        backend.index("def _crm_contact_summaries_payload")
    ]

    assert "for key in CRM_GOOGLE_ADS_TRACKING_KEYS" in summary
    assert 'summary["google_ads_tracking"] = google_ads_tracking' in summary
    assert 'summary["formulaire"]' not in summary
    assert 'CRM_ASSET_VERSION = "20260823-sales-dashboard-1"' in backend


def test_sales_dashboard_panels_are_responsive_and_actionable():
    stylesheet = CRM_CSS.read_text(encoding="utf-8")

    for selector in (
        ".analytics-secondary-origins{",
        ".google-ads-summary{",
        ".sales-priority-grid{",
        ".sales-priority-grid a:hover",
        ".analytics-commercials .analytics-table",
    ):
        assert selector in stylesheet
    assert "@media(max-width:900px)" in stylesheet
    assert "@media(max-width:650px)" in stylesheet
    assert 'href="/crm/relances"' in CRM_JS.read_text(encoding="utf-8")
