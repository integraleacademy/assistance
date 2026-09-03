from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[1]
WORKSPACE_JS = ROOT / "static" / "crm_workspace.js"
WORKSPACE_CSS = ROOT / "static" / "crm_workspace.css"
CRM_TEMPLATE = ROOT / "templates" / "crm.html"
APP_PY = ROOT / "app.py"


def test_reminder_cards_open_from_free_space_and_keyboard_without_hijacking_controls():
    javascript = WORKSPACE_JS.read_text(encoding="utf-8")
    stylesheet = WORKSPACE_CSS.read_text(encoding="utf-8")
    template = CRM_TEMPLATE.read_text(encoding="utf-8")
    backend = APP_PY.read_text(encoding="utf-8")

    helpers = javascript[
        javascript.index("function reminderCardControl"):
        javascript.index("function remindersPage")
    ]
    script = f"""
{helpers}
const opened=[];
const ctx={{showContact:(id,tab)=>opened.push([id,tab])}};
const contact={{id:'contact-42'}};
const card={{onclick:null,onkeydown:null}};
const link={{onclick:null}};
const plain={{closest:()=>null}};
const button={{closest:selector=>selector.includes('button')?{{}}:null}};
const event=(target,key,extra={{}})=>({{
  target,
  key,
  button:0,
  metaKey:false,
  ctrlKey:false,
  shiftKey:false,
  altKey:false,
  prevented:false,
  preventDefault(){{this.prevented=true}},
  ...extra
}});
const assert=(condition,message)=>{{if(!condition)throw new Error(message)}};

const open=bindReminderCardNavigation(card,contact,ctx);
card.onclick(event(plain));
assert(JSON.stringify(opened)==='[["contact-42","contactRelanceTab"]]','free card click');
card.onclick(event(button));
assert(opened.length===1,'button click remains isolated');

const enter=event(plain,'Enter');
card.onkeydown(enter);
assert(enter.prevented&&opened.length===2,'Enter opens and prevents default');
const space=event(plain,' ');
card.onkeydown(space);
assert(space.prevented&&opened.length===3,'Space opens and prevents default');
const escape=event(plain,'Escape');
card.onkeydown(escape);
assert(!escape.prevented&&opened.length===3,'other keys do nothing');
const controlEnter=event(button,'Enter');
card.onkeydown(controlEnter);
assert(!controlEnter.prevented&&opened.length===3,'control keyboard action remains isolated');
bindReminderLinkNavigation(link,open);
const simpleLinkClick=event(link);
link.onclick(simpleLinkClick);
assert(simpleLinkClick.prevented&&opened.length===4,'simple link click reuses in-app navigation');
const ctrlLinkClick=event(link,undefined,{{ctrlKey:true}});
link.onclick(ctrlLinkClick);
assert(!ctrlLinkClick.prevented&&opened.length===4,'Ctrl-click remains browser-native');
const middleLinkClick=event(link,undefined,{{button:1}});
link.onclick(middleLinkClick);
assert(!middleLinkClick.prevented&&opened.length===4,'middle-click remains browser-native');
console.log('CRM reminder card navigation: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM reminder card navigation: OK" in completed.stdout
    assert 'role="link" tabindex="0" aria-label="Ouvrir le suivi des relances de ' in javascript
    assert "open=bindReminderCardNavigation(card,contact,ctx)" in javascript
    assert "bindReminderLinkNavigation(card.querySelector('[data-reminder-open]'),open)" in javascript
    assert '<a class="btn" data-reminder-open href="${contactUrl}">Ouvrir</a>' in javascript
    assert 'contactUrl=`/crm/contacts?fiche=${encodeURIComponent(contact.id)}`' in javascript
    assert "ctx.relaunchModal(contact,{returnTab:'contactRelanceTab'})" in javascript
    assert "ctx.callModal(contact,{relance,returnTab:'contactRelanceTab'})" in javascript
    assert "ctx.noAnswerRelanceModal(contact,relance)" in javascript
    assert ".reminder-command:hover{" in stylesheet
    assert ".reminder-command:focus-visible{" in stylesheet
    assert ".reminder-command-actions a.btn{text-decoration:none}" in stylesheet
    assert "relances_navigation_version='20260903-1'" in template
    assert 'CRM_ASSET_VERSION = "20260903-multiple-email-attachments-1"' in backend
