from pathlib import Path
import subprocess

import pytest

import app as application
from candidate_scoring import calculate_candidate_integration_score


ROOT = Path(__file__).resolve().parents[1]
CRM_JS = ROOT / "static" / "crm.js"
CRM_CSS = ROOT / "static" / "crm.css"
CRM_WORKSPACE_JS = ROOT / "static" / "crm_workspace.js"


def crm_client(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application._DATA_CACHE_PAYLOAD = None
    application._DATA_CACHE_SIGNATURE = None
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    client = application.app.test_client()
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return client


@pytest.mark.parametrize(("contact", "expected"), [
    ({"formation": "APS"}, 1650),
    ({"formation": "A3P"}, 4200),
    ({"formation": "Chauffeur VTC"}, 1500),
    ({"formation": "VTC"}, 1500),
    ({"formation": "DESP", "desp_type": "INITIAL"}, 4300),
    ({"formation": "DESP", "desp_type": "VAE"}, 3800),
    ({"formation": "VAE"}, 3800),
    ({"formation": "SSIAP 1"}, 1230),
])
def test_requested_sale_prices_are_used_by_the_backend(contact, expected):
    assert application._crm_default_sale_price(contact) == expected


def test_ssiap_price_is_consistent_and_legacy_defaults_are_migrated():
    score = calculate_candidate_integration_score({"formation": "SSIAP 1"})
    assert score["training_price_eur"] == 1230

    for legacy_price in ("980", "1200"):
        contact = {"formation": "SSIAP 1", "prix_vente": legacy_price}
        assert application._crm_workspace_backfill(contact) is True
        assert contact["prix_vente"] == 1230

    custom = {"formation": "SSIAP 1", "prix_vente": "1100"}
    application._crm_workspace_backfill(custom)
    assert custom["prix_vente"] == "1100"

    vtc = {"formation": "Chauffeur VTC", "prix_vente": "1600"}
    assert application._crm_workspace_backfill(vtc) is True
    assert vtc["prix_vente"] == 1500


def test_requested_prices_are_used_by_quotes_and_secretariat_content():
    vtc_quote = application.build_devis_context(
        "VTC", application.PLAN_FORMATIONS["VTC"], ""
    )

    assert vtc_quote["devis_total"] == "1500 €"
    assert application.get_formation_tarif(
        "SSIAP", {"ssiap_secourisme_valide": "OUI"}
    ) == 1230
    assert application.get_formation_tarif(
        "SSIAP", {"ssiap_secourisme_valide": "NON"}
    ) == 1230
    assert application.SECRETARIAT_FORMATIONS["SSIAP"]["price"] == "1 230 € TTC"
    assert application.SECRETARIAT_FORMATIONS["VTC"]["price"] == "1 500 € TTC"


def test_training_changes_refresh_the_default_sale_price_without_overwriting_custom_prices(
        tmp_path, monkeypatch):
    client = crm_client(tmp_path, monkeypatch)
    contact = client.post(
        "/api/crm/contacts", json={"prenom": "Lina", "formation": "APS"}
    ).get_json()
    assert contact["prix_vente"] == 1650

    vtc = client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"formation": "Chauffeur VTC"},
    ).get_json()
    assert vtc["prix_vente"] == "1500"

    custom = client.patch(
        f"/api/crm/contacts/{contact['id']}", json={"prix_vente": "1400"}
    ).get_json()
    assert custom["prix_vente"] == "1400"

    a3p = client.patch(
        f"/api/crm/contacts/{contact['id']}", json={"formation": "A3P"}
    ).get_json()
    assert a3p["prix_vente"] == "1400"


def test_requested_sale_prices_are_used_by_the_contact_form():
    javascript = CRM_JS.read_text(encoding="utf-8")
    helper = javascript[
        javascript.index("const CRM_SALE_PRICES"):javascript.index(
            "const calendlyAppointmentCutoff"
        )
    ]
    script = helper + r"""
const cases=[
 ['APS','',1650],['A3P','',4200],['Chauffeur VTC','',1500],
 ['VTC','',1500],['DESP','INITIAL',4300],['DESP','VAE',3800],
 ['VAE','',3800],['SSIAP 1','',1230]
];
for(const [formation,despType,expected] of cases){
 const actual=crmSalePriceFor(formation,despType);
 if(actual!==expected)throw new Error(`${formation}/${despType}: ${actual}`);
}
console.log('CRM sale prices: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM sale prices: OK" in completed.stdout
    assert "form.prix_vente.value=String(defaultPrice)" in javascript


def test_contact_identity_is_edited_in_the_header_without_a_duplicate_block():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")

    assert "function contactHeaderEditor(c,last)" in javascript
    assert 'form="contactForm" data-header-contact-field name="${name}"' in javascript
    for marker in (
        "input('prenom','Prénom','text'",
        "input('nom','Nom','text'",
        "coordinate('telephone','Téléphone','tel'",
        "coordinate('mail','E-mail','email'",
    ):
        assert marker in javascript
    assert 'data-copy-field="${name}"' in javascript
    assert "headerContactEditor.oninput=handleContactInput" in javascript
    assert 'class="form-section section-user"' not in javascript
    assert ".contact-header-editor input{" in stylesheet


def test_next_phone_appointment_is_displayed_beside_the_next_action():
    javascript = CRM_JS.read_text(encoding="utf-8")
    stylesheet = CRM_CSS.read_text(encoding="utf-8")

    header = javascript[javascript.index('page.innerHTML=`${contactBackLink'):]
    assert header.index('class="contact-next-row"') < header.index(
        "${contactTimelines(c)}"
    )
    next_row = header[header.index('class="contact-next-row"'):header.index(
        "${contactTimelines(c)}"
    )]
    assert next_row.index('class="next-action"') < next_row.index(
        "PROCHAIN RDV TÉLÉPHONIQUE"
    )
    assert "function nextTelephoneAppointment(c,now=Date.now())" in javascript
    assert "if(contact)renderContactNextTelephoneAppointment(contact)" in javascript
    assert ".contact-next-row{grid-column:2/-1;display:grid" in stylesheet

    calendly_helpers = javascript[
        javascript.index("const CALENDLY_APPOINTMENT_PAST_DELAY_MS"):javascript.index(
            "const calendlyAppointmentIsPast"
        )
    ] + javascript[
        javascript.index("function isTelephoneAppointment"):javascript.index(
            "function telephoneAppointmentLabel"
        )
    ]
    script = r"""
let crmAppointments=[];
const window={CRMAppointmentState:{nextAppointment:(contactId,appointments)=>
 appointments.filter(item=>item.contact_id===contactId)
  .sort((a,b)=>Date.parse(a.start_time)-Date.parse(b.start_time))[0]||null}};
""" + calendly_helpers + r"""
const now=Date.parse('2026-09-01T08:00:00Z');
crmAppointments=[
 {id:'video',contact_id:'contact-1',name:'Visio',start_time:'2026-09-01T09:00:00Z',status:'active'},
 {id:'past',contact_id:'contact-1',name:'Appel passé',start_time:'2026-09-01T07:59:00Z',status:'active'},
 {id:'done',contact_id:'contact-1',name:'Appel terminé',start_time:'2026-09-01T08:30:00Z',status:'active',response_status:'answered'},
 {id:'other',contact_id:'contact-2',name:'Appel découverte',start_time:'2026-09-01T08:45:00Z',status:'active'},
 {id:'phone',contact_id:'contact-1',name:'Appel découverte',start_time:'2026-09-01T10:00:00Z',status:'active'},
];
if(!isTelephoneAppointment({location:'outbound_call'}))throw new Error('String phone location not detected');
if(!isTelephoneAppointment({name:'RDV téléphonique'}))throw new Error('French phone event not detected');
if(nextTelephoneAppointment({id:'contact-1'},now)?.id!=='phone')throw new Error('Wrong next phone appointment');
console.log('CRM next phone appointment: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script], cwd=ROOT, check=True, capture_output=True, text=True
    )
    assert "CRM next phone appointment: OK" in completed.stdout


def test_tracking_and_activity_panels_use_the_requested_default_layout():
    javascript = CRM_JS.read_text(encoding="utf-8")
    workspace = CRM_WORKSPACE_JS.read_text(encoding="utf-8")

    assert (
        '<details class="card form-section section-activity tracking-details">'
        '<summary>'
    ) in javascript
    assert '<label>Coût estimé (€)</label>' not in workspace
    assert "let activityExpanded=false,activityFilter='all'" in javascript
    filters = javascript[javascript.index("const activityFilterDefinitions=["):]
    assert filters.index("{key:'all',label:'Tout'") < filters.index(
        "{key:'appel',label:'Appels consignés'"
    )
    assert (
        'class="publications-card activity-publications-panel" '
        'id="activityPublicationsPanel" hidden'
    ) in javascript
    assert "activityPublicationsPanel.hidden=!showPublications" in javascript


def test_training_place_dates_and_cpf_tier_offer_explicit_choices():
    javascript = CRM_JS.read_text(encoding="utf-8")

    assert 'form.lieu.innerHTML=`<option value="À déterminer">À déterminer</option>' in javascript
    assert (
        'form.dates_formation.innerHTML=`<option value="À déterminer">'
        'À déterminer</option>'
    ) in javascript
    assert 'return`<select name="cpf_palier">' in javascript
    for tier in (
        "0 à 1 000 €",
        "1 000 à 2 000 €",
        "2 000 à 3 000 €",
        "3 000 à 4 000 €",
        "Plus de 4 000 €",
    ):
        assert tier in javascript


def test_personal_funding_question_is_hidden_when_cpf_covers_training():
    javascript = CRM_JS.read_text(encoding="utf-8")

    assert 'data-show="personal-financing"' in javascript
    assert (
        "cpfCoversTraining=yes('cpf')&&Number.isFinite(cpfAmount)&&"
        "trainingPrice!==null&&cpfAmount>=trainingPrice"
    ) in javascript
    assert (
        "document.querySelector('[data-show=personal-financing]')?.classList."
        "toggle('hidden',cpfCoversTraining)"
    ) in javascript
