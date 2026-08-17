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


def test_dashboard_origin_patch_removes_meta_kpi_and_guarantees_origins():
    javascript = read("static/crm_dashboard_origins.js")

    assert "label === 'Pistes META' ? ''" in javascript
    assert "requiredOrigins = ['Saisie manuelle', 'Google Ads']" in javascript
    assert "contact.gclid || contact.formulaire?.gclid" in javascript
    assert "normalized === 'ajout manuel'" in javascript
    assert "groups.push" in javascript
    assert "origin !== 'Ajout manuel'" in javascript


def test_dashboard_origin_patch_keeps_five_responsive_kpi_columns():
    stylesheet = read("static/crm_dashboard_origins.css")

    assert "repeat(5,minmax(0,1fr))" in stylesheet
    assert "@media(max-width:1250px)" in stylesheet
    assert "repeat(3,minmax(0,1fr))" in stylesheet
    assert "@media(max-width:650px)" in stylesheet
    assert "grid-template-columns:1fr 1fr" in stylesheet
