from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CRM_CSS = ROOT / "static" / "crm.css"


def test_all_contact_sheet_sections_scroll_with_the_document():
    stylesheet = CRM_CSS.read_text(encoding="utf-8")
    rule_start = stylesheet.index(
        ".back-contact{margin-bottom:10px}.contact-head{"
    )
    desktop_rules = stylesheet[
        rule_start:stylesheet.index("@media(max-width:1000px)", rule_start)
    ]

    assert ".contact-head{position:relative;" in desktop_rules
    assert ".contact-head{position:sticky" not in desktop_rules
    assert ".contact-head{position:fixed" not in desktop_rules
    assert ".contact-subnav{position:relative;" in desktop_rules
    assert ".contact-subnav{position:sticky" not in desktop_rules
    assert ".contact-subnav{position:fixed" not in desktop_rules
    assert ".contact-side-column{position:static}" in desktop_rules
    assert ".contact-side-column{position:sticky" not in desktop_rules
    assert ".contact-side-column{position:fixed" not in desktop_rules
