from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_financing_block_is_grouped_into_three_responsive_cards():
    javascript = (ROOT / "static/crm.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "static/crm.css").read_text(encoding="utf-8")

    start = javascript.index(
        'class="form-section section-wallet funding-section"'
    )
    end = javascript.index("${metaAnswersSection(c)}", start)
    block = javascript[start:end]

    cpf = block.index('class="funding-card funding-card-cpf"')
    france_travail = block.index('class="funding-card funding-card-ft"')
    personal = block.index('class="funding-card funding-card-personal conditional"')

    assert cpf < france_travail < personal
    assert 'class="funding-workspace"' in block
    assert "CPF et identité numérique" in block
    assert "France Travail" in block
    assert "Paiement personnel" in block
    assert 'data-show="personal-financing"' in block

    for marker in (
        "selectHtml('cpf'",
        'name="cpf_montant"',
        "cpfTierSelect(c.cpf_palier)",
        "selectField('identite_creation'",
        "selectField('identite_ok'",
        "selectHtml('financement_ft'",
        'name="statut_demande_financement_ft"',
        'name="montant_accorde_ft"',
        "selectField('inscrit_ft'",
        "selectHtml('financement_perso_possible'",
        'name="reste_a_charge_perso"',
    ):
        assert marker in block

    assert 'return`<select name="cpf_palier">' in javascript

    for selector in (
        ".funding-workspace{",
        ".funding-card{",
        ".funding-card-head{",
        ".funding-step{",
        ".funding-identity-fields{",
        "@media(max-width:900px){.funding-workspace",
        "@media(max-width:650px){.funding-workspace",
    ):
        assert selector in stylesheet
