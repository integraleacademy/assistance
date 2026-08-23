from pathlib import Path


def test_chatter_import_and_view_scripts_are_loaded():
    template = Path("templates/crm.html").read_text(encoding="utf-8")

    assert "crm_salesforce_chatter_import.js" in template
    assert "crm_salesforce_chatter_view.js" in template


def test_importer_requires_three_files_and_a_preview_token():
    script = Path("static/crm_salesforce_chatter_import.js").read_text(
        encoding="utf-8"
    )

    assert "publications_file" in script
    assert "comments_file" in script
    assert "users_file" in script
    assert "/api/crm/import-salesforce-chatter" in script
    assert "preview_token" in script
    assert "Aucune nouvelle personne ne sera créée" in script


def test_contact_view_is_read_only_and_separate_from_internal_publications():
    script = Path("static/crm_salesforce_chatter_view.js").read_text(
        encoding="utf-8"
    )

    assert "contactSalesforceChatterTab" in script
    assert "contactSalesforceChatterPanel" in script
    assert "Historique Salesforce" in script
    assert "Lecture seule" in script
    assert "salesforce_chatter" in script
