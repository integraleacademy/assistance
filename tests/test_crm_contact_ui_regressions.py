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
