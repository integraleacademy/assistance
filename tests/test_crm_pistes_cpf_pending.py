"""The pipeline counts pending EDOF dossiers independently of CRM contact stages."""
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


def test_contact_status_still_uses_latest_created_folder_and_updates_from_local_cache(
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

    # Individual contact badges still follow the newest folder.
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


def test_pipeline_snapshots_follow_complete_request_list(cpf_client, monkeypatch):
    contact = cpf_client.post("/api/crm/contacts", json={
        "prenom": "Alice", "nom": "Exemple", "force_create": True,
    }).json
    data = application.load_data()
    data["crm_contacts"][0]["statut"] = "Disqualifié"
    application.save_data(data)

    def request_row(ident, attendee):
        return {"externalId": ident, "provider": "integrale_edof", "type": "cpf",
                "state": "notProcessed", "createdAt": "2026-11-11", "attendee": attendee,
                "trainingActionInfo": {"title": "Formation VTC"}}

    same_person = {"firstName": "Alice", "lastName": "Exemple"}
    items = [request_row("100001", same_person), request_row("100002", same_person),
             request_row("100003", {})]
    total = 3

    def source(path, *, params):
        assert path == "/dossiers" and params == {"scope": "requests", "page": 1}
        return {"items": items}, {"X-Total-Count": str(total)}

    monkeypatch.setattr(application, "_wedof_request", source)
    bootstrap = "/api/crm/bootstrap?section=pistes"
    updates = "/api/crm/contacts/updates?section=pistes&delta=1"
    assert cpf_client.get(bootstrap).json["cpf_pending_folders"] is None
    application._edof_sync_requests()
    application._edof_sync_requests()
    result = cpf_client.get(bootstrap)
    etag = result.headers["ETag"]
    queue = result.json["cpf_pending_folders"]
    assert {r["folder_id"]: r["contact_id"] for r in queue} == {
        "100001": contact["id"], "100002": contact["id"], "100003": None}
    assert all(r["formation"] == "Chauffeur VTC" and r["edof_url"].endswith(r["folder_id"]) for r in queue)
    assert result.json["contacts"][0]["statut"] == "Disqualifié"
    assert "cpf_pending_folders" not in cpf_client.get("/api/crm/bootstrap?section=contacts").json
    initial = cpf_client.get(updates).json
    assert initial["cpf_pending_folders"] == queue
    items[:] = items[:1]
    with pytest.raises(application.WedofAPIError, match="incomplète"):
        application._edof_sync_requests()
    assert cpf_client.get(bootstrap).json["cpf_pending_folders"] == queue
    total = 1
    application._edof_sync_requests()
    assert cpf_client.get(bootstrap, headers={"If-None-Match": etag}).status_code == 200
    delta = cpf_client.get(updates + "&cursor=" + initial["cursor"]).json
    assert [r["folder_id"] for r in delta["cpf_pending_folders"]] == ["100001"]
    items.clear()
    total = 0
    application._edof_sync_requests()
    assert cpf_client.get(bootstrap).json["cpf_pending_folders"] == []


def test_pipeline_counts_folders_and_refreshes_even_when_no_contact_changes():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the CRM browser helpers")
    source = (ROOT / "static/crm.js").read_text(encoding="utf-8")

    def section(start, end):
        offset = source.index(start)
        return source[offset:source.index(end, offset)]

    production = "\n".join([
        section("const isActiveLead=", "const manualNextActionValue="),
        section("function listContactsForType(", "function listPage("),
        section("function refreshPipelineQueues()", "document.addEventListener('visibilitychange'"),
    ])
    script = r"""
const assert=require('node:assert/strict'),C={section:'pistes'},location={search:''};
const contacts=[{id:'one',statut:'Disqualifié',prenom:'Alice',nom:'Exemple'},
 {id:'two',statut:'Converti',archived_at:'2026-07-01'},{id:'three',statut:'En cours'}];
const initial=JSON.stringify(contacts),dash=()=>null,dashboardDrilldownState=dash;
let statusFilter='CPF à traiter',crmUpdateCursor='',crmRefreshInFlight=false,lastCrmRefreshAt=0;
const active=()=>contacts.filter(c=>!c.archived_at),crmActiveContacts=active;
const contactInStore=id=>contacts.find(c=>c.id===id),canonicalCrmOrigin=()=> 'Autre';
const displayName=c=>[c.prenom,c.nom].filter(Boolean).join(' '),esc=s=>String(s??'');
const contactSheetUrl=id=>'/crm/contacts?fiche='+id;
const selectedLeadIds=new Set();
const scheduleCrmRefresh=()=>{},updateLeadCount=()=>{},updateVisibleAppointmentData=()=>{};
const folder=(id,contact_id)=>({folder_id:id,contact_id,edof_url:'https://example.test/?folder='+id,
    prenom:'Alice',nom:'Exemple',formation:'APS',filed_at:'2026-11-10'});
let batch=[folder('100001','one'),folder('100002','one'),folder('100003','two'),folder('100004',null)];
const count={},input={value:'10000',dispatchEvent(){this.rows=listContactsForType('pistes')
    .filter(c=>contactHasPipelineStatus(c,statusFilter)&&c.cpf_folder_id.includes(this.value))}};
const document={hidden:false,querySelector(q){
 return q==='#listSearch'?input:q==='[data-status="CPF à traiter"] b'?count:null;
}};
const api=async url=>{assert.ok(url.includes('section=pistes'));return {delta:true,cpf_pending_folders:batch}};
""" + production + r"""
(async()=>{
 assert.equal(pipelineStatusCount(active(),statusFilter),'—');
 await refreshCrmSnapshot();
 assert.equal(count.textContent,4);
 assert.deepEqual(input.rows.map(r=>r.id),['one','one','two',undefined]);
 const html=cpfFolderTable(input.rows);
 assert.equal((html.match(/fiche=one/g)||[]).length,2);
 assert.equal((html.match(/Sans fiche CRM/g)||[]).length,1);
 assert.equal((html.match(/data-lead-select="one"/g)||[]).length,2);
 assert.ok(!html.includes('data-lead-select="undefined"'));
 assert.ok(html.includes('Dossier 100001')&&html.includes('Dossier 100002'));
 batch=batch.slice(1);await refreshCrmSnapshot();
 assert.equal(count.textContent,3);assert.equal(input.rows.length,3);assert.equal(input.value,'10000');
 batch=[];await refreshCrmSnapshot();assert.equal(count.textContent,0);
 assert.ok(cpfFolderTable(input.rows).includes('Aucun dossier CPF'));
 statusFilter='';assert.deepEqual(listContactsForType('pistes').map(c=>c.id),['three']);
 assert.equal(JSON.stringify(contacts),initial);
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
