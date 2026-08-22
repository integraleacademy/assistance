from pathlib import Path


CRM_JS = Path(__file__).parents[1] / "static" / "crm.js"
CRM_TITLE_JS = Path(__file__).parents[1] / "static" / "crm_title.js"
CRM_APPOINTMENT_STATE_JS = Path(__file__).parents[1] / "static" / "crm_appointment_state.js"
CRM_CSS = Path(__file__).parents[1] / "static" / "crm.css"
CRM_TEMPLATE = Path(__file__).parents[1] / "templates" / "crm.html"
APP_PY = Path(__file__).parents[1] / "app.py"


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


def test_wedof_cache_is_preloaded_and_refreshes_without_a_modal():
    javascript = CRM_JS.read_text(encoding="utf-8")

    assert "loadWedof(c,{refresh:true})" in javascript
    assert "const wedofContactIsCurrent=c=>activeWedofContactId===String(c.id)" in javascript
    assert "if(!wedofContactIsCurrent(c))return" in javascript
    assert "modal('Actualisation WEDOF en cours'" not in javascript
    assert "Les données en cache restent affichées." in javascript



def test_wedof_display_translates_known_english_business_values():
    javascript = CRM_JS.read_text(encoding="utf-8")

    assert "refusedByAttendee:'Refusé par le participant'" in javascript
    assert "cancelledByAttendee:'Annulé par le participant'" in javascript
    assert "refusedByTrainingOrganization:'Refusé par l’organisme de formation'" in javascript
    assert "const wedofLabelKey=" in javascript
    assert "wedofLabelsByCode[wedofLabelKey(raw)]" in javascript
    assert "typeof v==='string'?wedofLabel(v)" in javascript
    assert "esc(wedofValue(x.name||x))" in javascript
    assert "esc(wedofValue(v))" in javascript
    assert "JSON.stringify(p,null,2)" in javascript

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


def test_overdue_reminder_metric_filters_rows_and_is_keyboard_accessible():
    workspace = (Path(__file__).parents[1] / "static" / "crm_workspace.js").read_text(encoding="utf-8")
    css = (Path(__file__).parents[1] / "static" / "crm_workspace.css").read_text(encoding="utf-8")

    assert 'id="reminderOverdue"' in workspace
    assert 'role="button" tabindex="0" aria-pressed="false"' in workspace
    assert "overdueOnly?isOverdue(contact)" in workspace
    assert "dateLabel.textContent=overdueOnly?'Relances en retard'" in workspace
    assert "overdueMetric.setAttribute('aria-pressed',String(overdueOnly))" in workspace
    assert "const toggleOverdue=()=>{overdueOnly=!overdueOnly;showAll=false;renderRows()}" in workspace
    assert "event.key==='Enter'||event.key===' '" in workspace
    assert "selectedDate=today;showAll=false;overdueOnly=false" in workspace
    assert "selectedDate=dateInput.value;showAll=false;overdueOnly=false" in workspace
    assert ".workspace-metrics article.metric-action:focus-visible" in css
    assert ".workspace-metrics article.metric-action.active" in css


def test_crm_flag_ui_supports_selection_filter_sort_and_responsive():
    crm_js = CRM_JS.read_text(encoding="utf-8")
    workspace = (Path(__file__).parents[1] / "static" / "crm_workspace.js").read_text(encoding="utf-8")
    css = (Path(__file__).parents[1] / "static" / "crm_workspace.css").read_text(encoding="utf-8")

    assert "data-contact-flag" in workspace
    assert "qualification_flag:value" in workspace
    assert 'id="workspaceFlag"' in workspace
    assert 'id="workspaceSort"' in workspace
    assert "flagRank={green:0,'':1,red:2}" in workspace
    assert "contactFlagBadge(contact)" in workspace
    assert 'class="contact-flag-icon"' in workspace
    assert 'role="img"' in workspace
    assert 'aria-label="${label}"' in workspace
    assert 'title="${label}"' in workspace
    assert "['green','red'].includes(flag)" in workspace
    assert "contactTitle.querySelector('small')" in workspace
    assert "contactTitle.querySelector('.contact-flag-badge')" in workspace
    assert "function listQualificationFlag(contact)" in crm_js
    assert "${listQualificationFlag(c)}" in crm_js
    assert 'class="contact-flag-badge is-list ${flag}"' in crm_js
    assert "contactFlagBadge(contact,'header')" in workspace
    assert 'class="contact-flag-label"' in workspace
    assert ".contact-flag-badge.is-list" in css
    assert ".contact-flag-badge.is-header" in css
    assert ".contact-flag-badge.is-header .contact-flag-icon" in css
    assert ".contact-flag-badge.green" in css
    assert ".contact-flag-badge.red" in css
    assert ".contact-flag-icon" in css
    assert "@media(max-width:680px)" in css

def test_contact_header_displays_all_activity_counters_including_zero():
    crm_js = CRM_JS.read_text(encoding="utf-8")
    crm_css = CRM_CSS.read_text(encoding="utf-8")

    assert "function contactHeaderActivitySummary(contact)" in crm_js
    assert "const counts=listActivityCounts(contact)" in crm_js
    assert "['RDV total',counts.appointments]" in crm_js
    assert "['RDV réalisés',completedAppointments]" in crm_js
    assert "['E-mails',counts.emails]" in crm_js
    assert "['SMS',counts.sms]" in crm_js
    assert "['Relances',counts.relances]" in crm_js
    assert "${contactHeaderActivitySummary(c)}" in crm_js
    assert "filter(a=>a.kind==='calendly').length} RDV réalisés" not in crm_js
    assert ".contact-activity-summary" in crm_css
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in crm_css


def test_contact_completeness_modal_lists_every_missing_requirement():
    workspace = (Path(__file__).parents[1] / "static" / "crm_workspace.js").read_text(encoding="utf-8")
    css = (Path(__file__).parents[1] / "static" / "crm_workspace.css").read_text(encoding="utf-8")

    assert "Compléter les éléments" in workspace
    assert "details.missing.join(', ')" in workspace
    assert "completenessGroups" in workspace
    assert "data-completeness-key" in workspace
    assert "Object.fromEntries(new FormData(modalForm))" in workspace
    assert "item.key==='next_action'" in workspace
    assert "completeness-modal-field" in css
    assert "@media(max-width:680px)" in css
    assert " et ${remaining} autre" not in workspace


def test_calendly_booking_error_keeps_modal_retry_available():
    crm_js = CRM_JS.read_text(encoding="utf-8")

    assert "catch(e){toast(e.message,true);bookButton.disabled=false" in crm_js
    assert "bookButton.textContent='Confirmer le rendez-vous'" in crm_js


def test_programmed_appointment_date_is_rendered_under_pipeline_status():
    crm_js = CRM_JS.read_text(encoding="utf-8")
    appointment_state = CRM_APPOINTMENT_STATE_JS.read_text(encoding="utf-8")
    crm_css = CRM_CSS.read_text(encoding="utf-8")

    assert "nextProgrammedAppointmentDate" in crm_js
    assert "contactHasPipelineStatus(c,'RDV programmé')" in crm_js
    assert "window.CRMAppointmentState.dateLabel(c.id,crmAppointments)" in crm_js
    assert "['canceled','cancelled']" in appointment_state
    assert "ordered.find(row=>row.start>=now)" in appointment_state
    assert "'Date du RDV non renseignée'" in appointment_state
    assert "timeZone:'Europe/Paris'" in appointment_state
    assert "day:'2-digit',month:'2-digit',year:'numeric'" in appointment_state
    assert "contactPipelineStatusMarkup(c)" in crm_js
    assert ".pipeline-appointment-date" in crm_css


def test_dashboard_and_pipeline_explain_their_distinct_scopes():
    javascript = CRM_JS.read_text(encoding="utf-8")

    assert "const crmActiveContacts=()=>contacts.filter(contact=>!contact.archived_at)" in javascript
    assert "const dashboardContactsIn=range=>crmActiveContacts().filter" in javascript
    assert "Pistes créées sur la période" in javascript
    assert "Évolution des pistes créées" in javascript
    assert "const activeContacts=crmActiveContacts();let list=[...activeContacts]" in javascript
    assert "activeContacts.filter(c=>contactHasPipelineStatus(c,s)).length" in javascript
    assert "function bindList(type){let base=crmActiveContacts().filter" in javascript
    assert "dashboardKpi('Nouvelles pistes'" not in javascript


def test_bulk_messages_preview_and_offer_every_compatible_template():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")

    assert "modelPool=[...(isMail?templates.automatic_email||[]:[])" in javascript
    assert "new Map(modelPool.map(template=>[String(template.id),template]))" in javascript
    assert 'id="previewBulkMessage">Prévisualiser' in javascript
    assert 'id="bulkMessagePreview" hidden' in javascript
    assert "eligible[0].id}/message-preview" in javascript
    assert "previewFrame.srcdoc=smsPreviewHtml(messageField.value)" in javascript
    assert "previewRoot.hidden=false" in javascript
    assert "previewButton.disabled=true;try{" in javascript
    assert ".bulk-message-preview" in stylesheet


def test_calendly_refreshes_silently_after_cached_contact_render():
    javascript = CRM_JS.read_text(encoding="utf-8")

    assert "activeCalendlyContactId=''" in javascript
    assert "async function refreshCalendlyOnOpen(c,cached)" in javascript
    assert "calendly/appointments?refresh=1" in javascript
    assert "refresh_in_progress:true" in javascript
    assert "if(!manual)refreshCalendlyOnOpen(c,result)" in javascript
    assert "activeCalendlyContactId!==String(c.id)" in javascript
    assert "Calendly : actualisation en cours…" in javascript
    assert "cached.appointments||[]" in javascript
    assert "lookup_warning:error.message" in javascript
    assert "if(button&&manual){button.disabled=false" in javascript


def test_global_search_only_indexes_visible_contact_fields_and_ranks_names_first():
    javascript = CRM_JS.read_text(encoding="utf-8")

    assert "const normalizeGlobalSearch=value=>" in javascript
    assert "normalize('NFD')" in javascript
    assert "const contactSearchFields=c=>[c.prenom,c.nom,c.mail,c.telephone,c.formation,c.lieu,c.statut]" in javascript
    assert "Object.values(c)" not in javascript
    assert "function contactSearchRank(c,q)" in javascript
    assert "crmActiveContacts().filter(c=>searchable(c).includes(q))" in javascript
    assert "contactSearchRank(a,q)-contactSearchRank(b,q)" in javascript
    assert "filter(contact=>contact&&!contact.archived_at)" in javascript
    assert "normalizeGlobalSearch(label).includes(q)" in javascript



def test_contact_document_title_is_wired_to_real_contact_navigation():
    javascript = CRM_JS.read_text(encoding="utf-8")
    title_javascript = CRM_TITLE_JS.read_text(encoding="utf-8")
    template = CRM_TEMPLATE.read_text(encoding="utf-8")
    backend = APP_PY.read_text(encoding="utf-8")

    assert "const formatFirstName=window.CRMDocumentTitle.formatFirstName" in javascript
    assert "const displayName=window.CRMDocumentTitle.displayName" in javascript
    assert "window.CRMDocumentTitle.applyContact(c);" in javascript
    assert "if(!c){window.CRMDocumentTitle.reset();" in javascript
    render_body = javascript[
        javascript.index("function render(){"):
        javascript.index("async function init(){")
    ]
    assert "window.CRMDocumentTitle.reset();" in render_body
    assert "titleForContact" in title_javascript
    assert template.index("filename='crm_title.js'") < template.index("filename='crm.js'")
    assert "filename='crm_title.js',v=asset_version" in template
    assert "filename='crm.js',v=asset_version" in template
    assert 'CRM_ASSET_VERSION = "20260822-calendly-list-sync-1"' in backend

def test_programmed_appointment_date_refresh_is_wired_across_tabs():
    javascript = CRM_JS.read_text(encoding="utf-8")
    appointment_state = CRM_APPOINTMENT_STATE_JS.read_text(encoding="utf-8")
    template = CRM_TEMPLATE.read_text(encoding="utf-8")
    backend = APP_PY.read_text(encoding="utf-8")

    assert "window.CRMAppointmentState.dateLabel(c.id,crmAppointments)" in javascript
    assert "replaceContactAppointments(c.id,appointments);" in javascript
    assert "updateVisibleAppointmentData();" in javascript
    assert 'data-crm-cell="activities"' in javascript
    assert 'data-crm-cell="status"' in javascript
    assert "Array.isArray(snapshot.appointments)" in javascript
    assert "CRMAppointmentState.signature(snapshot.appointments)" in javascript
    assert '"appointments": appointments' in backend[
        backend.index("def crm_contact_updates():"):
        backend.index('@app.delete("/api/crm/database")')
    ]
    assert template.index("filename='crm_appointment_state.js'") < template.index(
        "filename='crm.js'"
    )
    assert "filename='crm_appointment_state.js',v=asset_version" in template
    assert "replaceContact" in appointment_state
    assert "nextAppointment" in appointment_state
    assert 'CRM_ASSET_VERSION = "20260822-calendly-list-sync-1"' in backend
