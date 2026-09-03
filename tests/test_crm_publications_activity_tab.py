from pathlib import Path


ROOT = Path(__file__).parents[1]
CRM_JS = ROOT / "static" / "crm.js"
CRM_CSS = ROOT / "static" / "crm.css"
APP_PY = ROOT / "app.py"


def test_publications_are_integrated_into_the_activity_journal_tab():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")
    backend = APP_PY.read_text(encoding="utf-8")

    info_start = javascript.index('id="contactInfoPanel"')
    activity_class = javascript.index(
        'class="wedof-panel contact-activity-panel" id="contactActivityPanel"'
    )
    activity_start = javascript.index('id="contactActivityPanel"', activity_class)
    relance_start = javascript.index('id="contactRelancePanel"', activity_start)
    info_panel = javascript[info_start:activity_class]
    activity_panel = javascript[activity_class:relance_start]

    assert 'id="activityPublicationsPanel"' not in info_panel
    assert activity_panel.count('id="activityPublicationsPanel"') == 1
    assert (
        'class="publications-card activity-publications-panel" '
        'id="activityPublicationsPanel" hidden'
    ) in activity_panel
    assert activity_panel.index("<h2>Journal d’activités</h2>") < activity_panel.index(
        "<h2>Publications</h2>"
    )
    assert activity_panel.index('id="activityFeed"') < activity_panel.index(
        'id="activityPublicationsPanel"'
    )
    assert 'role="tabpanel" aria-labelledby="contactActivityTab" hidden' in activity_panel
    assert javascript.count('id="publicationText"') == 1
    assert javascript.count('id="dictatePublication"') == 1
    assert javascript.count('id="dictatePublicationStatus"') == 1
    assert javascript.count('id="publishBtn"') == 1
    assert javascript.count('id="rephrasePublication"') == 1
    assert javascript.count('id="publicationFeed"') == 1
    assert activity_panel.index('id="publicationFeed"') < activity_panel.index(
        'id="publicationText"'
    )
    assert 'class="feed activity-publication-feed" id="publicationFeed"' in activity_panel

    assert "bindMentions(publicationText)" in javascript
    assert (
        "bindVoiceDictation(publicationText,publicationVoiceButton,publicationVoiceStatus)"
        in javascript
    )
    assert "pausePublicationVoice()" in javascript
    assert "publishBtn.onclick=publish" in javascript
    assert "publicationText.onkeydown=" in javascript
    assert "rephrasePublication.onclick=async" in javascript
    assert "/api/crm/contacts/${c.id}/publications" in javascript
    assert "document.querySelector('#publicationFeed')" in javascript

    assert "function activityTimeline(c)" in javascript
    assert "kind:'publication',title:'Publication ajoutée'" in javascript
    assert (
        "kind:'publication_comment',title:'Commentaire sur une publication'"
        in javascript
    )
    assert "const activities=activityTimeline(c)" in javascript
    assert "mergeContactInStore(c.id,updated);renderActivityFeed()" in javascript
    assert "let activityExpanded=false,activityFilter='all'" in javascript
    assert "showPublications=activityFilter==='publication'" in javascript
    assert "activityFeedRoot.hidden=showPublications" in javascript
    assert "activityPublicationsPanel.hidden=!showPublications" in javascript
    assert "activityPublicationFeed.innerHTML=feed(current,true,'publication')" in javascript

    filters = javascript[javascript.index("const activityFilterDefinitions=["):]
    assert filters.index("{key:'all',label:'Tout'") < filters.index(
        "{key:'appel',label:'Appels consignés'"
    )

    assert ".wedof-panel[hidden]{display:none}" in stylesheet
    assert ".contact-activity-panel{display:grid;align-content:start;gap:18px}" in stylesheet
    assert ".activity-publications-panel[hidden]{display:none!important}" in stylesheet
    assert ".activity-publication-feed{border-bottom:1px solid #e5eaf2" in stylesheet
    assert ".publication-compose-actions .publication-voice-button" in stylesheet
    assert 'CRM_ASSET_VERSION = "20260903-ft-refusal-header-priority-1"' in backend
