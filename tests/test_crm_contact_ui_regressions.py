from pathlib import Path
import subprocess


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

    assert "rejectedWithoutTitulaireSuite:'Refusé sans suite par le titulaire'" in javascript
    assert "refusedByAttendee:'Refusé par le participant'" in javascript
    assert "cancelledByAttendee:'Annulé par le participant'" in javascript
    assert "refusedByTrainingOrganization:'Refusé par l’organisme de formation'" in javascript
    assert "const wedofLabelKey=" in javascript
    assert "wedofLabelsByCode[wedofLabelKey(raw)]" in javascript
    assert "typeof v==='string'?wedofLabel(v)" in javascript
    assert "esc(wedofValue(x.name||x))" in javascript
    assert "esc(wedofValue(v))" in javascript
    assert "JSON.stringify(p,null,2)" in javascript


def test_wedof_business_labels_and_statuses_execute_in_javascript():
    completed = subprocess.run(
        ["node", "tests/wedof_status_smoke.js"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "WEDOF France Travail status mapping: OK" in completed.stdout


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

    assert "const contactSheetUrl=(id,section=C.section)=>" in javascript
    assert "window.open(contactSheetUrl(id),'_blank','noopener')" in javascript
    assert "C.section==='pistes'&&row.closest('#resultTable')" in javascript
    assert "link.target='_blank';link.rel='noopener'" in javascript
    assert "new MutationObserver(prepareGlobalContactLinks)" in javascript
    assert "event.target.closest('a[data-global-id]')" in javascript
    assert "event.target.closest('input,button,select,textarea,label,a')" in javascript
    assert "event.button!==1" in javascript
    assert ".global-results a" in stylesheet
    assert "Cette fiche n’existe plus ou n’est plus accessible." in javascript


def test_contact_back_navigation_preserves_the_safe_source_section():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")
    helpers = javascript[
        javascript.index("const contactReturnSection="):
        javascript.index("function openContactInNewTab")
    ]
    script = f"""
const C={{section:'contacts'}};
const location={{search:''}};
{helpers}
const assert=(condition,message)=>{{if(!condition)throw new Error(message)}};
assert(contactSheetUrl('a/b ?', 'pistes')==='/crm/contacts?fiche=a%2Fb%20%3F&retour=pistes','encoded pistes URL');
assert(contactSheetUrl('a/b ?', 'contacts')==='/crm/contacts?fiche=a%2Fb%20%3F','encoded contacts URL');
assert(contactReturnSection('?fiche=42&retour=pistes','contacts')==='pistes','pistes return parameter');
assert(contactReturnSection('', 'pistes')==='pistes','pistes section fallback');
assert(contactReturnSection('?retour=https://example.com','contacts')==='contacts','reject arbitrary return');
assert(contactListUrl('pistes')==='/crm/pistes','pistes list URL');
assert(contactListUrl('contacts')==='/crm/contacts','contacts list URL');
assert(contactBackLabel('pistes')==='Toutes les pistes','pistes label');
assert(contactBackLabel('contacts')==='Tous les contacts','contacts label');
assert(contactBackLink('pistes')==='<a class="btn back-contact" id="backList" href="/crm/pistes">← Toutes les pistes</a>','native pistes link');
assert(contactBackLink('contacts')==='<a class="btn back-contact" id="backList" href="/crm/contacts">← Tous les contacts</a>','native contacts link');
console.log('CRM contact return navigation: OK');
"""

    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM contact return navigation: OK" in completed.stdout
    assert "const returnSection=contactReturnSection()" in javascript
    assert "contactSheetUrl(id,returnSection)" in javascript
    assert "contactBackLink(returnSection)" in javascript
    assert "backList.onclick" not in javascript
    assert ".back-contact{display:inline-flex;align-items:center;" in stylesheet
    assert "text-decoration:none" in stylesheet


def test_toast_is_left_visible_and_guards_status_saves():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")
    template = CRM_TEMPLATE.read_text(encoding="utf-8")
    backend = APP_PY.read_text(encoding="utf-8")
    helpers = javascript[
        javascript.index("let toastTimer"):
        javascript.index("const initials=")
    ]
    script = f"""
let scheduled=[];
let cleared=[];
const toastNode={{
  textContent:'',
  className:'toast',
  attributes:{{}},
  setAttribute(name,value){{this.attributes[name]=value}},
  classList:{{remove(name){{toastNode.className=toastNode.className.split(' ').filter(value=>value!==name).join(' ')}}}}
}};
const document={{querySelector:selector=>selector==='#toast'?toastNode:null}};
const window={{listeners:{{}},addEventListener(type,handler){{this.listeners[type]=handler}}}};
const setTimeout=(callback,delay)=>{{scheduled.push({{callback,delay}});return scheduled.length}};
const clearTimeout=id=>{{cleared.push(id)}};
{helpers}
const assert=(condition,message)=>{{if(!condition)throw new Error(message)}};
toast('Statut enregistré');
assert(toastNode.textContent==='Statut enregistré','success text');
assert(toastNode.className.includes('is-success'),'success state');
assert(toastNode.attributes.role==='status','success role');
assert(scheduled.at(-1).delay===4500,'success remains readable');
beginStatusSave('Enregistrement du statut…');
assert(pendingStatusSaves===1,'pending save count');
assert(toastNode.className.includes('is-pending'),'pending state');
const pendingTimerCount=scheduled.length;
const event={{prevented:false,returnValue:null,preventDefault(){{this.prevented=true}}}};
window.listeners.beforeunload(event);
assert(event.prevented&&event.returnValue==='','guard navigation while saving');
assert(scheduled.length===pendingTimerCount,'pending toast remains visible');
finishStatusSave('Statut enregistré');
assert(pendingStatusSaves===0,'save guard released');
assert(toastNode.className.includes('is-success'),'success replaces pending');
const safeEvent={{prevented:false,preventDefault(){{this.prevented=true}}}};
window.listeners.beforeunload(safeEvent);
assert(!safeEvent.prevented,'navigation allowed after save');
toast('Échec de sauvegarde',true);
assert(toastNode.className.includes('is-error'),'error state');
assert(toastNode.attributes.role==='alert','error role');
assert(toastNode.attributes['aria-live']==='assertive','assertive error announcement');
assert(scheduled.at(-1).delay===7000,'error remains readable');
console.log('CRM save notifications: OK');
"""

    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM save notifications: OK" in completed.stdout
    assert ".toast{position:fixed;left:272px" in stylesheet
    assert "body.sidebar-collapsed .toast{left:100px" in stylesheet
    assert "@media(max-width:1000px){.toast,body.sidebar-collapsed .toast{left:18px" in stylesheet
    assert ".toast.is-pending" in stylesheet
    assert "right:25px" not in javascript
    assert '<div id="toast" class="toast" role="status" aria-live="polite" aria-atomic="true">' in template
    assert "beginStatusSave('Enregistrement du statut…')" in javascript
    assert "const statusSaveControls=()=>document.querySelectorAll('[data-primary-step],[data-secondary-step],#addSecondaryTimeline,#removeSecondaryTimeline')" in javascript
    assert "statusSaveControls().forEach(step=>step.disabled=true)" in javascript
    assert "finishStatusSave('Statut enregistré')" in javascript
    assert "beginStatusSave(next?'Enregistrement du deuxième statut…':'Suppression de la deuxième timeline…')" in javascript
    assert "finishStatusSave(next?'Deuxième statut enregistré':'Deuxième timeline retirée')" in javascript
    assert 'CRM_ASSET_VERSION = "20260823-reminder-period-filters-1"' in backend

def test_collapsed_sidebar_is_compact_accessible_and_persistent():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")
    template = CRM_TEMPLATE.read_text(encoding="utf-8")
    sidebar_state = (
        Path(__file__).parents[1] / "static" / "crm_sidebar_state.js"
    ).read_text(encoding="utf-8")
    script = r"""
require('./static/crm_sidebar_state.js');
const state=globalThis.CRMSidebarState;
const listeners={};
const classes=new Set();
const attributes={};
const button={
 textContent:'‹',
 title:'Replier la barre latérale',
 setAttribute:(name,value)=>{attributes[name]=value},
 addEventListener:(name,handler)=>{listeners[name]=handler},
};
const document={
 body:{classList:{
  toggle:(name,enabled)=>enabled?classes.add(name):classes.delete(name),
  contains:name=>classes.has(name),
 }},
 querySelector:selector=>selector==='#sidebarCollapse'?button:null,
};
const values=new Map([[state.STORAGE_KEY,'1']]);
const storage={
 getItem:key=>values.get(key)||null,
 setItem:(key,value)=>values.set(key,value),
};
const assert=(condition,message)=>{if(!condition)throw new Error(message)};
const initialized=state.initialize(document,storage);
assert(initialized.collapsed===true,'stored collapsed preference restored');
assert(classes.has('sidebar-collapsed'),'collapsed class applied');
assert(button.textContent==='›','expand direction shown while collapsed');
assert(button.title==='Déplier la barre latérale','title describes the next action');
assert(attributes['aria-label']==='Déplier la barre latérale','accessible label updated');
assert(attributes['aria-expanded']==='false','collapsed state exposed');
listeners.click();
assert(!classes.has('sidebar-collapsed'),'click expands the sidebar');
assert(values.get(state.STORAGE_KEY)==='0','expanded preference persisted');
assert(button.textContent==='‹','collapse direction restored');
assert(attributes['aria-expanded']==='true','expanded state exposed');
const blockedStorage={getItem(){throw new Error('blocked')},setItem(){throw new Error('blocked')}};
assert(state.initialize(document,blockedStorage)!==null,'blocked storage never prevents initialization');
console.log('CRM sidebar state: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=Path(__file__).parents[1],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM sidebar state: OK" in completed.stdout
    assert "window.CRMSidebarState?.initialize(document)" in javascript
    assert "classList.toggle('sidebar-collapsed')" not in javascript
    assert 'id="crmSidebar"' in template
    assert template.count("filename='favicon_32x32.png',v=asset_version") == 2
    assert 'class="brand-full" src="{{ url_for(\'static\',filename=\'iaconnectcrm.png\',v=asset_version) }}"' in template
    assert 'class="brand-compact" src="{{ url_for(\'static\',filename=\'favicon_32x32.png\',v=asset_version) }}" alt="" aria-hidden="true">' in template
    assert 'class="brand-compact" aria-hidden="true">IA</span>' not in template
    assert 'aria-controls="crmSidebar" aria-expanded="true"' in template
    for label in (
        "Accueil", "Calendrier", "Notifications", "Fil actu", "Pistes",
        "Relances", "Inscrits", "Disqualifiés", "Contacts", "Modèles",
    ):
        assert f'data-label="{label}"' in template
    assert "filename='crm_sidebar_state.js',v=asset_version" in template
    assert "body.sidebar-collapsed .brand img{display:none}" not in stylesheet
    assert "body.sidebar-collapsed .brand-full{display:none}" in stylesheet
    assert "body.sidebar-collapsed .brand-compact{display:block;width:32px;height:32px" in stylesheet
    assert "object-fit:contain" in stylesheet
    assert "body.sidebar-collapsed .brand-full{display:block;width:100%;max-width:190px" in stylesheet
    assert "body.sidebar-collapsed .nav-count{position:absolute" in stylesheet
    assert "content:attr(data-label)" in stylesheet
    assert "body.sidebar-collapsed .side-user form{display:block}" in stylesheet
    assert "body.sidebar-collapsed main{margin-left:80px}" in stylesheet
    assert "@media(max-width:1000px)" in stylesheet
    assert "STORAGE_KEY='crm-sidebar-collapsed'" in sidebar_state


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
        "Prochain RDV inscription",
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
    assert ".pipeline-stage-secondary .timeline{--pipeline-accent:#7652b5" in stylesheet
    assert ".pipeline-step.current .pipeline-step-marker" in stylesheet


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

    assert "PRIMARY_EXCLUDED_STATUSES=new Set(['Prochain RDV inscription','POEI','Session FT','Def MOB','Financement FT en cours','Financement FT refusé'])" in javascript
    assert "S=C.statuses.filter(status=>!PRIMARY_EXCLUDED_STATUSES.has(status))" in javascript
    assert "SECONDARY_STATUSES=['Prochain RDV inscription','Financement FT en cours'" in javascript

def test_registration_appointment_moves_to_secondary_timeline_and_migrates_history():
    backend = APP_PY.read_text(encoding="utf-8")
    definitions = backend[
        backend.index("CRM_STATUSES = ["):
        backend.index("CALENDLY_API_BASE")
    ]
    namespace = {}
    exec(definitions, namespace)

    assert "Prochain RDV inscription" not in namespace["CRM_STATUSES"]
    assert namespace["CRM_SECONDARY_STATUSES"][0] == "Prochain RDV inscription"
    configured = {
        "crm_statuses": [
            "Nouveaux",
            "Prochain RDV inscription",
            "Étape personnalisée",
            "A relancer",
            "Disqualifié",
            "Converti",
        ]
    }
    statuses = namespace["_crm_statuses"](configured)
    assert "Prochain RDV inscription" not in statuses
    assert "Étape personnalisée" in statuses

    migrate = namespace["_crm_migrate_registration_appointment_status"]
    without_secondary = {
        "statut": "Prochain RDV inscription",
        "statut_secondaire": "",
    }
    assert migrate(without_secondary) is True
    assert without_secondary == {
        "statut": "En cours",
        "statut_secondaire": "Prochain RDV inscription",
    }

    with_secondary = {
        "statut": "Prochain RDV inscription",
        "statut_secondaire": "Financement FT en cours",
    }
    assert migrate(with_secondary) is True
    assert with_secondary == {
        "statut": "En cours",
        "statut_secondaire": "Financement FT en cours",
    }
    assert migrate(with_secondary) is False



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


def test_reminder_period_metrics_filter_rows_and_are_keyboard_accessible():
    workspace = (Path(__file__).parents[1] / "static" / "crm_workspace.js").read_text(encoding="utf-8")
    css = (Path(__file__).parents[1] / "static" / "crm_workspace.css").read_text(encoding="utf-8")
    helper = workspace[
        workspace.index("function reminderPeriodMatches"):
        workspace.index("function remindersPage")
    ]
    script = f"""
const isOverdue=contact=>contact.relance_date<'2026-08-23';
{helper}
const assert=(condition,message)=>{{if(!condition)throw new Error(message)}};
const overdue={{relance_date:'2026-08-22'}};
const today={{relance_date:'2026-08-23'}};
const future={{relance_date:'2026-08-24'}};
assert(reminderPeriodMatches(overdue,'overdue','2026-08-23'),'overdue mode includes past reminders');
assert(!reminderPeriodMatches(today,'overdue','2026-08-23'),'overdue mode excludes today');
assert(reminderPeriodMatches(today,'date','2026-08-23'),'date mode includes the selected day');
assert(!reminderPeriodMatches(future,'date','2026-08-23'),'date mode excludes other days');
assert([overdue,today,future].every(contact=>reminderPeriodMatches(contact,'planned','2026-08-23')),'planned mode includes every scheduled reminder');
console.log('CRM reminder period filters: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM reminder period filters: OK" in completed.stdout
    assert 'id="reminderOverdue"' in workspace
    assert 'id="reminderTodayMetric"' in workspace
    assert 'id="reminderPlanned"' in workspace
    assert workspace.count('role="button" tabindex="0"') >= 3
    assert "reminderPeriodMatches(contact,periodMode,selectedDate)" in workspace
    assert "const showToday=()=>{selectedDate=today;periodMode='date';renderRows()}" in workspace
    assert "const togglePlanned=()=>{periodMode=periodMode==='planned'?'date':'planned';renderRows()}" in workspace
    assert "const toggleOverdue=()=>{periodMode=periodMode==='overdue'?'date':'overdue';renderRows()}" in workspace
    assert "bindMetricActivation(todayMetric,showToday)" in workspace
    assert "bindMetricActivation(plannedMetric,togglePlanned)" in workspace
    assert "event.key==='Enter'||event.key===' '" in workspace
    assert "selectedDate=dateInput.value;periodMode='date';renderRows()" in workspace
    assert ".workspace-metrics article.metric-action:focus-visible" in css
    assert ".workspace-metrics article.today-metric.active" in css
    assert ".workspace-metrics article.planned-metric.active" in css


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
    assert "contactTitle.insertAdjacentHTML('afterbegin',flagBadge)" in workspace
    assert "contactTitle.querySelector('.contact-flag-badge')" in workspace
    assert "function listQualificationFlag(contact)" in crm_js
    assert "${listQualificationFlag(contact)}${scoreBadge(contact)}" in crm_js
    assert "${listQualificationFlag(contact)}<b>${esc(displayName(contact))}" in crm_js
    assert "<td>${leadScoreCell(c)}</td>" in crm_js
    assert "<span>${globalContactName(c)}<small>" in crm_js
    assert "<b>${esc(displayName(c))}</b>${listQualificationFlag(c)}" not in crm_js
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
    template = (Path(__file__).parents[1] / "templates" / "crm.html").read_text(encoding="utf-8")
    assert "filename=\'crm.css\',v=asset_version" in template
    assert "filename=\'crm_workspace.css\',v=asset_version" in template
    assert "filename=\'crm_workspace.js\',v=asset_version" in template

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
    script = r"""
require('./static/crm_appointment_state.js');
const state=globalThis.CRMAppointmentState;
const now=Date.parse('2026-08-23T12:00:00+02:00');
const appointments=[
 {id:'yesterday',contact_id:'contact-1',status:'active',start_time:'2026-08-22T18:00:00+02:00'},
 {id:'today-past',contact_id:'contact-1',status:'active',start_time:'2026-08-23T09:00:00+02:00'},
 {id:'today-future',contact_id:'contact-1',status:'active',start_time:'2026-08-23T15:00:00+02:00'},
 {id:'tomorrow',contact_id:'contact-1',status:'active',start_time:'2026-08-24T10:00:00+02:00'},
 {id:'cancelled',contact_id:'contact-1',status:'canceled',start_time:'2026-08-23T13:00:00+02:00'},
 {id:'undated',contact_id:'contact-1',status:'active',start_time:''},
];
const assert=(condition,message)=>{if(!condition)throw new Error(message)};
assert(state.nextAppointment('contact-1',appointments,now).id==='today-future','the next future appointment today wins');
assert(state.dateLabel('contact-1',appointments,now)==='Prochain RDV le 23/08/2026','the label is explicit and formatted');
const withoutFutureToday=appointments.filter(item=>item.id!=='today-future');
assert(state.nextAppointment('contact-1',withoutFutureToday,now).id==='tomorrow','the next future day wins over an earlier appointment today');
const todayOnly=withoutFutureToday.filter(item=>item.id!=='tomorrow');
assert(state.nextAppointment('contact-1',todayOnly,now).id==='today-past','an appointment earlier on the same Paris day remains eligible');
const pastOnly=appointments.filter(item=>['yesterday','cancelled','undated'].includes(item.id));
assert(state.nextAppointment('contact-1',pastOnly,now)===null,'past-day, cancelled and undated appointments are ignored');
assert(state.parisDayKey('2026-08-22T22:30:00Z')==='2026-08-23','the day boundary uses Europe/Paris');
console.log('CRM appointment date state: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=Path(__file__).parents[1],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM appointment date state: OK" in completed.stdout
    assert "nextProgrammedAppointmentDate" in crm_js
    assert "contactHasPipelineStatus(c,'RDV programmé')" in crm_js
    assert "window.CRMAppointmentState.dateLabel(c.id,crmAppointments)" in crm_js
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
    assert 'id="bulkMessagePreview" aria-live="polite" aria-busy="false" hidden' in javascript
    assert "contact.id}/message-preview" in javascript
    assert "JSON.stringify({type,contenu:messageField.value" in javascript
    assert "result.sujet||'Sans objet'" in javascript
    assert "previewFrame.srcdoc=smsPreviewHtml(result.contenu)" in javascript
    assert "bulkPreviewRequest" in javascript
    assert "requestId!==bulkPreviewRequest" in javascript
    assert "Préparation de l’aperçu pour" in javascript
    assert "previewRoot.hidden=false" in javascript
    assert ".bulk-message-preview" in stylesheet
    assert '.bulk-message-preview[aria-busy="true"]' in stylesheet


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
    backend = APP_PY.read_text(encoding="utf-8")

    assert "const normalizeGlobalSearch=value=>" in javascript
    assert "normalize('NFD')" in javascript
    assert "const normalizePhoneSearch=value=>" in javascript
    assert "const phoneSearchQuery=value=>" in javascript
    assert "const contactSearchFields=c=>[c.prenom,c.nom,c.mail,c.telephone,c.formation,c.lieu,c.statut]" in javascript
    assert "Object.values(c)" not in javascript
    assert "function contactSearchRank(c,q)" in javascript
    assert "crmActiveContacts().filter(c=>contactMatchesSearch(c,q))" in javascript
    assert "&&contactMatchesSearch(c,q)" in javascript
    assert "contactSearchRank(a,q)-contactSearchRank(b,q)" in javascript
    assert "filter(contact=>contact&&!contact.archived_at)" in javascript
    assert "normalizeGlobalSearch(label).includes(q)" in javascript
    assert "contactCoordinateSummary(c)" in javascript
    assert '"id", "prenom", "nom", "telephone", "mail"' in backend


def test_phone_search_normalizes_french_formats_and_preserves_text_search():
    javascript = CRM_JS.read_text(encoding="utf-8")
    helpers = javascript[
        javascript.index("const normalizeGlobalSearch=value=>"):
        javascript.index("const prepareGlobalContactLinks=")
    ]
    script = f"""
const displayName=contact=>[contact.prenom,contact.nom].filter(Boolean).join(' ');
{helpers}
const assert=(condition,message)=>{{if(!condition)throw new Error(message)}};
const contact={{
  prenom:'Élodie',
  nom:'Martin',
  mail:'elodie@example.test',
  telephone:'06 12-34.56 78',
  formation:'APS',
  lieu:'Paris',
  statut:'Nouveau'
}};
assert(normalizePhoneSearch('+33 (0)6 12 34 56 78')==='0612345678','+33 (0) normalization');
assert(normalizePhoneSearch('0033 6 12 34 56 78')==='0612345678','0033 normalization');
assert(contactMatchesSearch(contact,'0612345678'),'compact local number');
assert(contactMatchesSearch(contact,'+33 6 12 34 56 78'),'international number');
assert(contactMatchesSearch(contact,'0033612345678'),'international 00 number');
assert(contactMatchesSearch(contact,'3456'),'partial phone number');
assert(!contactMatchesSearch(contact,'0699999999'),'different phone number');
assert(contactMatchesSearch(contact,'elodie'),'accent-insensitive name');
assert(contactMatchesSearch(contact,'example.test'),'email search');
assert(contactSearchRank(contact,'0612345678')===0,'exact canonical phone ranking');
assert(contactSearchRank(contact,'0612')===1,'phone prefix ranking');
console.log('CRM telephone search normalization: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM telephone search normalization: OK" in completed.stdout


def test_disqualification_summary_escapes_and_displays_persisted_details():
    workspace = (CRM_JS.parent / "crm_workspace.js").read_text(encoding="utf-8")
    stylesheet = (CRM_CSS.parent / "crm_workspace.css").read_text(encoding="utf-8")

    assert "function contactDisqualificationSummary(contact,ctx)" in workspace
    assert "contact.statut!=='Disqualifié'" in workspace
    assert "ctx.esc(contact.disqualification_reason" in workspace
    assert "ctx.esc(contact.disqualification_detail)" in workspace
    assert "contact.reactivation_date" in workspace
    assert "contactDisqualificationSummary(contact,ctx)+" in workspace
    assert ".contact-disqualification-summary" in stylesheet
    assert "white-space:pre-wrap" in stylesheet


def test_contact_document_title_is_wired_to_real_contact_navigation():
    javascript = CRM_JS.read_text(encoding="utf-8")
    title_javascript = CRM_TITLE_JS.read_text(encoding="utf-8")
    template = CRM_TEMPLATE.read_text(encoding="utf-8")
    backend = APP_PY.read_text(encoding="utf-8")

    assert "const formatFirstName=window.CRMDocumentTitle.formatFirstName" in javascript
    assert "const displayName=window.CRMDocumentTitle.displayName" in javascript
    assert "window.CRMDocumentTitle.applyContact(c,C.section,C.page_label);" in javascript
    assert "if(!c){window.CRMDocumentTitle.applySection(C.section,C.page_label);" in javascript
    render_body = javascript[
        javascript.index("function render(){"):
        javascript.index("async function init(){")
    ]
    assert "window.CRMDocumentTitle.applySection(C.section,C.page_label);" in render_body
    assert "titleForContact" in title_javascript
    assert template.index("filename='crm_title.js'") < template.index("filename='crm.js'")
    assert "filename='crm_title.js',v=asset_version" in template
    assert "filename='crm.js',v=asset_version" in template
    assert 'CRM_ASSET_VERSION = "20260823-reminder-period-filters-1"' in backend

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
    assert 'CRM_ASSET_VERSION = "20260823-reminder-period-filters-1"' in backend

def test_pistes_refreshes_recent_calendly_without_opening_a_contact():
    javascript = CRM_JS.read_text(encoding="utf-8")
    backend = APP_PY.read_text(encoding="utf-8")
    refresh_helpers = javascript[
        javascript.index("const CRM_REFRESH_INTERVAL_MS="):
        javascript.index("function scheduleCrmRefresh")
    ]
    script = """
const document={hidden:false};
const C={section:'pistes',is_admin:true};
const calls=[];
const api=async(url,options)=>{calls.push([url,options]);return{}};
const assert=(condition,message)=>{if(!condition)throw new Error(message)};
""" + refresh_helpers + """
(async()=>{
 assert(await refreshPipelineAppointments(1000)===true,'first Pistes refresh runs');
 assert(calls.length===1,'one sync request');
 assert(calls[0][0]==='/api/crm/calendly/sync','existing global sync endpoint');
 assert(calls[0][1].method==='POST','sync uses POST');
 assert(calls[0][1].timeout===60000,'sync has a bounded timeout');
 assert(JSON.parse(calls[0][1].body).restart===true,'sync refreshes the latest Calendly batch');
 assert(await refreshPipelineAppointments(2000)===false,'fresh sync is throttled');
 assert(calls.length===1,'throttled refresh does not call the API');
 assert(await refreshPipelineAppointments(301001)===true,'stale sync runs again');
 assert(calls.length===2,'five-minute refresh calls the API again');
 pipelineAppointmentRefreshInFlight=true;
 assert(await refreshPipelineAppointments(700000)===false,'concurrent sync is prevented');
 pipelineAppointmentRefreshInFlight=false;
 lastPipelineAppointmentRefreshAt=0;
 C.section='contacts';
 assert(await refreshPipelineAppointments(800000)===false,'other CRM sections do not sync');
 C.section='pistes';
 C.is_admin=false;
 assert(await refreshPipelineAppointments(800000)===false,'non-admin users do not call the protected endpoint');
 C.is_admin=true;
 document.hidden=true;
 assert(await refreshPipelineAppointments(800000)===false,'hidden tabs do not sync');
 console.log('CRM Pistes Calendly refresh: OK');
})().catch(error=>{console.error(error);process.exit(1)});
"""

    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM Pistes Calendly refresh: OK" in completed.stdout
    assert "if(!fiche&&C.section==='pistes')refreshCrmSnapshot()" in javascript
    refresh_body = javascript[
        javascript.index("async function refreshCrmSnapshot()"):
        javascript.index("document.addEventListener('visibilitychange'")
    ]
    assert "if(!id)await refreshPipelineAppointments();" in refresh_body
    assert refresh_body.index("if(!id)await refreshPipelineAppointments();") < refresh_body.index(
        "const snapshot=await api("
    )
    assert "updateVisibleAppointmentData();" in refresh_body
    assert "CRM_CALENDLY_LIST_REFRESH_INTERVAL_MS=300000" in javascript
    assert 'CRM_ASSET_VERSION = "20260823-reminder-period-filters-1"' in backend


def test_mobile_responsive_shell_is_operable_and_keeps_wide_views_accessible():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")
    workspace_stylesheet = (
        Path(__file__).parents[1] / "static" / "crm_workspace.css"
    ).read_text(encoding="utf-8")
    template = CRM_TEMPLATE.read_text(encoding="utf-8")
    backend = APP_PY.read_text(encoding="utf-8")
    script = r"""
require('./static/crm_sidebar_state.js');
const state=globalThis.CRMSidebarState;
const bodyClasses=new Set();
const sidebarClasses=new Set();
const documentListeners={};
const menuListeners={};
const backdropListeners={};
const navListeners={};
const menuAttributes={};
const backdropAttributes={};
const classList=values=>({
 toggle:(name,enabled)=>enabled?values.add(name):values.delete(name),
 contains:name=>values.has(name),
});
const navLink={addEventListener:(name,handler)=>{navListeners[name]=handler}};
const sidebar={classList:classList(sidebarClasses),querySelectorAll:selector=>selector==='a[data-nav]'?[navLink]:[]};
const menu={
 setAttribute:(name,value)=>{menuAttributes[name]=value},
 addEventListener:(name,handler)=>{menuListeners[name]=handler},
};
const backdrop={
 hidden:true,
 setAttribute:(name,value)=>{backdropAttributes[name]=value},
 addEventListener:(name,handler)=>{backdropListeners[name]=handler},
};
const document={
 body:{classList:classList(bodyClasses)},
 querySelector:selector=>({
  '#crmSidebar':sidebar,
  '#menuToggle':menu,
  '#sidebarBackdrop':backdrop,
 }[selector]||null),
 addEventListener:(name,handler)=>{documentListeners[name]=handler},
};
const assert=(condition,message)=>{if(!condition)throw new Error(message)};
const initialized=state.initialize(document,null);
assert(initialized!==null,'responsive controller initializes');
menuListeners.click();
assert(sidebarClasses.has('open'),'menu opens the mobile drawer');
assert(bodyClasses.has('sidebar-mobile-open'),'background scrolling is locked');
assert(backdrop.hidden===false,'backdrop becomes interactive');
assert(menuAttributes['aria-expanded']==='true','open state is announced');
backdropListeners.click();
assert(!sidebarClasses.has('open'),'backdrop closes the drawer');
menuListeners.click();
documentListeners.keydown({key:'Escape'});
assert(!sidebarClasses.has('open'),'Escape closes the drawer');
menuListeners.click();
navListeners.click();
assert(!sidebarClasses.has('open'),'navigation closes the drawer');
assert(menuAttributes['aria-expanded']==='false','closed state is announced');
assert(backdropAttributes['aria-hidden']==='true','closed backdrop is hidden accessibly');
console.log('CRM mobile responsive shell: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=Path(__file__).parents[1],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM mobile responsive shell: OK" in completed.stdout
    assert 'id="menuToggle" aria-label="Ouvrir le menu" aria-controls="crmSidebar" aria-expanded="false"' in template
    assert 'id="sidebarBackdrop"' in template
    assert "menuToggleButton.addEventListener('click'" not in javascript
    assert "window.CRMSidebarState?.initialize(document)" in javascript
    assert "body.sidebar-mobile-open{overflow:hidden;touch-action:none}" in stylesheet
    assert "header .search{display:flex;order:20;width:100%" in stylesheet
    assert ".table-wrap{overflow-x:auto;overflow-y:hidden" in stylesheet
    assert ".modal{display:flex;flex-direction:column;width:100%" in stylesheet
    assert ".workspace-table-card>.table-wrap{max-width:100%;overflow-x:auto" in workspace_stylesheet
    assert ".workspace-bulk{position:static;top:auto}" in workspace_stylesheet
    assert 'CRM_ASSET_VERSION = "20260823-reminder-period-filters-1"' in backend

def test_pistes_score_header_cycles_and_sorts_numeric_values():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")
    application = APP_PY.read_text(encoding="utf-8")
    helpers = javascript[
        javascript.index("function contactScoreValue"):
        javascript.index("const vaeEligibilityQuestions")
    ]
    script = helpers + r"""
const assert=(condition,message)=>{if(!condition)throw new Error(message)};
const contacts=[
  {id:'missing',created_at:'2026-08-23T12:00:00Z'},
  {id:'integration-low',integration_score:{score:'10'},created_at:'2026-08-23T11:00:00Z'},
  {id:'vae-high',vae_eligibility:{score:90},integration_score:{score:5},created_at:'2026-08-23T10:00:00Z'},
  {id:'integration-mid',integration_score:{score:20},created_at:'2026-08-23T09:00:00Z'}
];
assert(nextLeadScoreSortDirection('')==='asc','first click must sort ascending');
assert(nextLeadScoreSortDirection('asc')==='desc','second click must sort descending');
assert(nextLeadScoreSortDirection('desc')==='asc','following click must sort ascending again');
assert(sortLeadsByScore(contacts,'asc').map(contact=>contact.id).join(',')==='integration-low,integration-mid,vae-high,missing','ascending numeric order');
assert(sortLeadsByScore(contacts,'desc').map(contact=>contact.id).join(',')==='vae-high,integration-mid,integration-low,missing','descending numeric order');
assert(contactScoreValue(contacts[2])===90,'displayed VAE score must take priority');
console.log('CRM lead score sorting: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM lead score sorting: OK" in completed.stdout
    assert 'id="scoreSort"' not in javascript
    assert 'data-score-sort' in javascript
    assert 'aria-sort="${ariaSort}"' in javascript
    assert "scoreSortable:type==='pistes'" in javascript
    assert "leadScoreSort=nextLeadScoreSortDirection(leadScoreSort);filter()" in javascript
    assert ".crm-score-sort-arrows .up" in stylesheet
    assert ".crm-score-sort-arrows .down" in stylesheet
    assert "min-height:44px" in stylesheet
    assert "20260823-activity-tab-order-1" in application
    assert "20260823-mobile-responsive-1" not in application

def test_pistes_replaces_location_column_with_shared_completeness():
    javascript = CRM_JS.read_text(encoding="utf-8")
    workspace = (Path(__file__).parents[1] / "static" / "crm_workspace.js").read_text(encoding="utf-8")
    stylesheet = (Path(__file__).parents[1] / "static" / "crm_workspace.css").read_text(encoding="utf-8")
    application = APP_PY.read_text(encoding="utf-8")
    helpers = workspace[
        workspace.index("const normalize=value=>"):
        workspace.index("const hasContact=contact=>")
    ]
    script = helpers + r"""
const assert=(condition,message)=>{if(!condition)throw new Error(message)};
const base={
 prenom:'Ada',nom:'Lovelace',telephone:'0600000000',mail:'ada@example.test',
 formation:'SSIAP 1',lieu:'Paris',dates_formation:'Septembre',origine:'Site internet',
 statut:'Converti',cpf:'Non',identite_creation:'Non',financement_ft:'Non',
 statut_demande_financement_ft:'non_demandee',inscrit_ft:'Non'
};
assert(contactCompleteness({})===0,'an empty historical record is deterministic');
assert(contactCompleteness(base)===100,'a complete standard record reaches 100 percent');
const desp={...base,formation:'DESP'};
assert(contactCompleteness(desp)<100,'DESP requires its conditional journey');
assert(contactCompleteness({...desp,desp_type:'VAE'})===100,'DESP journey completes the record');
const cpf={...base,cpf:'Oui'};
assert(contactCompleteness(cpf)<100,'CPF amount is conditional');
assert(contactCompleteness({...cpf,cpf_montant:'1200'})===100,'CPF amount completes the record');
const ft={...base,financement_ft:'Oui'};
assert(contactCompleteness(ft)<100,'France Travail personal fallback is conditional');
assert(contactCompleteness({...ft,refus_ft_perso:'Non'})===100,'France Travail fallback completes the record');
const aps={...base,formation:'APS',carte_pro:'Non'};
assert(contactCompleteness(aps)<100,'APS without a professional card requires CNAPS answers');
assert(contactCompleteness({...aps,titre_sejour:'Non',titre_sejour_cnaps:'Non concerné',garde_vue:'Non',antecedents:'Non',compte_cnaps:'Non'})===100,'CNAPS answers complete the record');
console.log('CRM Pistes completeness: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=Path(__file__).parents[1],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM Pistes completeness: OK" in completed.stdout
    assert "function leadCompletenessValue(contact)" in javascript
    assert "window.CRMWorkspace?.contactCompleteness?.(contact)" in javascript
    assert 'role="progressbar"' in javascript
    assert 'aria-valuenow="${percent}"' in javascript
    assert "showCompleteness?'COMPLÉTUDE':'LIEU'" in javascript
    assert "showCompleteness?leadCompletenessCell(c):esc(c.lieu)" in javascript
    assert "crm-list-location" in javascript
    assert "showCompleteness:isLeads" in javascript
    assert "CRMWorkspace={listPage" in workspace
    assert "contactCompleteness,contactCompletenessDetails" in workspace
    assert ".workspace-completeness" in stylesheet
    assert ".crm-list-completeness" in stylesheet
    assert 'CRM_ASSET_VERSION = "20260823-reminder-period-filters-1"' in application

def test_pipeline_relance_date_only_announces_today_or_future():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")
    application = APP_PY.read_text(encoding="utf-8")
    helper = javascript[
        javascript.index("function relanceStatusDetails"):
        javascript.index("function contactRelanceStatusMarkup")
    ]
    script = r"""
const contactPipelineStatuses=contact=>contact.statuses||[];
const parisDateKey=()=>{throw new Error('explicit Paris day expected in this test')};
""" + helper + r"""
const assert=(condition,message)=>{if(!condition)throw new Error(message)};
const today='2026-08-23';
const status=relance_date=>({statuses:['A relancer'],relance_date});
assert(relanceStatusDetails(status('2026-08-23'),today).label==='Relance prévue le 23/08/2026','today is announced');
assert(relanceStatusDetails(status('2026-09-04'),today).label==='Relance prévue le 04/09/2026','future is announced');
assert(relanceStatusDetails(status('2026-08-22'),today).tone==='missing','past is not presented as upcoming');
assert(relanceStatusDetails(status(''),today).label==='Aucune relance prévue','missing date is explicit');
assert(relanceStatusDetails(status('2026-02-30'),today).tone==='missing','impossible date is rejected');
assert(relanceStatusDetails({statuses:['Nouveaux'],relance_date:'2026-09-04'},today)===null,'other statuses stay unchanged');
console.log('CRM pipeline relance date: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=Path(__file__).parents[1],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM pipeline relance date: OK" in completed.stdout
    assert "contactRelanceStatusMarkup(c)" in javascript
    assert "${relanceMarkup}</div>" in javascript
    assert "updateVisibleAppointmentData();" in javascript
    assert ".pipeline-relance-date.missing{color:#c23449}" in stylesheet
    assert ".pipeline-appointment-date,.pipeline-relance-date" in stylesheet
    assert 'CRM_ASSET_VERSION = "20260823-reminder-period-filters-1"' in application


def test_contact_pipeline_uses_accessible_saas_stepper_without_changing_actions():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")
    application = APP_PY.read_text(encoding="utf-8")
    helper = javascript[
        javascript.index("function timelineProgressDetails"):
        javascript.index("const secondaryTimelineRow")
    ]
    script = r"""
const esc=value=>String(value);
""" + helper + r"""
const assert=(condition,message)=>{if(!condition)throw new Error(message)};
const statuses=['Nouveaux','Qualification personnalisée','RDV programmé','En cours'];
const details=timelineProgressDetails(statuses,'Qualification personnalisée');
assert(details.index===1&&details.total===4,'dynamic status order is preserved');
assert(details.progress===33,'progress follows the current dynamic step');
assert(details.summary==='Étape 2 sur 4','progress summary is explicit');
const buttons=timelineButtons(statuses,'RDV programmé','primary');
assert(buttons.includes('class="pipeline-step done"'),'completed steps have a state');
assert(buttons.includes('>✓</span>'),'completed steps use a checkmark');
assert(buttons.includes('class="pipeline-step current" aria-current="step"'),'current step is exposed accessibly');
assert(buttons.includes('class="pipeline-step upcoming"'),'future steps have a state');
assert(buttons.includes('data-primary-step="Qualification personnalisée"'),'custom statuses keep the business hook');
const primary=timelineTrack(statuses,'RDV programmé','primary','Pipeline commercial');
assert(primary.includes('Pipeline commercial'),'primary track is labelled');
assert(primary.includes('width:67%'),'primary progress bar is rendered');
const secondary=timelineTrack(['POEI','Marché FT'],'','secondary','Suivi complémentaire');
assert(secondary.includes('pipeline-stage-secondary'),'secondary track has its own theme');
assert(secondary.includes('Aucune étape sélectionnée'),'empty secondary track stays usable');
assert(secondary.includes('data-secondary-step="POEI"'),'secondary business hook is preserved');
console.log('CRM SaaS pipeline: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=Path(__file__).parents[1],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM SaaS pipeline: OK" in completed.stdout
    assert "syncTimelineState(document.querySelector('.timeline-primary'),S,c.statut)" in javascript
    assert "if(b.dataset.primaryStep==='A relancer')return relaunchModal(c)" in javascript
    assert "if(b.dataset.primaryStep==='Converti')return openRegistrationDraft(c)" in javascript
    assert "document.querySelectorAll('[data-secondary-step]')" in javascript
    assert "addSecondary.closest('.timeline-row').insertAdjacentHTML('afterend',secondaryTimelineRow(''))" in javascript
    assert 'class="timeline-scroll" tabindex="0" role="group"' in javascript
    assert ".pipeline-stage-card" in stylesheet
    assert ".pipeline-line>span" in stylesheet
    assert ".pipeline-step:focus-visible .pipeline-step-marker" in stylesheet
    assert ".timeline-scroll{scroll-snap-type:x proximity;padding-bottom:6px}" in stylesheet
    assert ".timeline-toggle{flex-basis:44px;width:44px;height:44px}" in stylesheet
    assert "clip-path:polygon(0 0,calc(100% - 12px)" not in stylesheet
    assert 'CRM_ASSET_VERSION = "20260823-reminder-period-filters-1"' in application


def test_activity_journal_is_second_tab_before_wedof():
    javascript = CRM_JS.read_text(encoding="utf-8")
    application = APP_PY.read_text(encoding="utf-8")

    subnav = javascript[
        javascript.index('<div class="contact-subnav">'):
        javascript.index('</nav><div class="funding-badges"')
    ]
    assert subnav.index('id="contactInfoTab"') < subnav.index('id="contactActivityTab"')
    assert subnav.index('id="contactActivityTab"') < subnav.index('id="contactWedofTab"')
    assert subnav.index('id="contactWedofTab"') < subnav.index('id="contactVaeTab"')
    assert subnav.index('id="contactVaeTab"') < subnav.index('id="contactRelanceTab"')

    logical_tabs = javascript[
        javascript.index("const tabs=["):
        javascript.index("const selectContactTab=")
    ]
    assert (
        "const tabs=[contactInfoTab,contactActivityTab,contactWedofTab,"
        "document.querySelector('#contactVaeTab'),"
        "document.querySelector('#contactRelanceTab')].filter(Boolean);"
    ) in logical_tabs

    for tab_id, panel_id in (
        ("contactInfoTab", "contactInfoPanel"),
        ("contactActivityTab", "contactActivityPanel"),
        ("contactWedofTab", "contactWedofPanel"),
        ("contactVaeTab", "contactVaePanel"),
        ("contactRelanceTab", "contactRelancePanel"),
    ):
        assert f'id="{tab_id}"' in subnav
        assert f'aria-controls="{panel_id}"' in subnav
        assert f'id="{panel_id}"' in javascript
        assert f'aria-labelledby="{tab_id}"' in javascript

    assert "loadWedof(c,{refresh:true})" in javascript
    assert 'CRM_ASSET_VERSION = "20260823-reminder-period-filters-1"' in application
