import ast
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_python_function(name):
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    moments = iter([
        "2026-08-23T14:20:00+02:00",
        "2026-08-23T14:21:00+02:00",
        "2026-08-23T14:22:00+02:00",
    ])
    namespace = {
        "_crm_now": lambda: next(moments),
        "current_user": lambda: {"name": "Camille"},
    }
    exec(compile(module, "app.py", "exec"), namespace)
    return namespace[name]


def _extract_js_function(source, name):
    marker = f"function {name}("
    start = source.index(marker)
    if source[max(0, start - 6):start] == "async ":
        start -= 6
    brace = source.index("{", start)
    depth = 0
    quote = None
    escaped = False
    template_depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif quote == "`" and source[index:index + 2] == "${":
                template_depth += 1
            elif quote == "`" and char == "}" and template_depth:
                template_depth -= 1
            elif char == quote and not template_depth:
                quote = None
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"Fonction JavaScript incomplète : {name}")


def test_call_activity_edit_preserves_creation_metadata_and_audits_each_change():
    edit = _load_python_function("_crm_edit_call_activity")
    activity = {
        "id": "a1",
        "kind": "appel",
        "title": "Appel consigné",
        "detail": "Texte initial",
        "date": "2026-08-22T10:00:00+02:00",
        "author": "Alex",
    }

    assert edit(activity, "  Texte corrigé  ") == (True, "updated")
    assert activity["detail"] == "Texte corrigé"
    assert activity["id"] == "a1"
    assert activity["date"] == "2026-08-22T10:00:00+02:00"
    assert activity["author"] == "Alex"
    assert activity["edits"] == [{
        "detail": "Texte initial",
        "edited_at": "2026-08-23T14:20:00+02:00",
        "edited_by": "Camille",
    }]
    assert activity["edited_at"] == "2026-08-23T14:20:00+02:00"
    assert activity["edited_by"] == "Camille"

    assert edit(activity, "Deuxième correction") == (True, "updated")
    assert [item["detail"] for item in activity["edits"]] == [
        "Texte corrigé",
        "Texte initial",
    ]


def test_call_activity_edit_is_idempotent_and_rejects_invalid_input_or_kind():
    edit = _load_python_function("_crm_edit_call_activity")
    activity = {"kind": "appel", "detail": "Même texte"}
    snapshot = dict(activity)
    assert edit(activity, " Même texte ") == (False, "unchanged")
    assert activity == snapshot

    email = {"kind": "email", "detail": "Objet"}
    assert edit(email, "Nouveau") == (False, "invalid_kind")
    assert email == {"kind": "email", "detail": "Objet"}

    with pytest.raises(ValueError, match="compte-rendu"):
        edit(activity, "   ")


def test_patch_route_targets_one_call_and_saves_only_real_changes():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    decorator = (
        '@app.patch("/api/crm/contacts/<contact_id>/activities/<activity_id>")\n'
        "@login_required\n"
        "@_crm_serialized\n"
        "def crm_edit_call_activity(contact_id, activity_id):"
    )
    assert decorator in source
    start = source.index("def crm_edit_call_activity")
    end = source.index("\n\n@app.", start)
    route = source[start:end]
    for expected in (
        '"Contact introuvable"}), 404',
        '"Activité introuvable pour ce contact"}), 404',
        '"Seuls les appels consignés peuvent être modifiés"}), 409',
        '"Le corps JSON doit être un objet"}), 400',
        '_crm_edit_call_activity(activity, payload.get("commentaire"))',
        'if changed:',
        'save_data(data)',
        '"contact": _crm_contact_response(contact, data)',
        '"activity": activity',
        '"changed": changed',
    ):
        assert expected in route
    assert "_crm_send_appointment_followup" not in route
    assert "_crm_complete_relance" not in route


def test_feed_centralizes_all_events_and_exposes_edit_only_for_calls():
    source = (ROOT / "static" / "crm.js").read_text(encoding="utf-8")
    functions = "\n".join((
        _extract_js_function(source, "activityTimeline"),
        _extract_js_function(source, "activityCategory"),
        _extract_js_function(source, "activityDateGroup"),
        _extract_js_function(source, "activityEmptyState"),
        _extract_js_function(source, "feed"),
    ))
    harness = r"""
const assert = require('assert');
global.esc = value => String(value ?? '').replaceAll('&','&amp;').replaceAll('"','&quot;');
global.fmt = value => 'DATE:' + value;
global.crmIcon = name => `<svg>${name}</svg>`;
global.parisDateKey = value => new Date(value).toISOString().slice(0, 10);
global.activityFilterDefinitions = [
  {key:'all',label:'Tout',icon:'activity'},
  {key:'sms',label:'SMS',icon:'message'},
  {key:'email',label:'Mails',icon:'mail'},
  {key:'rdv',label:'RDV',icon:'calendar'},
  {key:'appel',label:'Appels consignés',icon:'phone'},
  {key:'publication',label:'Publications',icon:'users'},
  {key:'other',label:'Autre',icon:'activity'},
];
const contact = {
  activities: [
    {id:'call-1',kind:'appel',title:'Appel consigné',detail:'Corrigé',date:'2026-08-25T10:00:00+02:00',author:'Alex',edited_at:'d2',edited_by:'Camille'},
    {id:'mail-1',kind:'email',title:'E-mail envoyé',detail:'Objet',date:'2026-08-25T09:00:00+02:00',author:'Alex'},
    {id:'status-1',kind:'statut',title:'Statut : En cours',detail:'Ancien statut : Nouveau',date:'2026-08-25T08:00:00+02:00',author:'Camille'},
    {id:'followup-1',kind:'suivi',title:'Suivi mis à jour',detail:'Attendre le retour France Travail.',date:'2026-08-25T07:00:00+02:00',author:'Camille'}
  ],
  publications: [{
    id:'post-1',texte:'Pièce reçue.',date:'2026-08-25T11:00:00+02:00',author:'Alex',comments:[
      {id:'reply-1',texte:'Dossier vérifié.',date:'2026-08-25T12:00:00+02:00',author:'Camille'}
    ]
  }]
};
const html = feed(contact, true);
assert.strictEqual((html.match(/data-edit-call=/g) || []).length, 1);
assert.ok(html.includes('data-edit-call="call-1"'));
assert.ok(!html.includes('data-edit-call="mail-1"'));
assert.ok(html.includes('Modifier le compte-rendu de l’appel'));
assert.ok(html.includes('Modifié DATE:d2 · Camille'));
assert.ok(html.includes('Statut : En cours'));
assert.ok(html.includes('Suivi mis à jour'));
assert.ok(html.includes('Attendre le retour France Travail.'));
assert.ok(html.includes('Publication ajoutée'));
assert.ok(html.includes('Pièce reçue.'));
assert.ok(html.includes('Commentaire sur une publication'));
assert.ok(html.includes('Dossier vérifié.'));
assert.ok(html.indexOf('Dossier vérifié.') < html.indexOf('Pièce reçue.'));
const callsOnly = feed(contact, true, 'appel');
assert.ok(callsOnly.includes('Appel consigné'));
assert.ok(!callsOnly.includes('E-mail envoyé'));
const publicationsOnly = feed(contact, true, 'publication');
assert.ok(publicationsOnly.includes('Publication ajoutée'));
assert.ok(publicationsOnly.includes('Commentaire sur une publication'));
assert.ok(!publicationsOnly.includes('Statut : En cours'));
const otherOnly = feed(contact, true, 'other');
assert.ok(otherOnly.includes('Statut : En cours'));
assert.ok(otherOnly.includes('Suivi mis à jour'));
assert.ok(!otherOnly.includes('Appel consigné'));
"""
    completed = subprocess.run(
        ["node", "-e", functions + "\n" + harness],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr


def test_edit_modal_validates_patches_merges_and_recovers_from_error():
    source = (ROOT / "static" / "crm.js").read_text(encoding="utf-8")
    function = _extract_js_function(source, "editCallActivityModal")
    harness = r"""
const assert = require('assert');
let modalArgs;
let calls = [];
let merges = [];
let toasts = [];
let closed = 0;
let saved = 0;
global.esc = value => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;');
global.modal = (...args) => {
  modalArgs = args;
  global.cancelEditCall = {disabled:false};
  global.saveEditCall = {disabled:false,textContent:'Enregistrer'};
  global.editCallNote = {value:'Ancien texte',disabled:false,focus(){}};
};
global.closeModal = () => closed++;
global.toast = (...args) => toasts.push(args);
global.api = async (url, options) => {
  calls.push([url, options]);
  return {changed:true,contact:{id:'contact/1',activities:[]}};
};
global.mergeContactInStore = (...args) => merges.push(args);

(async () => {
  const contact = {id:'contact/1'};
  const activity = {id:'call 7',detail:'Ancien texte'};
  editCallActivityModal(contact, activity, () => saved++);
  assert.ok(modalArgs[1].includes('Ancien texte'));
  assert.ok(modalArgs[1].includes('ancien texte reste dans l’historique'));

  editCallNote.value = '   ';
  await saveEditCall.onclick();
  assert.strictEqual(calls.length, 0);
  assert.deepStrictEqual(toasts.at(-1), ['Saisissez un compte-rendu', true]);

  editCallNote.value = 'Texte corrigé';
  await saveEditCall.onclick();
  assert.deepStrictEqual(calls[0], [
    '/api/crm/contacts/contact%2F1/activities/call%207',
    {method:'PATCH',body:JSON.stringify({commentaire:'Texte corrigé'})}
  ]);
  assert.strictEqual(contact.activities.length, 0);
  assert.strictEqual(merges[0][0], 'contact/1');
  assert.strictEqual(saved, 1);
  assert.strictEqual(closed, 1);
  assert.deepStrictEqual(toasts.at(-1), ['Appel modifié']);

  global.api = async () => { throw new Error('hors ligne'); };
  editCallActivityModal({id:'c2'}, {id:'a2',detail:'Texte'}, () => {});
  editCallNote.value = 'Nouveau';
  await saveEditCall.onclick();
  assert.strictEqual(saveEditCall.disabled, false);
  assert.strictEqual(cancelEditCall.disabled, false);
  assert.strictEqual(editCallNote.disabled, false);
  assert.strictEqual(saveEditCall.textContent, 'Enregistrer');
  assert.deepStrictEqual(toasts.at(-1), [
    'L’appel n’a pas pu être modifié : hors ligne',
    true
  ]);
})().catch(error => {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        ["node", "-e", function + "\n" + harness],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr


def test_click_binding_accessibility_responsive_and_cache_version():
    js = (ROOT / "static" / "crm.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "crm.css").read_text(encoding="utf-8")
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "event.target.closest('[data-edit-call]')" in js
    assert "editCallActivityModal(current,activity,renderActivityFeed)" in js
    assert "activityExpanded=!activityExpanded;renderActivityFeed()" in js
    assert ".feed-edit-call:focus-visible" in css
    assert ".edit-call-modal textarea{min-height:190px}" in css
    assert ".feed-edit-call{min-height:38px" in css
    assert 'CRM_ASSET_VERSION = "20260901-contact-sheet-1"' in app
