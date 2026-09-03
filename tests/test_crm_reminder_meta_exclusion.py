from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[1]


def test_relance_origin_filter_can_exclude_meta_without_selecting_an_origin():
    javascript = (ROOT / "static" / "crm_workspace.js").read_text(encoding="utf-8")
    helpers = javascript[
        javascript.index("const normalize="):
        javascript.index("function nextAction")
    ]
    script = f"""
{helpers}
const assert=(condition,message)=>{{if(!condition)throw new Error(message)}};
const meta={{origine:'Facebook Lead Ads'}};
const inferredMeta={{meta_source:{{campaign:'APS'}}}};
const google={{origine:'Google Ads'}};
assert(reminderOriginMatches(meta,'',false),'all origins includes META');
assert(!reminderOriginMatches(meta,'',true),'META is excluded');
assert(!reminderOriginMatches(inferredMeta,'',true),'inferred META is excluded');
assert(reminderOriginMatches(google,'',true),'non-META remains visible');
assert(reminderOriginMatches(google,'Google Ads',false),'selected origin remains supported');
assert(!reminderOriginMatches(meta,'Google Ads',false),'selected origin still filters');
console.log('CRM relance META exclusion: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script], cwd=ROOT, check=True,
        capture_output=True, text=True,
    )

    assert "CRM relance META exclusion: OK" in completed.stdout
    assert 'id="reminderExcludeMeta" type="checkbox"' in javascript
    assert "reminderOriginMatches(contact,origin,excludeMetaFilter.checked)" in javascript
    assert "excludeMetaFilter].forEach(input=>input.oninput=renderRows)" in javascript
    stylesheet = (ROOT / "static" / "crm_workspace.css").read_text(encoding="utf-8")
    assert ".reminder-origin-exclusion" in stylesheet
    template = (ROOT / "templates" / "crm.html").read_text(encoding="utf-8")
    assert "relances_meta_version='20260903-1'" in template
