from pathlib import Path
import json
import subprocess
from types import SimpleNamespace

import requests
import pytest

import app as application
import crm_app as production
import crm_cnaps_tracking


def client(tmp_path, monkeypatch, flask_app=application.app):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    flask_app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    test_client = flask_app.test_client()
    with test_client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return test_client


def contact(client):
    return client.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin"}).get_json()


def vae_contact(client, **overrides):
    lead = contact(client)
    values = {"formation": "DESP", "desp_type": "VAE", "mail": "lina@example.com",
              "telephone": "0600000000", **overrides}
    return client.patch(f"/api/crm/contacts/{lead['id']}", json=values).get_json()


def response(status, payload):
    return SimpleNamespace(status_code=status, content=b"{}", json=lambda: payload)


def configure(monkeypatch):
    monkeypatch.setenv("GESTION_STAGIAIRES_API_URL", "https://gestion.example/api/preremplissage?x=1")
    monkeypatch.setenv("GESTION_STAGIAIRES_API_TOKEN", "top-secret")


def test_app_entrypoint_calls_stagiaires_by_permanent_crm_id(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    lead = vae_contact(test_client)
    configure(monkeypatch)
    captured = {}
    payload = {
        "trainee": {"id": 7, "public_token": "remove"},
        "cnaps": {"status": "ACCEPTÉ", "authorization": "remove"},
        "card_pro": {"titles": [{"title": "AP SH", "trainee_token": "remove"}]},
        "vae": {"applicable": True, "nested": [{"api_token": "remove", "ok": True}]},
    }
    monkeypatch.setattr(application.requests, "get", lambda url, **kwargs: (captured.update(url=url, **kwargs) or response(200, payload)))

    result = test_client.get(f"/api/crm/contacts/{lead['id']}/reglementaire")

    assert result.status_code == 200
    assert captured["url"] == "https://gestion.example/api/integrations/crm/stagiaires"
    assert captured["params"] == {"crm_contact_id": lead["id"]}
    assert "nom" not in captured["params"] and "prenom" not in captured["params"]
    assert captured["headers"] == {"Authorization": "Bearer top-secret", "Accept": "application/json"}
    body = result.get_json()
    assert {"trainee", "cnaps", "card_pro", "vae", "scoring_snapshot", "integration_score"} <= set(body)
    assert body["vae"]["nested"] == [{"ok": True}]
    assert all(secret not in result.data for secret in (b"top-secret", b"public_token", b"trainee_token", b"api_token", b"authorization"))


def test_reglementaire_reuses_cache_and_manual_refresh_bypasses_it(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    lead = vae_contact(test_client, formation="APS")
    configure(monkeypatch)
    calls = []

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        return response(200, {"found": True, "cnaps": {"status": "TRANSMIS"}})

    monkeypatch.setattr(application.requests, "get", fake_get)
    url = f"/api/crm/contacts/{lead['id']}/reglementaire"

    first = test_client.get(url)
    cached = test_client.get(url)
    refreshed = test_client.get(f"{url}?refresh=1")

    assert first.status_code == cached.status_code == refreshed.status_code == 200
    assert first.get_json()["cached"] is False
    assert cached.get_json()["cached"] is True
    assert refreshed.get_json()["cached"] is False
    assert len(calls) == 2


def test_production_entrypoint_installs_same_proxy(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch, production.app)
    lead = vae_contact(test_client)
    configure(monkeypatch)
    captured = {}
    monkeypatch.setattr(crm_cnaps_tracking.requests, "get", lambda url, **kwargs: (captured.update(url=url, **kwargs) or response(200, {"vae": {"applicable": True}})))
    result = test_client.get(f"/api/crm/contacts/{lead['id']}/reglementaire")
    assert result.status_code == 200
    assert captured["url"].endswith("/api/integrations/crm/stagiaires")
    assert captured["params"] == {"crm_contact_id": lead["id"]}


def test_tracked_contact_404_links_once_and_uses_post_payload_directly(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch, production.app)
    lead = vae_contact(test_client)
    configure(monkeypatch)
    calls = {"get": 0, "post": 0}
    monkeypatch.setattr(crm_cnaps_tracking.requests, "get", lambda *args, **kwargs:
                        (calls.__setitem__("get", calls["get"] + 1) or response(404, {})))
    linked = {"ok": True, "linked": True, "trainee": {"token": "remove"},
              "cnaps": {"status": "ACTIF"}, "card_pro": {"titles": []},
              "vae": {"applicable": True, "progress_percent": 50}}
    captured = {}
    def fake_post(url, **kwargs):
        calls["post"] += 1
        captured.update(url=url, **kwargs)
        return response(200, linked)
    monkeypatch.setattr(crm_cnaps_tracking.requests, "post", fake_post)

    result = test_client.get(f"/api/crm/contacts/{lead['id']}/reglementaire")

    assert result.status_code == 200
    assert calls == {"get": 1, "post": 1}
    assert captured["url"] == "https://gestion.example/api/integrations/crm/stagiaires/link-existing"
    assert captured["json"] == {"crm_contact_id": lead["id"], "prenom": "Lina", "nom": "MARTIN",
                                "email": "lina@example.com", "telephone": "0600000000",
                                "source": "integrale_connect"}
    assert captured["headers"] == {"Authorization": "Bearer top-secret", "Accept": "application/json",
                                    "Content-Type": "application/json"}
    assert result.get_json()["vae"]["progress_percent"] == 50
    assert b"token" not in result.data and b"top-secret" not in result.data


def test_direct_get_on_next_consultation_never_posts(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    lead = vae_contact(test_client)
    configure(monkeypatch)
    monkeypatch.setattr(crm_cnaps_tracking.requests, "get", lambda *args, **kwargs:
                        response(200, {"trainee": {}, "cnaps": {}, "card_pro": {}, "vae": {"applicable": True}}))
    monkeypatch.setattr(crm_cnaps_tracking.requests, "post", lambda *args, **kwargs:
                        (_ for _ in ()).throw(AssertionError("POST inattendu")))
    assert test_client.get(f"/api/crm/contacts/{lead['id']}/reglementaire").status_code == 200


@pytest.mark.parametrize("formation", ["APS", "A3P"])
def test_cnaps_training_uses_name_tracking_without_linking(tmp_path, monkeypatch, formation):
    test_client = client(tmp_path, monkeypatch)
    configure(monkeypatch)
    lead = vae_contact(test_client, formation=formation, nom="  Dùpré   Martin ",
                       prenom=" ÉLise  ", mail="", telephone="")
    captured = {}
    payload = {
        "found": True,
        "cnaps": {"status": "Aucun titre CNAPS trouvé"},
        "nub": "7654321",
        "inscription": "Non inscrite",
        "trainee": {"nom": "DUPRE MARTIN", "prenom": "Élise"},
        "source_url": "https://gestion.example/admin/sessions/suivi-cnaps",
    }
    monkeypatch.setattr(crm_cnaps_tracking.requests, "get", lambda url, **kwargs:
                        (captured.update(url=url, **kwargs) or response(200, payload)))
    monkeypatch.setattr(crm_cnaps_tracking.requests, "post", lambda *args, **kwargs:
                        (_ for _ in ()).throw(AssertionError("POST /link-existing inattendu")))

    result = test_client.get(f"/api/crm/contacts/{lead['id']}/reglementaire")

    assert result.status_code == 200
    assert captured["url"] == "https://gestion.example/api/integrations/crm/cnaps-tracking"
    assert captured["params"] == {"nom": "dupre martin", "prenom": "elise"}
    assert captured["headers"]["Authorization"] == "Bearer top-secret"
    assert payload.items() <= result.get_json().items()
    assert {"scoring_snapshot", "integration_score"} <= result.get_json().keys()


def test_cnaps_route_projects_production_status_into_integration_score(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    configure(monkeypatch)
    lead = vae_contact(test_client, formation="A3P", cpf="OUI", cpf_montant="1995",
                       identite_creation="OUI", identite_ok="OUI", financement_ft="NON",
                       refus_ft_perso="OUI", inscrit_ft="NON", carte_pro="NON",
                       compte_cnaps="NON", antecedents="NON", titre_sejour_cnaps="NON_CONCERNE")
    payload = {
        "found": True,
        "linked": True,
        "cnaps": {"cnaps_status": "TRANSMIS", "nub": "1084892", "titles": []},
    }
    monkeypatch.setattr(crm_cnaps_tracking.requests, "get", lambda *args, **kwargs: response(200, payload))

    result = test_client.get(f"/api/crm/contacts/{lead['id']}/reglementaire")

    assert result.status_code == 200
    body = result.get_json()
    assert body["scoring_snapshot"]["normalized_status"] == "transmitted"
    assert body["scoring_snapshot"]["raw_status"] == "TRANSMIS"
    assert body["integration_score"]["normalized_cnaps_status"] == "transmitted"
    assert body["integration_score"]["regulatory_score"] == 40
    assert body["integration_score"]["financial_score"] == 57
    assert body["integration_score"]["score"] == 50


def test_only_desp_vae_404_is_linked(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    configure(monkeypatch)
    monkeypatch.setattr(crm_cnaps_tracking.requests, "get", lambda *args, **kwargs: response(404, {}))
    posts = []
    monkeypatch.setattr(crm_cnaps_tracking.requests, "post", lambda *args, **kwargs:
                        (posts.append(kwargs) or response(200, {"vae": {}})))
    lead = vae_contact(test_client, formation="DESP", desp_type="VAE", nom="Martin VAE")
    assert test_client.get(f"/api/crm/contacts/{lead['id']}/reglementaire").status_code == 200
    lead = vae_contact(test_client, formation="DESP", desp_type="INITIAL", nom="Martin Initial")
    assert test_client.get(f"/api/crm/contacts/{lead['id']}/reglementaire").status_code == 404
    lead = vae_contact(test_client, mail="", telephone="", nom="Martin Incomplet")
    result = test_client.get(f"/api/crm/contacts/{lead['id']}/reglementaire")
    assert result.status_code == 422
    assert result.get_json()["reason"] == "insufficient_identity"
    assert len(posts) == 1


def test_cnaps_not_found_has_dedicated_message(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    configure(monkeypatch)
    lead = vae_contact(test_client, formation="APS")
    monkeypatch.setattr(crm_cnaps_tracking.requests, "get", lambda *args, **kwargs: response(404, {}))
    result = test_client.get(f"/api/crm/contacts/{lead['id']}/reglementaire")
    assert result.status_code == 404
    body = result.get_json()
    assert body["error"] == "Aucun dossier CNAPS correspondant à ce nom et ce prénom n’a été trouvé dans le suivi CNAPS."
    assert body["reason"] == "cnaps_not_found"
    assert body["scoring_snapshot"]["normalized_status"] == "no_result"
    assert "integration_score" in body


def test_cnaps_fix_contains_no_contact_specific_data():
    sources = "\n".join(
        (Path(application.app.root_path) / filename).read_text(encoding="utf-8")
        for filename in ("crm_cnaps_tracking.py", "static/crm.js")
    ).casefold()
    for specific_value in ("ronan", "godal", "1084892", "0686122220", "ronan.godal@icloud.com"):
        assert specific_value not in sources


def test_linking_reasons_are_preserved_without_remote_details(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    lead = vae_contact(test_client)
    configure(monkeypatch)
    monkeypatch.setattr(crm_cnaps_tracking.requests, "get", lambda *args, **kwargs: response(404, {}))
    reasons = ("trainee_not_found", "conflicting_matches", "ambiguous_match", "identity_mismatch",
               "crm_contact_id_already_used", "trainee_already_linked")
    for reason in reasons:
        monkeypatch.setattr(crm_cnaps_tracking.requests, "post", lambda *args, _reason=reason, **kwargs:
                            response(409, {"reason": _reason, "error": "technical", "api_token": "leak"}))
        result = test_client.get(f"/api/crm/contacts/{lead['id']}/reglementaire")
        assert result.status_code == 409
        assert result.get_json() == {"error": "Le rattachement automatique du stagiaire a échoué.", "reason": reason}


def test_linking_auth_timeout_and_invalid_json_are_safe(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    lead = vae_contact(test_client)
    configure(monkeypatch)
    monkeypatch.setattr(crm_cnaps_tracking.requests, "get", lambda *args, **kwargs: response(404, {}))
    cases = [
        (lambda *a, **k: response(401, {"error": "raw"}), 401, "L’intégration Gestion Stagiaires n’est pas correctement configurée."),
        (lambda *a, **k: (_ for _ in ()).throw(requests.Timeout()), 502, "Gestion Stagiaires est momentanément indisponible"),
        (lambda *a, **k: response(200, []), 502, "Réponse invalide de Gestion Stagiaires"),
    ]
    for post, status, message in cases:
        monkeypatch.setattr(crm_cnaps_tracking.requests, "post", post)
        result = test_client.get(f"/api/crm/contacts/{lead['id']}/reglementaire")
        assert result.status_code == status
        assert result.get_json()["error"] == message


def test_remote_business_statuses_are_preserved(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    lead = contact(test_client)
    lead = test_client.patch(f"/api/crm/contacts/{lead['id']}",
                             json={"formation": "Chauffeur VTC"}).get_json()
    configure(monkeypatch)
    for status in (400, 401, 404, 409):
        monkeypatch.setattr(crm_cnaps_tracking.requests, "get", lambda *args, _status=status, **kwargs: response(_status, {"error": "remote technical detail", "token": "leak"}))
        result = test_client.get(f"/api/crm/contacts/{lead['id']}/reglementaire")
        assert result.status_code == status
        assert b"remote technical detail" not in result.data
        assert b"leak" not in result.data


def test_timeout_and_remote_unavailability_are_safe(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    lead = contact(test_client)
    configure(monkeypatch)
    for error in (requests.Timeout(), requests.ConnectionError()):
        monkeypatch.setattr(crm_cnaps_tracking.requests, "get", lambda *args, _error=error, **kwargs: (_ for _ in ()).throw(_error))
        result = test_client.get(f"/api/crm/contacts/{lead['id']}/reglementaire")
        assert result.status_code == 502
        assert result.get_json() == {"error": "Gestion Stagiaires est momentanément indisponible"}


def test_frontend_has_shared_cnaps_vae_loading_and_safe_rendering():
    javascript = (Path(application.app.root_path) / "static/crm.js").read_text(encoding="utf-8")
    assert "const isDespVae=" in javascript
    assert "['APS','A3P']" in javascript
    assert "needsCnaps(c)&&!isDespVae(c)" in javascript
    assert javascript.count("/reglementaire${") == 1
    assert "force?'?refresh=1':''" in javascript
    assert "actionDates=vae.action_dates||{}" in javascript
    assert "scotia=vae.scotia||{}" in javascript
    assert "button.onclick=()=>loadReglementaire(c,true)" in javascript
    assert "if(regulatorySection.open)loadReglementaireOnce()" in javascript
    assert "if(selected.id==='contactVaeTab')loadReglementaireOnce()" in javascript
    assert "dossier.multiple_dossiers===true" in javascript
    assert "result.code||vae.status_code" in javascript
    assert "renderVaeDisplayError(c)" in javascript
    for text in ("Suivi du dossier VAE", "Récupération du suivi VAE…", "Aucun dossier VAE administratif", "Plusieurs dossiers VAE", "Des compléments sont demandés", "Diplôme obtenu"):
        assert text in javascript
    assert "vae.applicable===false" in javascript
    assert "target=\"_blank\" rel=\"noopener noreferrer\"" in javascript
    assert "dossier.admin_url:vae.trainee_admin_url" in javascript
    assert "GESTION_STAGIAIRES_API_TOKEN" not in javascript
    assert "loadScotia" not in javascript
    assert "loadVae" not in javascript
    assert "error.reason=payload.reason" in javascript
    assert "renderGestionError(c,error.status,error.reason)" in javascript
    assert "c.integration_score=data.integration_score" in javascript
    assert "contacts.find(item=>item.id===c.id)" in javascript
    assert "storedContact.integration_score=data.integration_score" in javascript
    assert "renderIntegrationScore(c)" in javascript
    assert "transmitted:'Transmis'" in javascript
    assert "Aucun dossier CNAPS correspondant à ce nom et ce prénom" in javascript
    assert "<span>NUB</span>" in javascript
    assert "<span>Inscription</span>" in javascript
    for reason in ("trainee_not_found", "conflicting_matches", "ambiguous_match", "identity_mismatch",
                   "crm_contact_id_already_used", "trainee_already_linked", "insufficient_identity"):
        assert reason in javascript
    for message in (
        "Aucun stagiaire correspondant exactement à cette piste n’a été trouvé",
        "L’adresse e-mail et le téléphone correspondent à deux stagiaires différents",
        "Plusieurs stagiaires correspondent à cette piste",
        "le nom ou le prénom ne correspond pas",
        "déjà rattachée à un autre stagiaire",
        "déjà rattaché à une autre piste CRM",
        "nécessite le nom, le prénom et au moins une adresse e-mail ou un téléphone",
    ):
        assert message in javascript


def test_vae_javascript_renders_french_and_iso_dates_without_second_request():
    javascript = (Path(application.app.root_path) / "static/crm.js").read_text(encoding="utf-8")
    vae_code = javascript[javascript.index("const vaeDates="):javascript.index("function renderGestionError")]
    payload = {
        "applicable": True, "status_code": "livret_2_analysis", "status_label": "Réception livret 2",
        "progress_percent": 65, "is_terminal": False, "is_success": False, "is_blocked": False,
        "next_action": {"code": "analyse_livret_2", "label": "Analyser le Livret 2"},
        "updated_at": "20/09/2026",
        "action_dates": {"livret_1_received_at": "27/07/2026", "livret_1_validated_at": "28/07/2026 à 16h30",
                         "livret_1_transmitted_scotia_at": None, "livret_2_received_at": "2026-08-04T09:32:00+02:00",
                         "livret_2_validated_at": None, "livret_2_transmitted_scotia_at": None, "diploma_obtained_at": None},
        "recevabilite": {"status_code": "recevable", "status_label": "Recevable", "attestation_available": False},
        "jury": {"scheduled": True, "date": "15/09/2026", "location": None},
        "final_result": {"code": None, "label": None, "diploma_obtained_at": None},
        "dossier": {"found": True, "status_label": "Soumis", "updated_at": "04/08/2026 à 09h32",
                    "dossier_count": 2, "multiple_dossiers": True,
                    "admin_url": "https://gestionstagiaires-r5no.onrender.com/admin/vae/test"},
        "scotia": {"status_label": "En attente <documents>", "status_tone": "warning",
                   "comment": '<script>alert("xss")</script>\nDeuxième ligne'},
        "trainee_admin_url": "https://gestionstagiaires-r5no.onrender.com/admin/sessions/test/stagiaires/test",
    }
    node_script = f"""
const panel={{innerHTML:'',className:''}};
const document={{querySelector:(selector)=>selector==='#vaeTrackingPanel'?panel:null,querySelectorAll:()=>[]}};
const esc=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
function safeAdminUrl(value){{try{{const url=new URL(String(value||''));return ['http:','https:'].includes(url.protocol)?url.href:''}}catch(_){{return''}}}}
function bindSharedRefresh(_){{}}
{vae_code}
const payload={json.dumps(payload, ensure_ascii=False)};
renderVaeTracking({{}},payload);
const valid=panel.innerHTML;
payload.dossier.multiple_dossiers=false;
payload.scotia.status_tone='classe-inconnue';
renderVaeTracking({{}},payload);
const unknownTone=panel.innerHTML;
delete payload.scotia;
delete payload.dossier;
renderVaeTracking({{}},payload);
const missing=panel.innerHTML;
payload.updated_at='valeur-invalide';
renderVaeTracking({{}},payload);
process.stdout.write(JSON.stringify({{valid,unknownTone,missing,invalid:panel.innerHTML}}));
"""
    completed = subprocess.run(["node", "-e", node_script], check=True, capture_output=True, text=True)
    rendered = json.loads(completed.stdout)

    for expected in ("Réception livret 2", "65 %", "27 juil. 2026", "28 juil. 2026, 16:30",
                     "4 août 2026, 07:32", "15 sept. 2026", "Plusieurs dossiers VAE sont liés (2)"):
        assert expected in rendered["valid"]
    for forbidden in ("undefined", "null", "Invalid Date", "Gestion Stagiaires est momentanément indisponible"):
        assert forbidden not in rendered["valid"]
    assert "Statut SCOTIA" in rendered["valid"]
    assert "En attente &lt;documents&gt;" in rendered["valid"]
    assert "vae-scotia-tone-warning" in rendered["valid"]
    assert "Commentaire SCOTIA" in rendered["valid"]
    assert '&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;\nDeuxième ligne' in rendered["valid"]
    assert '<script>alert("xss")</script>' not in rendered["valid"]
    assert "Plusieurs dossiers VAE" not in rendered["unknownTone"]
    assert "vae-scotia-tone-neutral" in rendered["unknownTone"]
    assert rendered["missing"].count("—") >= 2
    assert "Statut SCOTIA" in rendered["missing"] and "Commentaire SCOTIA" in rendered["missing"]
    assert "Date non disponible" in rendered["invalid"]
    assert javascript.count("/reglementaire${") == 1


def test_cnaps_credentials_can_be_generated_autofilled_and_copied():
    javascript = (Path(application.app.root_path) / "static/crm.js").read_text(encoding="utf-8")
    stylesheet = (Path(application.app.root_path) / "static/crm.css").read_text(encoding="utf-8")
    helpers = javascript[
        javascript.index("function cnapsBirthYear"):
        javascript.index("function selectHtml")
    ]
    node_script = helpers + r"""
const cases=[
 [{nom:'Durand',cnaps_birth_year:'1995'},'Durand1995@'],
 [{nom:'Vaillant',date_naissance:'1993-05-04'},'Vaillant1993@'],
 [{nom:'ÉLODIE-DUPONT',meta_answers:[{question:'Date de naissance',answer:'01/07/1988'}]},'Elodiedupont1988@'],
 [{nom:'Martin',birth_date:'2099-01-01'},''],
 [{nom:'',dateOfBirth:'1990-01-01'},'']
];
for(const [contact,expected] of cases){
 const actual=cnapsGeneratedPassword(contact);
 if(actual!==expected)throw new Error(JSON.stringify({contact,actual,expected}));
}
console.log('CRM CNAPS credentials: OK');
"""
    completed = subprocess.run(
        ["node", "-e", node_script], check=True, capture_output=True, text=True
    )

    assert "CRM CNAPS credentials: OK" in completed.stdout
    assert 'id="cnapsUsernameCopy"' in javascript
    assert 'label for="cnapsBirthYear">Année de naissance</label>' in javascript
    assert 'name="cnaps_birth_year"' in javascript
    assert "cnapsUsernameField?.insertAdjacentHTML('afterend'" in javascript
    assert 'id="cnapsPasswordGenerate"' in javascript
    assert 'id="cnapsPasswordCopy"' in javascript
    assert "await copyContactCoordinate(input.value)" in javascript
    assert "cnapsPassword.dispatchEvent(new Event('input',{bubbles:true}))" in javascript
    assert "cnaps_birth_year:cnapsBirthYearInput?.value||''" in javascript
    assert "form.compte_cnaps?.value==='OUI'" in javascript
    assert "!cnapsUsername.value.trim()&&form.mail.value.trim()" in javascript
    assert "form.addEventListener('input',autofillCnapsUsername)" in javascript
    assert "cnapsUsername.dispatchEvent(new Event('input',{bubbles:true}))" in javascript
    binding = javascript[javascript.index("const autofillCnapsUsername="):]
    assert binding.index("form.oninput=e=>") < binding.index("autofillCnapsUsername();")
    autofill_source = binding[:binding.index("\n")]
    autosave_script = r"""
let saves=0;
const listeners=[];
class Event { constructor(type, options){ this.type=type; this.bubbles=options?.bubbles } }
const form={
 compte_cnaps:{value:'OUI'},
 mail:{value:'lina@example.com'},
 addEventListener:(type, listener)=>listeners.push(listener),
 oninput:null
};
const cnapsUsername={
 value:'',
 dispatchEvent:event=>{
  for(const listener of listeners)listener(event);
  if(form.oninput)form.oninput(event);
 }
};
""" + autofill_source + r"""
form.addEventListener('input',autofillCnapsUsername);
form.oninput=()=>{saves+=1};
autofillCnapsUsername();
if(cnapsUsername.value!=='lina@example.com'||saves!==1){
 throw new Error(JSON.stringify({value:cnapsUsername.value,saves}));
}
console.log('CRM CNAPS autosave: OK');
"""
    autosave = subprocess.run(
        ["node", "-e", autosave_script], check=True, capture_output=True, text=True
    )
    assert "CRM CNAPS autosave: OK" in autosave.stdout
    assert ".cnaps-field-actions{" in stylesheet
