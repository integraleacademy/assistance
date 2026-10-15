"""Regression coverage for short calls, undecided dates and booking prefills."""

import copy
import shutil
import subprocess

import pytest

import app as application


@pytest.fixture
def desk(monkeypatch, tmp_path):
    data = copy.deepcopy(application.DEFAULT_DATA)
    data.update(crm_contacts=[], secretariat_demandes=[], demandes=[])
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    monkeypatch.setattr(application, "load_data", lambda: data)
    monkeypatch.setattr(application, "save_data", lambda *_: None)
    monkeypatch.setattr(application, "_calendly_token", lambda: "")
    monkeypatch.setattr(application, "_secretariat_refresh_calendly_appointments", lambda *_: None)
    monkeypatch.setattr(application, "_send_secretariat_information_messages", lambda *_: {})
    monkeypatch.setattr(application, "_SECRETARIAT_CALENDLY_SCHEMA_CACHE", {})
    salesforce = []
    monkeypatch.setattr(application, "creer_piste_salesforce", salesforce.append)
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    return application.app.test_client(), data, salesforce


def contact():
    return {"id": "existing", "prenom": "Camille", "nom": "Martin",
            "telephone": "06 01 02 03 04", "mail": "camille@example.com",
            "statut": "Converti", "activities": [], "publications": []}


def cassandre():
    return next(member["name"] for member in application.USERS.values()
                if member["first_name"] == "Cassandre")


@pytest.mark.parametrize("motif", ["suivi", "message"])
@pytest.mark.parametrize("known", [True, False])
def test_short_call_is_a_callback_without_email_qualification_or_commercial_sends(desk, monkeypatch, motif, known):
    client, data, salesforce = desk
    if known:
        data["crm_contacts"] = [contact()]
    monkeypatch.setattr(application, "_send_secretariat_information_messages",
                        lambda *_: pytest.fail("A simple message must not trigger commercial messages"))
    monkeypatch.setattr(application, "_ensure_secretariat_quote",
                        lambda *_: pytest.fail("A simple message must not create a quote"))
    result = client.post("/api/secretariat/demandes", json={
        "type": "autre", "callback_motif": motif, "callback_recipient": cassandre(),
        "nom": "Camille Martin", "telephone": "0601020304",
        "notes": "Souhaite un rappel concernant son dossier.", "rdv": "Non souhaité",
    })
    assert result.status_code == 201
    entry = data["secretariat_demandes"][0]
    assert entry["email"] == ""
    assert entry["callback_status"] == "pending"
    assert entry["callback_recipient"] == cassandre()
    assert entry["callback_motif"] == motif
    assert entry["crm_contact_id"] == ("existing" if known else "")
    assert len(data["crm_contacts"]) == int(known)
    assert not salesforce
    row = application._crm_callback_requests_payload(data)[0]
    assert row["recipient"] == cassandre()
    assert row["motif"] == motif
    if known:
        stored = data["crm_contacts"][0]
        assert stored["statut"] == "Converti"
        assert cassandre() in stored["activities"][0]["detail"]
        assert cassandre() in stored["publications"][0]["texte"]


def test_short_call_rejects_an_unknown_recipient(desk):
    client, data, _ = desk
    response = client.post("/api/secretariat/demandes", json={
        "type": "autre", "callback_recipient": "Unknown", "notes": "Message",
    })
    assert response.status_code == 400
    assert not data["secretariat_demandes"]


def test_phone_lookup_keeps_public_contacts_private_and_prefills_only_for_internal_staff(desk):
    client, data, _ = desk
    data["crm_contacts"] = [{**contact(), "commentaires": "Not for the intake form"}]
    response = client.post("/api/secretariat/crm-contact", json={"telephone": "0601020304"})
    assert response.get_json() == {"contact_id": "existing"}
    assert "no-store" in response.headers["Cache-Control"]
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    response = client.post("/api/secretariat/crm-contact", json={"telephone": "0601020304"})
    assert response.get_json()["contact"] == {
        "prenom": "Camille", "nom": "Martin", "telephone": "06 01 02 03 04", "email": "camille@example.com",
    }
    assert "commentaires" not in response.get_json()["contact"]


def test_ambiguous_coordinates_do_not_prefill_or_attach_an_arbitrary_contact(desk):
    client, data, _ = desk
    data["crm_contacts"] = [contact(), {**contact(), "id": "second", "mail": "other@example.com"}]
    response = client.post("/api/secretariat/crm-contact", json={"telephone": "0601020304"})
    assert response.get_json() == {"contact_id": None}
    response = client.post("/api/secretariat/demandes", json={
        "type": "autre", "crm_contact_id": "second", "telephone": "0601020304",
        "nom": "Camille Martin", "notes": "Rappeler",
    })
    assert response.status_code == 201
    assert response.get_json()["demande"]["crm_contact_id"] == ""
    assert all(not item["activities"] for item in data["crm_contacts"])


@pytest.mark.parametrize("formation", ["APS", "BTS_MOS", "BTS_MCO"])
def test_request_and_appointment_can_be_saved_with_no_training_session(desk, formation):
    client, data, salesforce = desk
    result = client.post("/api/secretariat/demandes", json={
        "type": "formation", "formation": formation, "formation_date_status": "a_definir",
        "formation_date_souhaitee": "stale date", "formation_session_label": "stale date",
        "formation_date_examen": "2026-12-01", "rentree_souhaitee": "Septembre 2027",
        "nom": "Camille Martin", "prenom": "Camille", "nom_famille": "Martin",
        "telephone": "0601020304", "email": "camille@example.com",
        "notes": "Projet de formation", "rdv": "Rendez-vous réservé via Calendly",
    })
    assert result.status_code == 201
    entry = data["secretariat_demandes"][0]
    for key in ("formation_date_souhaitee", "formation_session_label", "formation_date_examen"):
        assert entry[key] == ""
    assert entry["rdv"] == "Rendez-vous réservé via Calendly"
    assert salesforce[0]["dates"] == ""
    assert "Septembre 2027" in salesforce[0]["infos_complementaires"]
    assert "Septembre 2027" in data["crm_contacts"][0]["publications"][0]["texte"]


def test_bts_does_not_reuse_hidden_aps_answers_or_create_a_quote(desk, monkeypatch):
    client, data, _ = desk
    response = client.post("/api/secretariat/demandes", json={
        "type": "formation", "formation": "BTS_MOS", "nom": "Camille Martin",
        "prenom": "Camille", "nom_famille": "Martin", "telephone": "0601020304",
        "email": "camille@example.com", "notes": "Alternance", "cpf_consulte": "OUI",
        "cpf_montant": "1200", "identite_numerique": "OUI", "france_travail": "OUI", "devis": "OUI",
    })
    assert response.status_code == 201
    entry = data["secretariat_demandes"][0]
    for key in ("cpf_consulte", "cpf_montant", "identite_numerique", "france_travail", "devis"):
        assert entry[key] == ""
    assert not data["demandes"]


@pytest.mark.parametrize("code", ["", *application.SECRETARIAT_FORMATIONS])
def test_booking_schema_uses_each_configured_agenda_and_caches_public_fields(desk, monkeypatch, code):
    client, _, _ = desk
    url = application.SECRETARIAT_FORMATIONS.get(code, {}).get("calendly", application.SECRETARIAT_GENERIC_CALENDLY_URL)
    calls = []
    monkeypatch.setattr(application, "_calendly_token", lambda: "test-only")
    def event_types(_):
        calls.append(True)
        return [{"scheduling_url": url, "locations": [{"kind": "outbound_call"}], "secret": "do not expose",
                 "custom_questions": [
                     {"enabled": False, "position": 0, "type": "string", "name": "Disabled"},
                     {"enabled": True, "position": 1, "type": "phone_number", "name": "Téléphone"},
                 ]}]
    monkeypatch.setattr(application, "_calendly_event_types_for_context", event_types)
    response = client.get("/api/secretariat/calendly/prefill-schema", query_string={"formation": code})
    assert response.status_code == 200
    assert response.get_json() == {"outbound_call": True, "questions": [
        {"position": 1, "type": "phone_number", "name": "Téléphone", "answer_choices": None},
    ]}
    client.get("/api/secretariat/calendly/prefill-schema", query_string={"formation": code})
    assert len(calls) == 1


def test_booking_schema_unavailability_does_not_block_the_embed(desk, monkeypatch):
    client, _, _ = desk
    monkeypatch.setattr(application, "_calendly_token", lambda: "test-only")
    def unavailable(_):
        raise RuntimeError("No metadata")
    monkeypatch.setattr(application, "_calendly_event_types_for_context", unavailable)
    response = client.get("/api/secretariat/calendly/prefill-schema?formation=APS")
    assert response.status_code == 200
    assert response.get_json() == {"questions": [], "outbound_call": True}
    assert client.get("/api/secretariat/calendly/prefill-schema?formation=unknown").status_code == 404


def test_secretariat_complete_dom_journeys(desk, monkeypatch):
    """Execute the rendered inline script with a DOM, mocked CRM and Calendly.

    Optional developer runner: npm install --prefix /tmp/desk-dom jsdom;
    NODE_PATH=/tmp/desk-dom/node_modules pytest tests/test_secretariat_intake.py
    No real requests, appointments, messages or production records are created.
    """
    if not shutil.which("node") or subprocess.run(
        ["node", "-e", "require('jsdom')"], capture_output=True,
    ).returncode:
        pytest.skip("Optional DOM runner requires node and jsdom")
    client, _, _ = desk
    centre = next(iter(application.FORMATION_CENTRES))
    monkeypatch.setattr(application, "get_upcoming_formation_sessions", lambda *_: {
        centre: {"APS": [{"label": "Du 1 au 30 décembre 2026", "date_examen": "2026-12-31"}]},
    })
    rendered = client.get("/secretariat").get_data(as_text=True)
    script = r"""
const {JSDOM, VirtualConsole}=require('jsdom'), assert=require('node:assert/strict');
const html=require('fs').readFileSync(0,'utf8');
const wait=ms=>new Promise(r=>setTimeout(r,ms));
async function until(check){for(let i=0;i<200;i++){if(check())return;await wait(5)}throw Error('DOM action timed out')}
function setup(lookup={contact_id:null}){
  const calls=[],widgets=[],copied=[],clipboardState={reject:false},errors=[],vc=new VirtualConsole();vc.on('jsdomError',e=>errors.push(e));
  const dom=new JSDOM(html,{url:'https://intake.test/secretariat',runScripts:'dangerously',virtualConsole:vc,beforeParse(w){
    w.scrollTo=()=>{};w.AbortController=AbortController;
    Object.defineProperty(w.navigator,'clipboard',{configurable:true,value:{async writeText(value){if(clipboardState.reject)throw Error('Clipboard denied');copied.push(value)}}});
    w.Calendly={initInlineWidget(options){widgets.push(options);options.parentElement.appendChild(w.document.createElement('iframe'))}};
    w.fetch=async(url,options={})=>{const body=JSON.parse(options.body||'{}');calls.push({url,body});let data={};
      if(url.includes('crm-contact'))data=lookup;
      else if(url.includes('prefill-schema'))data={outbound_call:true,questions:[{name:'Votre projet',type:'string',position:0},{name:'Téléphone',type:'phone_number',position:1}]};
      else if(url.includes('/demandes'))data={ok:true};
      return {ok:true,json:async()=>data};
    };
  }});
  const w=dom.window,d=w.document,$=selector=>d.querySelector(selector);
  const fill=(id,value)=>{const e=d.getElementById(id);e.value=value;e.dispatchEvent(new w.Event('input',{bubbles:true}))};
  const click=selector=>{const e=$(selector);assert.ok(e,selector);e.click()};
  const step=()=>$('.step:not([hidden])').dataset.step;
  const close=()=>{dom.window.close();assert.deepEqual(errors.map(e=>e.message),[])};
  return {w,d,$,fill,click,step,close,calls,widgets,copied,clipboardState};
}
async function caller(t,type,mail='camille@example.com'){
  t.click(`[data-request="${type}"]`);assert.equal(t.step(),'2');
  t.fill('telephone','0601020304');t.fill('nom','Martin');t.fill('prenom','Camille');if(mail)t.fill('email',mail);
  t.click('#callerSubmit');
}
(async()=>{
  for(const type of ['suivi','autre']){
    const t=setup();assert.equal(t.step(),'1');await caller(t,type,'');assert.equal(t.step(),'7');
    assert.equal(t.$('#email').required,false);t.fill('precision','Rappeler au sujet du dossier');
    const recipient=[...t.$('#callbackRecipient').options].find(o=>o.textContent.includes('Cassandre'));assert.ok(recipient);
    t.$('#callbackRecipient').value=recipient.value;t.click('#finish');await until(()=>t.step()==='8');
    const saved=t.calls.find(c=>c.url==='/api/secretariat/demandes').body;
    assert.equal(saved.type,'autre');assert.equal(saved.callback_motif,type==='suivi'?'suivi':'message');
    assert.equal(saved.callback_recipient,recipient.value);assert.equal(saved.email,'');assert.equal(saved.formation,'');
    assert.equal(saved.cpf_consulte,undefined);assert.equal(saved.rdv,'Non souhaité');assert.equal(t.widgets.length,0);t.close();
  }
  {
    const t=setup({contact_id:'known',contact:{prenom:'Camille',nom:'Martin',telephone:'0601020304',email:'camille@example.com'}});
    t.click('[data-request="suivi"]');t.fill('telephone','0601020304');await until(()=>!t.$('#callerMatch').hidden);
    assert.equal(t.$('#nom').value,'');t.click('#useCallerMatch');assert.equal(t.$('#nom').value,'Martin');assert.equal(t.$('#email').value,'camille@example.com');
    t.click('#callerSubmit');assert.equal(t.step(),'7');t.close();
  }
  {
    const t=setup();await caller(t,'formation');t.click('[data-formation-code="BTS_MOS"]');t.click('#nextFormation');
    assert.equal(t.step(),'5');assert.ok(t.$('[data-undecided]').checked);assert.equal(t.$('[name="cpf_consulte"]').disabled,true);
    t.fill('rentreeSouhaitee','Septembre 2027');t.click('#nextDates');assert.equal(t.step(),'6');
    t.click('[name="wants_rdv"][value="oui"]');await until(()=>t.widgets.length===1);
    assert.equal(new URL(t.widgets[0].url).pathname,'/integraleacademy/btsmos');
    assert.ok(t.calls.some(call=>call.url==='/api/secretariat/calendly/prefill-schema?formation=BTS_MOS'));
    assert.equal(t.$('#bookingLayout').hidden,false);assert.ok(t.$('main').classList.contains('booking-active'));
    const iframe=t.$('#calendlyInline iframe'),requestsBeforeCopy=t.calls.length;
    for(const [key,value] of Object.entries({prenom:'Camille',nom:'Martin',email:'camille@example.com',telephone:'0601020304'})){
      const field=t.$(`[data-caller-field="${key}"]`);assert.equal(field.value,value);assert.equal(field.readOnly,true);
      t.click(`[data-copy-caller="${key}"]`);await until(()=>t.copied.at(-1)===value&&t.$('#callerCopyStatus').textContent.includes('copié'));
      assert.match(t.$('#callerCopyStatus').textContent,/copié/);
    }
    assert.equal(t.calls.length,requestsBeforeCopy);assert.equal(t.widgets.length,1);assert.equal(t.$('#calendlyInline iframe'),iframe);
    assert.equal(t.$('#reviewRequest').disabled,true);
    t.clipboardState.reject=true;t.click('[data-copy-caller="telephone"]');await until(()=>t.$('#callerCopyStatus').textContent.includes('Ctrl+C'));
    const phone=t.$('#referenceTelephone');assert.equal(t.d.activeElement,phone);assert.equal(phone.selectionStart,0);assert.equal(phone.selectionEnd,phone.value.length);
    assert.equal(t.copied.length,4);delete t.w.navigator.clipboard;t.click('[data-copy-caller="email"]');await wait(0);
    assert.equal(t.d.activeElement,t.$('#referenceEmail'));assert.match(t.$('#callerCopyStatus').textContent,/Texte sélectionné/);
    t.click('[name="wants_rdv"][value="non"]');assert.equal(t.$('#bookingLayout').hidden,true);t.click('#reviewRequest');
    assert.equal(t.$('main').classList.contains('booking-active'),false);
    t.fill('precision','Projet BTS en alternance');t.click('#finish');await until(()=>t.step()==='8');const saved=t.calls.find(c=>c.url==='/api/secretariat/demandes').body;
    assert.equal(saved.formation_date_souhaitee,'');assert.equal(saved.formation_date_status,'a_definir');assert.equal(saved.rentree_souhaitee,'Septembre 2027');assert.equal(saved.cpf_consulte,undefined);t.close();
  }
  {
    const t=setup();await caller(t,'formation');t.click('[data-formation-code="APS"]');t.click('#nextFormation');assert.equal(t.step(),'4');
    for(const [name,value] of Object.entries({cpf_consulte:'NON',france_travail:'NON',financement_perso:'NON',identite_numerique:'OUI',cnaps_ok:'OUI',devis:'NON'}))t.click(`[name="${name}"][value="${value}"]`);
    t.click('#nextQualification');assert.equal(t.step(),'5');t.click('[data-undecided]');t.click('#nextDates');t.click('[name="wants_rdv"][value="oui"]');
    await until(()=>t.widgets.length===1);const settings=t.widgets[0];assert.equal(settings.prefill.firstName,'Camille');assert.equal(settings.prefill.lastName,'Martin');
    assert.equal(settings.prefill.email,'camille@example.com');assert.equal(settings.prefill.customAnswers.a2,'+33601020304');assert.equal(settings.prefill.customAnswers.a1,undefined);
    assert.equal(new URL(settings.url).searchParams.get('location'),'+33601020304');assert.equal(t.$('#reviewRequest').disabled,true);
    t.w.dispatchEvent(new t.w.MessageEvent('message',{origin:'https://calendly.com',data:{event:'calendly.event_scheduled'}}));assert.equal(t.$('#reviewRequest').disabled,false);
    t.click('#reviewRequest');t.fill('precision','Projet APS sans date définie');t.click('#finish');await until(()=>t.step()==='8');
    const saved=t.calls.find(c=>c.url==='/api/secretariat/demandes').body;assert.equal(saved.formation_date_souhaitee,'');assert.match(saved.rdv,/réservé/);t.close();
  }
  {
    const t=setup();await caller(t,'suivi','');t.fill('prenom','');t.fill('precision','Rappel avec rendez-vous');t.click('#addAppointment');t.click('[name="wants_rdv"][value="oui"]');
    assert.equal(t.$('#bookingLayout').hidden,false);assert.equal(t.$('#bookingEmailField').hidden,false);assert.equal(t.widgets.length,0);
    assert.equal(t.$('#referencePrenom').value,'');assert.equal(t.$('[data-copy-caller="prenom"]').disabled,true);
    assert.equal(t.$('#referenceEmail').value,'');assert.equal(t.$('[data-copy-caller="email"]').disabled,true);
    t.fill('bookingEmail','camille@example.com');t.click('#confirmBookingEmail');
    await until(()=>t.widgets.length===1);assert.equal(t.widgets[0].prefill.email,'camille@example.com');assert.equal(new URL(t.widgets[0].url).pathname,'/integraleacademy');
    assert.equal(t.$('#referenceEmail').value,'camille@example.com');assert.equal(t.$('[data-copy-caller="email"]').disabled,false);t.close();
  }
  {
    const t=setup({contact_id:'known',contact:{prenom:'Camille',nom:'Martin',telephone:'0601020304',email:'updated@example.com'}});
    await caller(t,'suivi');t.fill('precision','Rappel avec rendez-vous');t.click('#addAppointment');t.click('[name="wants_rdv"][value="oui"]');
    await until(()=>t.widgets.length===1);
    t.w.dispatchEvent(new t.w.MessageEvent('message',{origin:'https://calendly.com',data:{event:'calendly.event_scheduled'}}));
    assert.equal(t.$('#reviewRequest').disabled,false);await until(()=>!t.$('#callerMatch').hidden);
    t.click('[data-step="6"] [data-back]');t.click('[data-step="7"] [data-back]');assert.equal(t.step(),'2');t.click('#useCallerMatch');
    assert.equal(t.$('#email').value,'updated@example.com');assert.equal(t.$('#reviewRequest').disabled,true);assert.equal(t.$('#bookingLayout').hidden,true);
    t.fill('prenom','  Élodie  ');t.fill('nom',"D’Angelo <test>");t.fill('telephone','+33 6 01 02 03 04');
    t.click('#callerSubmit');t.click('#addAppointment');t.click('[name="wants_rdv"][value="oui"]');await until(()=>t.widgets.length===2);
    assert.equal(t.$('#referencePrenom').value,'Élodie');assert.equal(t.$('#referenceNom').value,"D’Angelo <test>");
    assert.equal(t.$('#referenceTelephone').value,'+33 6 01 02 03 04');assert.equal(t.$('#referenceEmail').value,'updated@example.com');
    t.click('[data-copy-caller="email"]');await until(()=>t.copied.at(-1)==='updated@example.com');
    t.click('[data-copy-caller="telephone"]');await until(()=>t.copied.at(-1)==='+33 6 01 02 03 04');
    assert.equal(t.widgets[1].prefill.email,'updated@example.com');t.click('#reviewRequest');assert.equal(t.step(),'6');
    assert.equal(t.calls.filter(c=>c.url==='/api/secretariat/demandes').length,0);t.close();
  }
  console.log('All seven intake journeys passed without external requests.');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run(["node", "-e", script], input=rendered, text=True,
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
