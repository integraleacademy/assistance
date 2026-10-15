"""Regression tests for the October CRM contention / polling incident."""
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess

import pytest
import requests

import app as application
from crm_memory_optimizations import calendly_contact_get
from crm_read_cache import LastProjection, UpdateSnapshots
from test_crm_memory_optimizations import Legacy
from test_crm_calendly import authenticated_client


def test_local_appointments_finish_while_another_thread_holds_write_lock():
    legacy = Legacy()
    legacy._CRM_RECONCILIATION_LOCK = threading.RLock()
    legacy.store["crm_calendly_appointments"] = [{"id": "a", "contact_id": "one"}]
    with ThreadPoolExecutor(max_workers=4) as pool:
        with legacy._CRM_RECONCILIATION_LOCK:
            results = [pool.submit(calendly_contact_get, legacy, "one", False,
                                   threading.BoundedSemaphore(2)) for _ in range(4)]
            assert all(result.result(timeout=1)[0]["appointments"][0]["id"] == "a"
                       for result in results)
    assert (legacy.loads, legacy.saves, legacy.remote_calls) == (0, 0, 0)


def test_local_orphan_is_visible_and_manual_refresh_repairs_phone_only_contact(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post("/api/crm/contacts", json={"prenom": "Test", "telephone": "0600000000"}).get_json()
    data = application.load_data()
    data["crm_calendly_appointments"] = [{
        "id": "legacy", "contact_id": "deleted", "invitee_phone": "+33600000000",
        "start_time": "2099-01-01T10:00:00Z", "status": "active",
    }]
    application.save_data(data)
    monkeypatch.setattr(application, "_crm_calendly_fetch_contact_appointments",
                        lambda *a: pytest.fail("phone-only repair needs no remote request"))
    slots = threading.BoundedSemaphore(2)
    local, status = calendly_contact_get(application, contact["id"], False, slots)
    assert status == 200 and local["appointments"][0]["id"] == "legacy"
    assert application._load_data_snapshot()["crm_calendly_appointments"][0]["contact_id"] == "deleted"
    refreshed, status = calendly_contact_get(application, contact["id"], True, slots)
    assert status == 200 and refreshed["appointments"][0]["contact_id"] == contact["id"]
    assert application._load_data_snapshot()["crm_calendly_appointments"][0]["contact_id"] == contact["id"]


def test_prepared_contacts_survive_unrelated_saves_without_mutating_snapshot(monkeypatch):
    monkeypatch.setattr(application, "_CRM_PROJECTIONS", LastProjection())
    calls = []

    def prepare(data):
        calls.append(True)
        data["crm_contacts"][0]["statut"] = "En cours"

    monkeypatch.setattr(application, "_crm_prepare_contacts", prepare)
    monkeypatch.setattr(application, "_crm_backfill_callback_requests", lambda data: None)
    source = {"crm_contacts": [{"id": "a", "statut": "Nouveaux"}],
              "crm_settings": {"custom": "one"}, "other": {"large": []}}
    first = application._crm_prepare_read_snapshot(source, (1, None, 2))
    updated = copy.deepcopy(source)
    updated["crm_settings"]["custom"] = "two"
    second = application._crm_prepare_read_snapshot(updated, (2, None, 2))
    assert len(calls) == 1
    assert first["crm_contacts"] is second["crm_contacts"]
    assert second["crm_settings"]["custom"] == "two"
    second["crm_settings"]["custom"] = "private"
    assert updated["crm_settings"]["custom"] == "two"
    assert source["crm_contacts"][0]["statut"] == "Nouveaux"
    assert first["other"] is source["other"]
    application._crm_prepare_read_snapshot(updated, (2, "new-wedof", 2))
    assert len(calls) == 2
    updated = copy.deepcopy(updated)
    updated["crm_contacts"][0]["nom"] = "Updated"
    application._crm_prepare_read_snapshot(updated, (3, "new-wedof", 2))
    assert len(calls) == 3


def test_calendar_scores_once_and_invalidates_only_its_dependencies(monkeypatch):
    monkeypatch.setattr(application, "_CRM_PROJECTIONS", LastProjection())
    original = application.calculate_candidate_integration_score
    calls = []

    def score(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(application, "calculate_candidate_integration_score", score)
    data = {"crm_contacts": [{"id": "a", "formation": "APS", "cpf": "Oui"}],
            "crm_calendly_appointments": [
                {"id": "1", "contact_id": "a", "start_time": "2099-01-01"},
                {"id": "2", "contact_id": "a", "start_time": "2099-01-02"},
            ]}
    first = application._crm_calendly_appointments_payload(data)["appointments"]
    assert len(calls) == 1
    data["crm_contacts"][0]["activities"] = [{"detail": "A new email"}]
    assert application._crm_calendly_appointments_payload(data)["appointments"] is first
    assert len(calls) == 1
    data["crm_contacts"][0]["cpf"] = "Non"
    application._crm_calendly_appointments_payload(data)
    assert len(calls) == 2
    data["crm_calendly_appointments"][0]["status"] = "canceled"
    assert application._crm_calendly_appointments_payload(data)["appointments"][0]["status"] == "canceled"


def test_unchanged_contact_detail_does_not_deep_copy_database(monkeypatch):
    contact = {"id": "a", "statut": "En cours"}
    monkeypatch.setattr(application, "_CRM_READ_MODEL_KEY", (("signature",), None))
    monkeypatch.setattr(application, "_data_file_signature", lambda: ("signature",))
    monkeypatch.setattr(application, "_load_data_snapshot", lambda: {"crm_contacts": [contact]})
    monkeypatch.setattr(application, "load_data", lambda: pytest.fail("unnecessary full copy"))
    assert application._crm_persist_prepared_contact_if_idle(dict(contact)) is False


def test_delta_includes_changes_deletions_and_safe_fallback():
    cache = UpdateSnapshots(capacity=2)
    scope = ("user-a", "", "a")
    original = {"contacts": [{"id": "a", "statut": "Nouveaux"}, {"id": "b"}],
                "appointments": [{"id": "rdv", "status": "active"}],
                "selected": None, "callback_pending_count": 0}
    first = cache.response(original, "", scope)
    assert first["delta"] is False
    assert cache.response(original, first["cursor"], scope)["unchanged"]
    updated = copy.deepcopy(original)
    updated["contacts"] = [{"id": "a", "statut": "A relancer"}]
    updated["appointments"] = []
    delta = cache.response(updated, first["cursor"], scope)
    assert delta["contacts"] == updated["contacts"]
    assert delta["removed_contacts"] == ["b"]
    assert delta["appointments"] == [] and delta["removed_appointments"] == ["rdv"]
    assert "selected" not in delta
    assert cache.response(updated, first["cursor"], ("other-user", "", "a"))["delta"] is False
    assert len(cache.snapshots) == 2
    assert cache.response(original, first["cursor"], scope)["delta"] is False


def test_updates_endpoint_is_backward_compatible_and_sends_only_changed_rows(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    monkeypatch.setattr(application, "_CRM_UPDATE_SNAPSHOTS", UpdateSnapshots())
    contact = client.post("/api/crm/contacts", json={"prenom": "Test", "nom": "Cache"}).get_json()
    plain = client.get("/api/crm/contacts/updates").get_json()
    assert "cursor" not in plain and "appointments" in plain
    first = client.get("/api/crm/contacts/updates?delta=1").get_json()
    url = "/api/crm/contacts/updates?delta=1&cursor=" + first["cursor"]
    unchanged = client.get(url)
    assert unchanged.get_json()["unchanged"] and len(unchanged.data) < 150
    client.patch(f"/api/crm/contacts/{contact['id']}", json={"statut": "Disqualifié"})
    changed = client.get(url).get_json()
    assert changed["delta"] and changed["contacts"][0]["statut"] == "Disqualifié"
    assert "appointments" not in changed


def test_parallel_contact_reads_do_not_lose_followup_saves(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    created = [client.post("/api/crm/contacts", json={"prenom": f"Test{i}"}).get_json()
               for i in range(4)]
    barrier = threading.Barrier(8)

    def operation(index, write):
        isolated = application.app.test_client()
        with isolated.session_transaction() as session:
            session["user_email"] = "clement@integraleacademy.com"
        barrier.wait(timeout=5)
        url = f"/api/crm/contacts/{created[index]['id']}"
        if write:
            response = isolated.patch(url, json={"relance_date": "2099-01-05", "commentaires": f"saved-{index}"})
        else:
            response = isolated.get(url)
        assert response.status_code == 200

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(operation, index, write)
                   for index in range(4) for write in (True, False)]
        for future in futures:
            future.result(timeout=10)
    stored = application.load_data()["crm_contacts"]
    for index, contact in enumerate(created):
        saved = next(row for row in stored if row["id"] == contact["id"])
        assert saved["commentaires"] == f"saved-{index}"
        assert saved["relance_date"] == "2099-01-05"


def test_transport_failure_after_booking_is_explicit_and_never_retried(monkeypatch):
    monkeypatch.setenv("CALENDLY_ACCESS_TOKEN", "unit-test-token")
    calls = []

    def timeout(*args, **kwargs):
        calls.append(True)
        raise requests.Timeout("sensitive provider details")

    monkeypatch.setattr(application.requests, "request", timeout)
    with pytest.raises(application.CalendlyTransportError) as error:
        application._calendly_request("POST", "/invitees", json_body={"invitee": {"email": "test@example.invalid"}})
    assert len(calls) == 1
    with application.app.test_request_context():
        response, status = application._calendly_route_error(error.value)
    assert status == 504 and response.get_json()["reason"] == "booking_unconfirmed"
    assert "sensitive" not in response.get_json()["error"]


def test_calendly_refusal_logs_diagnostics_without_coordinates(monkeypatch, caplog):
    monkeypatch.setenv("CALENDLY_ACCESS_TOKEN", "unit-test-token")

    class Response:
        status_code = 400

        def json(self):
            return {"title": "Invalid Argument", "message": "private@example.invalid",
                    "details": [{"parameter": "invitee.text_reminder_number", "message": "+33600000000"}]}

    monkeypatch.setattr(application.requests, "request", lambda *a, **kw: Response())
    with pytest.raises(application.CalendlyAPIError):
        application._calendly_request("POST", "/invitees")
    assert "status=400" in caplog.text and "invitee.text_reminder_number" in caplog.text
    assert "private@example" not in caplog.text and "+33600000000" not in caplog.text
    assert "unit-test-token" not in caplog.text


def test_browser_polling_merges_deltas_and_ignores_late_navigation():
    source = (Path(__file__).parents[1] / "static/crm.js").read_text()
    start = source.index("const CRM_REFRESH_INTERVAL_MS=")
    end = source.index("document.addEventListener('visibilitychange'", start)
    script = r'''
const assert=require('node:assert/strict');
const C={section:'contacts'},document={hidden:false,querySelector:()=>null};
const location={search:''},window={CRMAppointmentState:{signature:JSON.stringify}};
let contacts=[{id:'a',statut:'Nouveaux'},{id:'b',statut:'En cours'}];
let crmAppointments=[{id:'x',start_time:'1'},{id:'y',start_time:'2'}];
let callbackRequests=[],callbackRequestPendingCount=3,calls=[],apiHook=null;
const clearTimeout=()=>{},setTimeout=()=>1;
const contactInStore=id=>contacts.find(c=>c.id===id);
const mergeContactInStore=(id,update)=>Object.assign(contactInStore(id),update);
const updateLeadCount=()=>{},updateVisibleAppointmentData=()=>{};
const updateCallbackRequestNavCount=()=>{},renderCalendarAppointments=()=>{};
let answer={delta:true,cursor:'next',contacts:[{id:'a',statut:'A relancer'}],appointments:[{id:'y',start_time:'3'}],removed_appointments:['x']};
const api=async url=>{calls.push(url);if(apiHook)return apiHook(url);return answer};
''' + source[start:end] + r'''
(async()=>{
 await refreshCrmSnapshot();
 assert.equal(contacts.length,2);assert.equal(contacts[0].statut,'A relancer');
 assert.deepEqual(crmAppointments,[{id:'y',start_time:'3'}]);
 assert.equal(calls.length,1);assert(calls[0].includes('delta=1'));
 answer={cursor:'next',unchanged:true};await refreshCrmSnapshot();
 assert(calls[1].includes('cursor=next'));assert.equal(crmAppointments.length,1);
 document.hidden=true;await refreshCrmSnapshot();assert.equal(calls.length,2);
 document.hidden=false;
 answer={delta:true,cursor:'callbacks',callback_requests:[{id:'callback',rdv:'new date'}]};
 await refreshCrmSnapshot();assert.equal(callbackRequestPendingCount,3);
 answer={delta:true,cursor:'count',callback_pending_count:2};
 await refreshCrmSnapshot();assert.equal(callbackRequestPendingCount,2);
 apiHook=async url=>{
  if(url.includes('/updates'))return {delta:true,cursor:'must-not-advance',contacts:[{id:'new'}]};
  throw Error('connection lost during summary reload');
 };
 await refreshCrmSnapshot();assert.equal(crmUpdateCursor,'');
 apiHook=async()=>{C.section='pistes';return {cursor:'old-page',contacts:[]}};
 await refreshCrmSnapshot();assert.equal(crmUpdateCursor,'');assert.equal(contacts.length,2);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_booking_double_click_and_uncertain_response_do_not_submit_again():
    source = (Path(__file__).parents[1] / "static/crm.js").read_text()
    booking = source[source.index("async function calendlyModal(c)"):source.index("async function convertContact(c)")]
    script = r'''
const assert=require('node:assert/strict');
const C={external_meta:false},calendlyBookingsInFlight=new Set();
const CALENDLY_AVAILABILITY_WINDOW_DAYS=7;
let crmAppointments=[],submissions=0,resolveBooking,rejectBooking,lastOptions,closed=0;
const nodes=new Map();
for(const id of ['#calCancel','#calBook','#calEventType','#calSlots','#calRange','#calLocationFields','#calTypeInfo','#calPrev','#calNext','#calQuestions','.modal-body']){
 nodes.set(id,{value:'0',disabled:id==='#calBook',innerHTML:'',style:{}});
}
const slot={dataset:{slot:'2099-01-01T10:00:00Z'},classList:{add(){},remove(){}}};
const controls=[...nodes.values(),slot];
const dialog={querySelector:id=>nodes.get(id),querySelectorAll:()=>controls};
const document={querySelector:id=>id==='.calendly-modal'?dialog:nodes.get(id),querySelectorAll:selector=>selector==='[data-slot]'?[slot]:[]};
const modal=()=>{},closeModal=()=>{closed++},showContact=()=>{},toast=()=>{};
const esc=String,displayName=()=> 'Test',initials=()=> 'T',calendlyQuestionHtml=()=> '';
const c={id:'a',mail:'test@example.invalid',formation:'APS'};
const api=async(url,options)=>{
 if(url.includes('/event-types'))return [{uri:'type',name:'Test',locations:[],custom_questions:[]}];
 if(url.includes('/availability'))return [{start_time:'2099-01-01T10:00:00Z'}];
 submissions++;lastOptions=options;
 return new Promise((resolve,reject)=>{resolveBooking=resolve;rejectBooking=reject});
};
''' + booking + r'''
(async()=>{
 await calendlyModal(c);await new Promise(setImmediate);slot.onclick();
 const book=nodes.get('#calBook');const first=book.onclick();
 assert.equal(submissions,1);assert.equal(lastOptions.timeout,110000);
 slot.onclick();assert.equal(book.disabled,true);
 await book.onclick();await calendlyModal(c);assert.equal(submissions,1);
 resolveBooking({contact:{id:'a'}});await first;
 assert.equal(closed,1);assert(!calendlyBookingsInFlight.has('a'));
 await calendlyModal(c);await new Promise(setImmediate);slot.onclick();
 const denied=book.onclick();
 rejectBooking(Object.assign(Error('permissions missing'),{status:503,reason:'calendly_rejected'}));await denied;
 assert(!calendlyBookingsInFlight.has('a'));assert.equal(book.disabled,false);
 await calendlyModal(c);await new Promise(setImmediate);slot.onclick();
 const second=book.onclick();
 rejectBooking(Object.assign(Error('unknown result'),{status:504,reason:'booking_unconfirmed'}));await second;
 assert(calendlyBookingsInFlight.has('a'));assert.equal(book.disabled,true);
 await book.onclick();await calendlyModal(c);assert.equal(submissions,3);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
