from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _voice_function():
    source = (ROOT / "static" / "crm.js").read_text(encoding="utf-8")
    start = source.index("function formatVoiceDictation(")
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
        "formatVoiceDictation(combine(finalText,interim),capitalizeFirst)",
        "Dites « point », « virgule » ou « à la ligne » pour ponctuer.",
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


def test_message_and_publication_fields_reuse_the_voice_dictation_controls():
    javascript = (ROOT / "static" / "crm.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "static" / "crm.css").read_text(encoding="utf-8")
    message_modal = javascript.split("function messageModal", 1)[1].split(
        "function bulkMessageModal", 1
    )[0]

    for marker in (
        'id="dictateMessage"',
        'id="dictateMessageStatus"',
        "Dicter le message",
        "bindVoiceDictation(messageField,voiceButton,voiceStatus)",
        "pauseVoiceDictation()",
        "destroyVoiceDictation();closeModal()",
    ):
        assert marker in message_modal

    for marker in (
        'id="dictatePublication"',
        'id="dictatePublicationStatus"',
        "Dicter la publication",
        "bindVoiceDictation(publicationText,publicationVoiceButton,publicationVoiceStatus)",
        "pausePublicationVoice()",
        "if(selected.id!=='contactActivityTab')stopActiveVoiceDictation()",
    ):
        assert marker in javascript

    assert "function stopActiveVoiceDictation()" in javascript
    assert "stopActiveVoiceDictation();modalRoot.innerHTML=" in javascript
    assert ".voice-input-heading" in stylesheet
    assert ".publication-compose-actions .publication-voice-button" in stylesheet


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
function makeButton(idleLabel = 'Dicter le résumé') {
  const label = {textContent: idleLabel};
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

assert.strictEqual(
  formatVoiceDictation(
    'ceci est un test point ce monsieur doit créer son identité numérique '
      + 'virgule puis vérifier son compte CPF point à la ligne prochaine étape '
      + 'deux points prendre un rendez-vous point d’exclamation'
  ),
  'Ceci est un test. Ce monsieur doit créer son identité numérique, puis vérifier son compte CPF.\n'
    + 'Prochaine étape: prendre un rendez-vous!'
);
assert.strictEqual(
  formatVoiceDictation(
    'première partie point-virgule deuxième partie point d’interrogation '
      + 'nouveau paragraphe conclusion point'
  ),
  'Première partie; deuxième partie?\n\nConclusion.'
);
assert.strictEqual(
  formatVoiceDictation('suite du texte point fin', false),
  'suite du texte. Fin'
);
assert.strictEqual(
  formatVoiceDictation('première ligne à la ligne'),
  'Première ligne\n'
);
assert.strictEqual(formatVoiceDictation('à la ligne', false), '\n');

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
    result('il souhaite suivre la formation A3P point', true),
    result('à la ligne un rendez-vous sera fixé', false)
  ]
});
assert.strictEqual(
  field.value,
  'Le candidat a appelé. Il souhaite suivre la formation A3P.\nUn rendez-vous sera fixé'
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
  results: [result('à la ligne', true)]
});
assert.ok(field.value.endsWith('Un rendez-vous sera fixé\n'));
voice.button.onclick();

voice.button.onclick();
currentRecognition.onresult({
  resultIndex: 0,
  results: [result('le financement sera vérifié point', true)]
});
assert.ok(field.value.endsWith('\nLe financement sera vérifié.'));
destroy();
assert.strictEqual(currentRecognition.aborted, true);
assert.strictEqual(voice.button.onclick, null);

const publication = makeButton('Dicter la publication');
bindVoiceDictation(makeField(), publication.button, {textContent: ''});
const publicationRecognition = currentRecognition;
publication.button.onclick();
assert.strictEqual(publication.label.textContent, 'Arrêter la dictée');

const message = makeButton('Dicter le message');
bindVoiceDictation(makeField(), message.button, {textContent: ''});
message.button.onclick();
assert.strictEqual(publicationRecognition.stopped, true);
assert.strictEqual(publication.label.textContent, 'Dicter la publication');
assert.strictEqual(message.label.textContent, 'Arrêter la dictée');
message.button.onclick();
assert.strictEqual(message.label.textContent, 'Dicter le message');

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
