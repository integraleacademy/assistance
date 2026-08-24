import json
import subprocess
from pathlib import Path


CRM_APPOINTMENT_STATE_JS = (
    Path(__file__).parents[1] / "static" / "crm_appointment_state.js"
)
CRM_JS = Path(__file__).parents[1] / "static" / "crm.js"


def run_appointment_state_scenario():
    script = r"""
const assert=require('node:assert/strict');
global.window=global;
require(process.argv[1]);
const state=global.CRMAppointmentState;
const now=Date.parse('2026-08-22T12:00:00Z');
const appointments=[
 {id:'past',contact_id:42,start_time:'2026-08-20T08:00:00Z',status:'active'},
 {id:'future-late',contact_id:'42',start_time:'2026-08-24T09:00:00Z',status:'active'},
 {id:'future-next',contact_id:'42',start_time:'2026-08-23T09:00:00Z',status:'active'},
 {id:'cancelled',contact_id:'42',start_time:'2026-08-22T13:00:00Z',status:'cancelled'},
 {id:'other',contact_id:'99',start_time:'2026-08-22T14:00:00Z',status:'active'},
];

assert.equal(state.nextAppointment('42',appointments,now).id,'future-next');
assert.equal(state.dateLabel(42,appointments,now),'Prochain RDV le 23/08/2026');
assert.equal(state.nextAppointment('42',[appointments[0]],now),null);
assert.equal(
 state.dateLabel('42',[appointments[3]],now),
 'Date du RDV non renseignée'
);
assert.equal(state.contactAppointments('42',appointments).length,3);

const contacts=[
 {id:'late'},
 {id:'missing'},
 {id:'early'},
 {id:'cancelled'},
 {id:'middle'},
];
const orderedAppointments=[
 {id:'late-rdv',contact_id:'late',start_time:'2026-08-24T09:00:00Z',status:'active'},
 {id:'middle-rdv',contact_id:'middle',start_time:'2026-08-23T10:00:00Z',status:'active'},
 {id:'early-rdv',contact_id:'early',start_time:'2026-08-23T08:00:00Z',status:'active'},
 {id:'cancelled-rdv',contact_id:'cancelled',start_time:'2026-08-22T13:00:00Z',status:'cancelled'},
];
const sortedContacts=state.sortContactsByNextAppointment(
 contacts,
 orderedAppointments,
 now
);
assert.deepEqual(
 sortedContacts.map(contact=>contact.id),
 ['early','middle','late','missing','cancelled']
);
assert.deepEqual(
 contacts.map(contact=>contact.id),
 ['late','missing','early','cancelled','middle'],
 'sorting does not mutate the source list'
);

const replaced=state.replaceContact(appointments,'42',[
 {id:'replacement',start_time:'2026-08-25T10:00:00Z',status:'active'},
 {id:'replacement',start_time:'2026-08-25T11:00:00Z',status:'active'},
]);
assert.deepEqual(
 replaced.map(item=>item.id).sort(),
 ['other','replacement']
);
assert.equal(replaced.find(item=>item.id==='replacement').contact_id,'42');
assert.equal(replaced.find(item=>item.id==='replacement').start_time,'2026-08-25T11:00:00Z');

const before=state.signature(appointments);
const sameDifferentOrder=state.signature([...appointments].reverse());
assert.equal(before,sameDifferentOrder);
assert.notEqual(
 before,
 state.signature(appointments.map(item=>item.id==='future-next'?{...item,status:'canceled'}:item))
);

process.stdout.write(JSON.stringify({
 ok:true,
 selected:state.nextAppointment('42',appointments,now).id,
 date:state.dateLabel('42',appointments,now),
}));
"""
    return subprocess.run(
        ["node", "-e", script, str(CRM_APPOINTMENT_STATE_JS)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_crm_appointment_state_behavior_executes_in_javascript():
    result = run_appointment_state_scenario()

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "ok": True,
        "selected": "future-next",
        "date": "Prochain RDV le 23/08/2026",
    }


def test_programmed_appointment_filter_uses_chronological_sort():
    crm_js = CRM_JS.read_text(encoding="utf-8")

    assert "function sortPipelineLeads" in crm_js
    assert "status==='RDV programmé'" in crm_js
    assert "CRMAppointmentState.sortContactsByNextAppointment" in crm_js
    assert crm_js.count("sortPipelineLeads(") == 3
