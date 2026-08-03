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
    contact = client.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin"}).get_json()
    return client.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"mail": email, "telephone": phone},
    ).get_json()


def folder(identifier, email="", phone=""):
    return {
        "externalId": identifier,
        "attendee": {"email": email, "phone": phone, "custom": "kept"},
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
    assert client.post("/api/crm/wedof/sync").status_code == 200
    assert [call[1]["params"]["page"] for call in calls] == [1, 2]
    assert client.post("/api/crm/wedof/sync").status_code == 200

    with sqlite3.connect(tmp_path / "wedof.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM wedof_resources").fetchone()[0] == 2
        stored = json.loads(db.execute(
            "SELECT payload_json FROM wedof_resources WHERE stable_id='folder-1'"
        ).fetchone()[0])
    assert stored == first
    assert stored["unknownFutureField"] == [1, 2, 3]


def test_unique_email_matching(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = create_contact(client, email="LINA@example.test")
    application._wedof_store_page([folder("email-folder", "lina@example.test")], application.load_data()["crm_contacts"], 1)
    resources = client.get(f"/api/crm/contacts/{contact['id']}/wedof").get_json()["resources"]
    assert resources[0]["stable_id"] == "email-folder"
    assert resources[0]["match_method"] == "email"


def test_unique_normalized_phone_matching(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    contact = create_contact(client, phone="06 12 34 56 78")
    application._wedof_store_page([folder("phone-folder", phone="+33 6 12 34 56 78")], application.load_data()["crm_contacts"], 1)
    resources = client.get(f"/api/crm/contacts/{contact['id']}/wedof").get_json()["resources"]
    assert resources[0]["match_method"] == "phone"


def test_ambiguous_match_is_not_linked_and_creates_no_contact(tmp_path, monkeypatch):
    client = authenticated_client(tmp_path, monkeypatch)
    create_contact(client, email="same@example.test")
    create_contact(client, email="same@example.test")
    before = len(application.load_data()["crm_contacts"])
    application._wedof_store_page([folder("ambiguous", "same@example.test")], application.load_data()["crm_contacts"], 1)
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
