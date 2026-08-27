from pathlib import Path

import app as application


def test_primary_crm_assets_share_the_backend_cache_version():
    template = Path("templates/crm.html").read_text(encoding="utf-8")

    versioned_assets = (
        "crm.css",
        "crm_workspace.css",
        "crm_title.js",
        "crm_appointment_state.js",
        "crm_workspace.js",
        "crm.js",
    )
    unversioned_assets = (
        "crm_dashboard_origins.css",
        "crm_dashboard_origins.js",
    )

    for asset in versioned_assets:
        assert f"filename='{asset}',v=asset_version" in template

    for asset in unversioned_assets:
        assert f"filename='{asset}')" in template


def test_crm_asset_version_changes_when_a_primary_asset_changes(tmp_path):
    asset = tmp_path / "crm.js"
    original_files = application.CRM_ASSET_FILES
    try:
        application.CRM_ASSET_FILES = ("crm.js",)
        asset.write_text("ancienne interface", encoding="utf-8")
        first = application._crm_asset_version(str(tmp_path))
        asset.write_text("nouvelle interface", encoding="utf-8")
        second = application._crm_asset_version(str(tmp_path))
    finally:
        application.CRM_ASSET_FILES = original_files

    assert first.startswith("crm-")
    assert second.startswith("crm-")
    assert first != second
