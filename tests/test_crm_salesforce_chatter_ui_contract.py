from pathlib import Path


def test_chatter_scripts_are_loaded_with_cache_busting():
    template = Path("templates/crm.html").read_text(encoding="utf-8")

    assert "crm_salesforce_chatter_import.js" in template
    assert "crm_salesforce_chatter_history.js" in template
    assert template.count("20260823-salesforce-chatter-1") == 2


def test_admin_import_requires_the_three_data_loader_files():
    script = Path("static/crm_salesforce_chatter_import.js").read_text(
        encoding="utf-8"
    )

    assert "Importer l’historique Salesforce" in script
    assert "publications_file" in script
    assert "comments_file" in script
    assert "users_file" in script
    assert "dry_run" in script
    assert "preview_token" in script
    assert "Aucune nouvelle personne ne sera créée" in script


def test_contact_history_is_read_only_and_separate_from_internal_publications():
    script = Path("static/crm_salesforce_chatter_history.js").read_text(
        encoding="utf-8"
    )

    assert "Historique Salesforce" in script
    assert "Lecture seule" in script
    assert "salesforce_chatter" in script
    assert "contactSalesforceHistoryTab" in script
    assert "publicationFeed" not in script
    assert "Fil actu" not in script
