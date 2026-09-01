from pathlib import Path
import subprocess

import app as application
import crm_cnaps_tracking


ROOT = Path(__file__).resolve().parents[1]
CRM_JS = ROOT / "static" / "crm.js"
CRM_CSS = ROOT / "static" / "crm.css"


class DummyResponse:
    def __init__(self, payload, status_code=200, content_type="application/json"):
        self._payload = payload
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}

    def json(self):
        return self._payload


class DummySession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def crm_client(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application._DATA_CACHE_PAYLOAD = None
    application._DATA_CACHE_SIGNATURE = None
    application._CRM_READ_MODEL_KEY = None
    application._CRM_READ_MODEL_VALUE = None
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    client = application.app.test_client()
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return client


def create_contact(client, *, nom="Lardjane"):
    response = client.post(
        "/api/crm/contacts",
        json={
            "prenom": "Zinedine",
            "nom": nom,
            "mail": "zinedine@example.com",
            "formation": "APS",
        },
    )
    assert response.status_code == 201
    return response.get_json()


def test_cnaps_lookup_matches_gestion_stagiaires_payload_and_result():
    session = DummySession([DummyResponse({
        "results": [
            {
                "nom": "LARDJANE",
                "prenom": "Zinedine",
                "nub": "1000731",
                "typeActivite": (
                    "Autorisation préalable - Surveillance humaine ou gardiennage"
                ),
                "agrementStatutEs": "ACTIF",
                "dateFinValidite": "2026-10-07",
            },
            {
                "nom": "LARDJANE",
                "prenom": "Zinedine",
                "nub": "1000731",
                "typeActivite": (
                    "Carte professionnelle - Protection physique des personnes"
                ),
                "agrementStatutEs": "ACTIF",
                "dateFinValidite": "2031-06-30",
            },
            {
                "nom": "DUPONT",
                "prenom": "Zinedine",
                "nub": "1000731",
                "typeActivite": "Carte professionnelle - Surveillance humaine",
                "agrementStatutEs": "ACTIF",
                "dateFinValidite": "2031-06-30",
            },
        ],
        "totalPages": 1,
    })])

    result = crm_cnaps_tracking.fetch_cnaps_card_validity(
        "Lardjane", "1000731", session=session,
    )

    assert session.calls[0]["url"] == (
        "https://espace-consultation.cnaps.interieur.gouv.fr/annuaire/"
        "api/back/public/annuaire/search/personne-physique"
    )
    assert session.calls[0]["json"] == {
        "nom": "LARDJANE",
        "nub": "1000731",
        "page": 0,
        "size": 10,
        "sorts": [
            {"field": "nom", "asc": True},
            {"field": "dateFinValidite", "asc": True},
        ],
    }
    assert result["check_status"] == "success"
    assert [title["display_status"] for title in result["titles"]] == [
        "AP SH ACTIF", "CP A3P ACTIF",
    ]
    assert result["titles"][1]["expires_at"] == "2031-06-30"
    assert len(result["active_titles"]) == 2


def test_cnaps_lookup_returns_safe_error_for_remote_failure():
    session = DummySession([DummyResponse({"error": "detail"}, status_code=500)])

    result = crm_cnaps_tracking.fetch_cnaps_card_validity(
        "Lardjane", "1000731", session=session,
    )

    assert result["check_status"] == "error"
    assert result["error"] == "cnaps_unavailable"
    assert result["http_status"] == 500
    assert "detail" not in str(result)


def test_crm_cnaps_validity_route_saves_nub_and_returns_titles(
    tmp_path, monkeypatch,
):
    client = crm_client(tmp_path, monkeypatch)
    contact = create_contact(client)
    captured = {}

    def fake_lookup(last_name, nub):
        captured.update(last_name=last_name, nub=nub)
        return {
            "check_status": "success",
            "checked_at": "2026-09-01T08:00:00+00:00",
            "nub": nub,
            "titles": [{
                "code": "CP SH",
                "label": "Carte professionnelle - Surveillance humaine",
                "status": "ACTIF",
                "display_status": "CP SH ACTIF",
                "expires_at": "2031-06-30",
            }],
            "active_titles": [],
            "message": None,
            "error": None,
        }

    monkeypatch.setattr(
        crm_cnaps_tracking, "fetch_cnaps_card_validity", fake_lookup,
    )

    response = client.post(
        f"/api/crm/contacts/{contact['id']}/cnaps-card-validity",
        json={"nub": "1000731"},
    )

    assert response.status_code == 200
    assert response.get_json()["titles"][0]["display_status"] == "CP SH ACTIF"
    assert captured == {"last_name": "LARDJANE", "nub": "1000731"}
    stored = client.get(f"/api/crm/contacts/{contact['id']}").get_json()
    assert stored["cnaps_nub"] == "1000731"


def test_crm_cnaps_validity_route_validates_nub_and_name(tmp_path, monkeypatch):
    client = crm_client(tmp_path, monkeypatch)
    contact = create_contact(client, nom="")

    invalid_nub = client.post(
        f"/api/crm/contacts/{contact['id']}/cnaps-card-validity",
        json={"nub": "123"},
    )
    missing_name = client.post(
        f"/api/crm/contacts/{contact['id']}/cnaps-card-validity",
        json={"nub": "1234567"},
    )

    assert invalid_nub.status_code == 400
    assert missing_name.status_code == 422


def test_cnaps_validity_frontend_uses_the_api_and_renders_the_result():
    javascript = CRM_JS.read_text(encoding="utf-8")
    styles = CRM_CSS.read_text(encoding="utf-8")

    assert "CNAPS_CARD_LOOKUP_URL" not in javascript
    assert "/cnaps-card-validity" in javascript
    assert 'id="cnapsCardValidityResult"' in javascript
    assert "cnapsCardValidityMarkup" in javascript
    assert "CP SH ACTIF" not in javascript
    assert "Portail CNAPS ouvert" not in javascript
    assert ".cnaps-card-validity-chip.is-active" in styles
    assert ".cnaps-card-validity-chip.is-inactive" in styles


def test_cnaps_validity_frontend_distinguishes_active_and_expired_titles():
    javascript = CRM_JS.read_text(encoding="utf-8")
    renderer = javascript[
        javascript.index("const cnapsCardTitleTone="):
        javascript.index("function safeAdminUrl")
    ]
    script = renderer + r"""
const active=cnapsCardValidityMarkup({checked_at:'2026-09-01T08:00:00Z',titles:[
 {code:'CP SH',status:'ACTIF',display_status:'CP SH ACTIF',label:'Carte professionnelle',expires_at:'2099-06-30'}
]});
const expired=cnapsCardValidityMarkup({checked_at:'2026-09-01T08:00:00Z',titles:[
 {code:'CP SH',status:'ACTIF',display_status:'CP SH ACTIF',label:'Carte professionnelle',expires_at:'2020-06-30'}
]});
const empty=cnapsCardValidityMarkup({titles:[]});
if(!active.includes('is-active')||!active.includes('CP SH ACTIF'))process.exit(1);
if(!expired.includes('is-inactive'))process.exit(2);
if(!empty.includes('Aucun titre CNAPS trouvé'))process.exit(3);
"""
    harness = r"""
const esc=value=>String(value??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const cnapsDate=value=>value;
const relativeSync=value=>'à l’instant';
"""

    completed = subprocess.run(
        ["node", "-e", harness + script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
