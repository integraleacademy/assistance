from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _voice_function():
    source = (ROOT / "static" / "crm.js").read_text(encoding="utf-8")
    start = source.index("function bindVoiceDictation(")
    end = source.index("\nfunction callModal(", start)
    return source[start:end]


def test_call_modal_exposes_accessible_voice_dictation_controls():
    javascript = (ROOT / "static" / "crm.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "static" / "crm.css").read_text(encoding="utf-8")

    for marker in (
        'id="dictateCall"',
        'id="dictateCallStatus"',
        'aria-live="polite"',
        "bindVoiceDictation(callNote,voiceButton,voiceStatus)",
        "recognition.lang='fr-FR'",
        "recognition.continuous=true",
        "recognition.interimResults=true",
        "stopVoiceDictation();closeModal()",
    ):
        assert marker in javascript

    for selector in (
        ".voice-input-button",
        ".voice-input-button.is-listening",
        ".voice-input-status",
        "@media(prefers-reduced-motion:reduce)",
    ):
        assert selector in stylesheet


def test_voice_dictation_appends_results_handles_errors_and_has_a_fallback():
    harness = r"""
const assert = require('assert');
let currentRecognition;
class FakeRecognition {
  constructor() { currentRecognition = this; }
  start() { this.started = true; this.onstart(); }
  stop() { this.stopped = true; this.onend(); }
  abort() { this.aborted = true; }
}
function makeButton() {
  const label = {textContent: ''};
  const classes = new Set();
  const attrs = {};
  const button = {
    disabled: false,
    title: '',
    onclick: null,
    querySelector() { return label; },
    setAttribute(name, value) { attrs[name] = value; },
    getAttribute(name) { return attrs[name]; },
    classList: {
      toggle(name, enabled) {
        if (enabled) classes.add(name);
        else classes.delete(name);
      }
    }
  };
  return {button, label, classes, attrs};
}
function makeField(value = '') {
  return {
    value,
    scrollHeight: 120,
    scrollTop: 0,
    events: [],
    dispatchEvent(event) { this.events.push(event.type); }
  };
}
function result(transcript, isFinal) {
  const item = [{transcript}];
  item.isFinal = isFinal;
  return item;
}

global.window = {webkitSpeechRecognition: FakeRecognition};
global.Event = function(type) { this.type = type; };
const toasts = [];
global.toast = (...args) => toasts.push(args);

const voice = makeButton();
const field = makeField('Le candidat a appelé.');
const destroy = bindVoiceDictation(field, voice.button, {textContent: ''});

assert.strictEqual(currentRecognition.lang, 'fr-FR');
assert.strictEqual(currentRecognition.continuous, true);
assert.strictEqual(currentRecognition.interimResults, true);
voice.button.onclick();
assert.strictEqual(voice.attrs['aria-pressed'], 'true');
assert.strictEqual(voice.label.textContent, 'Arrêter la dictée');
assert.ok(voice.classes.has('is-listening'));

currentRecognition.onresult({
  resultIndex: 0,
  results: [
    result('Il souhaite suivre la formation A3P.', true),
    result('Un rendez-vous sera fixé', false)
  ]
});
assert.strictEqual(
  field.value,
  'Le candidat a appelé. Il souhaite suivre la formation A3P. Un rendez-vous sera fixé'
);
assert.deepStrictEqual(field.events, ['input']);
assert.strictEqual(field.scrollTop, 120);

voice.button.onclick();
assert.strictEqual(currentRecognition.stopped, true);
assert.strictEqual(voice.attrs['aria-pressed'], 'false');
assert.strictEqual(voice.label.textContent, 'Dicter le résumé');

voice.button.onclick();
currentRecognition.onresult({
  resultIndex: 0,
  results: [result('Le financement sera vérifié.', true)]
});
assert.ok(field.value.endsWith('Le financement sera vérifié.'));
destroy();
assert.strictEqual(currentRecognition.aborted, true);
assert.strictEqual(voice.button.onclick, null);

const denied = makeButton();
const deniedStatus = {textContent: ''};
bindVoiceDictation(makeField(), denied.button, deniedStatus);
denied.button.onclick();
currentRecognition.onerror({error: 'not-allowed'});
currentRecognition.onend();
assert.ok(deniedStatus.textContent.includes('Autorisez l’accès au micro'));
assert.deepStrictEqual(toasts[toasts.length - 1], [
  'Autorisez l’accès au micro dans Chrome (icône à gauche de l’adresse), puis réessayez.',
  true
]);

global.window = {};
const unsupported = makeButton();
const unsupportedStatus = {textContent: ''};
bindVoiceDictation(makeField(), unsupported.button, unsupportedStatus);
assert.strictEqual(unsupported.button.disabled, true);
assert.strictEqual(unsupported.label.textContent, 'Dictée indisponible');
assert.ok(unsupportedStatus.textContent.includes('Chrome ou Edge'));
"""
    completed = subprocess.run(
        ["node", "-e", _voice_function() + "\n" + harness],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
