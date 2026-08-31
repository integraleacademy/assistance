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
