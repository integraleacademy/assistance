import pytest

from candidate_scoring import (calculate_candidate_integration_score,
    calculate_security_regulatory_score, normalize_cnaps_tracking_status, normalize_cpf_amount)


def test_candidate_score_styles_are_bundled():
    with open("static/crm.css", encoding="utf-8") as source:
        css = source.read()

    assert ".integration-score-card{padding:18px" in css
    assert ".score-main strong{font:850 32px Manrope}" in css
    assert ".score-progress i,.cpf-coverage i" in css
    assert ".score-money{display:grid" in css


def contact(**updates):
    base = {"formation": "APS", "cpf": "OUI", "cpf_montant": "1650",
            "identite_creation": "OUI", "identite_ok": "OUI",
            "financement_ft": "NON", "refus_ft_perso": "OUI", "inscrit_ft": "NON"}
    base.update(updates)
    return base


def points(result, key):
    return next(row["points"] for row in result["breakdown"] if row["key"] == key)


def test_ideal_aps_profile():
    result = calculate_candidate_integration_score(contact(carte_pro="OUI"))
    assert (result["score"], result["level"], result["label"]) == (100, "excellent", "Dossier très avancé")
    assert result["cpf_coverage_percent"] == 100
    assert result["remaining_to_finance_eur"] == 0
    assert result["blockers"] == []
    assert result["operational_status"] == "ready"


def test_non_security_training_keeps_financial_score():
    result = calculate_candidate_integration_score(contact(formation="SSIAP 1", cpf_montant="980"))
    assert result["score"] == result["financial_score"]
    assert result["regulatory_applicable"] is False


@pytest.mark.parametrize("raw,expected", [("ACCEPTÉ", "accepted"), ("ACCEPTE", "accepted"),
    ("TRANSMIS", "transmitted"), ("EN INSTRUCTION", "in_review"),
    ("ENREGISTRÉ", "registered"), ("REFUSÉ", "refused"), ("AUCUN RÉSULTAT", "no_result")])
def test_cnaps_status_normalization(raw, expected):
    assert normalize_cnaps_tracking_status({"cnaps": {"cnaps_status": raw, "titles": []}}) == expected


def test_transmitted_weighting_and_no_title_precedence():
    lead = contact(carte_pro="NON", compte_cnaps="OUI", antecedents="NON", titre_sejour_cnaps="CONFORME")
    result = calculate_candidate_integration_score(lead, {"found": True, "raw_status": "TRANSMIS", "has_active_professional_title": False})
    assert (result["regulatory_score"], result["regulatory_contribution"], result["score"]) == (70, 28, 88)
    assert result["normalized_cnaps_status"] == "transmitted"
    assert result["operational_status"] == "action_required"


def test_no_result_refusal_and_accepted_priority():
    lead = contact(carte_pro="NON", compte_cnaps="OUI", antecedents="NON", titre_sejour_cnaps="CONFORME")
    missing = calculate_candidate_integration_score(lead, {"found": False})
    assert (missing["regulatory_score"], missing["score"], missing["level"]) == (30, 72, "qualify")
    refused = calculate_candidate_integration_score(lead, {"raw_status": "REFUSÉ"})
    assert refused["regulatory_score"] == 0 and refused["operational_status"] == "blocked"
    accepted = calculate_candidate_integration_score({**lead, "formation": "A3P", "cpf_montant": "4200", "antecedents": "OUI"}, {"raw_status": "ACCEPTÉ"})
    assert accepted["regulatory_score"] == 100 and accepted["score"] == 100


def test_declarative_safeguards_and_stay_status():
    base = contact(carte_pro="NON", compte_cnaps="OUI", antecedents="OUI", titre_sejour="OUI")
    risk = calculate_security_regulatory_score(base, {"raw_status": "TRANSMIS"})
    assert risk["score"] <= 25 and risk["status"] == "high_risk"
    assert not any("juridiquement refus" in text.lower() for text in risk["warnings"])
    assert any("titre de séjour" in text for text in risk["warnings"])
    blocked = calculate_candidate_integration_score({**base, "titre_sejour_cnaps": "NON_CONFORME"}, {"raw_status": "TRANSMIS"})
    assert blocked["regulatory_score"] <= 10 and blocked["operational_status"] == "blocked"


def test_half_covered_cpf():
    result = calculate_candidate_integration_score(contact(cpf_montant="825"))
    assert points(result, "cpf_coverage") == 20
    assert result["cpf_coverage_percent"] == 50
    assert result["remaining_to_finance_eur"] == 825


def test_cpf_above_ssiap_price_is_capped():
    result = calculate_candidate_integration_score(contact(formation="SSIAP 1", cpf_montant="1500"))
    assert result["cpf_coverage_percent"] == 100
    assert points(result, "cpf_coverage") == 40
    assert result["remaining_to_finance_eur"] == 0
    assert result["cpf_amount_eur"] == 1500


def test_amount_is_ignored_without_cpf_account():
    result = calculate_candidate_integration_score(contact(cpf="NON", cpf_montant="1000"))
    assert points(result, "cpf_coverage") == 0
    assert any("montant CPF est enregistré" in warning for warning in result["warnings"])


@pytest.mark.parametrize("wants,registered,personal,expected", [
    ("NON", "NON", "NON", 15), ("OUI", "OUI", "OUI", 10),
    ("OUI", "OUI", "NON", 6), ("OUI", "NON", "OUI", 4),
    ("OUI", "NON", "NON", 0),
])
def test_france_travail_scale(wants, registered, personal, expected):
    result = calculate_candidate_integration_score(contact(
        financement_ft=wants, inscrit_ft=registered, refus_ft_perso=personal))
    assert points(result, "france_travail_strategy") == expected


def test_legacy_lead_missing_fields_is_supported():
    result = calculate_candidate_integration_score({"formation": "APS"})
    assert isinstance(result["score"], int)
    assert result["warnings"]


def test_unknown_training_is_not_calculable():
    result = calculate_candidate_integration_score(contact(formation="Formation future"))
    assert result["score"] is None
    assert result["label"] == "Score indisponible — tarif de la formation non configuré"
    assert any("Tarif non configuré" in warning for warning in result["warnings"])


def test_no_funding_is_blocked():
    result = calculate_candidate_integration_score(contact(
        cpf="NON", cpf_montant="", financement_ft="NON", refus_ft_perso="NON"))
    assert result["operational_status"] == "blocked"
    assert any("Aucune solution" in blocker for blocker in result["blockers"])


def test_inconsistent_identity_warns_without_error():
    result = calculate_candidate_integration_score(contact(identite_creation="NON", identite_ok="OUI"))
    assert isinstance(result["score"], int)
    assert any("fonctionnelle" in warning and "création" in warning for warning in result["warnings"])


def test_training_change_recalculates_all_financial_values():
    aps = calculate_candidate_integration_score(contact(cpf_montant="1650"))
    a3p = calculate_candidate_integration_score(contact(formation="A3P", cpf_montant="1650"))
    assert (aps["training_price_eur"], a3p["training_price_eur"]) == (1650, 4200)
    assert aps["cpf_coverage_percent"] != a3p["cpf_coverage_percent"]
    assert aps["remaining_to_finance_eur"] != a3p["remaining_to_finance_eur"]
    assert aps["score"] != a3p["score"]


def test_amount_validation_is_decimal_exact_and_rejects_invalid_values():
    assert normalize_cpf_amount("1 650,5") == "1650.50"
    with pytest.raises(ValueError):
        normalize_cpf_amount("-1")
    with pytest.raises(ValueError):
        normalize_cpf_amount("12.345")
