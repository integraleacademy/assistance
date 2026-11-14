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

    def request_row(ident, attendee, title="Formation VTC"):
        return {"externalId": ident, "provider": "integrale_edof", "type": "cpf",
                "state": "notProcessed", "createdAt": "2026-11-11", "attendee": attendee,
                "trainingActionInfo": {"title": title}}

    same_person = {"firstName": "Alice", "lastName": "Exemple"}
    items = [request_row("100001", same_person), request_row("100002", same_person),
             request_row("100003", {}, "DESP VAE")]
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
    assert {r["folder_id"]: (r["formation"], r["desp_type"]) for r in queue} == {
        "100001": ("Chauffeur VTC", ""), "100002": ("Chauffeur VTC", ""), "100003": ("DESP", "VAE")}
    assert all(r["edof_url"].endswith(r["folder_id"]) for r in queue)
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


def test_team_can_hide_and_restore_one_folder_across_syncs(cpf_client, monkeypatch):
    cpf_client.post("/api/crm/contacts", json={
        "prenom": "Marie", "nom": "Exemple", "force_create": True,
    })
    rows = [{"externalId": str(100001+i), "type": "cpf", "state": "notProcessed",
             "provider": "integrale_edof", "createdAt": "2026-11-12",
             "attendee": {"firstName": "Marie", "lastName": "Exemple"} if i < 2 else {}}
            for i in range(3)]

    def directory(path, *, params):
        assert (path, params) == ("/dossiers", {"scope": "requests", "page": 1})
        return {"items": rows}, {"X-Total-Count": str(len(rows))}

    monkeypatch.setattr(application, "_wedof_request", directory)
    application._edof_sync_requests()
    data_before = copy.deepcopy(application.load_data()["crm_contacts"])
    endpoint = "/api/crm/cpf-dossiers/100001/visibilite"
    bootstrap = "/api/crm/bootstrap?section=pistes"
    updates = "/api/crm/contacts/updates?section=pistes&delta=1"
    other = application.app.test_client()
    assert other.put(endpoint, json={"hidden": True}).status_code == 302
    with other.session_transaction() as session:
        session["user_email"] = "cassandre@integraleacademy.com"
    etag = other.get(bootstrap).headers["ETag"]
    cursor = other.get(updates).json["cursor"]

    def ids(payload, key):
        return {row["folder_id"] for row in payload[key]}

    def check_split(payload):
        assert ids(payload, "cpf_pending_folders") == {"100002", "100003"}
        assert ids(payload, "cpf_hidden_folders") == {"100001"}

    for value in ["true", 1, None]:
        assert other.put(endpoint, json={"hidden": value}).status_code == 400
    assert other.put(endpoint.replace("100001", "xxxxxx"), json={"hidden": True}).status_code == 400
    assert other.put(endpoint.replace("100001", "999999"), json={"hidden": True}).status_code == 409
    with monkeypatch.context() as local:
        local.setattr(application, "_wedof_request", lambda *a, **k: pytest.fail("EDOF called by CRM action"))
        check_split(other.put(endpoint, json={"hidden": True}).json)
        check_split(cpf_client.put(endpoint, json={"hidden": True}).json)
    delta = other.get(updates + "&cursor=" + cursor).json
    assert delta["delta"]
    check_split(delta)
    fresh = other.get(bootstrap, headers={"If-None-Match": etag})
    assert fresh.status_code == 200
    check_split(fresh.json)
    application._edof_sync_requests()
    check_split(cpf_client.get(bootstrap).json)
    assert application.load_data()["crm_contacts"] == data_before
    for _ in range(2):
        restored = cpf_client.put(endpoint, json={"hidden": False}).json
        assert ids(restored, "cpf_pending_folders") == {"100001", "100002", "100003"}
        assert restored["cpf_hidden_folders"] == []
    lone_folder = endpoint.replace("100001", "100003")
    assert other.put(lone_folder, json={"hidden": True}).json["cpf_hidden_folders"][0]["contact_id"] is None
    rows.pop()
    application._edof_sync_requests()
    assert other.get(bootstrap).json["cpf_hidden_folders"] == []
    assert other.put(lone_folder, json={"hidden": False}).status_code == 409
    assert ids(other.get(bootstrap).json, "cpf_pending_folders") == {"100001", "100002"}
    assert "cpf_hidden_folders" not in other.get("/api/crm/bootstrap?section=contacts").json


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
        section("const badge=", "const vaeEligibilityQuestions="),
        section("function listOriginTone(", "const dashboardPeriods="),
        section("function listContactsForType(", "function listPage("),
        section("function refreshPipelineQueues()", "document.addEventListener('visibilitychange'"),
    ])
    script = r"""
const assert=require('node:assert/strict'),C={section:'pistes'},location={search:''};
const contacts=[{id:'one',statut:'Disqualifié',prenom:'Alice',nom:'Exemple',formation:'DESP',desp_type:'INITIAL',
 mail:'alice@example.test',activity_counts:{emails:2},_summary:true,
 integration_score:{score:72,level:'high'},origine:'Site internet'},
 {id:'two',statut:'Converti',archived_at:'2026-07-01'},{id:'three',statut:'En cours'}];
const initial=JSON.stringify(contacts),dash=()=>null,dashboardDrilldownState=dash;
let statusFilter='CPF à traiter',crmUpdateCursor='',crmRefreshInFlight=false,lastCrmRefreshAt=0;
const active=()=>contacts.filter(c=>!c.archived_at),crmActiveContacts=active;
const contactInStore=id=>contacts.find(c=>c.id===id),canonicalCrmOrigin=c=>c.origine||'Autre';
const displayName=c=>[c.prenom,c.nom].filter(Boolean).join(' '),esc=s=>String(s??'');
const contactSheetUrl=id=>'/crm/contacts?fiche='+id;
const initials=()=> 'AE',contactCoordinateSummary=c=>c.mail||'',fmt=v=>v||'—';
const contactPipelineStatusMarkup=c=>badge(c.statut),crmAppointments=[],leadScoreSort='',shortScoreLabel={high:'Élevé'};
const window={CRMWorkspace:{contactCompleteness:c=>c===contacts[0]?83:0}};
const selectedLeadIds=new Set();
const scheduleCrmRefresh=()=>{},updateLeadCount=()=>{},updateVisibleAppointmentData=()=>{};
const folder=(id,contact_id)=>({folder_id:id,contact_id,edof_url:'https://example.test/?folder='+id,
    prenom:'Alice',nom:'Exemple',formation:'APS',dates_formation:'Du 2 au 5 novembre',filed_at:'2026-11-10'});
let batch=[folder('100001','one'),folder('100002','one'),folder('100003','two'),folder('100004',null)];
Object.assign(batch[1],{formation:'DESP',desp_type:'VAE'});
const count={},input={value:'10000',dispatchEvent(){this.rows=listContactsForType('pistes')
    .filter(c=>contactHasPipelineStatus(c,statusFilter)&&c.cpf_folder_id.includes(this.value))}};
const document={hidden:false,querySelector(q){
 return q==='#listSearch'?input:q==='[data-status="CPF à traiter"] b'?count:null;
}};
let hidden=[],visibilityError=false,putCalls=0,lastToast='',interceptRead=null;
document.querySelectorAll=()=>[];
const toast=message=>{lastToast=message};
const api=async(url,options)=>{
 if(options?.method==='PUT'){
  putCalls++;
  assert.equal(url,'/api/crm/cpf-dossiers/100001/visibilite');
  if(visibilityError)throw Error('Échec temporaire');
  const makeHidden=JSON.parse(options.body).hidden;
  const all=[...batch,...hidden],item=all.find(row=>row.folder_id==='100001');
  batch=all.filter(row=>row!==item);hidden=makeHidden?[item]:[];
  if(!makeHidden)batch.unshift(item);
  return {cpf_pending_folders:batch,cpf_hidden_folders:hidden};
 }
 assert.ok(url.includes('section=pistes'));
 if(interceptRead)return interceptRead;
 return {delta:true,cpf_pending_folders:batch,cpf_hidden_folders:hidden};
};
""" + production + r"""
(async()=>{
 assert.equal(pipelineStatusCount(active(),statusFilter),'—');
 await refreshCrmSnapshot();
 assert.equal(count.textContent,4);
 assert.deepEqual(input.rows.map(r=>r.id),['one','one','two',undefined]);
 const options={selectable:true,scoreSortable:true,showCompleteness:true,showDespJourney:true};
 const render=rows=>table(rows,{...options,cpfView:true});
 const html=render(input.rows);
 const headings=markup=>markup.match(/<thead>([\s\S]*?)<\/thead>/)[1];
 assert.equal(headings(html),headings(table([contacts[0]],options)));
 assert.match(html,/class="crm-activity-table"/);
 assert.equal((html.match(/class="crm-row-link crm-row-link-primary" href="\/crm\/contacts\?fiche=one"/g)||[]).length,2);
 assert.match(html,/class="avatar"/);
 for(const value of ['alice@example.test','Site internet','2</strong> mails','72 — Élevé','83 %','APS','Du 2 au 5 novembre','10/11/2026','crm-desp-journey">VAE']){
   assert.ok(html.includes(value),value);
 }
 assert.equal((html.match(/data-cpf-folder=/g)||[]).length,4);
 assert.equal((html.match(/class="crm-cpf-reference"/g)||[]).length,4);
 assert.doesNotMatch(html,/href="[^"]*undefined|data-id="undefined"|<a\b[^>]*>[^<]*<a\b/);
 assert.equal((html.match(/Sans fiche CRM/g)||[]).length,1);
 assert.equal((html.match(/data-lead-select="one"/g)||[]).length,2);
 assert.ok(!html.includes('data-lead-select="undefined"'));
 assert.ok(html.includes('Dossier 100001')&&html.includes('Dossier 100002'));
 assert.equal((html.match(/data-cpf-hide=/g)||[]).length,4);
 let release;
 interceptRead=new Promise(resolve=>{release=resolve});
 const olderSnapshot={delta:true,cursor:'older',cpf_pending_folders:batch,cpf_hidden_folders:[]};
 const polling=refreshCrmSnapshot();
 const saving=changeCpfVisibility('100001',true);
 await changeCpfVisibility('100001',true);await saving;
 assert.equal(putCalls,1);assert.equal(count.textContent,3);
 assert.equal(input.value,'10000');assert.equal(input.rows.filter(row=>row.id==='one').length,1);
 assert.deepEqual(cpfHiddenFolders.map(row=>row.folder_id),['100001']);
 release(olderSnapshot);await polling;interceptRead=null;
 assert.equal(count.textContent,3);assert.equal(crmUpdateCursor,'');
 visibilityError=true;
 await changeCpfVisibility('100001',false);
 assert.equal(lastToast,'Échec temporaire');assert.equal(count.textContent,3);
 visibilityError=false;
 await changeCpfVisibility('100001',false);
 assert.equal(count.textContent,4);assert.equal(cpfHiddenFolders.length,0);
 batch=batch.slice(1);await refreshCrmSnapshot();
 assert.equal(count.textContent,3);assert.equal(input.rows.length,3);assert.equal(input.value,'10000');
 batch=[];await refreshCrmSnapshot();assert.equal(count.textContent,0);
 assert.ok(render(input.rows).includes('Aucun dossier CPF'));
 statusFilter='';assert.deepEqual(listContactsForType('pistes').map(c=>c.id),['three']);
 assert.equal(JSON.stringify(contacts),initial);
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
