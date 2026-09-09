import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_JS = ROOT / "static" / "crm_workspace.js"
CRM_TEMPLATE = ROOT / "templates" / "crm.html"


def test_pistes_exclude_meta_filter_behavior_and_ui_contract():
    javascript = WORKSPACE_JS.read_text(encoding="utf-8")
    helpers = javascript[
        javascript.index("const workspaceOriginOptions="):
        javascript.index("function nextAction")
    ]
    script = f"""
const normalize=value=>String(value||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase().trim();
{helpers}
const assert=(condition,message)=>{{if(!condition)throw new Error(message)}};
const metaVariants=[
 {{origine:'META'}},
 {{origine:'Facebook Lead Ads'}},
 {{source:'Instagram'}},
 {{meta_source:{{campaign_name:'A3P'}}}},
];
for(const contact of metaVariants){{
 assert(leadOriginMatches(contact,false),'META remains visible when the filter is disabled');
 assert(!leadOriginMatches(contact,true),'META is excluded when the filter is enabled');
}}
for(const contact of [
 {{origine:'Google Ads'}},
 {{origine:'Site internet'}},
 {{origine:'Mon Compte Formation'}},
 {{origine:'Secrétariat'}},
 {{origine:'Calendly'}},
 {{origine:'Ajout manuel'}},
])assert(leadOriginMatches(contact,true),'non-META origin remains visible');
console.log('CRM pistes exclude META: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM pistes exclude META: OK" in completed.stdout
    assert "id=\"workspaceExcludeMeta\"" in javascript
    assert "> Exclure META</label>" in javascript
    assert "type==='pistes'?`<label" in javascript
    assert "f.excludeMeta=event.target.checked" in javascript
    assert "type==='pistes'&&!leadOriginMatches(contact,filters.excludeMeta)" in javascript
    assert "filters:{...f}" in javascript
    template = CRM_TEMPLATE.read_text(encoding="utf-8")
    assert "pistes_exclude_meta_version='20260909-1'" in template
