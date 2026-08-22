from pathlib import Path


def test_genuine_new_script_is_loaded_after_the_scope_guardrail():
    template = Path("templates/crm.html").read_text(encoding="utf-8")

    scope_position = template.index("crm_salesforce_scope.js")
    genuine_position = template.index("crm_salesforce_genuine_new.js")

    assert genuine_position > scope_position
    assert "20260822-salesforce-genuine-new-1" in template


def test_ui_only_unlocks_explicit_salesforce_new_statuses():
    script = Path("static/crm_salesforce_genuine_new.js").read_text(
        encoding="utf-8"
    )

    assert "new_status_source_counts" in script
    assert "genuine_new_count" in script
    assert "unexpected_new_count" in script
    assert "Les statuts vides ou inconnus continuent d’être bloqués" in script
    assert "confirm.disabled = importable === 0 || !hasPreviewToken" in script
    assert "confirm.disabled = true" in script
