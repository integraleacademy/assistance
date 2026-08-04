import pytest

from candidate_scoring import calculate_candidate_integration_score, normalize_cpf_amount


def contact(**updates):
    base = {"formation": "APS", "cpf": "OUI", "cpf_montant": "1650",
            "identite_creation": "OUI", "identite_ok": "OUI",
            "financement_ft": "NON", "refus_ft_perso": "OUI", "inscrit_ft": "NON"}
    base.update(updates)
    return base


def points(result, key):
    return next(row["points"] for row in result["breakdown"] if row["key"] == key)


def test_ideal_aps_profile():
    result = calculate_candidate_integration_score(contact())
    assert (result["score"], result["level"], result["label"]) == (100, "excellent", "Très bon profil")
    assert result["cpf_coverage_percent"] == 100
    assert result["remaining_to_finance_eur"] == 0
    assert result["blockers"] == []
    assert result["operational_status"] == "ready"


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
