from pathlib import Path


CRM_JS = Path(__file__).parents[1] / "static" / "crm.js"
CRM_CSS = Path(__file__).parents[1] / "static" / "crm.css"


def test_calendly_refresh_keeps_cached_appointments_and_formats_paris_sync_time():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")

    assert "manual?{timeout:60000}:{}" in javascript
    assert "if(list&&manual)list.innerHTML" not in javascript
    assert "Europe/Paris" in javascript
    assert "parisDateKey(date)===parisDateKey(new Date())" in javascript
    assert "return`le ${day} à ${time}`" in javascript
    assert "if(Number.isNaN(date.getTime()))return'pas encore synchronisé'" in javascript
    assert ".calendly-lookup-warning .btn" in stylesheet


def test_contact_appointment_modal_uses_scoped_explicit_controls():
    javascript = CRM_JS.read_text(encoding="utf-8")

    assert "const dialog=document.querySelector('.calendly-modal')" in javascript
    assert "const cancelButton=dialog.querySelector('#calCancel')" in javascript
    assert "const bookButton=dialog.querySelector('#calBook')" in javascript
    assert "calCancel.onclick" not in javascript
    assert "dialog.querySelector('.modal-body').innerHTML" in javascript


def test_pistes_and_global_people_open_encoded_contact_links_in_new_tabs():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")

    assert "const contactSheetUrl=id=>`/crm/contacts?fiche=${encodeURIComponent(id)}`" in javascript
    assert "window.open(contactSheetUrl(id),'_blank','noopener')" in javascript
    assert "C.section==='pistes'&&row.closest('#resultTable')" in javascript
    assert "link.target='_blank';link.rel='noopener'" in javascript
    assert "new MutationObserver(prepareGlobalContactLinks)" in javascript
    assert "event.target.closest('a[data-global-id]')" in javascript
    assert "event.target.closest('input,button,select,textarea,label,a')" in javascript
    assert "event.button!==1" in javascript
    assert ".global-results a" in stylesheet
    assert "Cette fiche n’existe plus ou n’est plus accessible." in javascript


def test_relaunch_template_is_available_for_every_formation():
    javascript = CRM_JS.read_text(encoding="utf-8")

    assert "isRelaunch=x=>String(x.nom||'').trim().toLocaleLowerCase('fr-FR')==='relance'" in javascript
    assert 'optgroup label="Tous les parcours"' in javascript


def test_contact_header_displays_the_scheduled_relaunch_date_explicitly():
    javascript = CRM_JS.read_text(encoding="utf-8")

    assert "`Relance prévue le ${" in javascript
    assert "day:'2-digit',month:'2-digit',year:'numeric'" in javascript


def test_contact_header_score_uses_server_contract_and_refreshes_with_card():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")

    assert 'id="contactHeaderScore"' in javascript
    assert 'aria-label="Score d’intégration"' in javascript
    assert "score.score!==null&&score.score!==undefined&&score.score!==''" in javascript
    assert "Score indisponible" in javascript
    for state in ("ready", "action_required", "blocked"):
        assert state in javascript
        assert f".contact-score-status.{state}" in stylesheet
    score_renderer = javascript[
        javascript.index("function renderIntegrationScore(c){"):
        javascript.index("const aiTiming=", javascript.index("function renderIntegrationScore(c){"))
    ]
    assert "renderContactHeaderScore(c);" in score_renderer
    assert "const score=c.integration_score" in score_renderer
    assert ".contact-header-score{display:flex" in stylesheet
    assert "flex-wrap:wrap" in stylesheet
    assert "@media(max-width:650px)" in stylesheet


def test_activity_log_only_displays_contact_communications_and_delegates_more():
    javascript = CRM_JS.read_text(encoding="utf-8")

    assert "visibleActivityKinds=new Set(['appel','email','sms','calendly','erreur'])" in javascript
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
    assert "Deuxième statut enregistré" in javascript
    assert ".timeline-secondary button.current" in stylesheet


def test_opening_secondary_timeline_is_blank_and_does_not_save_a_status():
    javascript = CRM_JS.read_text(encoding="utf-8")

    add_handler = javascript[
        javascript.index("if(addSecondary)addSecondary.onclick=()=>{"):
        javascript.index("actionsBtn.onclick", javascript.index("if(addSecondary)"))
    ]
    assert "secondaryTimelineRow('')" in add_handler
    assert "saveSecondaryStatus" not in add_handler
    assert "saveSecondaryStatus(SECONDARY_STATUSES[0])" not in javascript


def test_primary_timeline_excludes_secondary_only_steps():
    javascript = CRM_JS.read_text(encoding="utf-8")

    assert "PRIMARY_EXCLUDED_STATUSES=new Set(['POEI','Session FT','Def MOB','Financement FT en cours','Financement FT refusé'])" in javascript
    assert "S=C.statuses.filter(status=>!PRIMARY_EXCLUDED_STATUSES.has(status))" in javascript


def test_contact_relance_tracking_is_actionable_and_visually_scoped():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")

    activity_tab = javascript.index('id="contactActivityTab"')
    relance_tab = javascript.index('id="contactRelanceTab"')
    assert activity_tab < relance_tab
    for marker in (
        "Suivi des relances",
        "Les relances prévues",
        "Pas de réponse",
        "A répondu",
        "Pas de réponse relance",
        "function noAnswerRelanceModal",
        "function bindRelanceTracking",
        "/sans-reponse",
        "relance_id:options.relance?.id",
    ):
        assert marker in javascript
    for selector in (
        ".relance-tracking",
        ".relance-hero",
        ".relance-metrics",
        ".relance-item",
        ".relance-actions",
        ".relance-history",
        ".relance-result-modal",
    ):
        assert selector in stylesheet


def test_pipeline_overview_displays_primary_and_secondary_steps():
    javascript = CRM_JS.read_text(encoding="utf-8")

    assert "pipelineOverviewStatuses=()=>[...new Set([...S,...SECONDARY_STATUSES])]" in javascript
    assert "pipelineOverviewStatuses().map(s=>" in javascript


def test_contact_completeness_modal_lists_every_missing_requirement():
    workspace = (ROOT / "static" / "crm_workspace.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "crm_workspace.css").read_text(encoding="utf-8")

    assert "Compléter les éléments" in workspace
    assert "details.missing.join(', ')" in workspace
    assert "completenessGroups" in workspace
    assert "data-completeness-key" in workspace
    assert "Object.fromEntries(new FormData(modalForm))" in workspace
    assert "item.key==='next_action'" in workspace
    assert "completeness-modal-field" in css
    assert "@media(max-width:680px)" in css
    assert " et ${remaining} autre" not in workspace
