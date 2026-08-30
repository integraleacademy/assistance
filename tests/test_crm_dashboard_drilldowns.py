import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
CRM_JS = ROOT / "static" / "crm.js"
CRM_CSS = ROOT / "static" / "crm.css"
CRM_TEMPLATE = ROOT / "templates" / "crm.html"


def test_dashboard_kpis_link_to_exact_filtered_lead_views():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")
    template = CRM_TEMPLATE.read_text(encoding="utf-8")

    for label in (
        "Pistes créées sur la période",
        "Pistes Google Ads",
        "Pistes META",
        "Autres pistes",
        "Taux de conversion",
        "Pistes avec RDV",
        "Pistes contactées",
        "Relances échues",
    ):
        assert label in javascript

    for view in (
        "created",
        "google",
        "meta",
        "other",
        "converted",
        "appointments",
        "contacted",
        "overdue",
    ):
        assert f"dashboard_view:{view}" in javascript or f"'{view}'" in javascript

    assert "function dashboardDrilldownUrl" in javascript
    assert "function dashboardDrilldownContacts" in javascript
    assert "function listContactsForType" in javascript
    assert "dashboardView?dashboardDrilldownContacts" in javascript
    assert "dashboard-drilldown-banner" in javascript
    assert "Voir les pistes →" in javascript
    assert "a.analytics-kpi:hover" in stylesheet
    assert ".dashboard-drilldown-banner{" in stylesheet
    assert "20260830-dashboard-drilldowns-1" in template
    assert "repeat(4,minmax(0,1fr))" in (ROOT / "static" / "crm_dashboard_origins.css").read_text(encoding="utf-8")


def test_dashboard_drilldowns_preserve_period_and_filter_the_same_contacts():
    javascript = CRM_JS.read_text(encoding="utf-8")
    helpers = javascript[
        javascript.index("const dashboardIsMetaLead="):
        javascript.index("function dashboardGroup")
    ]
    script = f"""
const crmOriginLabels=contact=>contact.origins;
const isActiveLead=contact=>!['Converti','Disqualifié'].includes(contact.statut);
const contactContactActivityKinds=new Set(['appel','email','sms','demande_rappel']);
const crmAppointments=[{{contact_id:'google',status:'active',response_status:null}}];
const dashboardHasContact=contact=>(contact.activities||[]).some(activity=>contactContactActivityKinds.has(activity.kind))||crmAppointments.some(appointment=>String(appointment.contact_id)===String(contact.id)&&appointment.response_status==='answered');
const dashboardHasAppointment=contact=>crmAppointments.some(appointment=>String(appointment.contact_id)===String(contact.id)&&appointment.status!=='canceled');
const dashboardDate=value=>{{const date=new Date(value);return Number.isNaN(date.getTime())?null:date}};
const dashboardContactDate=contact=>dashboardDate(contact.created_at||contact.received_at||contact.updated_at);
const esc=value=>String(value);
let dashboardPeriod='month';
{helpers}
const assert=(condition,message)=>{{if(!condition)throw new Error(message)}};
const contacts=[
 {{id:'meta',created_at:'2026-08-10T08:00:00Z',origins:['META'],statut:'Nouveaux',activities:[{{kind:'email'}}]}},
 {{id:'google',created_at:'2026-08-11T08:00:00Z',origins:['Google Ads'],statut:'Nouveaux',activities:[]}},
 {{id:'other',created_at:'2026-08-12T08:00:00Z',origins:['Site internet'],statut:'Converti',activities:[]}},
 {{id:'outside',created_at:'2026-07-12T08:00:00Z',origins:['META'],statut:'Nouveaux',activities:[]}},
 {{id:'overdue',created_at:'2025-01-01T08:00:00Z',origins:['Autre'],statut:'Nouveaux',relance_date:'2000-01-01',activities:[]}},
];
const range='&from=2026-08-01T00%3A00%3A00.000Z&to=2026-09-01T00%3A00%3A00.000Z';
const ids=view=>dashboardDrilldownContacts(contacts,dashboardDrilldownState(`?dashboard_view=${{view}}${{range}}`)).map(contact=>contact.id);
assert(ids('created').join(',')==='meta,google,other','created view keeps the exact period');
assert(ids('meta').join(',')==='meta','META view matches the META KPI');
assert(ids('google').join(',')==='google','Google view matches the Google Ads KPI');
assert(ids('other').join(',')==='other','other view excludes META and Google Ads');
assert(ids('converted').join(',')==='other','conversion view exposes converted contacts');
assert(ids('appointments').join(',')==='google','appointment view exposes booked contacts');
assert(ids('contacted').join(',')==='meta','contacted view exposes contacted contacts');
const overdueState=dashboardDrilldownState('?dashboard_view=overdue');
assert(dashboardDrilldownContacts(contacts,overdueState).map(contact=>contact.id).join(',')==='overdue','overdue callbacks cover all periods');
assert(dashboardDrilldownState('?dashboard_view=meta')===null,'dated views reject a missing period');
const url=dashboardDrilldownUrl('meta',{{start:new Date('2026-08-01T00:00:00Z'),end:new Date('2026-09-01T00:00:00Z')}});
assert(url.includes('dashboard_view=meta')&&url.includes('from=')&&url.includes('to='),'dashboard links carry their range');
console.log('CRM dashboard drilldowns: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM dashboard drilldowns: OK" in completed.stdout
