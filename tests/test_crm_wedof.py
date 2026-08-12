import json
import sqlite3
from types import SimpleNamespace

import app as application


SECRET = "wedof-super-secret"


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._payload


def authenticated_client(tmp_path, monkeypatch, email="clement@integraleacademy.com"):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    monkeypatch.setenv("WEDOF_DB_PATH", str(tmp_path / "wedof.sqlite3"))
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    client = application.app.test_client()
    with client.session_transaction() as session:
        session["user_email"] = email
    return client


def create_contact(client, *, email="", phone=""):
    contact = client.post("/api/crm/contacts", json={
        "prenom": "Lina", "nom": "Martin", "force_create": True,
    }).get_json()
    return client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"mail": email, "telephone": phone},
    ).get_json()


def folder(identifier, email="", phone="", first_name="", last_name=""):
    return {
        "externalId": identifier,
        "attendee": {
            "email": email, "phone": phone, "firstName": first_name,
            "lastName": last_name, "custom": "kept",
        },
        "state": "accepted",
        "billingState": "paid",
        "controlState": "ok",
        "type": "cpf",
        "history": [{"state": "accepted"}],
        "trainingActionInfo": {"title": "APS"},
        "files": [{"name": "proof.pdf"}],
        "metadata": {"arbitrary": {"nested": True}},
        "tags": ["important"],
        "_links": {"self": {"href": "/folder"}},
        "unknownFutureField": [1, 2, 3],
    }


def test_missing_configuration_and_private_status(tmp_path, monkeypatch):
    monkeypatch.delenv("WEDOF_API_KEY", raising=False)
    client = authenticated_client(tmp_path, monkeypatch)
    payload = client.get("/api/crm/wedof/status").get_json()
    assert payload["configured"] is False
    assert payload["connected"] is False
    assert application.app.test_client().get("/api/crm/wedof/status").status_code == 302


def test_background_sync_starts_only_when_wedof_is_configured(monkeypatch):
    monkeypatch.setattr(application, "_WEDOF_POLLER_STARTED", False)
    monkeypatch.delenv("WEDOF_API_KEY", raising=False)
    assert application._start_wedof_background_sync() is False

    started = []

    class FakeThread:
        def __init__(self, **kwargs):
            started.append(kwargs)

        def start(self):
            started.append("started")

    monkeypatch.setenv("WEDOF_API_KEY", SECRET)
    monkeypatch.setattr(application.threading, "Thread", FakeThread)
    assert application._start_wedof_background_sync() is True
    assert started[0]["name"] == "wedof-crm-auto-sync"
    assert started[0]["daemon"] is True
    assert started[1] == "started"
    assert application._start_wedof_background_sync() is False


def test_concurrent_sync_returns_without_starting_a_second_scan():
    application._WEDOF_SYNC_LOCK.acquire()
    try:
        result = application._wedof_sync()
    finally:
        application._WEDOF_SYNC_LOCK.release()
    assert result["ok"] is True
    assert result["in_progress"] is True
    assert result["created_contacts"] == 0


def test_api_key_header_and_errors_never_leak_secret(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    monkeypatch.setenv("WEDOF_API_KEY", SECRET)
    captured = {}

    def fake_get(url, **kwargs):
        captured.update(url=url, **kwargs)
        return FakeResponse({"message": SECRET}, 401)

    monkeypatch.setattr(application.requests, "get", fake_get)
    response = client.get("/api/crm/wedof/status")
    assert captured["headers"] == {"X-Api-Key": SECRET, "Accept": "application/json"}
    assert SECRET.encode() not in response.data
    assert SECRET not in json.dumps(response.get_json())


def test_pagination_complete_json_and_idempotent_upsert(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    monkeypatch.setenv("WEDOF_API_KEY", SECRET)
    calls = []
    first = folder("folder-1", "lina@example.test")
    second_page = folder("folder-2", "other@example.test")

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        page = kwargs["params"]["page"]
        payload = [first] * 100 if page == 1 else [second_page]
        # Repeated stable IDs deliberately exercise the upsert.
        return FakeResponse(payload, headers={
            "x-current-page": str(page), "x-item-per-page": "100", "x-total-count": "101"
        })

    monkeypatch.setattr(application.requests, "get", fake_get)
    first_sync = client.post("/api/crm/wedof/sync")
    assert first_sync.status_code == 200
    assert first_sync.get_json()["created_contacts"] == 2
    assert [call[1]["params"]["page"] for call in calls] == [1, 2]
    second_sync = client.post("/api/crm/wedof/sync")
    assert second_sync.status_code == 200
    assert second_sync.get_json()["created_contacts"] == 0
    assert len(application.load_data()["crm_contacts"]) == 2

    with sqlite3.connect(tmp_path / "wedof.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM wedof_resources").fetchone()[0] == 2
        stored = json.loads(db.execute(
            "SELECT payload_json FROM wedof_resources WHERE stable_id='folder-1'"
        ).fetchone()[0])
    assert stored == first
    assert stored["unknownFutureField"] == [1, 2, 3]


def test_new_cpf_folder_creates_and_links_one_crm_lead(tmp_path, monkeypatch):
    authenticated_client(tmp_path, monkeypatch)
    cpf_request = folder(
        "cpf-new-lead", "lina@example.test", "+33 6 12 34 56 78",
        "Lina", "Martin",
    )
    cpf_request["trainingActionInfo"].update({
        "title": "Agent de prévention et de sécurité",
        "sessionStartDate": "2026-09-07",
        "sessionEndDate": "2026-10-09",
        "address": {"city": "Puget-sur-Argens"},
    })

    first = application._wedof_store_page(
        [cpf_request], application.load_data(), 1,
    )
    second = application._wedof_store_page(
        [cpf_request], application.load_data(), 1,
    )
    stored = application.load_data()

    assert first["created_contacts"] == 1
    assert second["created_contacts"] == 0
    assert len(stored["crm_contacts"]) == 1
    contact = stored["crm_contacts"][0]
    assert contact["prenom"] == "Lina"
    assert contact["nom"] == "MARTIN"
    assert contact["mail"] == "lina@example.test"
    assert contact["telephone"] == "+33 6 12 34 56 78"
    assert contact["formation"] == "Agent de prévention et de sécurité"
    assert contact["lieu"] == "Puget-sur-Argens"
    assert contact["cpf"] == "OUI"
    assert contact["origine"] == "Mon Compte Formation"
    assert contact["source"] == "wedof_cpf"
    assert contact["source_wedof_folder_id"] == "cpf-new-lead"
    assert len(stored["crm_inbound_requests"]) == 1
    assert stored["crm_inbound_requests"][0]["external_id"] == "cpf-new-lead"
    with sqlite3.connect(tmp_path / "wedof.sqlite3") as db:
        link = db.execute(
            "SELECT contact_id, match_method FROM wedof_contact_links "
            "WHERE resource_id='cpf-new-lead'"
        ).fetchone()
    assert link == (contact["id"], "stable")


def test_existing_person_is_reused_for_cpf_folder(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = create_contact(client, email="LINA@example.test")
    client.patch(f"/api/crm/contacts/{contact['id']}", json={"cpf": "NON"})
    result = application._wedof_store_page(
        [folder(
            "cpf-existing-person", "lina@example.test",
            first_name="Lina", last_name="Martin",
        )],
        application.load_data(), 1,
    )
    stored = application.load_data()

    assert result["created_contacts"] == 0
    assert len(stored["crm_contacts"]) == 1
    assert stored["crm_contacts"][0]["id"] == contact["id"]
    assert stored["crm_contacts"][0]["cpf"] == "OUI"


def test_folder_without_usable_identity_never_creates_blank_lead(tmp_path, monkeypatch):
    authenticated_client(tmp_path, monkeypatch)
    result = application._wedof_store_page(
        [folder("cpf-no-identity")], application.load_data(), 1,
    )
    assert result["created_contacts"] == 0
    assert application.load_data()["crm_contacts"] == []


def test_closed_historical_folder_never_creates_new_lead(tmp_path, monkeypatch):
    authenticated_client(tmp_path, monkeypatch)
    historical = folder(
        "cpf-historical", "former@example.test",
        first_name="Ancien", last_name="Stagiaire",
    )
    historical["state"] = "terminated"
    result = application._wedof_store_page(
        [historical], application.load_data(), 1,
    )
    assert result["created_contacts"] == 0
    assert application.load_data()["crm_contacts"] == []


def test_accepted_folder_with_past_session_never_creates_late_lead(tmp_path, monkeypatch):
    authenticated_client(tmp_path, monkeypatch)
    historical = folder(
        "cpf-past-accepted", "past@example.test",
        first_name="Passé", last_name="Stagiaire",
    )
    historical["trainingActionInfo"]["sessionEndDate"] = "2020-01-02"
    result = application._wedof_store_page(
        [historical], application.load_data(), 1,
    )
    assert result["created_contacts"] == 0
    assert application.load_data()["crm_contacts"] == []


def test_unique_email_matching(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = create_contact(client, email="LINA@example.test")
    application._wedof_store_page([folder("email-folder", "lina@example.test")], application.load_data(), 1)
    resources = client.get(f"/api/crm/contacts/{contact['id']}/wedof").get_json()["resources"]
    assert resources[0]["stable_id"] == "email-folder"
    assert resources[0]["match_method"] == "email"


def test_unique_normalized_phone_matching(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = create_contact(client, phone="06 12 34 56 78")
    application._wedof_store_page([folder("phone-folder", phone="+33 6 12 34 56 78")], application.load_data(), 1)
    resources = client.get(f"/api/crm/contacts/{contact['id']}/wedof").get_json()["resources"]
    assert resources[0]["match_method"] == "phone"


def test_contact_list_exposes_ft_instruction_even_with_scheduled_appointment_status(
        tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = create_contact(client, email="lina@example.test")
    client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"statut": "RDV programmé"},
    )
    ft_folder = folder("ft-folder", "lina@example.test")
    ft_folder["state"] = "waitingAcceptation"
    ft_folder["history"] = [{"state": "waitingAcceptation"}]
    application._wedof_store_page(
        [ft_folder], application.load_data(), 1)

    listed = client.get("/api/crm/contacts").get_json()
    result = next(item for item in listed if item["id"] == contact["id"])

    assert result["statut"] == "RDV programmé"
    assert result["statut_demande_financement_ft"] == "en_cours_instruction"


def test_unique_name_matching_ignores_accents(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = client.post(
        "/api/crm/contacts", json={"prenom": "Clément", "nom": "Lévy"}
    ).get_json()
    application._wedof_store_page(
        [folder("name-folder", first_name="Clement", last_name="Levy")],
        application.load_data(), 1,
    )
    resources = client.get(
        f"/api/crm/contacts/{contact['id']}/wedof"
    ).get_json()["resources"]
    assert resources[0]["match_method"] == "name"


def test_same_name_with_conflicting_email_is_not_attached(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    existing = client.post(
        "/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin"}
    ).get_json()
    client.patch(
        f"/api/crm/contacts/{existing['id']}",
        json={"mail": "existing@example.test"},
    )
    application._wedof_store_page(
        [folder(
            "conflicting-email", "other@example.test",
            first_name="Lina", last_name="Martin",
        )],
        application.load_data(), 1,
    )
    with sqlite3.connect(tmp_path / "wedof.sqlite3") as db:
        assert db.execute(
            "SELECT COUNT(*) FROM wedof_contact_links WHERE resource_id='conflicting-email'"
        ).fetchone()[0] == 0
    stored = application.load_data()
    assert len(stored["crm_contacts"]) == 1
    assert stored["crm_inbound_requests"][0]["status"] == "pending_review"


def test_shared_email_is_disambiguated_by_accent_insensitive_name(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    clement = client.post(
        "/api/crm/contacts", json={"prenom": "Clément", "nom": "VAILLANT"}
    ).get_json()
    other = client.post(
        "/api/crm/contacts", json={"prenom": "Autre", "nom": "Personne"}
    ).get_json()
    for contact in (clement, other):
        client.patch(
            f"/api/crm/contacts/{contact['id']}",
            json={"mail": "accueil@example.test"},
        )

    application._wedof_store_page(
        [folder(
            "shared-email-name", "accueil@example.test",
            first_name="Clement", last_name="Vaillant",
        )],
        application.load_data(), 1,
    )

    resources = client.get(
        f"/api/crm/contacts/{clement['id']}/wedof"
    ).get_json()["resources"]
    assert resources[0]["stable_id"] == "shared-email-name"
    assert resources[0]["match_method"] == "name"


def test_wedof_folder_is_visible_on_accented_duplicate_contact(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    original = client.post(
        "/api/crm/contacts", json={"prenom": "Clement", "nom": "Vaillant"}
    ).get_json()
    client.patch(
        f"/api/crm/contacts/{original['id']}",
        json={"mail": "accueil@example.test"},
    )
    application._wedof_store_page(
        [folder(
            "existing-person-folder", "accueil@example.test",
            first_name="Clement", last_name="Vaillant",
        )],
        application.load_data(), 1,
    )

    duplicate = client.post(
        "/api/crm/contacts", json={
            "prenom": "Clément", "nom": "VAILLANT", "force_create": True,
        }
    ).get_json()
    client.patch(
        f"/api/crm/contacts/{duplicate['id']}",
        json={"mail": "accueil@example.test"},
    )

    resources = client.get(
        f"/api/crm/contacts/{duplicate['id']}/wedof"
    ).get_json()["resources"]
    stored_duplicate = next(
        contact for contact in application.load_data()["crm_contacts"]
        if contact["id"] == duplicate["id"]
    )
    assert resources[0]["stable_id"] == "existing-person-folder"
    assert resources[0]["match_method"] == "name"
    assert stored_duplicate["prenom"] == "Clément"


def test_ambiguous_normalized_name_is_not_linked(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    client.post("/api/crm/contacts", json={"prenom": "Élodie", "nom": "André"})
    client.post("/api/crm/contacts", json={
        "prenom": "Elodie", "nom": "Andre", "force_create": True,
    })
    application._wedof_store_page(
        [folder("ambiguous-name", first_name="Elodie", last_name="Andre")],
        application.load_data(), 1,
    )
    with sqlite3.connect(tmp_path / "wedof.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM wedof_contact_links").fetchone()[0] == 0


def test_ambiguous_match_is_not_linked_and_creates_no_contact(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    create_contact(client, email="same@example.test")
    create_contact(client, email="same@example.test")
    before = len(application.load_data()["crm_contacts"])
    application._wedof_store_page([folder("ambiguous", "same@example.test")], application.load_data(), 1)
    with sqlite3.connect(tmp_path / "wedof.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM wedof_contact_links").fetchone()[0] == 0
    assert len(application.load_data()["crm_contacts"]) == before


def test_route_access_control_and_all_error_responses_hide_secret(tmp_path, monkeypatch):
    admin = authenticated_client(tmp_path, monkeypatch)
    monkeypatch.setenv("WEDOF_API_KEY", SECRET)
    user = application.app.test_client()
    with user.session_transaction() as session:
        session["user_email"] = "elsaduq83@gmail.com"
    assert user.post("/api/crm/wedof/sync").status_code == 403

    monkeypatch.setattr(
        application.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            application.requests.RequestException(f"X-Api-Key: {SECRET}")
        ),
    )
    monkeypatch.setattr(application.time, "sleep", lambda *_: None)
    response = admin.post("/api/crm/wedof/sync")
    assert response.status_code == 503
    assert SECRET.encode() not in response.data
