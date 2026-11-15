from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_stored_location_selects_the_city_and_preserves_unresolved_answers():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const sheet = fs.readFileSync('static/crm.js', 'utf8');
const helper = sheet.slice(sheet.indexOf('function metaFieldPlaceholder('), sheet.indexOf('function selectHtml('));
const placeholder = vm.runInNewContext(helper + '\nmetaFieldPlaceholder');
const note = (lieu, answer) => placeholder({
  lieu, formation: 'DESP', meta_source: {formation: 'DESP', field_answers: {lieu: answer}},
}, 'lieu');
assert.equal(note('Paris', 'À paris.'), '');
assert.match(note('À déterminer', 'Paris ou Puget selon les dates.'), /Paris ou Puget/);
assert.equal(note('', ''), 'Aucun lieu communiqué dans Meta');

const fix = fs.readFileSync('static/crm_salesforce_contact_display_fix.js', 'utf8');
function openFiche(lieu, choices, initialValue) {
  const select = {value: initialValue, options: choices.map(([value, textContent]) => ({value, textContent})),
    prepend(option) { this.options.unshift(option); }};
  const form = {dataset: {}, querySelector: key => key === '[name="lieu"]' ? select : null};
  const document = {documentElement: {}, querySelector: () => form, createElement: () => ({dataset: {}})};
  vm.runInNewContext(fix, {
    contacts: [{id: 'lead', lieu}], window: {location: {search: '?fiche=lead'}}, document, URLSearchParams,
    MutationObserver: class { observe() {} },
  });
  return select;
}
const choices = [['', 'Meta · À paris.'], ['Côte d’Azur', 'Côte d’Azur'], ['Paris', 'Paris']];
for (const previous of ['', 'Paris']) {
  const select = openFiche('Paris', choices, previous);
  assert.equal(select.value, 'Paris');
  assert.equal(select.options.find(o => o.value === select.value).textContent, 'Paris');
}
assert.equal(openFiche("Côte d'Azur", [['', 'Meta · Puget-sur-Argens'], ...choices.slice(1)], '').value, 'Côte d’Azur');
assert.equal(openFiche('', [['', 'Meta · Paris ou Puget'], ...choices.slice(1)], '').value, '');
const offline = openFiche('Paris', [choices[0], choices[1]], '');
assert.equal(offline.value, 'Paris');
assert(offline.options.some(option => option.value === 'Paris'));
"""
    subprocess.run(["node", "-e", script], cwd=ROOT, check=True)
