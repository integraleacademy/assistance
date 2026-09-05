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


def test_assistance_homepage_keeps_standard_without_online_form_or_cannes_promotion():
    source = _homepage_source()

    assert "04 22 47 07 68" in source
    assert "ecole@integraleacademy.com" in source
    assert '<a class="topbar-cta" href="tel:+33422470768"' in source
    assert "Appeler le standard" in source
    assert "<form" not in source
    assert "Demande en ligne" not in source
    assert "toggleJustificatif" not in source
    assert "Cannes" not in source
    assert "poei_agent_securite_cannes" not in source
