import pytest

from candidate_scoring import (
    CANDIDATE_SCORING_VERSION,
    calculate_candidate_integration_score,
    calculate_security_regulatory_score,
    normalize_cnaps_tracking_status,
    normalize_cpf_amount,
    parse_cpf_tier,
)


TRAINING_CASES = (
    ("APS", {}, 1650),
    ("A3P", {}, 4200),
    ("SSIAP 1", {}, 980),
    ("Chauffeur VTC", {}, 1500),
    ("DESP", {"desp_type": "INITIAL"}, 4300),
    ("DESP", {"desp_type": "VAE"}, 3800),
)


def financial_contact(formation="A3P", cpf_amount="4200", **updates):
    value = {
        "formation": formation,
        "cpf": "OUI",
        "cpf_montant": cpf_amount,
        "identite_creation": "OUI",
        "identite_ok": "OUI",
        "financement_ft": "NON",
    }
    value.update(updates)
    return value


def financial_points(result, key):
    return next(
        row["points"] for row in result["financial_breakdown"]
        if row["key"] == key
    )


def test_candidate_score_styles_are_bundled():
    with open("static/crm.css", encoding="utf-8") as source:
        css = source.read()

    assert ".integration-score-card{padding:18px" in css
    assert ".score-main strong{font:850 32px Manrope}" in css
    assert ".score-progress i,.cpf-coverage i" in css
    assert ".score-confidence{" in css
    assert ".integration-score-card.incomplete" in css


def test_v5_uses_60_percent_financial_and_40_percent_verified_regulatory():
    declared = calculate_candidate_integration_score(
        financial_contact(carte_pro="OUI")
    )
    verified = calculate_candidate_integration_score(
        financial_contact(carte_pro="OUI"),
        {"has_active_professional_title": True},
    )

    assert CANDIDATE_SCORING_VERSION == declared["version"] == 5
    assert declared["financial_score"] == 100
    assert declared["regulatory_score"] == 70
    assert declared["score"] == 88
    assert declared["operational_status"] == "action_required"
    assert verified["score"] == 100
    assert verified["regulatory_source"] == "verified_cnaps_title"
    assert verified["operational_status"] == "ready"


@pytest.mark.parametrize(("snapshot", "contact_updates", "expected"), [
    ({"raw_status": "ACCEPTÉ"}, {"carte_pro": "NON"}, 100),
    ({"raw_status": "EN INSTRUCTION"}, {"carte_pro": "NON"}, 55),
    ({"raw_status": "TRANSMIS"}, {"carte_pro": "NON"}, 40),
    ({"raw_status": "ENREGISTRÉ"}, {"carte_pro": "NON"}, 25),
    ({}, {"carte_pro": "NON", "compte_cnaps": "OUI"}, 25),
    ({}, {"carte_pro": "OUI"}, 70),
])
def test_regulatory_progress_ladder(snapshot, contact_updates, expected):
    result = calculate_security_regulatory_score(
        financial_contact(**contact_updates), snapshot
    )
    assert result["score"] == expected


@pytest.mark.parametrize("raw,expected", [
    ("ACCEPTÉ", "accepted"),
    ("ACCEPTE", "accepted"),
    ("TRANSMIS", "transmitted"),
    ("EN INSTRUCTION", "in_review"),
    ("ENREGISTRÉ", "registered"),
    ("REFUSÉ", "refused"),
    ("AUCUN RÉSULTAT", "no_result"),
])
def test_cnaps_status_normalization(raw, expected):
    assert normalize_cnaps_tracking_status({
        "cnaps": {"cnaps_status": raw, "titles": []},
    }) == expected


def test_unknown_regulatory_state_keeps_a_numeric_provisional_lower_bound():
    result = calculate_candidate_integration_score(
        financial_contact(carte_pro="NON", compte_cnaps="NON"),
        {"found": False},
    )

    assert result["financial_score"] == 100
    assert result["regulatory_score"] is None
    assert result["score"] == 60
    assert result["level"] == "qualify"
    assert result["score_estimated"] is True
    assert result["score_complete"] is False
    assert "Score provisoire" in result["label"]
    assert result["operational_status"] == "action_required"


def test_cnaps_refusal_is_zero_and_blocking():
    result = calculate_candidate_integration_score(
        financial_contact(carte_pro="NON"), {"raw_status": "REFUSÉ"}
    )
    assert result["regulatory_score"] == 0
    assert result["score"] == 60
    assert result["operational_status"] == "blocked"
    assert any("refus" in blocker.lower() for blocker in result["blockers"])


def test_sensitive_declarations_trigger_review_without_numerical_penalty():
    base = financial_contact(carte_pro="NON", compte_cnaps="OUI")
    clear = calculate_security_regulatory_score(
        {**base, "antecedents": "NON", "garde_vue": "NON"},
        {"raw_status": "TRANSMIS"},
    )
    declared = calculate_security_regulatory_score(
        {**base, "antecedents": "OUI", "garde_vue": "OUI"},
        {"raw_status": "TRANSMIS"},
    )

    assert clear["score"] == declared["score"] == 40
    assert declared["status"] == "in_progress"
    assert len(declared["warnings"]) >= 2
    assert not any("juridiquement refus" in text.lower()
                   for text in declared["warnings"])


def test_non_conforming_stay_status_is_a_human_review_blocker():
    result = calculate_candidate_integration_score(
        financial_contact(
            carte_pro="NON", compte_cnaps="OUI",
            titre_sejour_cnaps="NON_CONFORME",
        ),
        {"raw_status": "TRANSMIS"},
    )
    assert result["regulatory_score"] == 40
    assert result["operational_status"] == "blocked"
    assert any("titre de séjour" in blocker for blocker in result["blockers"])


@pytest.mark.parametrize(("formation", "extra", "price"), TRAINING_CASES)
def test_every_supported_training_uses_the_same_financial_model(
        formation, extra, price):
    result = calculate_candidate_integration_score(financial_contact(
        formation=formation, cpf_amount=str(price), **extra,
    ))
    assert result["training_price_eur"] == price
    assert result["financial_score"] == 100
    if formation not in {"APS", "A3P"}:
        assert result["score"] == 100
        assert result["regulatory_applicable"] is False


@pytest.mark.parametrize(("raw", "minimum", "maximum"), [
    ("0-1000 euros", 0, 1000),
    ("1 000–2 000 €", 1000, 2000),
    ("2000-3000 euros", 2000, 3000),
    ("3000 à 4000 euros", 3000, 4000),
    ("Plus de 4000 euros", 4000, None),
])
def test_cpf_tiers_are_parsed_as_ranges(raw, minimum, maximum):
    tier = parse_cpf_tier(raw)
    assert tier["min_cents"] == minimum * 100
    assert tier["max_cents"] == (maximum * 100 if maximum is not None else None)
    assert tier["estimated"] is True


def test_open_cpf_tier_is_scored_from_lower_bound_and_displayed_as_range():
    result = calculate_candidate_integration_score(financial_contact(
        cpf_amount="", cpf_palier="Plus de 4000 euros",
        financement_perso_possible="OUI",
    ))

    assert result["cpf_amount_eur"] is None
    assert result["cpf_amount_min_eur"] == 4000
    assert result["cpf_amount_max_eur"] is None
    assert result["cpf_range_open_ended"] is True
    assert result["cpf_coverage_min_percent"] == 95
    assert result["cpf_coverage_max_percent"] == 100
    assert result["remaining_to_finance_min_eur"] == 0
    assert result["remaining_to_finance_max_eur"] == 200
    assert result["financial_score"] == 81
    assert result["score_estimated"] is True


def test_exact_cpf_amount_wins_over_an_old_tier():
    result = calculate_candidate_integration_score(financial_contact(
        cpf_amount="2500", cpf_palier="Plus de 4000 euros",
        financement_perso_possible="OUI",
    ))
    assert result["cpf_amount_eur"] == 2500
    assert result["cpf_amount_min_eur"] == 2500
    assert result["cpf_amount_max_eur"] == 2500
    assert result["cpf_amount_estimated"] is False


def test_unknown_financing_gets_a_lower_bound_without_inventing_money():
    unknown = calculate_candidate_integration_score({
        "formation": "SSIAP 1", "cpf": "OUI",
    })
    confirmed_none = calculate_candidate_integration_score({
        "formation": "SSIAP 1", "cpf": "NON", "financement_ft": "NON",
        "financement_perso_possible": "NON",
    })

    assert unknown["financial_score"] == 0
    assert unknown["score"] == 0
    assert unknown["score_estimated"] is True
    assert unknown["score_complete"] is False
    assert "Score provisoire" in unknown["label"]
    assert unknown["cpf_coverage_percent"] is None
    assert unknown["remaining_to_finance_eur"] is None
    assert unknown["unsecured_amount_eur"] is None
    assert unknown["operational_status"] == "action_required"
    assert confirmed_none["financial_score"] == 0
    assert confirmed_none["remaining_to_finance_eur"] == 980
    assert confirmed_none["unsecured_amount_eur"] == 980
    assert confirmed_none["operational_status"] == "blocked"


@pytest.mark.parametrize(("formation", "extra"), [
    (formation, extra) for formation, extra, _price in TRAINING_CASES
])
def test_every_supported_training_has_a_numeric_score_with_missing_answers(
        formation, extra):
    result = calculate_candidate_integration_score({
        "formation": formation,
        **extra,
    })

    assert result["financial_score"] == 0
    assert result["score"] == 0
    assert result["score_estimated"] is True
    assert result["score_complete"] is False
    assert result["label"] == "Score provisoire — informations à compléter"
    assert result["operational_status"] == "action_required"
    assert result["cpf_amount_eur"] is None
    assert result["remaining_to_finance_eur"] is None


def test_identity_only_changes_score_when_cpf_is_used():
    no_cpf = {
        "formation": "SSIAP 1", "cpf": "NON", "financement_ft": "NON",
        "financement_perso_possible": "OUI",
    }
    without_identity = calculate_candidate_integration_score(no_cpf)
    with_identity = calculate_candidate_integration_score({
        **no_cpf, "identite_creation": "NON", "identite_ok": "NON",
    })
    assert without_identity["financial_score"] == with_identity["financial_score"]
    assert without_identity["financial_breakdown"] == with_identity["financial_breakdown"]
    assert not any("identité numérique" in action.lower()
                   for action in with_identity["next_actions"])

    cpf_not_ready = calculate_candidate_integration_score(financial_contact(
        formation="SSIAP 1", cpf_amount="980",
        identite_creation="NON", identite_ok="NON",
    ))
    cpf_ready = calculate_candidate_integration_score(financial_contact(
        formation="SSIAP 1", cpf_amount="980",
    ))
    assert financial_points(cpf_not_ready, "route_readiness") == 0
    assert financial_points(cpf_ready, "route_readiness") == 20


def test_france_travail_actual_status_changes_progress_and_readiness():
    base = {
        "formation": "SSIAP 1", "cpf": "NON", "financement_ft": "OUI",
        "inscrit_ft": "OUI", "financement_perso_possible": "NON",
    }
    scores = {
        status: calculate_candidate_integration_score({
            **base, "statut_demande_financement_ft": status,
        })
        for status in (
            "a_preparer", "transmise", "en_cours_instruction", "acceptee",
        )
    }

    assert [scores[key]["financial_score"] for key in (
        "a_preparer", "transmise", "en_cours_instruction", "acceptee",
    )] == [20, 33, 40, 46]
    assert all(result["operational_status"] == "action_required"
               for result in scores.values())
    accepted = calculate_candidate_integration_score({
        **base, "statut_demande_financement_ft": "acceptee",
        "montant_accorde_ft": "980",
    })
    assert accepted["financial_score"] == 100
    assert accepted["funding_solution_status"] == "secured_france_travail"
    assert accepted["financial_data_confidence_percent"] == 100
    assert accepted["score_complete"] is True
    assert accepted["operational_status"] == "ready"


def test_france_travail_status_and_amount_are_ignored_when_route_is_refused():
    base = {
        "formation": "SSIAP 1", "cpf": "NON", "financement_ft": "NON",
        "financement_perso_possible": "NON",
    }
    without_history = calculate_candidate_integration_score(base)
    with_stale_history = calculate_candidate_integration_score({
        **base,
        "inscrit_ft": "OUI",
        "statut_demande_financement_ft": "acceptee",
        "montant_accorde_ft": "980",
    })

    assert with_stale_history["financial_score"] == without_history["financial_score"]
    assert with_stale_history["funding_solution_status"] == "unsecured"
    assert with_stale_history["france_travail_request_status"] == "aucune_demande"
    assert with_stale_history["france_travail_awarded_amount_eur"] is None


def test_refused_france_travail_distinguishes_no_fallback_capacity_and_exact_payment():
    base = financial_contact(
        cpf_amount="2000", financement_ft="OUI",
        statut_demande_financement_ft="refusee",
    )
    none = calculate_candidate_integration_score({
        **base, "financement_perso_possible": "NON",
    })
    capacity = calculate_candidate_integration_score({
        **base, "financement_perso_possible": "OUI",
    })
    exact = calculate_candidate_integration_score({
        **base, "financement_perso_possible": "OUI",
        "reste_a_charge_perso": "OUI",
    })

    assert none["financial_score"] < capacity["financial_score"] < exact["financial_score"]
    assert none["operational_status"] == "blocked"
    assert capacity["operational_status"] == "action_required"
    assert exact["financial_score"] == 100
    assert exact["funding_solution_status"] == "secured_personal"


def test_personal_remainder_needs_an_exact_reliable_amount():
    tier = calculate_candidate_integration_score(financial_contact(
        cpf_amount="", cpf_palier="1000-2000 euros",
        financement_perso_possible="OUI", reste_a_charge_perso="OUI",
    ))
    exact = calculate_candidate_integration_score(financial_contact(
        cpf_amount="2000", financement_perso_possible="OUI",
        reste_a_charge_perso="OUI",
    ))

    assert tier["funding_solution_status"] != "secured_personal"
    assert tier["personal_remainder_applicable"] is False
    assert any("sans montant exact fiable" in warning for warning in tier["warnings"])
    assert exact["funding_solution_status"] == "secured_personal"
    assert exact["personal_remainder_amount_eur"] == 2200


def test_legacy_personal_fallback_and_universal_field_score_identically():
    old = calculate_candidate_integration_score(financial_contact(
        cpf_amount="2000", refus_ft_perso="OUI",
    ))
    new = calculate_candidate_integration_score(financial_contact(
        cpf_amount="2000", financement_perso_possible="OUI",
    ))
    compared = (
        "financial_score", "funding_solution_status", "unsecured_amount_eur",
        "financial_breakdown", "blockers", "warnings", "next_actions",
    )
    assert {key: old[key] for key in compared} == {
        key: new[key] for key in compared
    }


def test_q1_to_q4_do_not_change_any_score_or_action():
    base = financial_contact(formation="SSIAP 1", cpf_amount="980")
    enriched = {
        **base,
        "q1_projet_formation": "OUI",
        "q2_niveau_francais": "OUI",
        "q3_disponibilite_neuf_semaines": "OUI",
        "q4_tarif_accepte": "OUI",
    }
    assert calculate_candidate_integration_score(base) == (
        calculate_candidate_integration_score(enriched)
    )


@pytest.mark.parametrize("origin", [
    "META", "Site internet", "Secrétariat", "Mon Compte Formation",
    "Ajout manuel", "Google Ads",
])
def test_origin_never_changes_the_integration_score(origin):
    base = financial_contact(formation="SSIAP 1", cpf_amount="980")
    assert calculate_candidate_integration_score({**base, "origine": origin}) == (
        calculate_candidate_integration_score(base)
    )


def test_amount_is_ignored_when_cpf_is_explicitly_no():
    result = calculate_candidate_integration_score({
        "formation": "SSIAP 1", "cpf": "NON", "cpf_montant": "1000",
        "financement_ft": "NON", "financement_perso_possible": "OUI",
    })
    assert result["cpf_coverage_percent"] == 0
    assert financial_points(result, "funding_coverage") == 0
    assert any("indiqué NON" in warning for warning in result["warnings"])


def test_unknown_training_is_not_calculable():
    result = calculate_candidate_integration_score(financial_contact(
        formation="Formation future",
    ))
    assert result["score"] is None
    assert result["score_estimated"] is False
    assert result["label"] == "Score indisponible — tarif de la formation non configuré"
    assert any("Tarif non configuré" in warning for warning in result["warnings"])


def test_amount_validation_is_decimal_exact_and_rejects_invalid_values():
    assert normalize_cpf_amount("1 650,5") == "1650.50"
    with pytest.raises(ValueError):
        normalize_cpf_amount("-1")
    with pytest.raises(ValueError):
        normalize_cpf_amount("12.345")


def test_frontend_uses_backend_ranges_and_universal_financing_fields():
    source = open("static/crm.js", encoding="utf-8").read()
    assert "personal_remainder_applicable" in source
    assert "personal_remainder_amount_eur" in source
    assert "remaining_to_finance_min_eur" in source
    assert "remaining_to_finance_max_eur" in source
    assert 'name="cpf_palier"' in source
    assert 'name="montant_accorde_ft"' in source
    assert "financement_perso_possible" in source
    assert "Le candidat financera-t-il personnellement le reste à charge exact de" in source
    assert "TRAINING_PRICES" not in source


def test_workspace_priority_has_no_origin_bonus():
    source = open("static/crm_workspace.js", encoding="utf-8").read()
    priority_source = source.split("function priority(contact)", 1)[1].split(
        "function duplicateMap", 1
    )[0]
    assert "origine" not in priority_source
    assert "META" not in priority_source
