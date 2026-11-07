import subprocess
from pathlib import Path

from crm_funding_safety import merge_folder


def test_new_detail_replaces_previous_snapshot_and_survives_directory_reads():
    original = {"provider":"integrale_edof","edofDetail":{"readAt":10,"files":[{"name":"Ancien.pdf"}],"attendee":{"phoneNumber":"old"}}}
    current = {"provider":"integrale_edof","edofDetail":{"readAt":20,"files":[],"attendee":{"email":"new@example.test"}}}
    updated = merge_folder(original,current)
    assert updated["edofDetail"] == current["edofDetail"]
    assert merge_folder(updated,original)["edofDetail"] == current["edofDetail"]
    assert merge_folder(updated,{"provider":"integrale_edof","state":"accepted"})["edofDetail"] == current["edofDetail"]


def test_contact_card_uses_official_details_and_leaves_unreported_terms_unknown():
    root = Path(__file__).resolve().parents[1]
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('static/crm.js','utf8');
const helpers=source.slice(source.indexOf('const wedofLabels='),source.indexOf('function renderWedof(c,'));
const env={URL, C:{is_admin:false}, esc:text=>String(text)};
vm.runInNewContext(helpers+'; globalThis.card=wedofFolder;globalThis.money=wedofMoney;',env);
const card={stable_id:'123456',payload:{provider:'integrale_edof',state:'notProcessed',
  trainingActionInfo:{totalIncl:'0.0',indicativeDuration:'0.0',hoursInCenter:'0.0',hoursInCompany:'0.0'},
  attendee:{firstName:'Exemple',lastName:'Test'}}};
const summary=env.card(card,0,{mail:'crm@example.test'});
assert.doesNotMatch(summary,/0,00\s*€|0\.0 h/);
assert.match(summary,/Non renseigné par EDOF/);
assert.match(summary,/Coordonnées de la fiche CRM/);
assert.match(summary,/liste des pièces n’a pas été transmise/);
card.payload.edofDetail={readAt:1810000000,trainingActionInfo:{totalIncl:'3900,50',indicativeDuration:'70',hoursInCompany:'0'},
  attendee:{email:'officiel@example.test',phoneNumber:'0600000000'},certification:{name:'Diplôme',externalId:'RNCP99999'},
  history:[{state:'accepted',date:'2026-11-06'}],files:[{name:'Proposition.pdf'}]};
const detail=env.card(card,0,{mail:'crm@example.test'});
for(const text of ['70 h','0 h','officiel@example.test','0600000000','RNCP99999','Proposition.pdf','Accepté'])assert.ok(detail.includes(text),text);
assert.match(detail,/3.900,50.€/); assert.doesNotMatch(detail,/crm@example.test/);
assert.equal(env.money('inconnu'),'Non renseigné');
"""
    subprocess.run(["node","-e",script],cwd=root,check=True)
