from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[1]
CRM_JS = ROOT / "static" / "crm.js"
CRM_CSS = ROOT / "static" / "crm.css"
CRM_HTML = ROOT / "templates" / "crm.html"
APP_PY = ROOT / "app.py"


def test_contact_coordinates_are_larger_and_french_phones_are_formatted_for_display_only():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")
    template = CRM_HTML.read_text(encoding="utf-8")
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
(async()=>{
 let modernCopied='';
 Object.defineProperty(globalThis,'navigator',{value:{clipboard:{writeText:async value=>{modernCopied=value}}},configurable:true});
 if(!await copyContactCoordinate('06 65 24 52 71')||modernCopied!=='06 65 24 52 71')throw new Error('Modern clipboard copy failed');
 let fallbackCopied=false,removed=false;
 Object.defineProperty(globalThis,'navigator',{value:{clipboard:{writeText:async()=>{throw new Error('denied')}}},configurable:true});
 globalThis.document={
  body:{appendChild:()=>{}},
  createElement:()=>({value:'',style:{},setAttribute:()=>{},select:()=>{},remove:()=>{removed=true}}),
  execCommand:command=>{fallbackCopied=command==='copy';return fallbackCopied}
 };
 if(!await copyContactCoordinate('contact@example.com')||!fallbackCopied||!removed)throw new Error('Fallback clipboard copy failed');
 console.log('CRM contact coordinates: OK');
})().catch(error=>{console.error(error);process.exit(1)});
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM contact coordinates: OK" in completed.stdout
    assert 'href="tel:${esc(c.telephone)}"' in javascript
    assert "<span>${esc(formatContactPhone(c.telephone))}</span>" in javascript
    assert 'href="mailto:${esc(c.mail)}"' in javascript
    assert "<span>${esc(c.mail)}</span>" in javascript
    assert 'data-copy-contact="${esc(c.telephone)}"' in javascript
    assert 'aria-label="Copier le numéro de téléphone"' in javascript
    assert 'data-copy-contact="${esc(c.mail)}"' in javascript
    assert 'aria-label="Copier l’adresse e-mail"' in javascript
    assert "await copyContactCoordinate(button.dataset.copyContact)" in javascript
    assert "navigator.clipboard.writeText(text)" in javascript
    assert "document.execCommand('copy')" in javascript
    assert "toast(button.dataset.copySuccess)" in javascript
    assert ".contact-coordinate{display:inline-flex" in stylesheet
    assert ".contact-copy{" in stylesheet
    assert template.count("copy_coordinates_version='20260825-copy-contact-coordinates-1'") == 2
    assert "font-size:14px;font-weight:750" in stylesheet
    assert ".contact-coordinates .crm-icon{flex:none;width:17px;height:17px}" in stylesheet
    assert "overflow-wrap:anywhere" in stylesheet
    assert "text-overflow:ellipsis;white-space:nowrap" in stylesheet
    assert 'CRM_ASSET_VERSION = "20260830-scoring-provisoire-v5-2"' in backend
