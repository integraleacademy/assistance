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

assert.equal(titles.displayName({prenom:'  éLODIE-anne  ',nom:"  d'angelo  "}), "Élodie-Anne D'ANGELO");
assert.equal(titles.displayName({prenom:'clement',nom:'dupont'}), 'Clément DUPONT');
assert.equal(titles.titleForContact({prenom:'jean',nom:'dupont'}), 'Jean DUPONT - Intégrale CRM');
assert.equal(titles.applyContact({prenom:'marie-claire',nom:'de la tour'}), 'Marie-Claire DE LA TOUR - Intégrale CRM');
assert.equal(global.document.title, 'Marie-Claire DE LA TOUR - Intégrale CRM');
assert.equal(titles.titleForContact({prenom:'',nom:''}), 'Intégrale Connect CRM');
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


def test_contact_document_title_behavior_executes_in_javascript():
    result = run_title_scenario()

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "ok": True,
        "title": "Intégrale Connect CRM",
    }
