"""The activity center must load its text after navigating from compact lists."""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("section", ["notifications", "fil-actu"])
@pytest.mark.parametrize("entry", ["sidebar", "search"])
def test_activity_navigation_restores_titles_and_details(section, entry):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the CRM browser helpers")
    source = (ROOT / "static/crm.js").read_text(encoding="utf-8")
    workspace = (ROOT / "static/crm_workspace.js").read_text(encoding="utf-8")
    router = source[source.index("async function refreshCrmSectionData("):
                    source.index("document.querySelectorAll('.sidebar a[data-nav]')")]
    search_binding = source[
        source.index("document.querySelectorAll('[data-global-page]')"):
        source.index("}globalSearch.onfocus=renderGlobalSearch")
    ]
    activity_page = workspace[workspace.index("function activityPage("):
                              workspace.index("\n", workspace.index("function activityPage("))]
    script = r"""
const assert = require('node:assert/strict');
const section = process.argv[1], entry = process.argv[2];
const C = {section:'pistes'}, state = {activityTab:'system'};
const history = {pushState(){}}, window = {scrollTo(){}};
const activitySearch = {value:''}, activityCenter = {innerHTML:''};
const markAllNotifications = {}, requests = [], errors = [];
let resolveRequest, snapshotRefreshes = 0;
let contacts = [{id:'one',prenom:'Lina',nom:'Martin',_summary:true,
 activities:[{id:'event',kind:'statut',date:'2026-09-23T10:00:00Z'}]}];
const fresh = [{...contacts[0],activities:[{...contacts[0].activities[0],
 title:'Statut : A relancer',detail:'Ancien statut : Nouveaux'}]}];
const button = {dataset:{globalPage:section,globalUrl:`/crm/${section}`}};
const document = {querySelectorAll(selector){
 return selector==='[data-global-page]'?[button]:[];
}};
const globalSearch = {value:'Notifications'}, globalResults = {classList:{remove(){}}};
const normalize = value=>String(value||'').toLowerCase();
const esc = value=>String(value||'');
const api = async url=>{requests.push(url);return new Promise(resolve=>{resolveRequest=resolve})};
const toast = message=>errors.push(message);
const refreshCrmSnapshot = async()=>{snapshotRefreshes++};
const page = {innerHTML:''};
function render(){activityPage({contacts,notifications:[],page,esc,api,
 header:()=>'',displayName:c=>`${c.prenom} ${c.nom}`,fmt:date=>date,
 updateNotificationCount(){},toast})}
""" + activity_page + "\n" + router + "\n" + search_binding + ";\n" + r"""
(async()=>{
 if(entry==='search')button.onclick();
 else navigateCrmSection(section,`/crm/${section}`,section);
 assert.equal(C.section,section);
 assert.equal(snapshotRefreshes,0,'Compact polling cannot supply activity text');
 assert.deepEqual(requests,[`/api/crm/contacts?section=${section}`]);
 resolveRequest(fresh);
 await new Promise(resolve=>setImmediate(resolve));
 assert.ok(activityCenter.innerHTML.includes('<b>Statut : A relancer</b>'));
 assert.ok(activityCenter.innerHTML.includes('<p>Ancien statut : Nouveaux</p>'));
 assert.ok(activityCenter.innerHTML.includes('Lina Martin'));
 activitySearch.value='Ancien statut';
 activitySearch.oninput();
 assert.ok(activityCenter.innerHTML.includes('Statut : A relancer'));
 // A late response must not replace the list after leaving the activity page.
 const pending=refreshCrmSectionData(section);
 C.section='pistes';
 const current=contacts;
 resolveRequest([]);
 await pending;
 assert.equal(contacts,current);
 assert.deepEqual(errors,[]);
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run(
        [node, "-e", script, section, entry],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
