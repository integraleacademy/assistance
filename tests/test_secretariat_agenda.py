"""Reception desk views use shared CRM records and scoped, verified identities."""

import copy
from html.parser import HTMLParser

import pytest

import crm_cnaps_tracking
import crm_external_access as access
from crm_app import legacy_app as app


@pytest.fixture
def desk(monkeypatch, tmp_path):
    data = copy.deepcopy(app.DEFAULT_DATA)
    data["crm_contacts"] = [{"id": "test", "nom": "Du-Pré", "prenom": "Chloé", "mail": "chloe@example.test",
        "telephone": "0600000000", "formation": "APS", "statut": "Disqualifié",
        "disqualification_reason": "Projet reporté", "reactivation_date": "2027-01-10",
        "lieu": "Paris", "dates_formation": "Février 2027", "commercial": "Conseiller Test",
        "activities": [], "publications": [], "financement_ft": "OUI", "prochaine_action_manuelle": "Faire le point en janvier"}]
    data["crm_cnaps_scoring_snapshots"] = {"test": {"raw_status": "TRANSMIS", "last_checked_at": "2026-11-03T11:20:00Z"}}
    data["secretariat_demandes"] = []
    data["crm_calendly_appointments"] = []
    monkeypatch.setattr(app, "DATA_FILE", str(tmp_path / "desk.json"))
    monkeypatch.setattr(app, "_load_data_snapshot", lambda: data)
    monkeypatch.setattr(app, "load_data", lambda: data)
    monkeypatch.setattr(app, "save_data", lambda *_: None)
    monkeypatch.setattr(app, "_CRM_REGLEMENTAIRE_CACHE", {})
    app.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    return app.app.test_client(), data


def sign_in(client):
    with client.session_transaction() as s:
        s["user_email"] = "clement@integraleacademy.com"


def test_home_action_order_and_directory(desk):
    class Menu(HTMLParser):
        def __init__(self):
            super().__init__()
            self.keys = []
            self.links = []

        def handle_starttag(self, tag, attrs):
            fields = dict(attrs)
            if tag == "button" and ("data-request" in fields or fields.get("id") == "browseCourses"):
                self.keys.append(fields.get("data-request") or fields["id"])
            if tag == "a":
                self.links.append(fields.get("href"))

    client, _ = desk
    body = client.get("/secretariat").get_data(as_text=True)
    menu = Menu()
    menu.feed(body)
    assert menu.keys == ["formation", "suivi", "autre", "browseCourses"]
    assert all(label in body for label in ["Nouveau prospect", "Suivre un dossier", "Transmettre un message", "Les formations en détails"])
    assert set(["tel:+33487830615", "tel:+33769390457", "tel:+33487830616", "tel:+33743582264",
                "mailto:aurelie@integraleacademy.com", "mailto:cassandre@integraleacademy.com",
                "mailto:ecole@integraleacademy.com"]) <= set(menu.links)
    assert "54 chemin du Carreou" in body and "83480 PUGET SUR ARGENS" in body


def test_day_includes_all_phone_slots_in_paris_keeps_cancellations_and_recorded_outcomes(desk):
    client, data = desk
    def meeting(start, **fields):
        return {"name": "Rendez-vous téléphonique", "start_time": start, "host_name": "Conseiller Test",
                "contact_id": "test", "invitee_email": "private@example.test", "cancel_url": "https://private.invalid", **fields}
    data["crm_calendly_appointments"] = [
        meeting("2026-03-29T13:00:00Z", status="canceled", rescheduled=True),
        meeting("2026-03-28T23:30:00Z", response_status="answered"),
        meeting("2026-03-29T10:00:00Z", name="Dossier", location={"type": "outbound_call"}, response_status="no_answer"),
        meeting("2026-03-29T12:00:00Z", name="Visio", location={"kind": "zoom"}),
        meeting("2026-03-29T14:00:00Z", invitee_name="Appelant A"),
        meeting("2026-03-29T15:00:00Z", status="cancelled"),
        meeting("2026-03-29T22:00:00Z"), meeting("bad"),
    ]
    assert client.get("/api/secretariat/agenda?date=2026-03-29").status_code == 401
    sign_in(client)
    response = client.get("/api/secretariat/agenda?date=2026-03-29")
    assert response.status_code == 200 and "no-store" in response.headers["Cache-Control"]
    rows = response.json["appointments"]
    assert [(r["time"], r["state"]) for r in rows] == [
        ("00:30", "repondu"), ("12:00", "sans_reponse"), ("15:00", "reporte"), ("16:00", "sans_resultat"), ("17:00", "annule")]
    assert rows[0]["person"] == "Chloé Du-Pré" and rows[3]["person"] == "Appelant A"
    assert "private" not in response.get_data(as_text=True) and "contact_id" not in rows[0]
    assert client.get("/api/secretariat/agenda?date=2026-03-30").json["appointments"][0]["time"] == "00:00"
    assert client.get("/api/secretariat/agenda?date=2026-02-30").status_code == 400


def test_confirmed_dossier_and_live_cnaps_are_separate_from_declared_card(desk, monkeypatch):
    client, data = desk
    person = data["crm_contacts"][0]
    person["carte_pro"] = "OUI"
    lookup = {"telephone": person["telephone"], "confirmed_contact_id": "test"}
    assert "summary" not in client.post("/api/secretariat/crm-contact", json=lookup).json
    assert client.post("/api/secretariat/suivi-cnaps", json=lookup).status_code == 401
    sign_in(client)
    summary = client.post("/api/secretariat/crm-contact", json=lookup).json["summary"]
    assert (summary["statut"], summary["motif_disqualification"], summary["reactivation"]) == ("Disqualifié", "Projet reporté", "10/01/2027")
    assert (summary["centre"], summary["session"], summary["conseiller"]) == ("Paris", "Février 2027", "Conseiller Test")
    assert summary["cnaps"]["status"] == "Transmis au CNAPS" and summary["cnaps"]["card"] == "Oui"
    assert "03/11/2026" in summary["cnaps"]["checked"]
    calls = []
    def live(ident, *, refresh_requested):
        calls.append((ident, refresh_requested))
        return app.jsonify({"cnaps": {"cnaps_status": "EN INSTRUCTION", "nub": "private",
            "last_checked_at": "2026-11-07T12:30:00Z", "titles": [{"type": "AP SH", "status": "ACTIF", "expires_at": "2027-01-01"}]}})
    monkeypatch.setattr(app, "_crm_read_reglementaire", live)
    response = client.post("/api/secretariat/suivi-cnaps", json={**lookup, "refresh": True})
    assert calls == [("test", True)] and response.json["cnaps"]["status"] == "En instruction"
    assert "01/01/2027" in response.json["cnaps"]["titles"][0] and "private" not in response.get_data(as_text=True)
    assert client.post("/api/secretariat/suivi-cnaps", json={**lookup, "confirmed_contact_id": "other"}).status_code == 403
    monkeypatch.setattr(app, "_crm_read_reglementaire", lambda *a, **kw: (app.jsonify(error="remote credentials"), 502))
    assert client.post("/api/secretariat/suivi-cnaps", json=lookup).status_code == 503
    assert data["crm_cnaps_scoring_snapshots"]["test"]["raw_status"] == "TRANSMIS"
    person.update(statut="Converti", formation="BTS_MCO")
    assert client.post("/api/secretariat/crm-contact", json=lookup).json["summary"]["statut"] == "Converti"
    assert client.post("/api/secretariat/suivi-cnaps", json=lookup).status_code == 400


def test_alternance_uses_read_only_cnaps_lookup_and_handles_missing_file(desk, monkeypatch):
    client, data = desk
    data["crm_contacts"][0]["formation"] = "Parcours sécurité en alternance"
    sign_in(client)
    seen = []
    def lookup(_, contact, *, cnaps_only):
        seen.append((contact["id"], cnaps_only))
        return app.jsonify(reason="cnaps_not_found"), 404
    monkeypatch.setattr(crm_cnaps_tracking, "proxy_reglementaire", lookup)
    response = client.post("/api/secretariat/suivi-cnaps", json={"telephone": "0600000000", "confirmed_contact_id": "test"})
    assert response.status_code == 200 and response.json["cnaps"]["status"] == "Aucun dossier retrouvé"
    assert seen == [("test", True)]


def test_external_operator_requires_desk_permission_and_verified_identity(desk):
    admin, data = desk
    sign_in(admin)
    admin.get("/crm/acces-secretariat")
    with admin.session_transaction() as s:
        headers = {"X-CSRF-Token": s["external_access_csrf"]}
    login = {"email": "operator@example.test", "password": "fixture-password-1234"}
    assert admin.post("/api/crm/external-accounts", json={**login, "name": "Opérateur Test"}, headers=headers).status_code == 201
    operator = app.app.test_client()
    assert operator.post("/login?next=/secretariat", data=login).location == access.HOME
    assert operator.get("/api/secretariat/agenda").status_code == 403
    assert admin.patch("/api/crm/external-accounts", json={"email": login["email"], "desk_access": True}, headers=headers).json["desk_access"]
    assert operator.get("/api/secretariat/agenda").status_code == 401
    assert operator.post("/login?next=/secretariat", data=login).location == "/secretariat"
    operator.get("/secretariat")
    with operator.session_transaction() as s:
        scoped = {"X-CSRF-Token": s["external_access_csrf"]}
    assert operator.get("/api/secretariat/agenda").status_code == 200
    assert operator.get("/api/crm/calendly/appointments").status_code == 403
    url = "/api/secretariat/crm-contact"
    known = {"telephone": "0600000000", "nom": "Du Pré", "prenom": "Chloe", "verify_identity": True}
    assert operator.post(url, json=known).status_code == 403
    assert operator.post(url, json={"telephone": known["telephone"]}, headers=scoped).json == {"contact_id": None, "verify_identity": True}
    assert operator.post(url, json={**known, "nom": "Autre"}, headers=scoped).status_code == 404
    assert operator.post(url, json={**known, "email": "wrong@example.test"}, headers=scoped).status_code == 404
    limited = operator.post(url, json=known, headers=scoped).json
    assert limited["summary"]["statut"] == "Disqualifié" and "contact" not in limited
    assert not limited["summary"]["prochaine_action"]
    data["crm_contacts"].append({"id": "second", "telephone": known["telephone"]})
    assert operator.post(url, json=known, headers=scoped).status_code == 404
