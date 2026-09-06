import os


ROOT = os.path.dirname(os.path.dirname(__file__))


def read(relative_path):
    with open(os.path.join(ROOT, relative_path), encoding="utf-8") as source:
        return source.read()


def test_dashboard_origin_patch_is_loaded_after_main_crm_script():
    template = read("templates/crm.html")

    assert "crm_dashboard_origins.css" in template
    assert "crm_dashboard_origins.js" in template
    assert template.index("crm.js") < template.index("crm_dashboard_origins.js")


def test_dashboard_origin_compatibility_asset_does_not_override_kpi_rendering():
    compatibility = read("static/crm_dashboard_origins.js")
    javascript = read("static/crm.js")

    assert "dashboardKpi = function" not in compatibility
    assert "dashboard = function" not in compatibility
    assert "dashboardKpi('Pistes META'" in javascript
    assert "dashboardKpi('Pistes Google Ads'" in javascript
    assert "Saisie manuelle" not in compatibility
    assert "requiredOrigins" not in compatibility


def test_dashboard_origin_patch_keeps_four_responsive_kpi_columns():
    stylesheet = read("static/crm_dashboard_origins.css")

    assert "repeat(4,minmax(0,1fr))" in stylesheet
    assert "@media(max-width:1250px)" in stylesheet
    assert "repeat(3,minmax(0,1fr))" in stylesheet
    assert "@media(max-width:650px)" in stylesheet
    assert "grid-template-columns:1fr 1fr" in stylesheet


def test_crm_origin_options_are_canonical_and_legacy_values_are_mapped():
    crm = read("static/crm.js")
    workspace = read("static/crm_workspace.js")
    expected = "['META','Google Ads','Site internet','Bouche à oreilles','Mon Compte Formation','Secrétariat','Simulateur VAE','Calendly','Autre']"

    assert f"const crmOriginFilterValues={expected}" in crm
    assert f"const workspaceOriginOptions={expected}" in workspace
    assert "function canonicalCrmOrigin(contact)" in crm
    assert "calendly_origin_version='20260906-1'" in read("templates/crm.html")
    assert read("templates/crm.html").count("calendly_origin_version='20260906-1'") == 2
    assert "return'Autre'" in crm
    assert "return'Autre'" in workspace
    assert "observed" not in crm[crm.index("function leadOriginFilterOptions"):crm.index("const dashboardHasContact")]
