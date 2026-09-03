const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/crm.js', 'utf8');
const start = source.indexOf('const wedofLabels=');
const end = source.indexOf('function wedofCategory', start);
assert.ok(start >= 0 && end > start, 'Bloc WEDOF introuvable');

const context = {};
vm.runInNewContext(`${source.slice(start, end)}\nglobalThis.helpers={wedofLabel,wedofValue,wedofFranceTravailStatus,wedofMainStatusLabel,wedofLatestResource,wedofFundingSummary,contactWedofStatusDetails};`, context);
const {wedofLabel, wedofValue, wedofFranceTravailStatus, wedofMainStatusLabel, wedofLatestResource, wedofFundingSummary, contactWedofStatusDetails} = context.helpers;

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
assert.equal(wedofFranceTravailStatus({state:'accepted',history:{refusedByFinancerDate:'2026-08-23'}}), 'refusee');
assert.equal(wedofFranceTravailStatus({state:'validated',history:{refusedByFinancerDate:null}}), '');

const williamFolders = [
  {stable_id:'40609198536',payload:{state:'serviceDoneValidated',createdAt:'2025-11-25T07:41:00+01:00'}},
  {stable_id:'401684627811',payload:{state:'validated',createdAt:'2026-08-19T13:55:00+02:00',history:{refusedByFinancerDate:'2026-08-23T13:35:00+02:00'}}},
];
assert.equal(wedofLatestResource(williamFolders).stable_id, '401684627811');
assert.deepEqual(
  JSON.parse(JSON.stringify(wedofFundingSummary(williamFolders))),
  {latest:williamFolders[1],cpf:'validated',ft:'refusee'},
);
assert.deepEqual(
  JSON.parse(JSON.stringify(contactWedofStatusDetails([{payload:{state:'notProcessed'}}]))),
  {state:'notprocessed',label:'CPF - À traiter',tone:'danger'},
);
assert.deepEqual(
  JSON.parse(JSON.stringify(contactWedofStatusDetails([{payload:{state:'validated'}}]))),
  {state:'validated',label:'CPF - En attente d’acceptation du titulaire',tone:'warning'},
);
assert.deepEqual(
  JSON.parse(JSON.stringify(contactWedofStatusDetails([{payload:{state:'waitingAcceptation'}}]))),
  {state:'waitingacceptation',label:'CPF - Demande financement FT en cours',tone:'orange'},
);
assert.deepEqual(
  JSON.parse(JSON.stringify(contactWedofStatusDetails([{payload:{state:'accepted'}}]))),
  {state:'accepted',label:'CPF - Accepté',tone:'success'},
);
assert.deepEqual(
  JSON.parse(JSON.stringify(contactWedofStatusDetails([{
    payload:{state:'validated',history:{refusedByFinancerDate:'2026-08-23'}},
  }]))),
  {state:'ft-refused',label:'CPF - Demande FT refusée',tone:'danger'},
);
assert.deepEqual(
  JSON.parse(JSON.stringify(contactWedofStatusDetails(
    [{payload:{state:'validated'}}],
    {statut_demande_financement_ft:'refusee',statut_demande_financement_ft_source:'manual'},
  ))),
  {state:'ft-refused',label:'CPF - Demande FT refusée',tone:'danger'},
);
assert.deepEqual(
  JSON.parse(JSON.stringify(contactWedofStatusDetails(
    [{payload:{state:'validated'}}],
    {statut_secondaire:'Financement FT refusé'},
  ))),
  {state:'ft-refused',label:'CPF - Demande FT refusée',tone:'danger'},
);
assert.equal(contactWedofStatusDetails([]), null);

const serverMarkedLatest = [
  {stable_id:'newer-sync-but-historical',payload:{state:'serviceDoneValidated',createdAt:'2026-08-20'}},
  {stable_id:'server-authoritative',is_latest:true,payload:{state:'validated',createdAt:'2026-08-19'}},
];
assert.equal(wedofLatestResource(serverMarkedLatest).stable_id, 'server-authoritative');

console.log('WEDOF France Travail status mapping: OK · contact header · latest folder only');
