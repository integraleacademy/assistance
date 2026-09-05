from io import BytesIO
from pathlib import Path
import subprocess

import pytest

import app as application


ROOT = Path(__file__).resolve().parents[1]
CRM_JS = ROOT / "static" / "crm.js"
CRM_CSS = ROOT / "static" / "crm.css"


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


def create_contact(client, *, formation, desp_type="", financement_ft="OUI"):
    contact = client.post(
        "/api/crm/contacts",
        json={
            "prenom": "Lina",
            "nom": "Martin",
            "mail": "lina@example.com",
            "formation": formation,
        },
    ).get_json()
    response = client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"desp_type": desp_type, "financement_ft": financement_ft},
    )
    assert response.status_code == 200
    return response.get_json()


def install_ft_templates(client):
    templates = (
        (
            "Financement FT A3P",
            "Financement A3P de {{ prenom }}",
            "Demande-de-financement-Formation-A3P.pdf",
        ),
        (
            "Financement FT DESP",
            "Financement DESP de {{ prenom }}",
            "Demande_de_financement_DESP.pdf",
        ),
    )
    for name, subject, attachment_name in templates:
        response = client.post(
            "/api/crm/templates",
            data={
                "type": "email",
                "nom": name,
                "sujet": subject,
                "contenu": "<p>Dossier pour {{ formation }}</p>",
                "attachment": (BytesIO(b"PDF funding form"), attachment_name),
            },
            content_type="multipart/form-data",
        )
        assert response.status_code == 201


def test_cnaps_nub_is_persisted_and_limited_to_seven_digits(tmp_path, monkeypatch):
    client = crm_client(tmp_path, monkeypatch)
    contact = create_contact(client, formation="APS", financement_ft="NON")

    saved = client.patch(
        f"/api/crm/contacts/{contact['id']}", json={"cnaps_nub": "1234567"},
    )
    too_long = client.patch(
        f"/api/crm/contacts/{contact['id']}", json={"cnaps_nub": "12345678"},
    )
    letters = client.patch(
        f"/api/crm/contacts/{contact['id']}", json={"cnaps_nub": "ABC1234"},
    )

    assert saved.status_code == 200
    assert saved.get_json()["cnaps_nub"] == "1234567"
    assert too_long.status_code == letters.status_code == 400
    stored = client.get(f"/api/crm/contacts/{contact['id']}").get_json()
    assert stored["cnaps_nub"] == "1234567"


@pytest.mark.parametrize(
    ("formation", "desp_type", "template_name", "attachment_name"),
    [
        (
            "A3P",
            "",
            "Financement FT A3P",
            "Demande-de-financement-Formation-A3P.pdf",
        ),
        (
            "DESP",
            "INITIAL",
            "Financement FT DESP",
            "Demande_de_financement_DESP.pdf",
        ),
    ],
)
def test_ft_funding_file_sends_the_training_template(
    tmp_path, monkeypatch, formation, desp_type, template_name, attachment_name,
):
    client = crm_client(tmp_path, monkeypatch)
    contact = create_contact(
        client, formation=formation, desp_type=desp_type, financement_ft="OUI",
    )
    install_ft_templates(client)
    deliveries = []

    def fake_send(to, subject, plain, html, attachments_paths=None):
        paths = [Path(path) for path in attachments_paths or []]
        deliveries.append({
            "to": to,
            "attachment_names": [path.name for path in paths],
            "attachment_contents": [path.read_bytes() for path in paths],
        })
        return True

    monkeypatch.setattr(application, "send_email_html", fake_send)

    response = client.post(
        f"/api/crm/contacts/{contact['id']}/france-travail-funding-file"
    )

    assert response.status_code == 200
    assert response.get_json()["template_name"] == template_name
    assert len(deliveries) == 1
    assert deliveries[0]["to"] == "lina@example.com"
    assert deliveries[0]["attachment_names"] == [attachment_name]
    assert deliveries[0]["attachment_contents"] == [b"PDF funding form"]
    data = application.load_data()
    template = next(
        item for item in data["crm_email_templates"] if item["nom"] == template_name
    )
    stored = next(
        item for item in data["crm_contacts"] if item["id"] == contact["id"]
    )
    assert template["usage_count"] == 1
    assert template["last_used_at"] == stored["updated_at"]
    assert stored["activities"][0]["title"] == f"E-mail « {template_name} » envoyé"


@pytest.mark.parametrize(
    ("formation", "desp_type", "financement_ft"),
    [
        ("APS", "", "OUI"),
        ("DESP", "VAE", "OUI"),
        ("A3P", "", "NON"),
    ],
)
def test_ft_funding_file_rejects_ineligible_contacts(
    tmp_path, monkeypatch, formation, desp_type, financement_ft,
):
    client = crm_client(tmp_path, monkeypatch)
    contact = create_contact(
        client,
        formation=formation,
        desp_type=desp_type,
        financement_ft=financement_ft,
    )
    install_ft_templates(client)
    monkeypatch.setattr(application, "_crm_send_email_html", lambda *args, **kwargs: True)

    response = client.post(
        f"/api/crm/contacts/{contact['id']}/france-travail-funding-file"
    )

    assert response.status_code == 409


def test_crm_bootstrap_only_exposes_sessions_that_have_not_started(
    tmp_path, monkeypatch,
):
    client = crm_client(tmp_path, monkeypatch)
    data = application.load_data()
    data["formation_sessions"] = {
        "cote_azur": {
            "APS": [
                {"label": "Du 1er janvier au 31 janvier 2020", "badge": ""},
                {"label": "Du 1er janvier au 31 janvier 2099", "badge": ""},
            ]
        }
    }
    application.save_data(data)

    response = client.get("/api/crm/bootstrap?section=contacts")

    assert response.status_code == 200
    labels = [
        row["label"]
        for row in response.get_json()["formation_sessions"]["cote_azur"]["APS"]
    ]
    assert labels == ["Du 1er janvier au 31 janvier 2099"]


def test_contact_sheet_frontend_contract_for_requested_rules():
    javascript = CRM_JS.read_text(encoding="utf-8")
    styles = CRM_CSS.read_text(encoding="utf-8")
    backend = (ROOT / "app.py").read_text(encoding="utf-8")

    assert 'name="cnaps_nub"' in javascript
    assert 'data-show="with-card"' in javascript
    assert 'class="cnaps-panel conditional" data-show="without-card"' in javascript
    assert "needsCnapsTracking" in javascript
    assert "/cnaps-card-validity" in javascript
    assert "CNAPS_CARD_LOOKUP_URL" not in javascript
    assert "Résumé IA ·" not in javascript
    assert "['RDV',counts.appointments]" in javascript
    assert "['RDV total'" not in javascript
    assert 'id="sendFtFundingFile"' in javascript
    assert 'data-show="ft-file-send"' in javascript
    assert "isFranceTravailFundingFileEligible" in javascript
    assert "france-travail-funding-file" in javascript
    assert '"Financement FT A3P"' in backend
    assert '"Financement FT DESP"' in backend
    for tone in (
        "formation-a3p", "formation-aps", "formation-vtc",
        "formation-desp", "formation-ssiap",
    ):
        assert f".contact-journey-label.{tone}" in styles
    assert 'disabled>${esc(selectedSession)} — date enregistrée, non proposée' in javascript


def test_today_appointment_stays_in_the_spotlight_until_a_result_is_saved():
    javascript = CRM_JS.read_text(encoding="utf-8")
    appointment_helpers = javascript[
        javascript.index("const CALENDLY_APPOINTMENT_PAST_DELAY_MS="):
        javascript.index("const esc=")
    ]
    paris_date_key = javascript[
        javascript.index("const parisDateKey="):
        javascript.index("\n", javascript.index("const parisDateKey="))
    ]
    groups = javascript[
        javascript.index("function calendlyAppointmentGroups"):
        javascript.index("function calendlyDateParts")
    ]
    script = appointment_helpers + paris_date_key + "\n" + groups + r"""
const assert=require('node:assert/strict');
const now=Date.parse('2026-09-01T15:00:00Z');
const tomorrow={id:'tomorrow',start_time:'2026-09-02T08:00:00Z',status:'active'};
const later={id:'later',start_time:'2026-09-09T08:00:00Z',status:'active'};
const todayPast={id:'today',start_time:'2026-09-01T08:00:00Z',status:'active'};
const completed={id:'completed',start_time:'2026-08-31T08:00:00Z',status:'active',response_status:'answered'};
const canceled={id:'canceled',start_time:'2026-09-10T08:00:00Z',status:'canceled'};
const result=calendlyAppointmentGroups([tomorrow,later,todayPast,completed,canceled],now);
assert.equal(result.today.id,'today');
assert.equal(result.next.id,'tomorrow');
assert.equal(result.past.some(item=>item.id==='today'),true);
const display=calendlyAppointmentDisplay(result);
assert.deepEqual(display.spotlight.map(([,appointment])=>appointment.id),['today','tomorrow','later']);
assert.deepEqual(display.spotlight.map(([label])=>label),['Rendez-vous du jour','Prochain rendez-vous','Rendez-vous à venir']);
assert.deepEqual(display.past.map(appointment=>appointment.id),['completed']);
assert.deepEqual(display.canceled.map(appointment=>appointment.id),['canceled']);
assert.equal(display.historyCount,2);
for(const response_status of ['answered','no_answer']){
 const treated=calendlyAppointmentGroups([tomorrow,{...todayPast,response_status}],now);
 assert.equal(treated.today,undefined);
 assert.equal(treated.next.id,'tomorrow');
 assert.equal(treated.past.some(item=>item.id==='today'),true);
}
"""

    completed = subprocess.run(
        ["node", "-e", script], check=False, capture_output=True, text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "a.status!=='canceled'&&!calendlyAppointmentHasResult(a)" in javascript
    assert "const sections=historyCount?" in javascript
    assert "Autres rendez-vous à venir" not in javascript
