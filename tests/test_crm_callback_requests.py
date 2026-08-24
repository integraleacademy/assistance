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
    bootstrap_payload = bootstrap.get_json()
    rows = bootstrap_payload["callback_requests"]
    assert bootstrap_payload["callback_pending_count"] == 2
    assert [row["id"] for row in rows] == ["callback-unlinked", "callback-linked"]
    assert rows[0]["crm_contact_id"] == ""
    assert rows[1]["crm_contact_id"] == contact["id"]
    assert rows[1]["crm_contact_name"] == "Camille MARTIN"
    assert rows[0]["status"] == "pending"
    assert rows[1]["status"] == "pending"

    detail = client.get(f"/api/crm/contacts/{contact['id']}").get_json()
    callback_activity = next(
        activity for activity in detail["activities"]
        if activity.get("callback_request_id") == "callback-linked"
    )
    assert callback_activity["kind"] == "demande_rappel"
    assert callback_activity["title"] == "Demande de rappel reçue"
    assert callback_activity["callback_status"] == "pending"
    assert "Demande : Question sur son dossier" in callback_activity["detail"]
    assert "Rendez-vous : Demain matin" in callback_activity["detail"]


def test_callback_pending_count_is_available_on_every_crm_page(
        tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    data = application.load_data()
    data["secretariat_demandes"] = [
        {"id": "pending", "type": "autre", "callback_status": "pending"},
        {"id": "processed", "type": "autre", "callback_status": "processed"},
        {"id": "training", "type": "formation"},
    ]
    application.save_data(data)

    response = client.get("/api/crm/bootstrap?section=accueil")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["callback_requests"] == []
    assert payload["callback_pending_count"] == 1


def test_callback_request_can_be_processed_and_reopened(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post("/api/crm/contacts", json={
        "prenom": "Camille", "nom": "Martin",
        "mail": "camille@example.com", "telephone": "0601020304",
    }).get_json()
    data = application.load_data()
    data["secretariat_demandes"] = [{
        "id": "callback-status", "type": "autre", "nom": "Camille Martin",
        "email": "camille@example.com", "telephone": "0601020304",
        "notes": "Souhaite connaître l'état de son dossier.",
        "rdv": "Non souhaité", "crm_contact_id": contact["id"],
        "created_at": "2026-08-24T16:48:00+02:00", "statut": "Traité",
    }]
    application.save_data(data)
    client.get("/api/crm/bootstrap?section=demandes-rappel")
    stale_pending_snapshot = application.load_data()

    processed_response = client.patch(
        "/api/crm/callback-requests/callback-status",
        json={"status": "processed"},
    )

    assert processed_response.status_code == 200
    processed_payload = processed_response.get_json()
    processed = processed_payload["request"]
    assert processed["status"] == "processed"
    assert processed["processed_at"]
    assert processed["processed_by"]
    assert processed_payload["callback_pending_count"] == 0
    assert processed_payload["contact"]["id"] == contact["id"]
    assert any(
        activity.get("title") == "Demande de rappel traitée"
        for activity in processed_payload["contact"]["activities"]
    )
    stored = application.load_data()
    assert len(stored["crm_contacts"]) == 1
    assert stored["secretariat_demandes"][0]["statut"] == "Traité"
    assert any(
        activity.get("title") == "Demande de rappel traitée"
        and activity.get("callback_request_id") == "callback-status"
        for activity in stored["crm_contacts"][0]["activities"]
    )

    # Une autre sauvegarde partie avant le clic ne doit pas rétablir l'ancien
    # statut ni supprimer la trace du traitement dans le journal.
    stale_pending_snapshot["crm_contacts"][0]["commentaires"] = (
        "Modification concurrente à conserver"
    )
    application.save_data(stale_pending_snapshot)
    stored = application.load_data()
    assert stored["secretariat_demandes"][0]["callback_status"] == "processed"
    assert stored["crm_contacts"][0]["commentaires"] == (
        "Modification concurrente à conserver"
    )
    assert any(
        activity.get("title") == "Demande de rappel traitée"
        for activity in stored["crm_contacts"][0]["activities"]
    )
    refreshed_contact = client.get(
        f"/api/crm/contacts/{contact['id']}"
    ).get_json()
    receipt = next(
        activity for activity in refreshed_contact["activities"]
        if activity.get("callback_event") == "received"
        and activity.get("callback_request_id") == "callback-status"
    )
    assert receipt["callback_status"] == "processed"
    stale_processed_snapshot = application.load_data()

    reopened_response = client.patch(
        "/api/crm/callback-requests/callback-status",
        json={"status": "pending"},
    )

    assert reopened_response.status_code == 200
    reopened_payload = reopened_response.get_json()
    reopened = reopened_payload["request"]
    assert reopened["status"] == "pending"
    assert reopened["processed_at"] == ""
    assert reopened_payload["callback_pending_count"] == 1
    assert reopened_payload["contact"]["id"] == contact["id"]
    assert any(
        activity.get("title") == "Demande de rappel rouverte"
        for activity in reopened_payload["contact"]["activities"]
    )
    stored = application.load_data()
    assert len(stored["crm_contacts"]) == 1
    assert stored["secretariat_demandes"][0]["statut"] == "À traiter"
    assert any(
        activity.get("title") == "Demande de rappel rouverte"
        and activity.get("callback_request_id") == "callback-status"
        for activity in stored["crm_contacts"][0]["activities"]
    )

    # La même protection s'applique dans l'autre sens après « Rouvrir ».
    application.save_data(stale_processed_snapshot)
    stored = application.load_data()
    assert stored["secretariat_demandes"][0]["callback_status"] == "pending"
    assert any(
        activity.get("title") == "Demande de rappel rouverte"
        for activity in stored["crm_contacts"][0]["activities"]
    )
    receipt = next(
        activity for activity in stored["crm_contacts"][0]["activities"]
        if activity.get("callback_event") == "received"
        and activity.get("callback_request_id") == "callback-status"
    )
    assert receipt["title"] == "Demande de rappel reçue"
    assert receipt["callback_status"] == "pending"


def test_callback_workspace_repairs_legacy_call_activity_without_duplicate(
        tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post("/api/crm/contacts", json={
        "prenom": "Clément", "nom": "Vaillant",
        "mail": "clement@example.com", "telephone": "066525271",
    }).get_json()
    data = application.load_data()
    stored_contact = next(
        item for item in data["crm_contacts"] if item["id"] == contact["id"]
    )
    stored_contact["activities"] = [{
        "id": "legacy-call", "kind": "appel",
        "title": "Demande de rappel reçue",
        "detail": "Détail de la demande existante\nRendez-vous : Non souhaité",
        "date": "2026-08-24T16:48:00+02:00", "author": "Secrétariat",
    }]
    data["secretariat_demandes"] = [{
        "id": "legacy-callback", "type": "autre", "nom": "Clément Vaillant",
        "email": "clement@example.com", "telephone": "066525271",
        "notes": "Détail de la demande existante", "rdv": "Non souhaité",
        "crm_contact_id": contact["id"],
        "created_at": "2026-08-24T16:48:00+02:00", "statut": "Traité",
    }]
    application.save_data(data)

    response = client.get("/api/crm/bootstrap?section=demandes-rappel")

    assert response.status_code == 200
    row = response.get_json()["callback_requests"][0]
    assert row["status"] == "pending"
    stored = application.load_data()
    activities = stored["crm_contacts"][0]["activities"]
    assert len(activities) == 1
    assert activities[0]["id"] == "legacy-call"
    assert activities[0]["kind"] == "demande_rappel"
    assert activities[0]["callback_request_id"] == "legacy-callback"
    assert activities[0]["callback_event"] == "received"
    assert activities[0]["detail"] == (
        "Demande : Détail de la demande existante\n"
        "Rendez-vous : Non souhaité"
    )


def test_callback_workspace_ui_explains_lead_linking():
    javascript = (ROOT / "static" / "crm.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "static" / "crm.css").read_text(encoding="utf-8")
    template = (ROOT / "templates" / "crm.html").read_text(encoding="utf-8")

    assert "function callbackRequestsPage()" in javascript
    assert "if(C.section==='demandes-rappel')return callbackRequestsPage();" in javascript
    assert "callbackRequests=snapshot.callback_requests||[]" in javascript
    assert "Aucune piste créée automatiquement" in javascript
    assert 'href="/crm/contacts?fiche=' in javascript
    assert "Marquer comme traitée" in javascript
    assert 'data-callback-filter="pending"' in javascript
    assert "/api/crm/callback-requests/" in javascript
    assert "'demande_rappel'" in javascript
    assert "data-callback-action" in javascript
    assert "async function saveCallbackRequestStatus" in javascript
    assert "activityFeed.onclick=async event=>" in javascript
    assert "function updateCallbackRequestNavCount()" in javascript
    assert "snapshot.callback_pending_count" in javascript
    assert 'id="callbackRequestNavCount"' in template
    assert ".callback-request-table" in stylesheet
    assert ".callback-request-status.pending" in stylesheet
    assert ".feed-item.callback-activity" in stylesheet
    assert ".feed-callback-action" in stylesheet
    assert ".callback-nav-count" in stylesheet
