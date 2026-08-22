from pathlib import Path


def test_primary_crm_assets_are_not_marked_immutable_by_template():
    template = Path("templates/crm.html").read_text(encoding="utf-8")

    primary_assets = (
        "crm.css",
        "crm_workspace.css",
        "crm_dashboard_origins.css",
        "crm_workspace.js",
        "crm.js",
        "crm_dashboard_origins.js",
    )

    for asset in primary_assets:
        assert f"filename='{asset}',v=asset_version" not in template
        assert f"filename='{asset}')" in template
