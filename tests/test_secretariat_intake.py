"""Regression coverage for short calls, undecided dates and booking prefills."""

import copy
import datetime
import json
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
    monkeypatch.setattr(application, "_load_data_snapshot", lambda: data)
    monkeypatch.setattr(application, "save_data", lambda *_: None)
    monkeypatch.setattr(application, "_calendly_token", lambda: "")
    monkeypatch.setattr(application, "_secretariat_refresh_calendly_appointments", lambda *_: None)
    monkeypatch.setattr(application, "_send_secretariat_information_messages", lambda *_: {})
    monkeypatch.setattr(application, "_crm_send_email_html", lambda *_: True)
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
        received = next(a for a in stored["activities"] if a.get("kind") == "demande_rappel")
        assert cassandre() in received["detail"]
        assert cassandre() in stored["publications"][0]["texte"]


def test_short_call_rejects_an_unknown_recipient(desk):
    client, data, _ = desk
    response = client.post("/api/secretariat/demandes", json={
        "type": "autre", "callback_recipient": "Unknown", "notes": "Message",
    })
    assert response.status_code == 400
    assert not data["secretariat_demandes"]


@pytest.mark.parametrize(("choice", "note", "expected"), [
    ("auto", "Demande si possible de joindre rapidement pour le tarif, sans attendre vendredi.", True),
    (None, "Elle souhaite être rappelée demain pour un devis.", True),
    ("auto", "À rappeler avant midi.", True),
    ("auto", "Demande à être rappelé pour ne pas attendre vendredi.", True),
    ("auto", "Souhaite être rappelé car pas de créneau.", True),
    ("auto", "Merci de me rappeler.", True),
    ("oui", "Question sur le prix de la formation.", True),
    ("non", "Souhaite être rappelé, mais sa question a été résolue pendant l’appel.", False),
    ("auto", "Ne souhaite pas être rappelé. Il nous rappellera lui-même.", False),
    ("auto", "Pas de rappel demandé. Un devis suffit.", False),
    ("auto", "Rappel demandé. Demande annulée, ne pas rappeler.", False),
    ("auto", "Nous n’avons pas demandé un rappel.", False),
    ("auto", "Demande un rappel des dates de formation par mail.", False),
    ("auto", "Souhaite connaître le prix. Pas de rendez-vous vendredi.", False),
])
def test_training_callback_choice_and_original_note(choice, note, expected):
    assert application.secretariat_calls.prospect_wants_callback(choice, note) is expected


def test_prospect_callback_stays_on_one_training_intake_through_completion(desk):
    client, data, salesforce = desk
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    payload = {
        "type": "formation", "formation": "VTC", "nom": "Camille Martin",
        "telephone": "0601020304", "email": "camille@example.com", "rdv": "Non souhaité",
        "notes": "Souhaite être rappelée pour le tarif.", "call_key": "b" * 32,
    }
    response = client.post("/api/secretariat/demandes", json=payload)
    assert response.status_code == 201
    receipt = response.get_json()["demande"]
    assert receipt["callback_requested"] is True
    entry, = data["secretariat_demandes"]
    prospect, = data["crm_contacts"]
    assert entry["id"] == receipt["id"]
    assert entry["type"] == "formation"
    assert entry["statut"] == "Traité"
    assert len(salesforce) == 1

    row, = application._crm_callback_requests_payload(data)
    assert row["status"] == "pending" and row["crm_contact_id"] == prospect["id"]
    assert row["motif"] == "formation" and row["notes"] == payload["notes"]
    assert application._crm_callback_pending_count(data) == 1
    received = [a for a in prospect["activities"] if a.get("callback_request_id") == entry["id"]
                and a.get("kind") == "demande_rappel"]
    assert len(received) == 1

    result = client.patch(f"/api/crm/callback-requests/{entry['id']}", json={"status": "processed"})
    assert result.status_code == 200 and result.get_json()["callback_pending_count"] == 0
    assert client.post("/api/secretariat/demandes", json=payload).status_code == 200
    row, = application._crm_callback_requests_payload(data)
    assert row["status"] == "processed"
    assert entry["statut"] == "Traité" and len(data["secretariat_demandes"]) == 1
    assert len(data["crm_contacts"]) == len(salesforce) == 1


def test_stale_training_snapshot_keeps_the_finished_intake_and_its_callback():
    initial = {"id": "intake", "type": "formation", "statut": "RDV à prendre",
               "created_at": "2026-10-01T08:00:00+02:00"}
    stale = {"secretariat_demandes": [initial.copy()]}
    completed = {**initial, "statut": "Traité", "callback_requested": True, "callback_status": "processed",
                 "callback_status_updated_at": "2026-10-01T09:00:00+02:00", "desk_completed": True,
                 "call_key_hash": "completed-key", "information_email_status": "sent", "information_sms_status": "sent",
                 "notes": "À rappeler", "crm_contact_id": "linked", "devis_id": "quote"}
    persisted = {"secretariat_demandes": [completed], "crm_contacts": [{"id": "linked", "activities": []}],
                 "demandes": [{"id": "quote", "source_secretariat_id": "intake"}],
                 "crm_inbound_requests": [{"id": "form", "source": "assistant-secretariat", "external_id": "intake"}]}
    application._preserve_newer_callback_lifecycle(stale, persisted)
    saved, = stale["secretariat_demandes"]
    assert saved == completed
    assert all(stale[key] == persisted[key] for key in ("crm_contacts", "demandes", "crm_inbound_requests"))
    _, linked = application._crm_prepare_callback_request(stale, saved)
    assert linked["id"] == "linked"
    assert application._crm_callback_pending_count(stale) == 0


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


@pytest.mark.parametrize("formation", ["APS", "BTS_MOS", "BTS_MCO", "PARCOURS_SECURITE_ALTERNANCE"])
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


@pytest.mark.parametrize("formation", ["BTS_MOS", "PARCOURS_SECURITE_ALTERNANCE"])
def test_apprenticeship_does_not_reuse_hidden_aps_answers_or_create_a_quote(desk, formation):
    client, data, _ = desk
    response = client.post("/api/secretariat/demandes", json={
        "type": "formation", "formation": formation, "nom": "Camille Martin",
        "prenom": "Camille", "nom_famille": "Martin", "telephone": "0601020304",
        "email": "camille@example.com", "notes": "Alternance", "cpf_consulte": "OUI",
        "cpf_montant": "1200", "identite_numerique": "OUI", "france_travail": "OUI", "devis": "OUI",
    })
    assert response.status_code == 201
    entry = data["secretariat_demandes"][0]
    for key in ("cpf_consulte", "cpf_montant", "identite_numerique", "france_travail", "devis"):
        assert entry[key] == ""
    assert not data["demandes"]
    if formation == "PARCOURS_SECURITE_ALTERNANCE":
        contact = data["crm_contacts"][0]
        assert contact["formation"] == "Parcours sécurité en alternance"
        assert application._crm_formation_code(contact) == formation
        config = application.SECRETARIAT_FORMATIONS[formation]
        assert entry["calendly_url"] == "https://calendly.com/integraleacademy/parcourssecurite"
        assert config["dossier_url"] in application._build_secretariat_followup_sms(formation)
        assert "CPF" not in application._build_secretariat_followup_sms(formation)
        with application.app.test_request_context():
            email = application._secretariat_automatic_information_template(data, entry, contact)
        assert config["dossier_url"] in email["contenu"]



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
    assert response.get_json() == {"questions": [], "outbound_call": False}
    assert client.get("/api/secretariat/calendly/prefill-schema?formation=unknown").status_code == 404


def test_directory_prefills_only_phone_slots_compatible_with_all_listed_events(desk, monkeypatch):
    client, _, _ = desk
    directory = application.SECRETARIAT_GENERIC_CALENDLY_URL
    def event(slug, types, hidden=False):
        return {"scheduling_url": directory + "/" + slug, "secret": hidden,
                "locations": [{"kind": "custom"}],
                "custom_questions": [{"enabled": True, "position": pos, "type": kind}
                                     for pos, kind in enumerate(types)]}

    events = [event("aps", ["text", "phone_number", "phone_number"]),
              event("vtc", ["text", "phone_number", "text"]),
              event("sans-telephone", ["text"]), event("masque", ["text", "text"], hidden=True)]
    monkeypatch.setattr(application, "_calendly_token", lambda: "test-only")
    monkeypatch.setattr(application, "_calendly_event_types_for_context", lambda _: events)
    response = client.get("/api/secretariat/calendly/prefill-schema")
    assert response.get_json() == {"questions": [{"position": 1, "type": "phone_number"}], "outbound_call": False}


@pytest.fixture
def set_offer_month(monkeypatch):
    def set_month(year, month):
        class OfferClock(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                instant = cls(year, month, 8, 10, tzinfo=datetime.timezone.utc)
                return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
        monkeypatch.setattr(application.datetime, "datetime", OfferClock)
    return set_month


@pytest.mark.parametrize(("year", "month", "expected"), [
    (2026, 11, ["Janvier 2027", "Juin 2027"]),
    (2027, 2, ["Juin 2027"]),
    (2027, 6, ["Juin 2027"]),
    (2027, 7, []),
])
def test_training_sheet_offers_only_current_or_future_brochure_intakes(desk, set_offer_month, year, month, expected):
    set_offer_month(year, month)
    client, _, _ = desk
    html = client.get("/secretariat").get_data(as_text=True)
    catalogue, _ = json.JSONDecoder().raw_decode(html.split("const formations=", 1)[1])
    course = next(record for record in catalogue if record["code"] == "PARCOURS_SECURITE_ALTERNANCE")
    assert [intake["label"] for intake in course["intakes"]] == expected


def test_secretariat_complete_dom_journeys(desk, monkeypatch, set_offer_month):
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
    set_offer_month(2026, 11)
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
      else if(url.includes('/demandes'))data={ok:true,demande:{type:body.type,callback_requested:body.type==='formation'&&body.prospect_callback==='oui'}};
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
  {
    const t=setup(), url='https://calendly.com/integraleacademy/btsmos';
    const messages=[{status:'processed',events:[],subject:'Suivi du dossier',recipient:'Accueil'}];
    t.fill('prenom','Élodie');t.fill('nom','Le Gall');t.fill('email','contact+rdv@example.com');t.fill('telephone','0033 6 00 00 00 01');
    await t.w.renderDossierMessages(messages,url);
    const link=new URL(t.$('#messageAppointment').href);
    assert.equal(t.$('#messageAppointment').hidden,false);
    assert.equal(link.searchParams.get('name'),'Élodie Le Gall');assert.equal(link.searchParams.get('email'),'contact+rdv@example.com');
    assert.equal(link.searchParams.get('a2'),'+33600000001');assert.equal(link.searchParams.has('a1'),false);
    assert.equal(t.$('#supplementAppointment').href,link.href);
    assert.ok(t.calls.some(c=>c.url.endsWith('formation=BTS_MOS')));
    // An outstanding schema request for a former caller must not restore their link.
    let finishSchema;
    t.w.fetch=()=>new Promise(resolve=>{finishSchema=()=>resolve({ok:true,json:async()=>({questions:[]})})});
    const pending=t.w.renderDossierMessages(messages,'https://calendly.com/integraleacademy/aps');
    t.w.clearDeskMessages();finishSchema();await pending;
    assert.equal(t.$('#messageAppointment').hidden,true);assert.equal(new URL(t.$('#supplementAppointment').href).search,'');t.close();
  }
  {
    const t=setup();
    assert.match(t.$('[data-request="formation"]').textContent,/Nouveau prospect/);
    t.click('#browseCourses');
    for(const course of t.d.querySelectorAll('[data-formation-code]')){
      course.click();t.click('#nextFormation');assert.equal(t.step(),'5');
      assert.ok(t.$('#trainingTopics').children.length);
      t.click('[data-step="5"] [data-back]');
    }
    assert.equal(t.calls.length,0);
    t.click('[data-formation-code="APS"]');t.click('#nextFormation');
    const session=t.$('#sessionOptions input[data-session-label]');session.click();
    t.click('#nextDates');assert.equal(t.step(),'2');
    t.fill('telephone','0601020304');t.fill('nom','Martin');t.fill('prenom','Camille');t.fill('email','camille@example.com');t.click('#callerSubmit');
    assert.equal(t.step(),'4');assert.equal(session.checked,true);
    t.click('[name="cpf_consulte"][value="OUI"]');t.click('#cpfPending');
    assert.equal(t.$('#cpf_montant').required,false);assert.equal(t.$('#cpf_montant').disabled,true);
    t.click('#cpfPending');assert.equal(t.$('#cpf_montant').required,true);t.close();
  }
  for(const type of ['suivi','autre']){
    const t=setup();assert.equal(t.step(),'1');await caller(t,type,'');assert.equal(t.step(),'7');
    assert.equal(t.$('#prospectCallbackField').hidden,true);
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
    assert.match(t.$('#trainingFacts').textContent,/15 jours/);
    t.click('[data-intake="Septembre 2027"]');t.click('#nextDates');assert.equal(t.step(),'6');
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
    assert.equal(t.$('#prospectCallbackField').hidden,false);assert.equal(t.$('#recipientField').hidden,true);
    const callback=t.$('#prospectCallback');callback.value='oui';callback.dispatchEvent(new t.w.Event('change'));
    assert.equal(t.$('#recipientField').hidden,false);assert.equal(t.$('#callbackDueField').hidden,false);
    t.fill('callbackDue','2026-11-20T14:30');t.fill('precision','Projet BTS en alternance');
    t.click('#finish');await until(()=>t.step()==='8');const saved=t.calls.find(c=>c.url==='/api/secretariat/demandes').body;
    assert.equal(saved.prospect_callback,'oui');assert.equal(saved.callback_due_at,'2026-11-20T14:30');
    assert.match(t.$('#callReceipt').textContent,/Ajoutée aux demandes à traiter/);
    assert.equal(saved.formation_date_souhaitee,'');assert.equal(saved.formation_date_status,'a_definir');assert.equal(saved.rentree_souhaitee,'Septembre 2027');assert.equal(saved.cpf_consulte,undefined);t.close();
  }
  {
    const t=setup();await caller(t,'formation');t.click('[data-formation-code="APS"]');t.click('#nextFormation');assert.equal(t.step(),'5');
    assert.equal(t.$('#qualificationForm').closest('.step').hidden,true);
    t.click('#nextDates');assert.equal(t.step(),'5');assert.equal(t.$('#sessionError').hidden,false);
    t.click('[data-undecided]');t.click('#nextDates');assert.equal(t.step(),'4');
    for(const [name,value] of Object.entries({cpf_consulte:'NON',france_travail:'NON',financement_perso:'NON',identite_numerique:'OUI',cnaps_ok:'OUI',devis:'NON'}))t.click(`[name="${name}"][value="${value}"]`);
    t.click('#nextQualification');assert.equal(t.step(),'6');t.click('[name="wants_rdv"][value="oui"]');
    await until(()=>t.widgets.length===1);const params=new URL(t.widgets[0].url).searchParams;
    assert.equal(params.get('first_name'),'Camille');assert.equal(params.get('last_name'),'Martin');assert.equal(params.get('name'),'Camille Martin');
    assert.equal(params.get('email'),'camille@example.com');assert.equal(params.get('a2'),'+33601020304');assert.equal(params.has('a1'),false);
    assert.equal(params.get('location'),'+33601020304');assert.equal(t.$('#reviewRequest').disabled,true);
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
    await until(()=>t.widgets.length===1);const directory=new URL(t.widgets[0].url);
    assert.equal(directory.searchParams.get('email'),'camille@example.com');assert.equal(directory.searchParams.has('first_name'),false);assert.equal(directory.pathname,'/integraleacademy');
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
    const updated=new URL(t.widgets[1].url);
    assert.equal(updated.searchParams.get('email'),'updated@example.com');assert.equal(updated.searchParams.get('name'),"Élodie D’Angelo <test>");
    assert.ok(updated.search.includes('D%E2%80%99Angelo%20%3Ctest%3E'));t.click('#reviewRequest');assert.equal(t.step(),'6');
    assert.equal(t.calls.filter(c=>c.url==='/api/secretariat/demandes').length,0);t.close();
  }
  {
    const t=setup(); await caller(t,'formation');
    const visible=()=>[...t.d.querySelectorAll('[data-formation-code]')].filter(button=>!button.hidden);
    const course='PARCOURS_SECURITE_ALTERNANCE', selector='[data-formation-code="'+course+'"]';
    assert.equal(t.$('#nextFormation').disabled,true);
    t.click('[data-filter="alternance"]'); assert.ok(visible().some(b=>b.dataset.formationCode===course));
    assert.ok(visible().some(b=>b.dataset.formationCode==='BTS_MOS'));
    t.fill('formationSearch','securite parcours'); assert.deepEqual(visible().map(b=>b.dataset.formationCode),[course]);
    t.fill('formationSearch','inconnu'); assert.equal(t.$('#formationEmpty').hidden,false);
    t.click('#resetFormationFilters'); assert.equal(visible().length,13);
    t.click('[data-filter="transport"]'); assert.deepEqual(visible().map(b=>b.dataset.formationCode),['VTC']);
    t.click('[data-filter="security"]'); t.fill('formationSearch','apprentissage');
    t.$('#formationSearch').dispatchEvent(new t.w.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
    assert.equal(t.$(selector).getAttribute('aria-pressed'),'true');
    assert.match(t.$('#formationSelection').textContent,/Parcours sécurité en alternance/);
    t.click('[data-filter="bts"]'); assert.equal(t.$('#nextFormation').disabled,true);
    t.click('[data-filter="security"]'); assert.equal(t.$('#nextFormation').disabled,false);
    t.click('#nextFormation'); assert.equal(t.step(),'5');
    t.click('[data-step="5"] [data-back]'); assert.equal(t.step(),'3');
    assert.equal(t.$(selector).getAttribute('aria-pressed'),'true');
    t.click('#nextFormation');
    assert.equal(t.$('#trainingTitle').textContent,'Parcours sécurité en alternance');
    assert.equal(t.$('#trainingFacts').closest('details'),null); assert.equal(t.$('#trainingAiPanel').open,false);
    assert.match(t.$('#trainingFacts').textContent,/573 h/); assert.match(t.$('#trainingFacts').textContent,/0 €/);
    assert.match(t.$('#trainingFacts').textContent,/Puget-sur-Argens/);
    assert.match(t.$('#trainingTopics').textContent,/175 h/); assert.match(t.$('#trainingTopics').textContent,/328 h/);
    assert.match(t.$('#trainingTopics').textContent,/70 h/); assert.match(t.$('#trainingAdmission').textContent,/26 ans/);
    const january=t.$('[data-intake="Janvier 2027"]'), june=t.$('[data-intake="Juin 2027"]');
    assert.match(january.closest('label').textContent.replace(/\s/g,''),/1106h/);
    assert.match(june.closest('label').textContent.replace(/\s/g,''),/1099h/);
    t.click('[data-custom-intake]'); t.fill('rentreeSouhaitee','Une autre année');
    t.click('[data-intake="Juin 2027"]'); assert.equal(t.$('#rentreeSouhaitee').disabled,true);
    t.click('[data-step="5"] [data-back]'); t.click('#nextFormation'); assert.equal(june.checked,true);
    t.click('#nextDates');
    t.click('[name="wants_rdv"][value="oui"]'); await until(()=>t.widgets.length===1);
    const agenda='https://calendly.com/integraleacademy/parcourssecurite';
    const url=new URL(t.widgets[0].url); assert.equal(url.origin+url.pathname,agenda);
    assert.equal(t.$('#reviewRequest').disabled,true);
    t.w.dispatchEvent(new t.w.MessageEvent('message',{origin:'https://calendly.com',data:{event:'calendly.event_scheduled'}}));
    t.click('#reviewRequest'); t.fill('precision','Souhaite candidater en alternance'); t.click('#finish');
    await until(()=>t.step()==='8');
    const payload=t.calls.find(c=>c.url==='/api/secretariat/demandes').body;
    assert.equal(payload.formation,course); assert.equal(payload.calendly_url,agenda);
    assert.equal(payload.cpf_consulte,undefined); assert.equal(payload.rentree_souhaitee,'Juin 2027');
    assert.equal(payload.formation_date_souhaitee,''); assert.equal(payload.formation_date_status,'a_definir');
    assert.equal(payload.formation_centre,'cote_azur');
    assert.match(payload.rdv,/réservé/); t.close();
  }
  {
    const t=setup(); await caller(t,'formation');
    const codes=[...t.d.querySelectorAll('[data-formation-code]')].map(b=>b.dataset.formationCode);
    const profiles={
      PARCOURS_SECURITE_ALTERNANCE:'surveillance', A3P:'garde du corps', APS:'magasin',
      SSIAP:'sécurité incendie', DESP_INIT:'responsable d’exploitation', DESP_VAE:'faisabilité', VTC:'Chauffeur',
      BTS_MOS:'chef d’équipe', BTS_MCO:'responsable de rayon', BTS_NDRC:'animateur de réseau',
      BTS_CI:'agent de transit', BTS_PI:'copropriété', BTS_CG:'cabinet'
    };
    assert.deepEqual(new Set(codes),new Set(Object.keys(profiles)));
    for(const code of codes){
      t.click('[data-formation-code="'+code+'"]'); t.click('#nextFormation');
      assert.equal(t.step(),'5',code+' must open its sheet directly'); assert.equal(t.$('#trainingInfo').dataset.formation,code);
      assert.ok(t.$('#trainingTitle').textContent.trim()); assert.ok(t.$('#trainingPurpose').textContent.trim());
      assert.ok(t.$('#trainingFacts').children.length>=3); assert.ok(t.$('#trainingTopics').children.length>=3);
      assert.equal(t.$('#trainingAward').hidden,false); assert.ok(t.$('#trainingAdmission').children.length>=3);
      assert.ok(t.$('#trainingProfession').textContent.includes('Évaluation'));
      assert.ok(t.$('#trainingInfo').textContent.toLowerCase().includes(profiles[code].toLowerCase()),code);
      assert.doesNotMatch(t.$('#trainingInfo').textContent,/NaN|undefined/);
      assert.equal(t.$('#trainingAiPanel').open,false);
      if(code.startsWith('BTS_')){
        assert.ok(t.$('[data-intake="Septembre 2027"]'));assert.ok(t.$('#trainingAdmission').textContent.includes('Entreprises d’accueil'));
        assert.match(t.$('#trainingFacts').textContent,code==='BTS_MOS'?/15 jours/:/3 jours/);
        t.click('[data-intake="Septembre 2027"]');
      } else if(code==='PARCOURS_SECURITE_ALTERNANCE'){
        t.click('[data-intake="Juin 2027"]');
      } else if(code==='APS'){
        const choice=t.$('[data-date-examen="2026-12-31"]');
        assert.ok(choice);choice.click();assert.match(choice.closest('label').textContent,/Examen/);
        t.click('#nextDates');assert.equal(t.step(),'4');t.click('[data-step="4"] [data-back]');assert.equal(choice.checked,true);
      } else if(code==='VTC'||code==='DESP_VAE'){
        t.fill('rentreeSouhaitee','Mars 2027'); assert.equal(t.$('#rentreeSouhaitee').disabled,false);
        assert.equal(t.w.eval('callData(true).rentree_souhaitee'),'Mars 2027');
        assert.equal(t.w.eval('callData(true).formation_date_souhaitee'),'');
      }
      t.click('#nextDates');
      const alternate=code.startsWith('BTS_')||code==='PARCOURS_SECURITE_ALTERNANCE';
      assert.equal(t.step(),alternate?'6':'4',code);
      if(!alternate){
        t.click('#nextQualification');
        if(t.step()==='4'){
          for(const name of ['cpf_consulte','france_travail','financement_perso','identite_numerique','cnaps_ok','devis']){
            const input=t.$('[name="'+name+'"][value="'+(name==='cnaps_ok'?'OUI':'NON')+'"]');
            if(!input.disabled)input.click();
          }
          t.click('#nextQualification');
        }
        assert.equal(t.step(),'6',code+' requires completed qualification before booking');
      }
      t.click('[data-step="6"] [data-back]'); assert.equal(t.step(),alternate?'5':'4');
      t.click('[data-go-step="3"]'); assert.equal(t.step(),'3');
    }
    t.close();
  }
  console.log('All intake journeys passed without external requests.');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run(["node", "-e", script], input=rendered, text=True,
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
