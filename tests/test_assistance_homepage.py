from pathlib import Path


TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "index.html"


def _homepage_source():
    return TEMPLATE.read_text(encoding="utf-8")


def test_assistance_homepage_routes_visitors_to_the_right_contact():
    source = _homepage_source()

    assert "Vous avez besoin" in source
    assert "Trouvez votre interlocuteur" in source
    assert "Aurélie CHAUSSEZ" in source
    assert "Chargée des relations clients et Responsable des BTS" in source
    assert "aurelie@integraleacademy.com" in source
    assert "04 87 83 06 15" in source
    assert "07 69 39 04 57" in source
    assert "Cassandre MENARD" in source
    assert "Responsable commerciale" in source
    assert "cassandre@integraleacademy.com" in source
    assert "04 87 83 06 16" in source
    assert "07 43 58 22 64" in source


def test_assistance_homepage_keeps_standard_and_existing_form_contract():
    source = _homepage_source()

    assert "04 22 47 07 68" in source
    assert "ecole@integraleacademy.com" in source
    assert 'method="POST"' in source
    assert 'enctype="multipart/form-data"' in source
    for field_name in ("nom", "prenom", "telephone", "mail", "motif", "details", "justificatif"):
        assert f'name="{field_name}"' in source
    assert "toggleJustificatif()" in source
    assert "url_for('poei_agent_securite_cannes')" in source
