import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
CRM_JS = ROOT / "static" / "crm.js"
CRM_CSS = ROOT / "static" / "crm.css"


def test_activity_journal_has_the_requested_filters_and_chronological_groups():
    javascript = CRM_JS.read_text(encoding="utf-8")

    expected_filters = (
        "{key:'sms',label:'SMS',icon:'message'}",
        "{key:'email',label:'Mails',icon:'mail'}",
        "{key:'rdv',label:'RDV',icon:'calendar'}",
        "{key:'appel',label:'Appels consignés',icon:'phone'}",
        "{key:'publication',label:'Publications',icon:'users'}",
        "{key:'other',label:'Autre',icon:'activity'}",
    )
    for activity_filter in expected_filters:
        assert activity_filter in javascript

    filter_definitions = javascript[
        javascript.index("const activityFilterDefinitions=["):
        javascript.index("function activityCategory")
    ]
    assert filter_definitions.index("{key:'all',label:'Tout',icon:'activity'}") < (
        filter_definitions.index("{key:'appel',label:'Appels consignés',icon:'phone'}")
    )

    assert "if(kind==='sms')return'sms'" in javascript
    assert "if(kind==='email')return'email'" in javascript
    assert "['calendly','rdv','appointment'].includes(kind)" in javascript
    assert "if(kind==='appel')return'appel'" in javascript
    assert "['publication','publication_comment'].includes(kind)" in javascript
    assert "return'other'" in javascript

    assert "function activityDateGroup(value)" in javascript
    assert "label:'Aujourd’hui'" in javascript
    assert "label:'Hier'" in javascript
    assert "data-activity-category" in javascript
    assert "activity-day-group" in javascript
    assert "activity-day-heading" in javascript

    assert "activityFiltersRoot.id='activityFilters'" in javascript
    assert "activityCard.insertBefore(activityFiltersRoot,activityFeedRoot)" in javascript
    assert "activityFilter=button.dataset.activityFilter" in javascript
    assert "activityExpanded=false;renderActivityFeed()" in javascript


def test_activity_journal_uses_larger_saas_typography_and_cards():
    stylesheet = CRM_CSS.read_text(encoding="utf-8")

    assert "/* Journal d'activités : vue SaaS lisible, filtrée et chronologique. */" in stylesheet
    assert ".contact-activity-panel .activity-card-head h2" in stylesheet
    assert "font:800 22px Manrope" in stylesheet
    assert ".activity-filter" in stylesheet
    assert "font:750 13px 'DM Sans'" in stylesheet
    assert ".contact-activity-panel .feed-item" in stylesheet
    assert "border-radius:14px" in stylesheet
    assert ".contact-activity-panel .feed-item b" in stylesheet
    assert "font-size:14px" in stylesheet
    assert ".contact-activity-panel .feed-item p" in stylesheet
    assert "font-size:13px" in stylesheet
    assert ".contact-activity-panel .contact-journey-summary span" in stylesheet
    assert ".activity-empty-state" in stylesheet


def test_activity_journal_displays_the_current_appointment_result():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")
    template = (ROOT / "templates" / "crm.html").read_text(encoding="utf-8")

    helpers = javascript[
        javascript.index("function calendlyActivityDetail"):
        javascript.index("function activityTimeline")
    ]
    script = "let crmAppointments=[];\n" + helpers + r"""
const assert=require('node:assert/strict');
const contact={id:'lead-1'};
const answered={id:'rdv-answered',contact_id:'lead-1',name:'RDV téléphonique',start_time:'2026-08-31T12:00:00Z',status:'active',response_status:'answered'};
const noAnswer={id:'rdv-no-answer',contact_id:'lead-1',name:'RDV téléphonique',start_time:'2026-08-28T11:45:00Z',status:'active',response_status:'no_answer'};
const pending={id:'rdv-pending',contact_id:'lead-1',name:'RDV téléphonique',start_time:'2026-08-27T08:00:00Z',status:'active'};
const upcoming={id:'rdv-upcoming',contact_id:'lead-1',name:'RDV téléphonique',start_time:'2026-09-02T08:00:00Z',status:'active'};
const canceled={id:'rdv-canceled',contact_id:'lead-1',name:'RDV téléphonique',start_time:'2026-08-26T08:00:00Z',status:'canceled'};
crmAppointments=[answered,noAnswer,pending,upcoming,canceled];
const activity=appointment=>({kind:'calendly',title:'Rendez-vous Calendly planifié',detail:calendlyActivityDetail(appointment)});

assert.equal(calendlyActivityDetail(answered),'RDV téléphonique — 31/08/2026 à 14:00');
assert.deepEqual(appointmentActivityResult(contact,activity(answered)),{tone:'answered',label:'A répondu'});
assert.deepEqual(appointmentActivityResult(contact,activity(noAnswer)),{tone:'no-answer',label:'Sans réponse'});
assert.deepEqual(appointmentActivityResult(contact,activity(pending),Date.parse('2026-08-31T12:00:00Z')),{tone:'pending',label:'Résultat à renseigner'});
assert.deepEqual(appointmentActivityResult(contact,activity(upcoming),Date.parse('2026-08-31T12:00:00Z')),{tone:'upcoming',label:'À venir'});
assert.deepEqual(appointmentActivityResult(contact,activity(canceled)),{tone:'canceled',label:'Annulé'});
assert.equal(appointmentActivityResult(contact,{kind:'calendly',title:'Rendez-vous Calendly planifié',detail:'Rendez-vous inconnu'}),null);
"""

    completed = subprocess.run(
        ["node", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "feed-appointment-result ${appointmentResult.tone}" in javascript
    assert "document.querySelector('#activityFilters')?.dispatchEvent(new Event('crm:refresh'))" in javascript
    assert ".feed-appointment-result.answered" in stylesheet
    assert ".feed-appointment-result.no-answer" in stylesheet
    assert ".feed-appointment-result.pending" in stylesheet
    assert template.count("activity_rdv_result_version='20260831-activity-rdv-results-1'") == 2
