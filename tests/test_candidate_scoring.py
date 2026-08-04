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
            "financement_ft": "NON", "refus_ft_perso": "OUI", "reste_a_charge_perso": "OUI", "inscrit_ft": "NON"}
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
    assert (result["regulatory_score"], result["regulatory_contribution"], result["score"]) == (45, 18, 78)
    assert result["normalized_cnaps_status"] == "transmitted"
    tracking = next(row for row in result["regulatory_breakdown"] if row["key"] == "cnaps_tracking")
    assert (tracking["points"], tracking["max_points"], tracking["detail"]) == (15, 30, "Demande CNAPS transmise")
    assert not any("Aucun résultat CNAPS exploitable" in warning for warning in result["warnings"])
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
    assert blocked["regulatory_score"] == 25 and blocked["operational_status"] == "blocked"


def test_raw_status_wins_over_stale_unknown_snapshot():
    assert normalize_cnaps_tracking_status({"normalized_status": "unknown", "raw_status": "TRANSMIS"}) == "transmitted"


def test_production_capture_weighting_is_61():
    lead = contact(formation="A3P", cpf_montant="1995", carte_pro="NON", compte_cnaps="NON",
                   antecedents="NON", titre_sejour_cnaps="NON_CONCERNE")
    result = calculate_candidate_integration_score(
        lead, {"found": True, "raw_status": "TRANSMIS", "has_active_professional_title": False})
    assert result["financial_score"] == 79
    assert (result["regulatory_score"], result["regulatory_contribution"], result["score"]) == (35, 14, 61)
    assert result["normalized_cnaps_status"] == "transmitted"


def test_no_result_accepted_without_title_and_refused_rules():
    lead = contact(carte_pro="NON", compte_cnaps="NON", antecedents="NON", titre_sejour_cnaps="NON_CONCERNE")
    missing = calculate_candidate_integration_score(lead, {"found": False})
    tracking = next(row for row in missing["regulatory_breakdown"] if row["key"] == "cnaps_tracking")
    assert missing["normalized_cnaps_status"] == "no_result" and tracking["points"] == 0 and tracking["max_points"] == 30
    assert any("Aucun résultat CNAPS exploitable" in warning for warning in missing["warnings"])
    accepted = calculate_candidate_integration_score(lead, {"raw_status": "ACCEPTÉ", "has_active_professional_title": False})
    assert accepted["regulatory_score"] == 100 and accepted["regulatory_status"] == "ready"
    refused = calculate_candidate_integration_score(lead, {"raw_status": "REFUSÉ"})
    assert refused["regulatory_score"] == 0 and refused["operational_status"] == "blocked"


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
        cpf="NON", cpf_montant="", financement_ft="NON", refus_ft_perso="NON", reste_a_charge_perso="NON"))
    assert result["operational_status"] == "blocked"
    assert any("ne financera pas personnellement" in blocker for blocker in result["blockers"])


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


def test_personal_remainder_confirmed_a3p():
    result = calculate_candidate_integration_score(contact(formation="A3P", cpf_montant="2000", reste_a_charge_perso="OUI"))
    assert result["financial_score"] == 79
    assert result["personal_remainder_applicable"] is True
    assert result["personal_remainder_amount_eur"] == 2200
    assert result["personal_remainder_status"] == "confirmed"
    assert result["funding_solution_status"] == "secured_personal"
    assert result["unsecured_amount_eur"] == 0
    assert points(result, "personal_funding") == points(result, "france_travail_strategy") == 15
    assert not result["blockers"]
    assert not any("Sécuriser le financement" in action for action in result["next_actions"])


def test_personal_remainder_refused_and_unknown():
    refused = calculate_candidate_integration_score(contact(formation="A3P", cpf_montant="2000", reste_a_charge_perso="NON"))
    assert refused["financial_score"] == 49
    assert refused["personal_remainder_status"] == "refused"
    assert refused["funding_solution_status"] == "unsecured"
    assert refused["unsecured_amount_eur"] == 2200
    assert refused["operational_status"] == "blocked"
    unknown = calculate_candidate_integration_score(contact(formation="A3P", cpf_montant="2000", reste_a_charge_perso=""))
    assert unknown["financial_score"] == 49 and not unknown["blockers"]
    assert unknown["operational_status"] == "action_required"
    assert any("n’est pas renseignée" in warning for warning in unknown["warnings"])
    assert any("Confirmer si" in action for action in unknown["next_actions"])


def test_ft_fallback_is_distinct_from_personal_remainder():
    legacy = calculate_candidate_integration_score(contact(formation="A3P", cpf_montant="2000", refus_ft_perso="OUI", reste_a_charge_perso=""))
    assert legacy["personal_remainder_status"] == "unknown"
    assert points(legacy, "personal_funding") == 0
    ft = calculate_candidate_integration_score(contact(formation="A3P", cpf_montant="2000", financement_ft="OUI", refus_ft_perso="OUI", reste_a_charge_perso="NON", inscrit_ft="OUI"))
    assert ft["personal_remainder_applicable"] is False
    assert ft["funding_solution_status"] == "secured_personal_fallback"
    assert points(ft, "personal_funding") == 15


def test_cpf_missing_is_not_reliable_for_personal_question():
    result = calculate_candidate_integration_score(contact(cpf="OUI", cpf_montant="", reste_a_charge_perso="OUI"))
    assert result["personal_remainder_applicable"] is False
    assert result["personal_remainder_status"] == "not_applicable"


def test_frontend_uses_backend_remainder_without_training_price_table():
    source = open("static/crm.js", encoding="utf-8").read()
    assert "personal_remainder_applicable" in source
    assert "personal_remainder_amount_eur" in source
    assert "Le candidat financera-t-il personnellement le reste à charge de" in source
    assert "TRAINING_PRICES" not in source and "4200" not in source and "1650" not in source
