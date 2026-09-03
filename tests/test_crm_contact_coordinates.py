from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[1]
CRM_JS = ROOT / "static" / "crm.js"
CRM_CSS = ROOT / "static" / "crm.css"
CRM_HTML = ROOT / "templates" / "crm.html"
APP_PY = ROOT / "app.py"


def test_contact_coordinates_are_editable_in_the_header_and_phone_helpers_still_work():
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
const compactCases=[
 ['06 65 24 52 71','0665245271'],
 ['+33 6 65 24 52 71','+33665245271'],
 ['0033 6 65 24 52 71','0033665245271'],
 ['poste 123','poste 123'],
 ['','']
];
for(const [value,expected] of compactCases){
 const actual=compactContactPhone(value);
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
    assert "function contactHeaderEditor(c,last)" in javascript
    assert 'form="contactForm" data-header-contact-field name="${name}"' in javascript
    assert "input('prenom','Prénom','text'" in javascript
    assert "input('nom','Nom','text'" in javascript
    assert "coordinate('telephone','Téléphone','tel'" in javascript
    assert "coordinate('mail','E-mail','email'" in javascript
    assert "formatContactPhone(c.telephone)" in javascript
    assert "contact-header-coordinate-${name}" in javascript
    assert 'data-copy-field="${name}"' in javascript
    assert "document.querySelectorAll('[data-copy-contact],[data-copy-field]')" in javascript
    assert "button.closest('label')?.querySelector('input')?.value" in javascript
    assert "Numéro de téléphone copié" in javascript
    assert "Adresse e-mail copiée" in javascript
    assert "headerContactEditor.oninput=handleContactInput" in javascript
    assert "headerPhoneInput.addEventListener('blur'" in javascript
    assert "contactFormPayload(form)" in javascript
    assert "form.prenom.value=c.prenom" in javascript
    assert "form.telephone.value=formatContactPhone(c.telephone)" in javascript
    assert 'class="form-section section-user"' not in javascript
    assert "navigator.clipboard.writeText(text)" in javascript
    assert "document.execCommand('copy')" in javascript
    assert ".contact-header-editor{display:grid" in stylesheet
    assert ".contact-header-name-fields,.contact-header-coordinate-fields" in stylesheet
    assert ".contact-header-editor input{" in stylesheet
    assert ".contact-header-coordinate-control{display:flex" in stylesheet
    assert ".contact-header-coordinate-control .contact-copy{" in stylesheet
    assert ".contact-header-coordinate-telephone input{" in stylesheet
    assert "font:900 16px Manrope" in stylesheet
    assert template.count("copy_coordinates_version='20260825-copy-contact-coordinates-1'") == 2
    assert 'CRM_ASSET_VERSION = "20260903-wedof-header-status-1"' in backend
