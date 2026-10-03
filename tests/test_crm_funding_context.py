"""Financing notes remain visible without inventing structured answers."""

import copy
import json
import subprocess
from pathlib import Path

import pytest

from candidate_scoring import calculate_candidate_integration_score


ROOT = Path(__file__).resolve().parents[1]


def run_funding_js(contact):
    source = (ROOT / "static/crm.js").read_text(encoding="utf-8")
    helpers = source[source.index("const sectionValues="):source.index("function publicationCard(")]
    script = """const esc=value=>String(value??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;').replace(/'/g,'&#39;');
const fmt=value=>value;
""" + helpers + "\nconst contact=" + json.dumps(contact) + ";\n" + """
const before=JSON.stringify(contact);
console.log(JSON.stringify({summary:fundingSummary(contact),markup:fundingSourceNoteMarkup(contact),unchanged:before===JSON.stringify(contact)}));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def test_callback_note_is_visible_but_does_not_supply_a_score_or_amount():
    contact = {
        "formation": "DESP", "desp_type": "VAE", "origine": "Calendly",
        "publications": [{
            "source": "assistant-secretariat", "date": "2026-09-20T10:00:00+02:00",
            "texte": "Projet VAE. Souhaite mobiliser son CPF et ses points de pénibilité.",
        }],
    }
    before = copy.deepcopy(contact)
    display = run_funding_js(contact)
    score = calculate_candidate_integration_score(contact)

    assert "compte rendu" in display["summary"]
    assert "points de pénibilité" in display["markup"]
    assert "montants non précisés restent à vérifier" in display["markup"]
    assert display["unchanged"] and contact == before
    assert score["score"] is None
    assert score["cpf_amount_eur"] is None
    assert score["unsecured_amount_eur"] is None


def test_latest_secretary_funding_note_is_escaped_and_unrelated_notes_are_ignored():
    display = run_funding_js({"publications": [
        {"source": "assistant-secretariat", "date": "2026-09-18", "texte": "Ancien financement"},
        {"source": "assistant-secretariat", "date": "2026-09-21", "texte": "Demande de changement d’horaire"},
        {"source": "manual", "date": "2026-09-22", "texte": "Autre financement"},
        {"source": "assistant-secretariat", "date": "2026-09-20", "texte": 'CPF <img src=x onerror="alert(1)">'},
    ]})
    assert "&lt;img" in display["markup"]
    assert "<img" not in display["markup"]
    assert "Ancien financement" not in display["markup"]
    assert "Autre financement" not in display["markup"]
    assert display["unchanged"]


@pytest.mark.parametrize("publications", [None, [], [{"source": "assistant-secretariat", "texte": "Merci de rappeler"}]])
def test_no_note_or_an_unrelated_callback_does_not_claim_financing_information(publications):
    display = run_funding_js({"publications": publications})
    assert display["summary"] == "Financement à renseigner"
    assert display["markup"] == ""


@pytest.mark.parametrize("answers", [
    {"cpf": "OUI", "cpf_montant": "0"},
    {"cpf": "NON"},
    {"financement_ft": "OUI"},
    {"financement_perso_possible": "OUI"},
    {"statut_demande_financement_ft": "transmise"},
])
def test_explicit_financing_answers_keep_the_existing_numeric_score(answers):
    result = calculate_candidate_integration_score({"formation": "DESP", "desp_type": "VAE", **answers})
    assert isinstance(result["score"], int)


def test_financing_note_is_rendered_inside_the_financing_section():
    source = (ROOT / "static/crm.js").read_text(encoding="utf-8")
    section = source[source.index('class="form-section section-wallet funding-section"'):source.index("${metaAnswersSection(c)}")]
    assert "${fundingSourceNoteMarkup(c)}" in section
