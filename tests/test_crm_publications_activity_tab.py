from pathlib import Path


ROOT = Path(__file__).parents[1]
CRM_JS = ROOT / "static" / "crm.js"
CRM_CSS = ROOT / "static" / "crm.css"
APP_PY = ROOT / "app.py"


def test_publications_are_grouped_below_the_activity_journal():
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

    assert 'class="card publications-card"' not in info_panel
    assert activity_panel.count('class="card publications-card"') == 1
    assert activity_panel.index("<h2>Journal d’activités</h2>") < activity_panel.index(
        "<h2>Publications</h2>"
    )
    assert 'role="tabpanel" aria-labelledby="contactActivityTab" hidden' in activity_panel
    assert javascript.count('id="publicationText"') == 1
    assert javascript.count('id="dictatePublication"') == 1
    assert javascript.count('id="dictatePublicationStatus"') == 1
    assert javascript.count('id="publishBtn"') == 1
    assert javascript.count('id="rephrasePublication"') == 1
    assert javascript.count('id="publicationFeed"') == 1

    assert "bindMentions(publicationText)" in javascript
    assert (
        "bindVoiceDictation(publicationText,publicationVoiceButton,publicationVoiceStatus)"
        in javascript
    )
    assert "pausePublicationVoice()" in javascript
    assert "publishBtn.onclick=publish" in javascript
    assert "publicationText.onkeydown=" in javascript
    assert "rephrasePublication.onclick=async" in javascript
    assert "bindPublicationFeed(c,publicationFeed,renderActivityFeed)" in javascript
    assert "/api/crm/contacts/${c.id}/publications" in javascript
    assert "document.querySelector('#publicationFeed')" in javascript

    assert "function activityTimeline(c)" in javascript
    assert "kind:'publication',title:'Publication ajoutée'" in javascript
    assert (
        "kind:'publication_comment',title:'Commentaire sur une publication'"
        in javascript
    )
    assert "const activities=activityTimeline(c)" in javascript
    assert "bindPublicationFeed(c,publicationFeed,renderActivityFeed)" in javascript
    assert "mergeContactInStore(c.id,updated);renderActivityFeed()" in javascript

    assert ".wedof-panel[hidden]{display:none}" in stylesheet
    assert ".contact-activity-panel{display:grid;align-content:start;gap:18px}" in stylesheet
    assert ".contact-activity-panel .publications-card{margin-bottom:0}" in stylesheet
    assert ".publication-compose-actions .publication-voice-button" in stylesheet
    assert 'CRM_ASSET_VERSION = "20260831-activity-journal-1"' in backend
