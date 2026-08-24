from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[1]
CRM_JS = ROOT / "static" / "crm.js"
CRM_CSS = ROOT / "static" / "crm.css"
APP_PY = ROOT / "app.py"


def test_contact_coordinates_are_larger_and_french_phones_are_formatted_for_display_only():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")
    backend = APP_PY.read_text(encoding="utf-8")

    helper = javascript[
        javascript.index("function formatContactPhone"):
        javascript.index("const contactInStore")
    ]
    script = helper + r"""
const cases=[
 ['0665245271','06 65 24 52 71'],
 ['06.65-24 52 71','06 65 24 52 71'],
 ['+33665245271','+33 6 65 24 52 71'],
 ['0033665245271','+33 6 65 24 52 71'],
 ['33665245271','+33 6 65 24 52 71'],
 ['+33 (0)6 65 24 52 71','+33 6 65 24 52 71'],
 ['+44 20 7946 0958','+44 20 7946 0958'],
 ['066524','066524'],
 ['','']
];
for(const [value,expected] of cases){
 const actual=formatContactPhone(value);
 if(actual!==expected)throw new Error(value+' -> '+actual+' (expected '+expected+')');
}
console.log('CRM contact phone formatting: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM contact phone formatting: OK" in completed.stdout
    assert 'href="tel:${esc(c.telephone)}"' in javascript
    assert "<span>${esc(formatContactPhone(c.telephone))}</span>" in javascript
    assert 'href="mailto:${esc(c.mail)}"' in javascript
    assert "<span>${esc(c.mail)}</span>" in javascript
    assert "font-size:14px;font-weight:750" in stylesheet
    assert ".contact-coordinates .crm-icon{flex:none;width:17px;height:17px}" in stylesheet
    assert "overflow-wrap:anywhere" in stylesheet
    assert "text-overflow:ellipsis;white-space:nowrap" in stylesheet
    assert 'CRM_ASSET_VERSION = "20260824-callback-nav-count-1"' in backend
