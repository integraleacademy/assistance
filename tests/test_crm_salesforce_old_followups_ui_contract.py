from pathlib import Path


def test_old_followups_script_is_loaded_by_the_crm_template():
    template = Path("templates/crm.html").read_text(encoding="utf-8")

    assert "crm_salesforce_old_followups_import.js" in template
    assert "20260822-salesforce-old-followups-1" in template
    assert template.index("crm_salesforce_old_followups_import.js") < template.index(
        "crm_salesforce_tasks_import.js"
    )


def test_ui_requires_two_files_and_an_preview_token():
    script = Path("static/crm_salesforce_old_followups_import.js").read_text(
        encoding="utf-8"
    )

    assert "leads_file" in script
    assert "tasks_file" in script
    assert "dry_run" in script
    assert "preview_token" in script
    assert "import-salesforce-old-followups" in script
    assert "Importer les anciennes pistes avec une relance ouverte" in script
    assert "ne reprend que les pistes créées avant 2026" in script


def test_ui_never_presents_the_mode_as_a_full_historical_import():
    script = Path("static/crm_salesforce_old_followups_import.js").read_text(
        encoding="utf-8"
    )

    assert "Il n’importe pas toutes les anciennes pistes" in script
    assert "Les disqualifiés, convertis, BTS/CAP" in script
    assert "Une relance déjà traitée dans le CRM ne sera jamais rouverte" in script
