"""Pistes awaiting a follow-up must be distinguishable from scheduled reminders."""
from pathlib import Path
import shutil
import subprocess

import pytest

import app as application


ROOT = Path(__file__).resolve().parents[1]


def browser_helpers(*, refresh=False):
    source = (ROOT / "static/crm.js").read_text(encoding="utf-8")

    def section(start, end):
        start_at = source.index(start)
        return source[start_at:source.index(end, start_at)]

    helpers = [
        "const parisDateKey=()=> '2026-09-21';",
        "const canonicalCrmOrigin=()=> 'Autre';",
        section("const isActiveLead=", "const manualNextActionValue="),
        section("function relanceStatusDetails(", "function contactRelanceStatusMarkup("),
        section("const pipelineOverviewStatuses=", "const timelineButtons="),
    ]
    if refresh:
        helpers.append(section("function refreshPipelineQueues()",
                               "document.addEventListener('visibilitychange'"))
    return "\n".join(helpers)


def run_browser_test(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the CRM browser helpers")
    result = subprocess.run([node, "-e", script], capture_output=True,
                            text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_missing_relance_bucket_excludes_scheduled_overdue_and_inactive_leads():
    run_browser_test(r"""
const assert = require('node:assert/strict');
const S=['Nouveaux','A relancer','Disqualifié'], SECONDARY_STATUSES=['A relancer'];
const contacts=[
 {id:'missing',statut:'A relancer'},
 {id:'empty',statut:'A relancer',relance_date:'  '},
 {id:'secondary',statut:'En cours',statut_secondaire:'A relancer'},
 {id:'invalid',statut:'A relancer',relance_date:'2026-02-30'},
 {id:'history',statut:'A relancer',relance_date:'',relances:[{status:'answered',scheduled_date:'2026-09-20'}]},
 {id:'overdue',statut:'A relancer',relance_date:'2026-09-20'},
 {id:'today',statut:'A relancer',relance_date:'2026-09-21'},
 {id:'future',statut:'A relancer',relance_date:'2026-09-25'},
 {id:'unrelated',statut:'Nouveaux'},
 {id:'manual-label',statut:'À relancer sans relance programmée'},
 {id:'archived',statut:'A relancer',archived_at:'2026-09-20'},
 {id:'converted',statut:'Converti',statut_secondaire:'A relancer'},
 {id:'disqualified',statut:'Disqualifié',statut_secondaire:'A relancer'},
];
const before=JSON.stringify(contacts);
""" + browser_helpers() + r"""
assert.deepEqual(pipelineOverviewStatuses(),[
 'Nouveaux','Nouveaux META','A relancer','À relancer sans relance programmée','CPF à traiter',
]);
assert.deepEqual(contacts.filter(c=>contactHasPipelineStatus(c,MISSING_RELANCE_PIPELINE_STATUS)).map(c=>c.id),
 ['missing','empty','secondary','invalid','history']);
assert.equal(contactHasPipelineStatus(contacts.find(c=>c.id==='overdue'),'A relancer'),true);
assert.equal(JSON.stringify(contacts),before);
assert.deepEqual(S,['Nouveaux','A relancer','Disqualifié']);
""")


def test_poll_updates_missing_queue_after_scheduling_and_cancelling():
    run_browser_test(r"""
const assert = require('node:assert/strict');
const C={section:'pistes'},location={search:''};
let contacts=[{id:'one',nom:'Martin',statut:'A relancer',relance_date:''}];
let crmRefreshInFlight=false,crmAppointments=[],searches=0;
const count={textContent:1},allCount={textContent:1};
const input={value:'Martin',dispatchEvent(){searches++;
 this.visible=contacts.filter(c=>c.nom===this.value&&contactHasPipelineStatus(c,MISSING_RELANCE_PIPELINE_STATUS)).map(c=>c.id);
}};
const document={hidden:false,querySelector(selector){
 if(selector==='#listSearch')return input;
 if(selector==='[data-status="À relancer sans relance programmée"] b')return count;
 if(selector==='[data-status="A relancer"] b')return allCount;
 return null;
}};
const contactInStore=id=>contacts.find(c=>c.id===id);
const mergeContactInStore=(id,update)=>Object.assign(contactInStore(id),update);
const crmActiveContacts=()=>contacts.filter(c=>!c.archived_at);
const refreshPipelineAppointments=async()=>{};
const updateLeadCount=()=>{},updateVisibleAppointmentData=()=>{},scheduleCrmRefresh=()=>{};
let updates=[{id:'one',statut:'A relancer',relance_date:'2026-10-05'}],fresh=[];
const api=async url=>url.startsWith('/api/crm/contacts/updates')?{contacts:updates}:fresh;
""" + browser_helpers(refresh=True) + r"""
(async()=>{
 await refreshCrmSnapshot();
 assert.equal(count.textContent,0);
 assert.equal(allCount.textContent,1);
 assert.deepEqual(input.visible,[]);
 assert.equal(input.value,'Martin');
 assert.equal(searches,1);
 await refreshCrmSnapshot();
 assert.equal(searches,1,'unchanged data does not redraw the list');
 updates=[{id:'one',statut:'A relancer',relance_date:''}];
 await refreshCrmSnapshot();
 assert.equal(count.textContent,1);
 assert.deepEqual(input.visible,['one']);
 updates=[{id:'one',statut:'En cours',statut_secondaire:'',relance_date:''}];
 await refreshCrmSnapshot();
 assert.equal(count.textContent,0);
 assert.equal(allCount.textContent,0);
 updates=[{id:'one',statut:'En cours',statut_secondaire:'A relancer',relance_date:''}];
 await refreshCrmSnapshot();
 assert.equal(count.textContent,1,'secondary status changes refresh the queue');
 // Reloading summaries for a new contact must not restore old detail dates/statuses.
 updates=[{id:'one',relance_date:'2026-10-06'},{id:'two',relance_date:''}];
 fresh=[{id:'one',nom:'Martin',statut:'A relancer',statut_secondaire:'',relance_date:'2026-10-06',_summary:true},
        {id:'two',nom:'Martin',statut:'A relancer',relance_date:'',_summary:true}];
 await refreshCrmSnapshot();
 assert.equal(contactInStore('one').relance_date,'2026-10-06');
 assert.equal(contactInStore('one').statut,'A relancer');
 assert.equal(contactInStore('one').statut_secondaire,'');
 assert.equal(count.textContent,1);
 assert.equal(allCount.textContent,2);
 assert.deepEqual(input.visible,['two']);
 assert.equal(input.value,'Martin');
})().catch(error=>{console.error(error);process.exitCode=1});
""")


def test_collaborative_updates_include_current_scheduled_date(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    monkeypatch.setenv("WEDOF_DB_PATH", str(tmp_path / "wedof.sqlite3"))
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    client = application.app.test_client()
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    created = client.post("/api/crm/contacts", json={
        "prenom": "Lina", "nom": "Martin", "force_create": True,
    })
    assert created.status_code == 201
    contact_id = created.get_json()["id"]
    url = f"/api/crm/contacts/{contact_id}"
    assert client.patch(url, json={"statut": "A relancer"}).status_code == 200

    def current_date():
        response = client.get("/api/crm/contacts/updates")
        assert response.status_code == 200
        summary = next(c for c in response.get_json()["contacts"] if c["id"] == contact_id)
        assert summary["statut"] == "A relancer"
        assert "relances" not in summary  # Keep the collaborative payload lightweight.
        return summary["relance_date"]

    assert current_date() == ""
    planned = client.patch(url, json={"relance_date": "2099-09-03"})
    assert planned.status_code == 200
    assert current_date() == "2099-09-03"
    relance = next(r for r in planned.get_json()["relances"] if r["status"] == "scheduled")
    deleted = client.delete(f"{url}/relances/{relance['id']}")
    assert deleted.status_code == 200
    assert current_date() == ""
