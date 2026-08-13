from pathlib import Path


CRM_JS = Path(__file__).parents[1] / "static" / "crm.js"
CRM_CSS = Path(__file__).parents[1] / "static" / "crm.css"


def test_contact_appointment_modal_uses_scoped_explicit_controls():
    javascript = CRM_JS.read_text(encoding="utf-8")

    assert "const dialog=document.querySelector('.calendly-modal')" in javascript
    assert "const cancelButton=dialog.querySelector('#calCancel')" in javascript
    assert "const bookButton=dialog.querySelector('#calBook')" in javascript
    assert "calCancel.onclick" not in javascript
    assert "dialog.querySelector('.modal-body').innerHTML" in javascript


def test_relaunch_template_is_available_for_every_formation():
    javascript = CRM_JS.read_text(encoding="utf-8")

    assert "isRelaunch=x=>String(x.nom||'').trim().toLocaleLowerCase('fr-FR')==='relance'" in javascript
    assert 'optgroup label="Tous les parcours"' in javascript


def test_contact_header_displays_the_scheduled_relaunch_date_explicitly():
    javascript = CRM_JS.read_text(encoding="utf-8")

    assert "`Relance prévue le ${" in javascript
    assert "day:'2-digit',month:'2-digit',year:'numeric'" in javascript


def test_activity_log_only_displays_contact_communications_and_delegates_more():
    javascript = CRM_JS.read_text(encoding="utf-8")

    assert "visibleActivityKinds=new Set(['appel','email','sms','calendly'])" in javascript
    assert ".filter(a=>visibleActivityKinds.has(a.kind))" in javascript
    assert "activityFeed.onclick=event=>" in javascript
    assert "event.target.closest('#feedMore')" in javascript


def test_tracking_card_uses_the_standard_form_section_heading():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")

    assert '<h3 class="tracking-card-head">' in javascript
    assert '.form-section>h3.tracking-card-head' in stylesheet


def test_contact_supports_a_removable_secondary_timeline():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")

    for status in (
        "Financement FT en cours",
        "Financement FT refusé",
        "Def MOB",
        "POEI",
        "C2P en cours",
        "Marché FT",
    ):
        assert status in javascript
    assert "'Session FT','C2P en cours'" not in javascript
    assert 'id="addSecondaryTimeline"' in javascript
    assert 'id="removeSecondaryTimeline"' in javascript
    assert "statut_secondaire:next" in javascript
    assert ".timeline-secondary button.current" in stylesheet


def test_primary_timeline_excludes_secondary_only_steps():
    javascript = CRM_JS.read_text(encoding="utf-8")

    assert "PRIMARY_EXCLUDED_STATUSES=new Set(['POEI','Session FT','Def MOB','Financement FT en cours','Financement FT refusé'])" in javascript
    assert "S=C.statuses.filter(status=>!PRIMARY_EXCLUDED_STATUSES.has(status))" in javascript


def test_pipeline_overview_displays_primary_and_secondary_steps():
    javascript = CRM_JS.read_text(encoding="utf-8")

    assert "pipelineOverviewStatuses=()=>[...new Set([...S,...SECONDARY_STATUSES])]" in javascript
    assert "pipelineOverviewStatuses().map(s=>" in javascript
