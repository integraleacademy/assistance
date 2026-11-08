"""Course-specific information and its handoff through the actual call desk."""
import copy
import datetime
import json

import pytest

from crm_app import legacy_app as app
import crm_public_sessions as calendar


def test_calendar_combines_local_sessions_and_published_details(monkeypatch):
    centre = "Puget-sur-Argens / Côte d’Azur"
    def published(slug, start, end, title, **fields):
        return {"training": {"slug": slug}, "location": centre, "status": "OPEN",
                "title": title, "startDate": start, "endDate": end, **fields}

    source = [
        published("aps", "2027-01-04", "2027-02-08", "APS",
                  examDate="2027-02-09", remoteStartDate="2027-01-04", remoteEndDate="2027-01-14",
                  inPersonStartDate="2027-01-15", inPersonEndDate="2027-02-08"),
        published("a3p-apr", "2026-11-09", "2027-01-19", "A3P"),
        published("ssiap-1", "2027-02-11", "2027-02-26", "SSIAP 1 avec SST", showSeatsLeft=False, seatsLeft=1),
        published("ssiap-1", "2027-02-15", "2027-02-26", "SSIAP 1 sans SST", examDate="2027-03-01"),
        published("desp-dssp", "2027-03-01", "2027-04-20", "Non publié", status="HIDDEN"),
        published("aps", "2026-10-03", "2026-11-15", "Ancienne session"),
        published("aps", "date invalide", "2027-01-31", "Ligne incomplète"),
    ]
    local = {"cote_azur": {
        "A3P": [{"label": "Du 10 novembre 2026 au 14 janvier 2027"}],
        "APS": [{"label": "Du 4 février au 10 mars 2027"}, {"label": "Du 4 janvier au 8 février 2027"}],
        "DESP_VAE": [{"label": "Du 1er mars au 31 mars 2027"}, {"label": "Du 5 au 12 avril 2027"}],
    }}
    snapshot = copy.deepcopy(local)
    monkeypatch.setattr(calendar, "_published_sessions", lambda: calendar._public_calendar(source))
    result = calendar.desk_calendar(local, today=datetime.date(2026, 11, 8),
                                    date_range=app._session_date_range, use_public=True)["cote_azur"]
    assert local == snapshot
    assert [r["start_date"] for r in result["APS"]] == ["2027-01-04", "2027-02-04"]
    january = result["APS"][0]
    assert january["date_examen"] == "2027-02-09"
    assert january["periods"] == "À distance du 04/01 au 14/01/2027 · En centre du 15/01 au 08/02/2027"
    assert [r["label"] for r in result["A3P"]] == ["Du 10 novembre 2026 au 14 janvier 2027"]
    assert len(result["SSIAP"]) == 2
    assert "avec SST" in result["SSIAP"][0]["label"] and not result["SSIAP"][0].get("badge")
    assert "sans SST" in result["SSIAP"][1]["label"]
    assert result["DESP_VAE"][0]["month_preference"] == "Mars 2027"
    assert "month_preference" not in result["DESP_VAE"][1]
    assert not result.get("DESP_INIT")


@pytest.mark.parametrize(("course", "date", "period", "expected", "legacy"), [
    ("A3P", "Du 9 novembre 2026 au 19 janvier 2027", "", 4200, 4200),
    ("A3P", "Du 19 janvier au 16 mars 2027", "", 4250, 4200),
    ("APS", "Du 4 janvier au 8 février 2027", "", 1700, 1650),
    ("DESP_INIT", "Du 4 janvier au 18 février 2027", "", 4350, 4300),
    ("DESP_VAE", "", "Mars 2027", 3850, 3800),
    ("SSIAP", "Du 15 au 26 février 2027 (sans SST)", "", 980, 1230),
    ("SSIAP", "Du 11 au 26 février 2027 (avec SST)", "", 1230, 1230),
])
def test_selected_tariff_matches_quote_and_automatic_email(monkeypatch, course, date, period, expected, legacy):
    data = {"demandes": []}
    entry = {"id": "desk-1", "type": "formation", "formation": course, "devis": "OUI", "nom": "Test",
             "formation_session_label": date, "formation_date_souhaitee": date,
             "rentree_souhaitee": period, "created_at": "2026-11-08T12:00:00+01:00"}
    contact = {"id": "contact-1", "prenom": "Camille", "nom": "Test", "activities": []}
    monkeypatch.setattr(app, "load_data", lambda: data)
    monkeypatch.setattr(app, "_send_secretariat_information_messages", lambda *_: {})
    with app.app.test_request_context():
        quote = app._ensure_secretariat_quote(data, entry, contact)
        details = json.loads(quote["details"])
        assert app.get_formation_tarif(course, details) == expected
        assert app.build_devis_context(course, course, date, formation_details=details)["devis_total"] == f"{expected} €"
        assert app.get_formation_tarif(course, {"source_secretariat_id": "legacy"}) == legacy
        assert app.get_formation_tarif(course, {"secretariat_fee_euros": expected}) == legacy
        mail = app._secretariat_automatic_information_template(data, entry, contact)["contenu"]
        assert app._eur_display(expected) in mail
        assert "{tuition}" not in mail and "{lodging}" not in mail and "{deposit}" not in mail
        if course == "DESP_VAE":
            assert "1 155 €" in mail
        if course == "A3P":
            assert f"{350 if expected == 4250 else 300} € TTC" in mail

@pytest.mark.parametrize("code", sorted(app.BTS_COURSES))
def test_bts_registration_handoff_matches_the_sheet(code):
    course = app.SECRETARIAT_FORMATIONS[code]
    with app.app.test_request_context():
        template = app._secretariat_automatic_information_template({}, {"formation": code}, {"prenom": "Camille"})
    target = course["application_url"]
    assert target == "https://admissions.integraleconnect.com/"
    assert target in template["contenu"] and "candidature" in template["contenu"]
    sms = app._build_secretariat_followup_sms(code)
    assert target in sms and "CPF" not in sms
