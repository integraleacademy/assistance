from pathlib import Path

import app as application


ROOT = Path(__file__).parents[1]


def authenticated_client(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    test_client = application.app.test_client()
    with test_client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return test_client


def test_callback_workspace_only_lists_other_requests(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post("/api/crm/contacts", json={
        "prenom": "Camille", "nom": "Martin",
        "mail": "camille@example.com", "telephone": "0601020304",
    }).get_json()
    data = application.load_data()
    data["secretariat_demandes"] = [
        {
            "id": "callback-linked", "type": "autre", "nom": "Camille Martin",
            "email": "camille@example.com", "telephone": "0601020304",
            "notes": "Question sur son dossier", "rdv": "Demain matin",
            "crm_contact_id": contact["id"], "created_at": "2026-08-24T14:00:00+02:00",
        },
        {
            "id": "callback-unlinked", "type": "autre", "nom": "Nadia Durand",
            "email": "nadia@example.com", "telephone": "0611223344",
            "notes": "Duplicata de facture", "rdv": "Non souhaité",
            "created_at": "2026-08-24T15:00:00+02:00",
        },
        {
            "id": "training-request", "type": "formation", "nom": "Lina Test",
            "email": "lina@example.com", "created_at": "2026-08-24T16:00:00+02:00",
        },
    ]
    application.save_data(data)

    page = client.get("/crm/demandes-rappel")
    bootstrap = client.get("/api/crm/bootstrap?section=demandes-rappel")

    assert page.status_code == 200
    assert b'data-nav="demandes-rappel"' in page.data
    assert "Demande de rappel - Intégrale CRM".encode() in page.data
    assert bootstrap.status_code == 200
    rows = bootstrap.get_json()["callback_requests"]
    assert [row["id"] for row in rows] == ["callback-unlinked", "callback-linked"]
    assert rows[0]["crm_contact_id"] == ""
    assert rows[1]["crm_contact_id"] == contact["id"]
    assert rows[1]["crm_contact_name"] == "Camille MARTIN"


def test_callback_workspace_ui_explains_lead_linking():
    javascript = (ROOT / "static" / "crm.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "static" / "crm.css").read_text(encoding="utf-8")

    assert "function callbackRequestsPage()" in javascript
    assert "if(C.section==='demandes-rappel')return callbackRequestsPage();" in javascript
    assert "callbackRequests=snapshot.callback_requests||[]" in javascript
    assert "Aucune piste créée automatiquement" in javascript
    assert 'href="/crm/contacts?fiche=' in javascript
    assert ".callback-request-table" in stylesheet
