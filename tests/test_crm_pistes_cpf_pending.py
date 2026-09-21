"""The CPF queue follows the latest cached WEDOF folder, not a manual stage."""
import copy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

import app as application


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cpf_client(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    monkeypatch.setenv("WEDOF_DB_PATH", str(tmp_path / "wedof.sqlite3"))
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    client = application.app.test_client()
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return client


def store_folder(contact_id, identifier, state, created_at):
    payload = {
        "state": state, "createdAt": created_at,
        "attendee": {"firstName": "Lina", "lastName": "Martin"},
    }
    with application._wedof_connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO wedof_resources "
            "(resource_type, stable_id, payload_json, remote_date, synced_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("registrationFolders", identifier, json.dumps(payload),
             "2026-09-21T12:00:00Z", "2026-09-21T12:00:00Z"),
        )
        db.execute(
            "INSERT OR REPLACE INTO wedof_contact_links "
            "(resource_type, resource_id, contact_id, attendee_id, match_method, "
            "linked_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("registrationFolders", identifier, contact_id, "", "email",
             "2026-09-21T12:00:00Z", "2026-09-21T12:00:00Z"),
        )


def test_cpf_queue_uses_latest_created_folder_and_updates_from_local_cache(
        cpf_client, monkeypatch):
    contact = cpf_client.post("/api/crm/contacts", json={
        "prenom": "Lina", "nom": "Martin", "force_create": True,
    }).get_json()
    store_folder(contact["id"], "older", "accepted", "2026-08-01T00:00:00Z")
    store_folder(contact["id"], "newer", "notProcessed", "2026-09-01T00:00:00Z")

    def forbid_remote(*args, **kwargs):
        pytest.fail("Reading the CPF queue must not query WEDOF remotely")

    monkeypatch.setattr(application, "_wedof_request", forbid_remote)
    before = copy.deepcopy(application.load_data())
    listed = cpf_client.get("/api/crm/contacts?section=pistes").get_json()
    assert next(c for c in listed if c["id"] == contact["id"])["cpf_status"] == "notprocessed"
    assert application.load_data() == before

    # Reuse the same cache for FT and CPF; no second decode or per-contact read.
    def forbid_rescan(*args, **kwargs):
        pytest.fail("The existing batch cache should be reused")

    with monkeypatch.context() as cached:
        cached.setattr(application, "_wedof_connect", forbid_rescan)
        funding, cpf = application._wedof_funding_statuses_by_contact(
            application.load_data(), include_cpf=True,
        )
        assert cpf[contact["id"]] == "notprocessed"
        assert application._wedof_funding_statuses_by_contact(
            application.load_data()) == funding

    # A newer accepted request removes the contact, even with old pending history.
    store_folder(contact["id"], "latest", "accepted", "2026-09-20T00:00:00Z")
    payload = cpf_client.get("/api/crm/bootstrap?section=pistes").get_json()
    assert next(c for c in payload["contacts"] if c["id"] == contact["id"])["cpf_status"] == "accepted"
    updates = cpf_client.get("/api/crm/contacts/updates").get_json()
    assert next(c for c in updates["contacts"] if c["id"] == contact["id"])["cpf_status"] == "accepted"


def test_cpf_status_is_shared_with_duplicate_identities_without_persisting(cpf_client):
    store_folder("linked", "pending", "not_processed", "2026-09-20T00:00:00Z")
    data = {"crm_contacts": [
        {"id": "linked", "prenom": "Lina", "nom": "Martin"},
        {"id": "duplicate", "prenom": "LINA", "nom": "MARTIN"},
        {"id": "unrelated", "prenom": "Nora", "nom": "Durand"},
    ]}
    before = copy.deepcopy(data)
    _, states = application._wedof_funding_statuses_by_contact(data, include_cpf=True)
    assert states == {"linked": "notprocessed", "duplicate": "notprocessed"}
    assert data == before


def test_missing_wedof_cache_keeps_contacts_available(cpf_client):
    contact = cpf_client.post("/api/crm/contacts", json={
        "prenom": "Nora", "nom": "Durand", "force_create": True,
    }).get_json()
    listed = cpf_client.get("/api/crm/contacts?section=pistes").get_json()
    assert next(c for c in listed if c["id"] == contact["id"])["cpf_status"] == ""


def test_cpf_pipeline_bucket_counts_and_filters_without_changing_stages():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the CRM browser helpers")
    source = (ROOT / "static/crm.js").read_text(encoding="utf-8")

    def section(start, end):
        start_at = source.index(start)
        return source[start_at:source.index(end, start_at)]

    production = "\n".join([
        section("const isActiveLead=", "const manualNextActionValue="),
        section("const pipelineOverviewStatuses=", "const timelineButtons="),
        section("const crmOriginFilterValues=", "const dashboardHasContact="),
        section("const crmActiveContacts=", "const dashboardContactsIn="),
    ])
    script = r"""
const assert = require('node:assert/strict');
const S = ['Nouveaux', 'A relancer', 'Disqualifié'];
const SECONDARY_STATUSES = ['Transition pro'];
const contacts = [
  {id:'pending', statut:'A relancer', cpf_status:'notprocessed'},
  {id:'meta', statut:'Nouveaux', origine:'META', cpf_status:'notprocessed'},
  {id:'accepted', statut:'Nouveaux', cpf_status:'accepted'},
  {id:'unknown', statut:'Nouveaux', cpf:'OUI'},
  {id:'manual-label', statut:'CPF à traiter'},
  {id:'converted', statut:'Converti', cpf_status:'notprocessed'},
  {id:'disqualified', statut:'Disqualifié', cpf_status:'notprocessed'},
  {id:'archived', statut:'Nouveaux', archived_at:'2026-09-20', cpf_status:'notprocessed'},
];
const before = JSON.stringify(contacts);
""" + production + r"""
assert.deepEqual(pipelineOverviewStatuses(),
  ['Nouveaux','Nouveaux META','A relancer','CPF à traiter','Transition pro']);
const rows = crmActiveContacts().filter(c=>contactHasPipelineStatus(c,'CPF à traiter'));
assert.deepEqual(rows.map(c=>c.id), ['pending','meta']);
assert.deepEqual(rows.filter(c=>contactHasPipelineStatus(c,'A relancer')).map(c=>c.id), ['pending']);
assert.deepEqual(S, ['Nouveaux','A relancer','Disqualifié']);
assert.equal(JSON.stringify(contacts), before);
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_cpf_poll_refreshes_queue_without_reloading_or_losing_search():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the CRM browser helpers")
    source = (ROOT / "static/crm.js").read_text(encoding="utf-8")
    helpers = source[source.index("const isActiveLead="):source.index("const manualNextActionValue=")]
    refresh = source[source.index("function refreshCpfPipelineQueue()"):
                     source.index("document.addEventListener('visibilitychange'")]
    script = r"""
const assert = require('node:assert/strict');
const C = {section:'pistes'}, location = {search:''};
let contacts = [{id:'one',nom:'Martin',statut:'Nouveaux',cpf_status:'notprocessed'}];
let crmRefreshInFlight=false, crmAppointments=[], searches=0, schedules=0;
const count={textContent:1}, input={value:'Martin',dispatchEvent(event){
  assert.equal(event.type,'input'); searches++;
  this.visible=contacts.filter(c=>c.nom===this.value&&contactHasPipelineStatus(c,'CPF à traiter')).map(c=>c.id);
}};
const document={hidden:false,querySelector(selector){
  return selector==='#listSearch'?input:selector==='[data-status="CPF à traiter"] b'?count:null;
}};
const contactInStore=id=>contacts.find(c=>c.id===id);
const mergeContactInStore=(id,update)=>Object.assign(contactInStore(id),update);
const crmActiveContacts=()=>contacts.filter(c=>!c.archived_at);
const canonicalCrmOrigin=()=> 'Autre';
const refreshPipelineAppointments=async()=>{};
const updateLeadCount=()=>{},updateVisibleAppointmentData=()=>{};
const scheduleCrmRefresh=()=>{schedules++};
let updates=[{id:'one',cpf_status:'accepted'}], fresh=[];
const api=async(url)=>url.startsWith('/api/crm/contacts/updates')?{contacts:updates}:fresh;
""" + helpers + refresh + r"""
(async()=>{
 await refreshCrmSnapshot();
 assert.equal(contactInStore('one').cpf_status,'accepted');
 assert.equal(count.textContent,0);
 assert.deepEqual(input.visible,[]);
 assert.equal(input.value,'Martin');
 assert.equal(searches,1);
 assert.equal(schedules,1);
 // No queue redraw on an unchanged status.
 await refreshCrmSnapshot();
 assert.equal(searches,1);
 // New contact IDs reload summaries without overwriting the latest CPF state
 // of a contact whose detail was opened previously.
 updates=[{id:'one',cpf_status:'notprocessed'},{id:'two',cpf_status:'notprocessed'}];
 fresh=[{id:'one',nom:'Martin',statut:'Nouveaux',cpf_status:'notprocessed',_summary:true},
        {id:'two',nom:'Martin',statut:'Nouveaux',cpf_status:'notprocessed',_summary:true}];
 await refreshCrmSnapshot();
 assert.equal(count.textContent,2);
 assert.deepEqual(input.visible,['one','two']);
 assert.equal(input.value,'Martin');
 // A contact sheet in use is not redrawn by the pipeline refresh helper.
 location.search='?fiche=one';
 refreshCpfPipelineQueue();
 assert.equal(searches,2);
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
