"""Exercise the active crm.js pipeline, not the unused Workspace list view."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_native_pipeline_exclude_meta_preserves_filters_and_selection():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to exercise the CRM browser helpers")
    source = (ROOT / "static/crm.js").read_text(encoding="utf-8")
    # Execute the production list builder, filter closure and selection handlers.
    # Stub only the browser DOM and unrelated rendering/integration dependencies.
    def section(start, end):
        return source[source.index(start):source.index(end, source.index(start))]
    native = "\n".join([
        section("const crmOriginFilterValues=", "const dashboardHasContact="),
        section("function listContactsForType(", "const bulkDisqualificationReasons="),
        section("function bindList(type){", "const reservedStatuses="),
    ])
    extension = (ROOT / "static/crm_pistes_origin_filter.js").read_text(encoding="utf-8")
    script = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
let elements = new Map(), dataNodes = [], drawn = [];
class Element {
  constructor(attrs = {}) {
    this.id = attrs.id || ''; this.dataset = {}; this.events = {};
    for (const [key, val] of Object.entries(attrs)) if (key.startsWith('data-')) {
      this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = val;
    }
    this.value = attrs.value || ''; this.checked = 'checked' in attrs;
    this.hidden = 'hidden' in attrs; this.disabled = false; this.textContent = '';
    this._html = '';
  }
  addEventListener(type, fn) { (this.events[type] ||= []).push(fn); }
  dispatchEvent(event) {
    event.target = this;
    for (const fn of [...(this.events[event.type] || [])]) fn(event);
    this['on' + event.type]?.(event);
  }
  set innerHTML(value) {
    this._html = value;
    if (this.id === 'page') { elements = new Map([['page', this]]); dataNodes = []; parse(value); }
    if (this.id === 'resultTable') {
      dataNodes = dataNodes.filter(e => !('leadSelect' in e.dataset) && !('scoreSort' in e.dataset));
      elements.delete('leadSelectAll'); parse(value);
    }
  }
  get innerHTML() { return this._html; }
  get options() { return [...this._html.matchAll(/<option(?: value="([^"]*)")?[^>]*>([^<]*)<\/option>/g)].map(m => ({value: m[1] ?? m[2]})); }
}
function parse(html) {
  for (const tag of html.matchAll(/<(?:input|select|button|span|div|section|label)\b([^>]*)>/g)) {
    const attrs = {};
    for (const attr of tag[1].matchAll(/([\w:-]+)(?:="([^"]*)")?/g)) attrs[attr[1]] = attr[2] || '';
    if (!attrs.id && !Object.keys(attrs).some(k => k.startsWith('data-'))) continue;
    const e = new Element(attrs);
    if (e.id) elements.set(e.id, e);
    dataNodes.push(e);
  }
}
const document = {
  querySelector(selector) {
    if (selector[0] === '#') return elements.get(selector.slice(1)) || null;
    return this.querySelectorAll(selector)[0] || null;
  },
  querySelectorAll(selector) {
    const match = selector.match(/^\[data-([\w-]+)\]$/);
    if (!match) return [];
    const key = match[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    return dataNodes.filter(e => key in e.dataset);
  },
};
const context = {
  document, console, Event: class { constructor(type) { this.type = type; } },
  C: {section: 'pistes'}, S: ['Nouveaux', 'A relancer'], statusFilter: '', leadScoreSort: '',
  selectedLeadIds: new Set(), visibleLeadIds: [], page: new Element({id:'page'}),
  rows: [], drilldown: null, CRM_CONFIG: {section: 'pistes'},
  esc: s => String(s ?? ''), header: () => '', crmIcon: () => '',
  crmActiveContacts: () => context.rows.filter(c => !c.archived_at),
  dashboardDrilldownState: () => context.drilldown,
  dashboardDrilldownContacts: rows => rows.filter(c => c.formation === 'A3P'),
  dashboardDrilldownRangeLabel: () => 'test',
  pipelineOverviewStatuses: () => context.S,
  contactHasPipelineStatus: (c, s) => c.statut === s,
  contactMatchesSearch: (c, q) => !q || c.nom.toLowerCase().includes(q.toLowerCase()),
  sortPipelineLeads: (rows, direction) => direction === 'asc' ? [...rows].reverse() : rows,
  nextLeadScoreSortDirection: () => 'asc',
  sessionFilterSeparator: '\u001f',
  sessionFilterRows: () => [{formation:'A3P',lieu:'Paris',label:'Session A'}],
  sessionFilterValue: r => [r.formation,r.lieu,r.label].join('\u001f'),
  bindRows: () => {}, statusesModal: () => {}, bulkLeadStatusModal: () => {},
  bulkLeadDeleteModal: () => {}, bulkMessageModal: () => {},
  table: rows => {
    drawn = rows;
    return '<input id="leadSelectAll"><button data-score-sort=""></button>' +
      rows.map(c => `<input data-lead-select="${c.id}">`).join('');
  },
};
context.window = context;
vm.createContext(context);
vm.runInContext(NATIVE, context);
vm.runInContext(EXTENSION, context);
context.CRMPistesOriginFilter.install();
context.CRMPistesOriginFilter.install(); // idempotent: no nested wrappers
const meta = [
  {origine:'META'}, {origine:' Facebook '}, {source:'Instagram'},
  {origine:'meta ads'}, {meta_source:{lead_id:'synthetic'}},
];
const others = [
  {origine:'Google Ads'}, {origine:'Site internet'}, {origine:'CPF'},
  {origine:'Secrétariat'}, {origine:'Calendly'}, {origine:'Bouche à oreilles'},
  {origine:'Autre'}, {}, {origine:'Google Ads',meta_source:{lead_id:'synthetic'}},
];
context.rows = [...meta, ...others].map((c, i) => Object.freeze({
  id:String(i),nom:'Test '+i,formation:i===6?'APS':'A3P',lieu:'Paris',
  dates_formation:'Session A',statut:'Nouveaux',...c,
}));
const original = JSON.stringify(context.rows);
function mount(type='pistes') {
  context.C.section = type;
  context.page.innerHTML = context.listPage(type);
  context.bindList(type);
}
context.render = () => mount();
function emit(id, type='input') { const el=elements.get(id); assert.ok(el, id); el.dispatchEvent({type}); }
function set(id, value) { elements.get(id).value=value; emit(id); }
function toggle(on) { elements.get('leadExcludeMeta').checked=on; emit('leadExcludeMeta','change'); }
function ids() { return Array.from(drawn,c=>c.id); }
mount();
assert.equal(elements.get('leadExcludeMeta').checked,false);
assert.equal(context.page.innerHTML.match(/id="leadExcludeMeta"/g).length,1);
assert.match(context.page.innerHTML, /id="originFilter"[\s\S]*?<\/select><label[^>]*for="leadExcludeMeta"/);
assert.equal(drawn.length,14);
emit('selectAllLeads','click');
assert.equal(context.selectedLeadIds.size,14);
toggle(true);
assert.deepEqual(ids(),others.map((_,i)=>String(i+5)));
assert.equal(elements.get('filterResultCount').textContent,'9 pistes');
assert.equal(context.selectedLeadIds.size,9); // hidden META cannot receive bulk actions
assert.deepEqual(Array.from(context.visibleLeadIds), ids());
assert.equal(elements.get('selectAllLeads').textContent,'Tout désélectionner');
set('formationFilter','A3P'); set('lieuFilter','Paris');
set('sessionFilter',['A3P','Paris','Session A'].join('\u001f'));
set('listSearch','Test'); set('originFilter','Google Ads');
assert.deepEqual(ids(),['5','13']);
const saved = Object.fromEntries(['formationFilter','lieuFilter','sessionFilter','listSearch','originFilter'].map(id=>[id,elements.get(id).value]));
for (let i=0;i<5;i++) { toggle(false); toggle(true); }
assert.deepEqual(ids(),['5','13']);
for (const [id,value] of Object.entries(saved)) assert.equal(elements.get(id).value,value);
assert.equal(elements.get('listSearch').events.input.length,1);
assert.equal(elements.get('leadExcludeMeta').events.change.length,1);
set('originFilter','META'); assert.deepEqual(ids(),[]);
assert.equal(elements.get('filterResultCount').textContent,'0 piste');
assert.equal(elements.get('selectAllLeads').disabled,true);
toggle(false); assert.equal(drawn.length,5);
toggle(true); assert.equal(drawn.length,0);
set('originFilter',''); set('listSearch','Test 7'); assert.deepEqual(ids(),['7']);
emit('selectAllLeads','click'); assert.deepEqual(Array.from(context.selectedLeadIds),['7']);
mount(); assert.equal(elements.get('leadExcludeMeta').checked,true); assert.equal(drawn.length,9);
context.statusFilter='A relancer'; mount(); assert.equal(drawn.length,0);
context.statusFilter=''; mount();
context.drilldown={label:'A3P'}; mount(); assert.equal(drawn.length,8);
context.drilldown=null;
context.rows = [...context.rows,Object.freeze({id:'new',nom:'Test new',origine:'Site internet',formation:'A3P',lieu:'Paris',statut:'Nouveaux'})];
mount(); assert.equal(drawn.length,10); assert.ok(ids().includes('new'));
const score=document.querySelector('[data-score-sort]'); score.dispatchEvent({type:'click'});
assert.equal(context.leadScoreSort,'asc');
toggle(false); assert.equal(ids()[0],'new');
toggle(true); assert.equal(ids()[0],'new');
assert.equal(JSON.stringify(context.rows.slice(0,14)),original);
assert.equal(context.listContactsForType('contacts').length,15);
assert.ok(!context.listPage('contacts').includes('leadExcludeMeta'));
mount('contacts'); assert.equal(drawn.length,15); assert.ok(!document.querySelector('#leadExcludeMeta'));
console.log('Native pipeline: visibility, origins, combined filters, counts, bulk selection, sorting, rerenders and isolation OK');
'''
    script = "const NATIVE=" + json.dumps(native) + ";\nconst EXTENSION=" + json.dumps(extension) + ";\n" + script
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_pipeline_extension_is_loaded_at_the_active_entrypoint():
    template = (ROOT / "templates/crm.html").read_text(encoding="utf-8")
    source = (ROOT / "static/crm.js").read_text(encoding="utf-8")
    extension_at = template.index("filename='crm_pistes_origin_filter.js'")
    native_at = template.index("filename='crm.js'")
    install_at = template.index("window.CRMPistesOriginFilter?.install()")
    assert extension_at < native_at < install_at
    assert "pistes_exclude_meta_version='20260910-native-1'" in template
    assert '.filters .lead-origin-exclusion input{' in template
    assert "if(C.section==='pistes'){page.innerHTML=listPage(C.section);bindList(C.section);bindRows();return}" in source
