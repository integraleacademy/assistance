import json
from pathlib import Path
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
        "createdAt": "2026-08-12T00:00:00+02:00",
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


def test_contact_wedof_returns_empty_cache_and_status_without_remote_check(
        tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = create_contact(client, email="lina@example.test")
    monkeypatch.setenv("WEDOF_API_KEY", SECRET)
    monkeypatch.setattr(
        application, "_wedof_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cache reading must not call WEDOF")
        ),
    )

    response = client.get(f"/api/crm/contacts/{contact['id']}/wedof")

    assert response.status_code == 200
    assert response.get_json()["resources"] == []
    assert response.get_json()["status"]["configured"] is True


def test_contact_refresh_reuses_in_progress_sync_and_keeps_cached_resources(
        tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = create_contact(client, email="lina@example.test")
    application._wedof_store_page(
        [folder("cached-folder", "lina@example.test")],
        application.load_data(), 1,
    )
    application._WEDOF_SYNC_LOCK.acquire()
    try:
        response = client.post(
            f"/api/crm/contacts/{contact['id']}/wedof/refresh"
        )
    finally:
        application._WEDOF_SYNC_LOCK.release()

    assert response.status_code == 200
    assert response.get_json()["sync"]["in_progress"] is True
    assert response.get_json()["resources"][0]["stable_id"] == "cached-folder"


def test_contact_refresh_error_does_not_erase_cached_resources(
        tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = create_contact(client, email="lina@example.test")
    application._wedof_store_page(
        [folder("cached-folder", "lina@example.test")],
        application.load_data(), 1,
    )
    monkeypatch.setattr(
        application, "_wedof_sync",
        lambda: (_ for _ in ()).throw(application.WedofAPIError("indisponible")),
    )

    response = client.post(
        f"/api/crm/contacts/{contact['id']}/wedof/refresh"
    )

    assert response.status_code == 503
    cached = client.get(
        f"/api/crm/contacts/{contact['id']}/wedof"
    ).get_json()["resources"]
    assert cached[0]["stable_id"] == "cached-folder"


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
    assert contact["formation"] == "APS"
    assert contact["lieu"] == "Côte d’Azur"
    assert contact["dates_formation"] == (
        "Du 7 septembre au 9 octobre 2026 - examen le 12 octobre 2026"
    )
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


def test_cpf_desp_vae_title_populates_crm_fields_and_keeps_origin(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    cpf_request = folder(
        "cpf-desp-vae", "frederic@example.test", first_name="Frederic",
        last_name="Magnani",
    )
    cpf_request["trainingActionInfo"]["title"] = (
        "VAE TOTALE Dirigeant d'une entreprise sécurité privée – CQP dirigeant – "
        "Titre Dirigeant d'entreprise de sécurité privée (DESP) – "
        "Validation des acquis de l'expérience"
    )

    application._wedof_store_page([cpf_request], application.load_data(), 1)
    contact = application.load_data()["crm_contacts"][0]

    assert contact["formation"] == "DESP"
    assert contact["desp_type"] == "VAE"
    assert contact["origine"] == "Mon Compte Formation"

    updated = client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"formation": "A3P", "origine": ""},
    ).get_json()
    assert updated["formation"] == "A3P"
    assert updated["origine"] == "Mon Compte Formation"


def test_cpf_desp_initial_populates_selectable_training_campus_and_session(
        tmp_path, monkeypatch):
    authenticated_client(tmp_path, monkeypatch)
    cpf_request = folder(
        "cpf-desp-initial", "samuel@example.test", first_name="Samuel",
        last_name="Maclean",
    )
    cpf_request["trainingActionInfo"].update({
        "title": (
            "Formation Dirigeant d'une entreprise sécurité privée – "
            "CQP dirigeant – Titre Dirigeant d'entreprise de sécurité privée "
            "(DESP)"
        ),
        "sessionStartDate": "2026-09-07",
        "sessionEndDate": "2026-10-23",
        "address": {
            "streetAddress": "54 chemin du Carreou",
            "postalCode": "83480",
            "city": "Puget-sur-Argens",
        },
    })

    application._wedof_store_page([cpf_request], application.load_data(), 1)
    contact = application.load_data()["crm_contacts"][0]

    assert contact["formation"] == "DESP"
    assert contact["desp_type"] == "INITIAL"
    assert contact["lieu"] == "Côte d’Azur"
    assert contact["dates_formation"] == (
        "Du 7 septembre au 23 octobre 2026 (présentiel du 12 au 23/10) "
        "- examen le 26 octobre 2026"
    )


def test_nested_wedof_session_and_location_fields_are_supported(tmp_path, monkeypatch):
    authenticated_client(tmp_path, monkeypatch)
    cpf_request = folder(
        "cpf-nested-fields", "nested@example.test", first_name="Nora",
        last_name="Durand",
    )
    cpf_request["trainingActionInfo"] = {
        "title": "Agent de prévention et de sécurité (APS)",
    }
    cpf_request["session"] = {
        "startDate": "2026-11-03",
        "endDate": "2026-12-08",
        "location": {"name": "Puget-sur-Argens"},
    }

    application._wedof_store_page([cpf_request], application.load_data(), 1)
    contact = application.load_data()["crm_contacts"][0]

    assert contact["formation"] == "APS"
    assert contact["lieu"] == "Côte d’Azur"
    assert contact["dates_formation"] == (
        "Du 3 novembre au 8 décembre 2026 - examen le 9 décembre 2026"
    )


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
    stored_contact = stored["crm_contacts"][0]
    assert stored_contact["id"] == contact["id"]
    assert stored_contact["cpf"] == "OUI"
    assert stored_contact["origine"] == "Ajout manuel"
    assert any(
        item.get("origin") == "Mon Compte Formation"
        for item in stored_contact["source_history"]
    )


def test_existing_cpf_link_repairs_missing_origin(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    cpf_request = folder(
        "cpf-origin-repair", "lina@example.test",
        first_name="Lina", last_name="Martin",
    )
    application._wedof_store_page(
        [cpf_request], application.load_data(), 1,
    )
    contact = application.load_data()["crm_contacts"][0]
    data = application.load_data()
    data["crm_contacts"][0]["origine"] = ""
    application.save_data(data)

    application._wedof_store_page(
        [cpf_request], application.load_data(), 1,
    )

    repaired = client.get(
        f"/api/crm/contacts/{contact['id']}"
    ).get_json()
    assert repaired["origine"] == "Mon Compte Formation"


def test_existing_cpf_link_repairs_missing_or_legacy_training_fields(
        tmp_path, monkeypatch):
    authenticated_client(tmp_path, monkeypatch)
    cpf_request = folder(
        "cpf-training-repair", "samuel@example.test", first_name="Samuel",
        last_name="Maclean",
    )
    long_title = (
        "Formation Dirigeant d'une entreprise sécurité privée – CQP dirigeant – "
        "Titre Dirigeant d'entreprise de sécurité privée (DESP)"
    )
    cpf_request["trainingActionInfo"].update({
        "title": long_title,
        "sessionStartDate": "2026-09-07",
        "sessionEndDate": "2026-10-23",
        "address": {"city": "Puget-sur-Argens"},
    })
    application._wedof_store_page([cpf_request], application.load_data(), 1)

    data = application.load_data()
    contact = data["crm_contacts"][0]
    contact.update({
        "formation": long_title,
        "desp_type": "",
        "lieu": "",
        "dates_formation": "2026-09-07 → 2026-10-23",
    })
    application.save_data(data)
    activities_before = len(contact["activities"])

    application._wedof_store_page([cpf_request], application.load_data(), 1)
    repaired = application.load_data()["crm_contacts"][0]

    assert repaired["formation"] == "DESP"
    assert repaired["desp_type"] == "INITIAL"
    assert repaired["lieu"] == "Côte d’Azur"
    assert repaired["dates_formation"].startswith("Du 7 septembre au 23 octobre 2026")
    assert len(repaired["activities"]) == activities_before + 1
    assert repaired["activities"][0]["title"] == "Informations CPF synchronisées"

    application._wedof_store_page([cpf_request], application.load_data(), 1)
    idempotent = application.load_data()["crm_contacts"][0]
    assert len(idempotent["activities"]) == activities_before + 1


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


def test_open_cpf_folder_created_before_cutoff_never_creates_lead(tmp_path, monkeypatch):
    authenticated_client(tmp_path, monkeypatch)
    historical = folder(
        "cpf-before-cutoff", "former@example.test",
        first_name="Ancien", last_name="Stagiaire",
    )
    historical["createdAt"] = "2026-08-11T23:59:59+02:00"

    result = application._wedof_store_page(
        [historical], application.load_data(), 1,
    )

    assert result["created_contacts"] == 0
    assert application.load_data()["crm_contacts"] == []


def test_folder_without_creation_date_never_creates_lead(tmp_path, monkeypatch):
    authenticated_client(tmp_path, monkeypatch)
    undated = folder(
        "cpf-undated", "unknown@example.test",
        first_name="Date", last_name="Inconnue",
    )
    del undated["createdAt"]

    result = application._wedof_store_page(
        [undated], application.load_data(), 1,
    )

    assert result["created_contacts"] == 0
    assert application.load_data()["crm_contacts"] == []


def test_non_cpf_folder_never_creates_lead(tmp_path, monkeypatch):
    authenticated_client(tmp_path, monkeypatch)
    non_cpf = folder(
        "non-cpf", "other@example.test",
        first_name="Autre", last_name="Financement",
    )
    non_cpf["type"] = "france-travail"

    result = application._wedof_store_page(
        [non_cpf], application.load_data(), 1,
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
    paris_now = application.datetime.datetime.now(
        application.pytz.timezone("Europe/Paris")
    )
    data = application.load_data()
    data["crm_calendly_appointments"] = [{
        "id": "future-ft-appointment",
        "contact_id": contact["id"],
        "status": "active",
        "start_time": (
            paris_now + application.datetime.timedelta(days=1)
        ).astimezone(application.pytz.UTC).isoformat(),
    }]
    application.save_data(data)
    ft_folder = folder("ft-folder", "lina@example.test")
    ft_folder["state"] = "waitingAcceptation"
    ft_folder["history"] = [{"state": "waitingAcceptation"}]
    application._wedof_store_page(
        [ft_folder], application.load_data(), 1)

    listed = client.get("/api/crm/contacts").get_json()
    result = next(item for item in listed if item["id"] == contact["id"])

    assert result["statut"] == "RDV programmé"
    assert result["statut_demande_financement_ft"] == "en_cours_instruction"


def test_contact_list_decodes_each_wedof_folder_only_once(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    first = create_contact(client, email="first@example.test")
    second = client.post(
        "/api/crm/contacts",
        json={"prenom": "Nora", "nom": "Durand", "force_create": True},
    ).get_json()
    client.patch(
        f"/api/crm/contacts/{second['id']}",
        json={"mail": "second@example.test"},
    )
    folders = [
        folder(
            "ft-first", "first@example.test",
            first_name="Lina", last_name="Martin",
        ),
        folder(
            "ft-second", "second@example.test",
            first_name="Nora", last_name="Durand",
        ),
    ]
    for item in folders:
        item["state"] = "waitingAcceptation"
        item["history"] = [{"state": "waitingAcceptation"}]
    application._wedof_store_page(folders, application.load_data(), 1)

    decoded_folder_ids = []
    original_loads = json.loads

    def tracked_loads(value, *args, **kwargs):
        if isinstance(value, str) and '"externalId"' in value:
            decoded_folder_ids.append(original_loads(value)["externalId"])
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(application.json, "loads", tracked_loads)
    statuses = application._wedof_funding_statuses_by_contact(
        application.load_data()
    )

    assert sorted(decoded_folder_ids) == ["ft-first", "ft-second"]
    assert statuses[first["id"]] == "en_cours_instruction"
    assert statuses[second["id"]] == "en_cours_instruction"


def test_linked_contact_does_not_scan_unrelated_wedof_names(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = create_contact(client, email="lina@example.test")
    application._wedof_store_page(
        [folder("direct-folder", "lina@example.test")],
        application.load_data(), 1,
    )

    monkeypatch.setattr(
        application,
        "_wedof_contact_name_matches",
        lambda *_: (_ for _ in ()).throw(AssertionError("unexpected full scan")),
    )
    resources = client.get(
        f"/api/crm/contacts/{contact['id']}/wedof"
    ).get_json()["resources"]

    assert [resource["stable_id"] for resource in resources] == ["direct-folder"]


def test_completed_ft_instruction_clears_automatic_secondary_timeline(
        tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = create_contact(client, email="lina@example.test")
    ft_folder = folder(
        "ft-completed", "lina@example.test",
        first_name="Lina", last_name="Martin",
    )
    ft_folder["state"] = "waitingAcceptation"
    ft_folder["history"] = [{"state": "waitingAcceptation"}]
    application._wedof_store_page([ft_folder], application.load_data(), 1)

    in_progress = next(
        row for row in client.get("/api/crm/contacts").get_json()
        if row["id"] == contact["id"]
    )
    assert in_progress["statut_secondaire"] == "Financement FT en cours"

    ft_folder["state"] = "accepted"
    application._wedof_store_page([ft_folder], application.load_data(), 1)
    completed = next(
        row for row in client.get("/api/crm/contacts").get_json()
        if row["id"] == contact["id"]
    )
    assert completed["statut_demande_financement_ft"] == "acceptee"
    assert completed["statut_secondaire"] == ""


def test_refused_ft_instruction_returning_to_validated_updates_secondary_timeline(
        tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = create_contact(client, email="lina@example.test")
    ft_folder = folder(
        "ft-refused", "lina@example.test",
        first_name="Lina", last_name="Martin",
    )
    ft_folder["state"] = "waitingAcceptation"
    ft_folder["history"] = [{"state": "waitingAcceptation"}]
    application._wedof_store_page([ft_folder], application.load_data(), 1)

    in_progress = next(
        row for row in client.get("/api/crm/contacts").get_json()
        if row["id"] == contact["id"]
    )
    assert in_progress["statut_demande_financement_ft"] == "en_cours_instruction"
    assert in_progress["statut_secondaire"] == "Financement FT en cours"

    # WEDOF revient à « En attente d'acceptation du candidat » après le refus FT.
    ft_folder["state"] = "validated"
    ft_folder.pop("history")
    application._wedof_store_page([ft_folder], application.load_data(), 1)
    refused = next(
        row for row in client.get("/api/crm/contacts").get_json()
        if row["id"] == contact["id"]
    )

    assert refused["statut_demande_financement_ft"] == "refusee"
    assert refused["statut_secondaire"] == "Financement FT refusé"


def test_cached_financer_refusal_repairs_stale_automatic_timeline(
        tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = create_contact(client, email="lina@example.test")
    ft_folder = folder(
        "ft-cached-refusal", "lina@example.test",
        first_name="Lina", last_name="Martin",
    )
    ft_folder["state"] = "waitingAcceptation"
    application._wedof_store_page([ft_folder], application.load_data(), 1)

    ft_folder["state"] = "validated"
    ft_folder["history"] = {
        "validatedDate": "2026-08-23T13:30:00+00:00",
        "refusedByFinancerDate": "2026-08-23T13:35:00+00:00",
        "refusedByOrganismDate": None,
    }
    application._wedof_store_page([ft_folder], application.load_data(), 1)

    # Reproduit une fiche restée sur l'ancien état alors que le cache WEDOF
    # contient déjà le refus : la synchronisation suivante doit la réparer
    # sans dépendre de l'ouverture de la fiche.
    data = application.load_data()
    stale = next(row for row in data["crm_contacts"] if row["id"] == contact["id"])
    stale["statut_demande_financement_ft"] = "en_cours_instruction"
    stale["statut_secondaire"] = "Financement FT en cours"
    stale["statut"] = "Nouveaux"
    stale["relances"] = []
    stale["relance_date"] = ""
    stale["activities"] = [
        activity for activity in stale.get("activities", [])
        if activity.get("title") != "Relance France Travail planifiée"
    ]
    application.save_data(data)
    notification_count = len(data["crm_notifications"])

    application._wedof_store_page([ft_folder], application.load_data(), 1)
    repaired = next(
        row for row in application.load_data()["crm_contacts"]
        if row["id"] == contact["id"]
    )

    assert repaired["statut_demande_financement_ft"] == "refusee"
    assert repaired["statut_secondaire"] == "Financement FT refusé"
    assert repaired["statut"] == "A relancer"
    scheduled = [
        item for item in repaired["relances"]
        if item.get("status") == "scheduled"
    ]
    assert len(scheduled) == 1
    assert scheduled[0]["source"] == "wedof_ft_refusal"
    assert repaired["relance_date"] == scheduled[0]["scheduled_date"]
    assert len(application.load_data()["crm_notifications"]) == notification_count

    application._wedof_store_page([ft_folder], application.load_data(), 1)
    replayed = next(
        row for row in application.load_data()["crm_contacts"]
        if row["id"] == contact["id"]
    )
    assert len([
        item for item in replayed["relances"]
        if item.get("status") == "scheduled"
    ]) == 1


def test_explicit_ft_rejection_without_history_updates_secondary_timeline(
        tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = create_contact(client, email="lina@example.test")
    rejected_folder = folder(
        "ft-rejected", "lina@example.test",
        first_name="Lina", last_name="Martin",
    )
    rejected_folder["state"] = "rejected"
    rejected_folder.pop("history")
    application._wedof_store_page(
        [rejected_folder], application.load_data(), 1,
    )

    result = next(
        row for row in client.get("/api/crm/contacts").get_json()
        if row["id"] == contact["id"]
    )

    assert result["statut_demande_financement_ft"] == "refusee"
    assert result["statut_secondaire"] == "Financement FT refusé"
    update = next(
        row for row in client.get(
            f"/api/crm/contacts/updates?contact_id={contact['id']}"
        ).get_json()["contacts"]
        if row["id"] == contact["id"]
    )
    assert update["statut_demande_financement_ft"] == "refusee"
    assert update["statut_secondaire"] == "Financement FT refusé"


def test_ft_status_reads_nested_wedof_history():
    payload = {
        "registrationState": "validated",
        "events": {
            "changes": [
                {"details": {"registrationState": "waitingAcceptation"}},
            ],
        },
    }

    assert application._wedof_france_travail_status(payload) == "refusee"
    assert application._wedof_france_travail_status({
        "registrationState": "validated",
        "events": {"changes": [{"details": {"state": "validated"}}]},
    }) == ""
    assert application._wedof_france_travail_status({
        "state": "validated",
        "history": {
            "refusedByFinancerDate": "2026-08-23T13:35:00+00:00",
            "refusedByOrganismDate": None,
        },
    }) == "refusee"
    assert application._wedof_france_travail_status({
        "state": "validated",
        "history": {"refusedByFinancerDate": None},
    }) == ""


def test_collaborative_updates_endpoint_returns_a_small_payload(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = create_contact(client, email="lina@example.test")

    response = client.get(
        f"/api/crm/contacts/updates?contact_id={contact['id']}"
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert set(payload["contacts"][0]) == {
        "id", "statut", "statut_secondaire",
        "statut_demande_financement_ft", "updated_at", "activity_counts",
    }
    assert payload["selected"]["id"] == contact["id"]
    assert set(payload["selected"]) == {"id", "activities", "publications"}
    assert "integration_score" not in payload["contacts"][0]


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


def test_ft_refusal_notifies_each_crm_account_once_and_allows_a_new_cycle(
        tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = create_contact(client, email="lina@example.test")
    ft_folder = folder(
        "ft-notification", "lina@example.test",
        first_name="Lina", last_name="Martin",
    )
    ft_folder["state"] = "waitingAcceptation"
    ft_folder["history"] = [{"state": "waitingAcceptation"}]
    application._wedof_store_page(
        [ft_folder], application.load_data(), 1,
    )

    ft_folder["state"] = "validated"
    application._wedof_store_page(
        [ft_folder], application.load_data(), 1,
    )

    stored = application.load_data()
    alerts = [
        item for item in stored["crm_notifications"]
        if item.get("kind") == "funding_refused"
    ]
    assert sorted(item["recipient_email"] for item in alerts) == sorted(
        application.USERS
    )
    assert {item["contact_id"] for item in alerts} == {contact["id"]}
    assert {item["source_wedof_folder_id"] for item in alerts} == {
        "ft-notification"
    }
    assert all("Lina MARTIN" in item["text"] for item in alerts)

    refused_contact = next(
        item for item in stored["crm_contacts"]
        if item["id"] == contact["id"]
    )
    today = application.datetime.datetime.now(
        application.pytz.timezone("Europe/Paris")
    ).date().isoformat()
    scheduled_relances = [
        item for item in refused_contact["relances"]
        if item.get("status") == "scheduled"
    ]
    assert refused_contact["statut"] == "A relancer"
    assert refused_contact["statut_secondaire"] == "Financement FT refusé"
    assert refused_contact["relance_date"] == today
    assert len(scheduled_relances) == 1
    assert scheduled_relances[0]["scheduled_date"] == today
    assert scheduled_relances[0]["source"] == "wedof_ft_refusal"
    assert scheduled_relances[0]["created_by"] == "France Travail"
    assert scheduled_relances[0]["source_wedof_folder_id"] == "ft-notification"
    assert len([
        item for item in refused_contact["activities"]
        if item.get("title") == "Relance France Travail planifiée"
    ]) == 1

    own_alerts = [
        item for item in client.get("/api/crm/notifications").get_json()
        if item.get("kind") == "funding_refused"
    ]
    assert len(own_alerts) == 1
    assert own_alerts[0]["read"] is False

    application._wedof_store_page(
        [ft_folder], application.load_data(), 1,
    )
    replayed = application.load_data()
    assert len([
        item for item in replayed["crm_notifications"]
        if item.get("kind") == "funding_refused"
    ]) == len(application.USERS)
    replayed_contact = next(
        item for item in replayed["crm_contacts"]
        if item["id"] == contact["id"]
    )
    assert len([
        item for item in replayed_contact["relances"]
        if item.get("status") == "scheduled"
    ]) == 1
    assert len([
        item for item in replayed_contact["activities"]
        if item.get("title") == "Relance France Travail planifiée"
    ]) == 1

    ft_folder["state"] = "waitingAcceptation"
    application._wedof_store_page(
        [ft_folder], application.load_data(), 1,
    )
    ft_folder["state"] = "validated"
    application._wedof_store_page(
        [ft_folder], application.load_data(), 1,
    )
    final_data = application.load_data()
    assert len([
        item for item in final_data["crm_notifications"]
        if item.get("kind") == "funding_refused"
    ]) == 2 * len(application.USERS)
    final_contact = next(
        item for item in final_data["crm_contacts"]
        if item["id"] == contact["id"]
    )
    assert len([
        item for item in final_contact["relances"]
        if item.get("status") == "scheduled"
    ]) == 1
    assert len([
        item for item in final_contact["activities"]
        if item.get("title") == "Relance France Travail planifiée"
    ]) == 1


def test_ft_refusal_notification_ui_is_system_only():
    source = (
        Path(__file__).resolve().parents[1] / "static" / "crm.js"
    ).read_text(encoding="utf-8")

    assert "n.kind==='funding_refused'" in source
    assert "action:'a signalé un refus de financement'" in source
    assert "reply:false" in source
    assert "presentation.reply?" in source
