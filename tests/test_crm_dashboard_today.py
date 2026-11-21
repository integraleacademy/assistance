import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_home_shows_todays_leads_first_and_opens_the_matching_records():
    source = (ROOT / "static" / "crm.js").read_text(encoding="utf-8")
    script = """
const assert = require('node:assert/strict');
global.window = globalThis;
global.document = {querySelector: () => null};
window.CRM_CONFIG = {statuses: [], first_name: 'Test'};
require('./static/crm_title.js');
require('./static/crm_appointment_state.js');
const displayName = CRMDocumentTitle.displayName;
const header = () => '';
const isActiveLead = () => true;
const RealDate = Date;
global.Date = class extends RealDate {
  constructor(...args) { super(...(args.length ? args : ['2026-11-21T12:00:00Z'])); }
  static now() { return new Date().getTime(); }
};
"""
    script += source.splitlines()[0] + "\n"
    script += next(line for line in source.splitlines() if line.startswith("const esc=")) + "\n"
    script += next(line for line in source.splitlines() if line.startswith("const contactSheetUrl=")) + "\n"
    script += source[source.index("const dashboardPeriods="):source.index("function bindDashboard()")]
    script += r"""
const lead = (id, time) => ({id, nom: id, formation: 'APS', created_at: time});
contacts = [
  lead('yesterday', '2026-11-20T22:59:59Z'),
  lead('early', '2026-11-20T23:00:00Z'),
  lead('late/id', '2026-11-21T10:00:00Z'),
  {id: 'legacy', nom: '<test>', received_at: '2026-11-21T09:00:00Z'},
  ...[4, 5, 6].map(hour => lead('row' + hour, '2026-11-21T0' + hour + ':00:00Z')),
  {...lead('archived', '2026-11-21T11:00:00Z'), archived_at: '2026-11-21'},
  {...lead('updated', '2026-11-19T12:00:00Z'), updated_at: '2026-11-21T11:00:00Z'},
  lead('unknown', 'invalid'),
];
const html = dashboard();
assert.match(html, /data-dashboard-period="today" class="active"/);
assert.ok(html.indexOf('Pistes du jour') < html.indexOf('class="analytics-kpis"'));
const panel = html.match(/<section [^>]*dashboard-today-leads[\s\S]*?<\/section>/)[0];
const links = [...panel.matchAll(/class="dashboard-today-lead" href="([^"]+)"/g)]
  .map(([, href]) => new URL(href.replaceAll('&amp;', '&'), 'https://crm.test'));
assert.deepEqual(links.map(url => url.searchParams.get('fiche')), ['late/id', 'legacy', 'row6', 'row5', 'row4']);
assert.ok(links.every(url => url.pathname === '/crm/contacts' && url.searchParams.get('retour') === 'pistes'));
assert.ok(panel.includes('&lt;TEST&gt;'));
assert.ok(panel.includes('11:00'));
const all = panel.match(/href="([^"]+)">Voir les 6 pistes/)[1];
const query = new URL(all.replaceAll('&amp;', '&'), 'https://crm.test').search;
const filtered = dashboardDrilldownContacts(crmActiveContacts(), dashboardDrilldownState(query));
assert.deepEqual(filtered.map(c => c.id).sort(), ['early', 'late/id', 'legacy', 'row4', 'row5', 'row6']);
dashboardOffset = -1;
assert.ok(!dashboard().includes('Pistes du jour'));
dashboardPeriod = 'month';
dashboardOffset = 0;
assert.ok(!dashboard().includes('Pistes du jour'));
dashboardPeriod = 'today';
contacts = [];
assert.ok(dashboard().includes('Aucune piste créée aujourd’hui.'));
"""
    subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        env={**os.environ, "TZ": "Europe/Paris"},
        check=True,
    )
