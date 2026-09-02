import ast
import datetime
import subprocess
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_python_functions(*names):
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    selected = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    assert {node.name for node in selected} == set(names)
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    namespace = {
        "CRM_RELANCE_STATUSES": {
            "scheduled", "answered", "no_answer", "reprogrammed", "cancelled",
        },
        "datetime": datetime,
        "uuid": uuid,
        "_crm_now": lambda: "2026-08-23T14:10:00+02:00",
        "current_user": lambda: {"name": "Camille"},
    }
    exec(compile(module, "app.py", "exec"), namespace)
    return namespace


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
            elif quote == "`" and char == "$" and source[index:index + 2] == "${":
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


def test_delete_one_scheduled_relance_removes_it_and_refreshes_next_date():
    namespace = _load_python_functions("_crm_refresh_relance_date", "_crm_delete_relance")
    delete = namespace["_crm_delete_relance"]
    target = {"id": "r1", "status": "scheduled", "scheduled_date": "2026-08-24"}
    following = {"id": "r2", "status": "scheduled", "scheduled_date": "2026-08-26"}
    historical = {"id": "r0", "status": "answered", "scheduled_date": "2026-08-20"}
    following_snapshot = dict(following)
    historical_snapshot = dict(historical)
    contact = {"relance_date": "2026-08-24", "relances": [target, following, historical]}

    assert delete(contact, target) is True
    assert target not in contact["relances"]
    assert contact["relances"] == [following, historical]
    assert following == following_snapshot
    assert historical == historical_snapshot
    assert contact["relance_date"] == "2026-08-26"

def test_delete_refuses_history_or_foreign_object_and_clears_last_planned_date():
    namespace = _load_python_functions("_crm_refresh_relance_date", "_crm_delete_relance")
    delete = namespace["_crm_delete_relance"]
    completed = {"id": "done", "status": "answered", "scheduled_date": "2026-08-20"}
    snapshot = dict(completed)
    assert delete({"relance_date": "", "relances": [completed]}, completed) is False
    assert completed == snapshot

    stored = {"id": "kept", "status": "scheduled", "scheduled_date": "2026-08-25"}
    foreign = {"id": "foreign", "status": "scheduled", "scheduled_date": "2026-08-25"}
    foreign_contact = {"relance_date": "2026-08-25", "relances": [stored]}
    assert delete(foreign_contact, foreign) is False
    assert foreign_contact["relances"] == [stored]
    assert foreign_contact["relance_date"] == "2026-08-25"

    last = {"id": "last", "status": "scheduled", "scheduled_date": "2026-08-25"}
    contact = {"relance_date": "2026-08-25", "relances": [last]}
    assert delete(contact, last) is True
    assert contact["relances"] == []
    assert contact["relance_date"] == ""


def test_normalization_purges_legacy_cancellations_without_losing_real_history():
    namespace = _load_python_functions(
        "_crm_relance_date",
        "_crm_refresh_relance_date",
        "_crm_ensure_relances",
    )
    ensure = namespace["_crm_ensure_relances"]
    base = {
        "scheduled_date": "2026-08-23",
        "created_at": "2026-08-20T09:00:00+02:00",
        "created_by": "Camille",
    }
    contact = {
        "relance_date": "2026-08-27",
        "relances": [
            {**base, "id": "cancelled", "status": "cancelled"},
            {**base, "id": "answered", "status": "answered"},
            {**base, "id": "no-answer", "status": "no_answer"},
            {**base, "id": "reprogrammed", "status": "reprogrammed"},
            {
                **base,
                "id": "scheduled",
                "status": "scheduled",
                "scheduled_date": "2026-08-27",
            },
        ],
    }

    assert ensure(contact) is True
    assert [item["id"] for item in contact["relances"]] == [
        "answered",
        "no-answer",
        "reprogrammed",
        "scheduled",
    ]
    assert contact["relance_date"] == "2026-08-27"
    assert ensure(contact) is False


def test_normalization_does_not_resurrect_cancelled_legacy_date():
    namespace = _load_python_functions(
        "_crm_relance_date",
        "_crm_refresh_relance_date",
        "_crm_ensure_relances",
    )
    ensure = namespace["_crm_ensure_relances"]
    contact = {
        "relance_date": "2026-08-27",
        "relances": [{
            "id": "cancelled",
            "status": "cancelled",
            "scheduled_date": "2026-08-27",
            "created_at": "2026-08-20T09:00:00+02:00",
            "created_by": "Camille",
        }],
    }

    assert ensure(contact) is True
    assert contact["relances"] == []
    assert contact["relance_date"] == ""
    assert ensure(contact) is False


def test_normalization_keeps_only_the_nearest_active_relance():
    namespace = _load_python_functions(
        "_crm_relance_date",
        "_crm_refresh_relance_date",
        "_crm_ensure_relances",
    )
    ensure = namespace["_crm_ensure_relances"]
    answered = {
        "id": "answered",
        "status": "answered",
        "scheduled_date": "2026-08-20",
        "created_at": "2026-08-20T09:00:00+02:00",
        "created_by": "Camille",
    }
    contact = {
        "relance_date": "2026-09-10",
        "relances": [
            {
                "id": "later",
                "status": "scheduled",
                "scheduled_date": "2026-09-10",
            },
            answered,
            {
                "id": "nearest",
                "status": "scheduled",
                "scheduled_date": "2026-08-28",
            },
            {
                "id": "same-day-duplicate",
                "status": "scheduled",
                "scheduled_date": "2026-08-28",
            },
        ],
    }

    assert ensure(contact) is True
    active = [
        item for item in contact["relances"]
        if item["status"] == "scheduled"
    ]
    assert [item["id"] for item in active] == ["nearest"]
    assert contact["relance_date"] == "2026-08-28"
    assert answered == {
        "id": "answered",
        "status": "answered",
        "scheduled_date": "2026-08-20",
        "created_at": "2026-08-20T09:00:00+02:00",
        "created_by": "Camille",
    }
    for relance_id in ("later", "same-day-duplicate"):
        historical = next(
            item for item in contact["relances"] if item["id"] == relance_id
        )
        assert historical["status"] == "reprogrammed"
        assert historical["completed_at"] == "2026-08-23T14:10:00+02:00"
        assert historical["completed_by"] == "Automatisation CRM"
    assert ensure(contact) is False


def test_cancelling_followups_removes_every_open_item_and_is_idempotent():
    namespace = _load_python_functions(
        "_crm_relance_date",
        "_crm_refresh_relance_date",
        "_crm_ensure_relances",
        "_crm_schedule_relance",
    )
    cancel = namespace["_crm_schedule_relance"]
    contact = {
        "relance_date": "2026-08-24",
        "relances": [
            {"id": "first", "status": "scheduled", "scheduled_date": "2026-08-24"},
            {"id": "answered", "status": "answered", "scheduled_date": "2026-08-20"},
            {"id": "second", "status": "scheduled", "scheduled_date": "2026-08-26"},
            {"id": "moved", "status": "reprogrammed", "scheduled_date": "2026-08-18"},
        ],
    }

    planned, changed = cancel(contact, "", actor_name="Calendly")

    assert planned is None
    assert changed is True
    assert [item["id"] for item in contact["relances"]] == ["answered", "moved"]
    assert contact["relance_date"] == ""
    assert cancel(contact, "", actor_name="Calendly") == (None, False)

def test_delete_route_is_targeted_authenticated_permanent_and_persisted():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    decorator = (
        '@app.delete("/api/crm/contacts/<contact_id>/relances/<relance_id>")\n'
        "@login_required\n"
        "@_crm_serialized\n"
        "def crm_delete_relance(contact_id, relance_id):"
    )
    assert decorator in source
    start = source.index("def crm_delete_relance")
    end = source.index("\n\n@app.", start)
    route = source[start:end]
    for expected in (
        '"Contact introuvable"}), 404',
        '"Relance introuvable pour ce contact"}), 404',
        '"Seule une relance planifiée peut être supprimée"}), 409',
        "_crm_delete_relance(contact, relance)",
        "save_data(data)",
        '"contact": _crm_contact_response(contact, data)',
        '"deleted_relance_id": relance_id',
    ):
        assert expected in route
    assert "_crm_activity(" not in route
    assert '"relance": relance' not in route

def test_frontend_confirms_calls_exact_delete_and_recovers_from_error():
    source = (ROOT / "static" / "crm.js").read_text(encoding="utf-8")
    function = _extract_js_function(source, "deleteRelance")
    assert "Cette action est définitive : la relance ne sera conservée ni dans l’historique ni dans les compteurs." in function
    assert "restera visible comme annulée" not in function
    harness = r"""
const assert = require('assert');
let allow = false;
let calls = [];
let renders = [];
let merges = [];
let toasts = [];
global.relanceDateMeta = () => ({long: 'lundi 24 août 2026'});
global.confirm = () => allow;
global.api = async (url, options) => {
  calls.push([url, options]);
  return {contact: {id: 'contact/1', relance_date: '2026-08-26'}};
};
global.mergeContactInStore = (...args) => merges.push(args);
global.showContact = (...args) => renders.push(args);
global.toast = (...args) => toasts.push(args);

(async () => {
  const contact = {id: 'contact/1'};
  const relance = {id: 'relance 7', scheduled_date: '2026-08-24'};
  const button = {textContent: 'Supprimer', disabled: false};

  await deleteRelance(contact, relance, button);
  assert.strictEqual(calls.length, 0);
  assert.strictEqual(button.disabled, false);

  allow = true;
  await deleteRelance(contact, relance, button);
  assert.deepStrictEqual(calls[0], [
    '/api/crm/contacts/contact%2F1/relances/relance%207',
    {method: 'DELETE'}
  ]);
  assert.strictEqual(button.disabled, true);
  assert.strictEqual(contact.relance_date, '2026-08-26');
  assert.deepStrictEqual(merges[0][0], 'contact/1');
  assert.deepStrictEqual(renders[0], ['contact/1', 'contactRelanceTab']);
  assert.deepStrictEqual(toasts[0], ['Relance supprimée']);

  global.api = async () => { throw new Error('hors ligne'); };
  const retry = {textContent: 'Supprimer', disabled: false};
  await deleteRelance({id: 'c2'}, {id: 'r2', scheduled_date: '2026-08-25'}, retry);
  assert.strictEqual(retry.disabled, false);
  assert.strictEqual(retry.textContent, 'Supprimer');
  assert.deepStrictEqual(toasts.at(-1), [
    'La relance n’a pas pu être supprimée : hors ligne',
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


def test_tracking_ignores_legacy_cancellations_in_history_and_metrics():
    source = (ROOT / "static" / "crm.js").read_text(encoding="utf-8")
    function = _extract_js_function(source, "relanceTracking")
    harness = r"""
const assert = require('assert');
global.plannedRelances = contact => contact.relances.filter(item => item.status === 'scheduled');
global.relanceDateMeta = () => ({day: '23', month: 'août', long: '23 août 2026', relative: 'Aujourd’hui', tone: 'today'});
global.relanceHistoryTone = item => item.status;
global.relanceHistoryLabel = item => ({answered: 'A répondu', no_answer: 'Pas de réponse', cancelled: 'Annulée'}[item.status]);
global.relanceDelivery = () => '';
global.crmIcon = () => '';
global.esc = value => String(value ?? '');
global.fmt = value => String(value ?? '');

const html = relanceTracking({relances: [
  {id: 'cancelled', status: 'cancelled', scheduled_date: '2026-08-19'},
  {id: 'no-answer', status: 'no_answer', scheduled_date: '2026-08-20'},
  {id: 'answered', status: 'answered', scheduled_date: '2026-08-21'},
]});

assert(!html.includes('Annulée'), 'cancelled follow-up must stay hidden');
assert(html.includes('<span>Sans réponse</span><b>1</b>'));
assert(html.includes('<span>Ont répondu</span><b>1</b>'));
assert(html.includes('<span>Total</span><b>2</b>'));
assert(html.includes('<span>Historique des relances</span><b>2</b>'));
"""
    completed = subprocess.run(
        ["node", "-e", function + "\n" + harness],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr


def test_button_accessibility_responsive_style_and_cache_version():
    js = (ROOT / "static" / "crm.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "crm.css").read_text(encoding="utf-8")
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'data-relance-delete' in js
    assert 'aria-label="Supprimer la relance du ${esc(meta.long)}"' in js
    assert "event=>deleteRelance(c,relance,event.currentTarget)" in js
    assert ".relance-delete:focus-visible" in css
    assert ".relance-delete:disabled" in css
    assert ".relance-item-controls{display:grid;grid-template-columns:1fr 1fr;width:100%" in css
    assert 'CRM_ASSET_VERSION = "20260902-primary-origin-dashboard-1"' in app
