const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/crm.js', 'utf8');
const start = source.indexOf('const wedofLabels=');
const end = source.indexOf('function wedofCategory', start);
assert.ok(start >= 0 && end > start, 'Bloc WEDOF introuvable');
const context = {};
vm.runInNewContext(`${source.slice(start, end)}\nglobalThis.helpers={wedofLabel,wedofValue,wedofFranceTravailStatus,wedofMainStatusLabel,wedofLatestResource,wedofFundingSummary,contactWedofStatusDetails,contactExcludesFranceTravail};`, context);
const {wedofLabel, wedofValue, wedofFranceTravailStatus, wedofMainStatusLabel, wedofLatestResource, wedofFundingSummary, contactWedofStatusDetails, contactExcludesFranceTravail} = context.helpers;
const plain = value => JSON.parse(JSON.stringify(value));
const refusal = {state:'validated',trainingActionInfo:{solicitations:[{fundingType:9,status:'refused',submissionDate:'2026-10-01T09:00:00Z',returnDate:'2026-10-02T09:00:00Z'}]}};
const rejectedLabel = 'Refusé sans suite par le titulaire';
for(const code of ['Rejected Without Titulaire Suite','rejectedWithoutTitulaireSuite','REJECTED_WITHOUT_TITULAIRE_SUITE','rejected-without-titulaire-suite'])assert.equal(wedofLabel(code),rejectedLabel);
assert.equal(wedofLabel('futureUnknownStatus'),'Future Unknown Status');
assert.equal(wedofValue(['rejected_without_titulaire_suite','accepted']),`${rejectedLabel}, Accepté`);
assert.equal(wedofValue({reason:'Rejected Without Titulaire Suite'}),rejectedLabel);
for(const state of ['waitingAcceptation','validated','refused','rejected','refusedByFinancer','refusedByAttendee','refusedByOrganism','accepted','cancelled']){
 assert.equal(wedofFranceTravailStatus({state,history:{waitingAcceptationDate:'2026-10-01',refusedByFinancerDate:'2026-10-02'}}),'');
}
assert.equal(wedofMainStatusLabel({state:'waitingAcceptation'}),'En attente d’acceptation du financeur');
assert.equal(wedofMainStatusLabel({state:'validated'}),'En attente d’acceptation du candidat');
assert.equal(wedofFranceTravailStatus(refusal),'refusee');
for(const funder of [29,'C2P','REGION',null])assert.equal(wedofFranceTravailStatus({trainingActionInfo:{solicitations:[{fundingType:funder,status:'refused'}]}}),'');
assert.equal(wedofFranceTravailStatus({trainingActionInfo:{solicitations:[{fundingType:'POLE_EMPLOI',decisions:[{status:'refused'}]}]}}),'refusee');
assert.equal(wedofFranceTravailStatus({trainingActionInfo:{solicitations:[{fundingType:9,status:'accepted'}]}}),'acceptee');
const revised=plain(refusal);
revised.trainingActionInfo.solicitations.push({fundingType:9,status:'accepted',submissionDate:'2026-10-10T09:00:00Z'});
assert.equal(wedofFranceTravailStatus(revised),'acceptee');
const ambiguous=plain(refusal);
ambiguous.trainingActionInfo.solicitations.push({fundingType:9,status:'accepted'});
assert.equal(wedofFranceTravailStatus(ambiguous),'');
assert.deepEqual(plain(contactWedofStatusDetails([{payload:{state:'notProcessed'}}])),{state:'notprocessed',label:'CPF - À traiter',tone:'danger'});
assert.deepEqual(plain(contactWedofStatusDetails([{payload:{state:'validated'}}])),{state:'validated',label:'CPF - En attente d’acceptation du titulaire',tone:'warning'});
assert.deepEqual(plain(contactWedofStatusDetails([{payload:{state:'waitingAcceptation'}}])),{state:'waitingacceptation',label:'CPF - En attente du financeur',tone:'orange'});
assert.deepEqual(plain(contactWedofStatusDetails([{payload:refusal}])),{state:'ft-refused',label:'CPF - Demande FT refusée',tone:'danger'});
for(const c of [{statut_secondaire:'C2P en cours'},{statut_secondaire:'Transition pro'},{financement_ft:'NON'}]){
 assert.equal(contactExcludesFranceTravail(c),true);
 assert.equal(contactWedofStatusDetails([{payload:refusal}],c).state,'validated');
}
assert.equal(contactWedofStatusDetails([{payload:{state:'validated'}}],{statut_demande_financement_ft:'refusee'}).state,'validated');
assert.equal(contactWedofStatusDetails([{payload:{state:'validated'}}],{statut_demande_financement_ft:'refusee',statut_demande_financement_ft_source:'manual'}).state,'ft-refused');
assert.equal(contactWedofStatusDetails([]),null);
const folders=[{stable_id:'old',payload:{...refusal,createdAt:'2026-09-01'}},{stable_id:'new',payload:{state:'accepted',createdAt:'2026-10-01'}}];
assert.equal(wedofLatestResource(folders).stable_id,'new');
assert.deepEqual(plain(wedofFundingSummary(folders)),{latest:folders[1],cpf:'accepted',ft:''});
folders[0].is_latest=true;
assert.equal(wedofLatestResource(folders).stable_id,'old');
console.log('WEDOF France Travail status mapping: OK · explicit funder, C2P, latest decisions and contact header OK');
