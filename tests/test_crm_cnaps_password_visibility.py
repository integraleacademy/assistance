from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CRM_JS = ROOT / "static" / "crm.js"


def test_saved_cnaps_password_is_masked_and_can_be_revealed_without_being_cleared():
    script = CRM_JS.read_text(encoding="utf-8")

    assert (
        '<input id="cnapsPassword" name="cnaps_password" type="password" '
        'value="${esc(c.cnaps_password)}" autocomplete="off">'
    ) in script
    assert (
        '<button type="button" class="btn" id="cnapsPasswordToggle" '
        'aria-controls="cnapsPassword" aria-pressed="false">Afficher</button>'
    ) in script
    assert "cnapsPassword.type=visible?'password':'text'" in script
    assert "cnapsPasswordToggle.textContent=visible?'Afficher':'Masquer'" in script
    assert (
        "cnapsPasswordToggle.setAttribute('aria-pressed',String(!visible))"
        in script
    )
    assert (
        'name="cnaps_password" type="password" value="" '
        'autocomplete="new-password"'
    ) not in script
    assert 'data-show="cnaps-account"' in script
