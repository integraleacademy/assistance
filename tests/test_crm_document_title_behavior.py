import json
import subprocess
from pathlib import Path


CRM_TITLE_JS = Path(__file__).parents[1] / "static" / "crm_title.js"


def run_title_scenario():
    script = r"""
const assert=require('node:assert/strict');
global.window=global;
global.document={title:'Titre serveur'};
require(process.argv[1]);
const titles=global.CRMDocumentTitle;
const sections={
 accueil:'Accueil',
 'fil-actu':'Fil d’actualité',
 calendrier:'Calendrier',
 contacts:'Contacts',
 pistes:'Pistes',
 relances:'Relances',
 inscrits:'Inscrits',
 disqualifies:'Disqualifiés',
 notifications:'Notifications',
 modeles:'Modèles',
 exports:'Exports',
};

assert.deepEqual({...titles.SECTION_LABELS},sections);
Object.entries(sections).forEach(([section,label])=>{
 assert.equal(titles.titleForSection(section), label+' - Intégrale CRM');
});
assert.equal(titles.applySection('calendrier'), 'Calendrier - Intégrale CRM');
assert.equal(global.document.title, 'Calendrier - Intégrale CRM');
assert.equal(titles.titleForSection('future-section','Page future'), 'Page future - Intégrale CRM');
assert.equal(titles.displayName({prenom:'  éLODIE-anne   marie  ',nom:"  d'angelo   du pont  "}), "Élodie-Anne Marie D'ANGELO DU PONT");
assert.equal(titles.displayName({prenom:'clement',nom:'dupont'}), 'Clément DUPONT');
assert.equal(titles.titleForContact({prenom:'jean',nom:'dupont'},'contacts'), 'Jean DUPONT - Intégrale CRM');
assert.equal(titles.applyContact({prenom:'marie-claire',nom:'de la tour'},'pistes'), 'Marie-Claire DE LA TOUR - Intégrale CRM');
assert.equal(global.document.title, 'Marie-Claire DE LA TOUR - Intégrale CRM');
assert.equal(titles.titleForContact({prenom:'',nom:''},'contacts'), 'Contacts - Intégrale CRM');
assert.equal(titles.applyContact({prenom:'',nom:''},'pistes'), 'Pistes - Intégrale CRM');
assert.equal(global.document.title, 'Pistes - Intégrale CRM');
assert.equal(titles.titleForSection('unknown'), 'Intégrale Connect CRM');
assert.equal(titles.reset(), 'Intégrale Connect CRM');
assert.equal(global.document.title, 'Intégrale Connect CRM');

process.stdout.write(JSON.stringify({ok:true,title:global.document.title}));
"""
    return subprocess.run(
        ["node", "-e", script, str(CRM_TITLE_JS)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_contact_and_section_document_title_behavior_executes_in_javascript():
    result = run_title_scenario()

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "ok": True,
        "title": "Intégrale Connect CRM",
    }
