from pathlib import Path


def test_import_script_has_three_files_and_preview_contract():
    script = Path("static/crm_salesforce_chatter_import.js").read_text(
        encoding="utf-8"
    )

    assert "publications_file" in script
    assert "comments_file" in script
    assert "users_file" in script
    assert "dry_run" in script
    assert "preview_token" in script
    assert "Aucune personne ne sera créée" in script
    assert "/api/crm/import-salesforce-chatter" in script


def test_display_script_uses_a_separate_salesforce_history_tab():
    script = Path("static/crm_salesforce_chatter_display.js").read_text(
        encoding="utf-8"
    )

    assert "Historique Salesforce" in script
    assert "salesforce_chatter" in script
    assert "contactSalesforceHistoryTab" in script
    assert "salesforce_feed_comment_id" not in script
    assert 'target="_blank"' in script
