const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/crm.js', 'utf8');
const start = source.indexOf('const wedofLabels=');
const end = source.indexOf('function updateFundingBadges', start);
assert.ok(start >= 0 && end > start, 'Bloc WEDOF introuvable');

const context = {};
vm.runInNewContext(`${source.slice(start, end)}\nglobalThis.helpers={wedofFranceTravailStatus,wedofMainStatusLabel};`, context);
const {wedofFranceTravailStatus, wedofMainStatusLabel} = context.helpers;

assert.equal(wedofFranceTravailStatus({state:'waitingAcceptation'}), 'en_cours_instruction');
assert.equal(wedofMainStatusLabel({state:'waitingAcceptation'}), 'En cours d’instruction France Travail');
assert.equal(wedofFranceTravailStatus({state:'validated'}), '');
assert.equal(wedofMainStatusLabel({state:'validated'}), 'En attente d’acceptation du candidat');
assert.equal(wedofFranceTravailStatus({state:'accepted'}), '');
assert.equal(wedofFranceTravailStatus({state:'accepted',history:{waitingAcceptationDate:'2026-08-11'}}), 'acceptee');
assert.equal(wedofFranceTravailStatus({state:'refused',history:[{state:'waitingAcceptation'}]}), 'refusee');
assert.equal(wedofFranceTravailStatus({state:'cancelled',history:{waitingAcceptationDate:'2026-08-11'}}), 'annulee');

console.log('WEDOF France Travail status mapping: OK');
