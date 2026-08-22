const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/crm.js', 'utf8');
const start = source.indexOf('const wedofLabels=');
const end = source.indexOf('function wedofCategory', start);
assert.ok(start >= 0 && end > start, 'Bloc WEDOF introuvable');

const context = {};
vm.runInNewContext(`${source.slice(start, end)}\nglobalThis.helpers={wedofLabel,wedofValue,wedofFranceTravailStatus,wedofMainStatusLabel};`, context);
const {wedofLabel, wedofValue, wedofFranceTravailStatus, wedofMainStatusLabel} = context.helpers;

const rejectedWithoutTitulaireSuite = 'Refusé sans suite par le titulaire';
assert.equal(wedofLabel('Rejected Without Titulaire Suite'), rejectedWithoutTitulaireSuite);
assert.equal(wedofLabel('rejectedWithoutTitulaireSuite'), rejectedWithoutTitulaireSuite);
assert.equal(wedofLabel('REJECTED_WITHOUT_TITULAIRE_SUITE'), rejectedWithoutTitulaireSuite);
assert.equal(wedofLabel('rejected-without-titulaire-suite'), rejectedWithoutTitulaireSuite);
assert.equal(wedofLabel('futureUnknownStatus'), 'Future Unknown Status');
assert.equal(wedofValue(['rejected_without_titulaire_suite', 'accepted']), `${rejectedWithoutTitulaireSuite}, Accepté`);
assert.equal(wedofValue({reason:'Rejected Without Titulaire Suite'}), rejectedWithoutTitulaireSuite);

assert.equal(wedofFranceTravailStatus({state:'waitingAcceptation'}), 'en_cours_instruction');
assert.equal(wedofMainStatusLabel({state:'waitingAcceptation'}), 'En cours d’instruction France Travail');
assert.equal(wedofFranceTravailStatus({state:'validated'}), '');
assert.equal(wedofMainStatusLabel({state:'validated'}), 'En attente d’acceptation du candidat');
assert.equal(wedofFranceTravailStatus({state:'validated',history:[{state:'waitingAcceptation'}]}), 'refusee');
assert.equal(wedofMainStatusLabel({state:'validated',history:[{state:'waitingAcceptation'}]}), 'En attente d’acceptation du candidat');
assert.equal(wedofFranceTravailStatus({state:'accepted'}), '');
assert.equal(wedofFranceTravailStatus({state:'accepted',history:{waitingAcceptationDate:'2026-08-11'}}), 'acceptee');
assert.equal(wedofFranceTravailStatus({state:'refused',history:[{state:'waitingAcceptation'}]}), 'refusee');
assert.equal(wedofFranceTravailStatus({status:'rejected'}), 'refusee');
assert.equal(wedofFranceTravailStatus({registrationState:'validated',events:{changes:[{details:{registrationState:'waitingAcceptation'}}]}}), 'refusee');
assert.equal(wedofFranceTravailStatus({registrationState:'validated',events:{changes:[{details:{state:'validated'}}]}}), '');
assert.equal(wedofFranceTravailStatus({state:'cancelled',history:{waitingAcceptationDate:'2026-08-11'}}), 'annulee');

console.log('WEDOF France Travail status mapping: OK');
