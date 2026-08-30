import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_PY = ROOT / "app.py"
CRM_JS = ROOT / "static" / "crm.js"
CRM_TITLE_JS = ROOT / "static" / "crm_title.js"
CRM_TEMPLATE = ROOT / "templates" / "crm.html"

EXPECTED_LABELS = {
    "accueil": "Accueil",
    "fil-actu": "Fil d’actualité",
    "calendrier": "Calendrier",
    "contacts": "Contacts",
    "pistes": "Pistes",
    "relances": "Relances",
    "demandes-rappel": "Demande de rappel",
    "inscrits": "Inscrits",
    "disqualifies": "Disqualifiés",
    "notifications": "Notifications",
    "modeles": "Modèles",
    "exports": "Exports",
}


def _backend_page_labels():
    tree = ast.parse(APP_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "CRM_PAGE_LABELS"
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("CRM_PAGE_LABELS is missing")


def test_server_renders_a_specific_title_for_every_allowed_crm_section():
    backend = APP_PY.read_text(encoding="utf-8")
    template = CRM_TEMPLATE.read_text(encoding="utf-8")
    route = backend[
        backend.index('@app.route("/crm", defaults={"section": "accueil"})'):
        backend.index('@app.get("/api/crm/exports/<export_key>")')
    ]

    assert _backend_page_labels() == EXPECTED_LABELS
    assert "if section not in CRM_PAGE_LABELS:" in route
    assert "page_title=CRM_PAGE_LABELS[section]" in route
    assert "<title>{{ page_title }} - Intégrale CRM</title>" in template
    assert "'page_label':page_title" in template
    assert 'CRM_ASSET_VERSION = "20260830-crm-latency-1"' in backend


def test_client_navigation_uses_section_titles_without_breaking_contact_titles():
    javascript = CRM_JS.read_text(encoding="utf-8")
    title_javascript = CRM_TITLE_JS.read_text(encoding="utf-8")
    render = javascript[
        javascript.index("function render(){"):
        javascript.index("async function init(){")
    ]
    contact = javascript[
        javascript.index("async function showContact("):
        javascript.index("const cnapsValue=")
    ]

    assert "window.CRMDocumentTitle.applySection(C.section,C.page_label);" in render
    assert "window.CRMDocumentTitle.reset();" not in render
    assert (
        "if(!c){window.CRMDocumentTitle.applySection(C.section,C.page_label);"
        in contact
    )
    assert (
        "window.CRMDocumentTitle.applyContact(c,C.section,C.page_label);"
        in contact
    )
    assert (
        "C.section=b.dataset.globalPage;"
        "globalSearch.value='';globalResults.classList.remove('open');"
        "history.pushState({},'',b.dataset.globalUrl);render()"
        in javascript
    )
    assert "const SECTION_LABELS=Object.freeze({" in title_javascript
    for section, label in EXPECTED_LABELS.items():
        key = section if section.isidentifier() else f"'{section}'"
        assert f"{key}:'{label}'" in title_javascript


def test_title_module_loads_before_navigation_code_with_the_shared_cache_version():
    template = CRM_TEMPLATE.read_text(encoding="utf-8")

    assert template.index("filename='crm_title.js'") < template.index(
        "filename='crm.js'"
    )
    assert "filename='crm_title.js',v=asset_version" in template
    assert "filename='crm.js',v=asset_version" in template
