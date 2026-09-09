import json
from pathlib import Path
import subprocess

import pytest

from candidate_scoring import calculate_candidate_integration_score


BASE_CONTACT = {
    "formation": "A3P", "cpf": "OUI", "financement_ft": "OUI",
    "carte_pro": "NON",
}


def test_high_cpf_tier_is_valued_without_confirming_the_complement_or_cnaps():
    result = calculate_candidate_integration_score({
        **BASE_CONTACT, "cpf_palier": "3 000 à 4 000 €",
    }, {"found": False})

    assert result["score"] == 58
    assert result["financial_score"] == 72
    assert result["financial_weight"] == 80
    assert result["regulatory_weight"] == 20
    assert result["regulatory_score"] is None
    assert result["cpf_amount_eur"] is None
    assert result["cpf_coverage_min_percent"] == 71
    assert result["cpf_coverage_max_percent"] == 95
    assert result["remaining_to_finance_min_eur"] == 200
    assert result["remaining_to_finance_max_eur"] == 1200
    assert result["unsecured_amount_eur"] == 1200
    assert result["score_estimated"] is True
    assert result["score_complete"] is False
    assert result["operational_status"] == "action_required"
    assert result["france_travail_awarded_amount_eur"] is None
    assert any("France Travail" in action for action in result["next_actions"])


@pytest.mark.parametrize("updates", [
    {},
    {"inscrit_ft": "OUI", "statut_demande_financement_ft": "en_cours_instruction"},
    {"statut_demande_financement_ft": "acceptee", "montant_accorde_ft": "1000"},
    {"financement_ft": "NON", "financement_perso_possible": "OUI"},
    {"financement_ft": "NON", "financement_perso_possible": "OUI", "reste_a_charge_perso": "OUI"},
])
def test_more_cpf_never_lowers_score_including_rounding_and_full_coverage(updates):
    previous = -1
    for amount in range(0, 4202):
        result = calculate_candidate_integration_score({
            **BASE_CONTACT, **updates, "cpf_montant": str(amount),
        })
        assert previous <= result["score"] <= 100, (amount, previous, result)
        previous = result["score"]
        rows = result["financial_breakdown"]
        assert sum(row["points"] for row in rows) == result["financial_score"]
        assert sum(row["max_points"] for row in rows) == 100
        assert all(0 <= row["points"] <= row["max_points"] for row in rows)


@pytest.mark.parametrize("tier,lower", [
    ("0 à 1000 euros", 0), ("1000 à 2000 euros", 1000),
    ("2000 à 3000 euros", 2000), ("3000 à 4000 euros", 3000),
    ("Plus de 4000 euros", 4000),
])
def test_tier_score_matches_its_lower_bound(tier, lower):
    estimated = calculate_candidate_integration_score({**BASE_CONTACT, "cpf_palier": tier})
    exact = calculate_candidate_integration_score({**BASE_CONTACT, "cpf_montant": str(lower)})
    assert estimated["financial_score"] == exact["financial_score"]
    assert estimated["score"] == exact["score"]


def test_identical_cpf_is_valued_relative_to_each_training_price():
    results = [calculate_candidate_integration_score({
        **BASE_CONTACT, "formation": training, "cpf_montant": "1500",
    }) for training in ("Chauffeur VTC", "APS", "A3P")]
    assert [r["cpf_coverage_percent"] for r in results] == [100, 91, 36]
    assert results[0]["financial_score"] > results[1]["financial_score"] > results[2]["financial_score"]


def test_pending_france_travail_only_advances_the_unfunded_part():
    scores = []
    for amount in (0, 3000, 4200):
        initial = calculate_candidate_integration_score({**BASE_CONTACT, "cpf_montant": str(amount)})
        pending = calculate_candidate_integration_score({
            **BASE_CONTACT, "cpf_montant": str(amount),
            "statut_demande_financement_ft": "en_cours_instruction",
        })
        assert initial["unsecured_amount_eur"] == pending["unsecured_amount_eur"] == 4200 - amount
        scores.append(pending["financial_score"] - initial["financial_score"])
    assert scores[0] > scores[1] > scores[2] == 0


def test_score_card_renders_backend_weights_and_unknown_cnaps():
    source = Path("static/crm.js").read_text(encoding="utf-8")
    renderer = "function renderIntegrationScore(c)" + source.split(
        "function renderIntegrationScore(c)", 1
    )[1].split("const aiTiming=", 1)[0]
    score = calculate_candidate_integration_score({**BASE_CONTACT, "cpf_palier": "3000 à 4000 euros"})
    script = """
const assert=require('node:assert/strict');
const card={};
const document={querySelector:selector=>selector==='#integrationScoreCard'?card:null};
const renderContactHeaderScore=()=>{};
const hasIntegrationScore=()=>true;
const esc=value=>String(value);
const euro=value=>String(value);
const euroRange=(low,high)=>`${low}–${high}`;
""" + renderer + "\nrenderIntegrationScore({integration_score:" + json.dumps(score) + "});\n" + r"""
assert.match(card.innerHTML,/Contribution : 58 \/ 80/);
assert.match(card.innerHTML,/Contribution minimale provisoire : 0 \/ 20/);
assert.match(card.innerHTML,/71–95 %/);
assert.match(card.innerHTML,/200–1200/);
assert.match(card.innerHTML,/PROVISOIRE/);
assert.match(card.innerHTML,/Actions nécessaires/);
assert.doesNotMatch(card.innerHTML,/undefined|NaN|\/ 60|\/ 40/);
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    template = Path("templates/crm.html").read_text(encoding="utf-8")
    assert "scoring_version='20260909-cpf-coverage-8'" in template
