from pathlib import Path


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
