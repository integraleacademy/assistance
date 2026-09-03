import copy
import re
import subprocess
import unicodedata
from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_PY = ROOT / "app.py"
CRM_JS = ROOT / "static" / "crm.js"
WORKSPACE_JS = ROOT / "static" / "crm_workspace.js"
CRM_CSS = ROOT / "static" / "crm.css"
WORKSPACE_CSS = ROOT / "static" / "crm_workspace.css"


def origin_namespace():
    backend = APP_PY.read_text(encoding="utf-8")
    definitions = backend[
        backend.index("CRM_ORIGIN_SOURCE_LABELS = {"):
        backend.index("def _crm_information_request_origin")
    ]
    namespace = {
        "copy": copy,
        "re": re,
        "unicodedata": unicodedata,
        "_crm_now": lambda: "2026-08-23T12:00:00+02:00",
    }
    exec(definitions, namespace)
    return namespace


def test_first_detected_origin_stays_primary_and_secondary_is_idempotent():
    record = origin_namespace()["_crm_record_origin"]
    contact = {
        "origine": "Google Ads",
        "source": "demande_infos_formations",
        "created_at": "2026-08-23T09:00:00+02:00",
    }

    assert record(
        contact,
        "Secrétariat",
        source="assistant-secretariat",
        external_id="call-42",
        date="2026-08-23T10:00:00+02:00",
    )
    assert not record(
        contact,
        "secretariat",
        source="assistant-secretariat",
        external_id="call-42",
        date="2026-08-23T10:00:00+02:00",
    )

    assert contact["origine"] == "Google Ads"
    assert [entry["origin"] for entry in contact["source_history"]] == [
        "Google Ads",
        "Secrétariat",
    ]
    assert contact["source_history"][1]["external_id"] == "call-42"


def test_google_ads_then_vae_keeps_chronological_origin_order():
    record = origin_namespace()["_crm_record_origin"]
    contact = {
        "origine": "Google Ads",
        "source": "demande_infos_formations",
        "created_at": "2026-08-23T09:00:00+02:00",
        "source_history": [{"origin": "Google", "date": "2026-08-23T09:00:00+02:00"}],
    }

    record(
        contact,
        "Simulateur VAE",
        source="simulateur_vae_desp",
        external_id="vae-7",
        date="2026-08-23T11:00:00+02:00",
    )

    assert contact["origine"] == "Google Ads"
    assert [entry["origin"] for entry in contact["source_history"]] == [
        "Google Ads",
        "Simulateur VAE",
    ]


def test_historical_duplicate_origins_are_collapsed_without_losing_context():
    record = origin_namespace()["_crm_record_origin"]
    contact = {
        "origine": "Google Ads",
        "created_at": "2026-08-20T08:00:00+02:00",
        "source_history": [
            {"origin": "Google", "date": "2026-08-20T08:00:00+02:00"},
            {"origin": "google ads", "campaign": "Rentrée", "date": "2026-08-21T08:00:00+02:00"},
            {"origin": "Secrétariat", "date": "2026-08-22T08:00:00+02:00"},
        ],
    }

    assert record(contact, "Secrétariat", source="assistant-secretariat")
    assert [entry["origin"] for entry in contact["source_history"]] == [
        "Google Ads",
        "Secrétariat",
    ]
    assert contact["source_history"][0]["campaign"] == "Rentrée"


def test_inbound_flows_summary_merge_and_wedof_use_the_shared_origin_recorder():
    backend = APP_PY.read_text(encoding="utf-8")

    assert 'proposed.get("origine") or payload.get("origine")' in backend
    assert 'f"Origine secondaire : {_crm_canonical_origin(incoming_origin)}"' in backend
    assert '"origine", "source", "source_detail", "source_history", "commercial"' in backend
    assert 'target, source.get("origine") or source.get("source")' in backend
    assert 'source="wedof_cpf"' in backend
    assert 'contact["origine"] = "Mon Compte Formation"' not in backend
    attribution = backend[
        backend.index("def _crm_apply_information_request_attribution"):
        backend.index("def _crm_backfill_information_request_attribution")
    ]
    assert 'make_primary=legacy_primary_repair' in attribution
    assert 'contact["origine"] = CRM_GOOGLE_ADS_ORIGIN' not in attribution
    assert 'CRM_ASSET_VERSION = "20260903-remove-duplicate-cpf-badge-1"' in backend
    dashboard_patch = (ROOT / "static" / "crm_dashboard_origins.js").read_text(encoding="utf-8")
    assert "return gclid ? 'Google Ads'" not in dashboard_patch


def test_workspace_exposes_only_the_primary_origin():
    javascript = WORKSPACE_JS.read_text(encoding="utf-8")
    helpers = javascript[
        javascript.index("const workspaceOriginOptions="):
        javascript.index("function nextAction")
    ]
    script = f"""
const normalize=value=>String(value||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase().trim();
{helpers}
const assert=(condition,message)=>{{if(!condition)throw new Error(message)}};
const contact={{
 origine:'Google Ads',
 gclid:'late-click',
 source_history:[
  {{origin:'Google Ads'}},
  {{origin:'Secrétariat'}},
  {{origin:'Simulateur VAE'}},
  {{origin:'secretariat'}},
 ],
}};
assert(sourceLabel(contact)==='Google Ads','primary origin preserved');
assert(JSON.stringify(sourceLabels(contact))===JSON.stringify(['Google Ads']),'secondary history stays hidden');
const laterAds={{origine:'Secrétariat',gclid:'late-click',source_history:[{{origin:'Secrétariat'}},{{origin:'Google Ads'}}]}};
assert(sourceLabel(laterAds)==='Secrétariat','late gclid does not replace primary');
assert(JSON.stringify(sourceLabels(laterAds))===JSON.stringify(['Secrétariat']),'late Google Ads history is not exposed');
const abandoned={{origine:'Formulaire abandonné',source_history:[{{origin:'formulaire_abandonne_demande_infos'}}]}};
assert(sourceLabel(abandoned)==='Pistes abandonnées','abandoned form origin is canonicalized');
assert(workspaceLeadOriginOptions.includes('Pistes abandonnées'),'abandoned leads stay available in the workspace filter');
console.log('CRM multiple origins helpers: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM multiple origins helpers: OK" in completed.stdout
    assert "ctx.contacts.map(sourceLabel)" in javascript
    assert "sourceLabel(contact)!==filters.origin" in javascript
    assert "sourceLabel(contact)===calendarFilterOrigin.value" in javascript
    assert "Principale" in javascript
    assert "<small>Secondaire</small>" not in javascript
    assert "<span>ORIGINE PRINCIPALE</span>${originBadge(contact,ctx)}" in javascript
    assert "Origine utilisée sur l’ensemble du CRM." in javascript
    assert "source_history" not in helpers


def test_abandoned_form_origin_is_available_in_native_lead_filter():
    javascript = CRM_JS.read_text(encoding="utf-8")
    helpers = javascript[
        javascript.index("const crmOriginFilterValues="):
        javascript.index("const dashboardHasContact=")
    ]
    script = helpers + r"""
const assert=(condition,message)=>{if(!condition)throw new Error(message)};
const abandoned={
 origine:'Formulaire abandonné',
 source_history:[{origin:'formulaire_abandonne_demande_infos'}],
};
assert(canonicalCrmOrigin(abandoned)==='Pistes abandonnées','historical value is canonicalized');
assert(crmOriginLabels(abandoned).includes('Pistes abandonnées'),'the lead matches the filter');
assert(leadOriginFilterOptions().includes('Pistes abandonnées'),'the option is visible');
assert(canonicalCrmOriginValue('Google Ads')==='Google Ads','other origins stay unchanged');
console.log('CRM abandoned leads origin filter: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM abandoned leads origin filter: OK" in completed.stdout
    assert "const crmLeadOriginFilterValues=" in javascript
    assert "function leadOriginFilterOptions(){return[...crmLeadOriginFilterValues]}" in javascript


def test_native_list_and_contact_sheet_show_only_primary_origin_accessibly():
    javascript = CRM_JS.read_text(encoding="utf-8")
    crm_css = CRM_CSS.read_text(encoding="utf-8")
    workspace_css = WORKSPACE_CSS.read_text(encoding="utf-8")

    assert "function crmOriginLabels(contact)" in javascript
    assert "canonicalCrmOrigin(c)===origin" in javascript
    assert 'aria-label="Origine principale de la piste"' in javascript
    assert 'class="field full contact-origin-history"' not in javascript
    assert "source_history" not in javascript
    for stylesheet in (crm_css, workspace_css):
        assert ".workspace-origin-group{" in stylesheet
        assert ".workspace-origin-badge.secondary{" not in stylesheet
        assert "@media(max-width:650px)" in stylesheet
