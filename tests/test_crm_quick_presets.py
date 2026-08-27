from pathlib import Path
import subprocess

import app as application


ROOT = Path(__file__).resolve().parents[1]


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application._DATA_CACHE_PAYLOAD = None
    application._DATA_CACHE_SIGNATURE = None
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    test_client = application.app.test_client()
    with test_client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return test_client


def _extract_js_function(source, name):
    start = source.index(f"function {name}(")
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


def test_default_and_custom_call_and_relance_presets_persist(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    defaults = client.get("/api/crm/settings").get_json()
    assert defaults["call_note_presets"] == application.CRM_DEFAULT_CALL_NOTE_PRESETS
    assert defaults["relance_motif_presets"] == (
        application.CRM_DEFAULT_RELANCE_MOTIF_PRESETS
    )

    custom_call = "Attend la confirmation de son employeur"
    custom_relance = "Relancer après réception du devis"
    response = client.patch("/api/crm/settings", json={
        "call_note_presets": [
            *defaults["call_note_presets"],
            f"  {custom_call}  ",
            custom_call.lower(),
        ],
        "relance_motif_presets": [
            *defaults["relance_motif_presets"],
            custom_relance,
        ],
    })

    assert response.status_code == 200
    saved = response.get_json()
    assert saved["call_note_presets"].count(custom_call) == 1
    assert custom_relance in saved["relance_motif_presets"]
    assert client.get("/api/crm/settings").get_json() == saved
    assert client.get("/api/crm/bootstrap").get_json()["settings"] == saved


def test_preset_settings_reject_invalid_payloads(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    not_a_list = client.patch(
        "/api/crm/settings", json={"call_note_presets": "Réponse"},
    )
    too_long = client.patch(
        "/api/crm/settings", json={"relance_motif_presets": ["x" * 161]},
    )

    assert not_a_list.status_code == 400
    assert "liste" in not_a_list.get_json()["error"]
    assert too_long.status_code == 400
    assert "160 caractères" in too_long.get_json()["error"]


def test_preset_click_inserts_or_replaces_the_field_value():
    source = (ROOT / "static" / "crm.js").read_text(encoding="utf-8")
    function = _extract_js_function(source, "applyCrmPreset")
    harness = r"""
const assert = require('assert');
global.Event = function(type, options) { this.type = type; this.options = options; };
function field(value, start = value.length, end = start) {
  return {
    value, selectionStart: start, selectionEnd: end, events: [], focused: false,
    setRangeText(text, from, to) {
      this.value = this.value.slice(0, from) + text + this.value.slice(to);
      this.selectionStart = this.selectionEnd = from + text.length;
    },
    dispatchEvent(event) { this.events.push(event.type); },
    focus() { this.focused = true; }
  };
}
const note = field('Premier élément');
applyCrmPreset(note, 'Une relance a été programmée');
assert.strictEqual(note.value, 'Premier élément\nUne relance a été programmée');
assert.deepStrictEqual(note.events, ['input']);
assert.strictEqual(note.focused, true);

const motif = field('Ancien motif');
applyCrmPreset(motif, 'Suivi VAE', true);
assert.strictEqual(motif.value, 'Suivi VAE');
"""
    completed = subprocess.run(
        ["node", "-e", function + "\n" + harness],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr


def test_both_modals_expose_preset_choices_and_persistent_add_buttons():
    javascript = (ROOT / "static" / "crm.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "static" / "crm.css").read_text(encoding="utf-8")

    for marker in (
        "crmPresetMarkup('call_note_presets')",
        "crmPresetMarkup('relance_motif_presets')",
        "data-crm-preset-add",
        "[key]:[...current,value]",
        "api('/api/crm/settings'",
    ):
        assert marker in javascript
    for selector in (
        ".crm-preset-group",
        ".crm-preset-add",
        ".crm-preset-choice",
    ):
        assert selector in stylesheet
