import app as application
import json
import pytest


def _authenticated_client():
    application.app.config.update(TESTING=True)
    client = application.app.test_client()
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return client


def test_crm_url_opens_crm():
    client = _authenticated_client()

    response = client.get("/CRM", follow_redirects=True)

    assert response.status_code == 200
    assert b'Int\xc3\xa9grale Connect' in response.data


def test_crm_legacy_url_is_case_tolerant():
    client = _authenticated_client()

    response = client.get("/crm", follow_redirects=True)

    assert response.status_code == 200
    assert b'Int\xc3\xa9grale Connect' in response.data


@pytest.mark.parametrize(
    ("email", "first_name"),
    [
        ("cassandre@integraleacademy.com", "Cassandre"),
        ("clement@integraleacademy.com", "Clément"),
        ("elsa@integraleacademy.com", "Elsa"),
        ("aurelie@integraleacademy.com", "Aurélie"),
    ],
)
def test_crm_exposes_connected_users_first_name(email, first_name):
    client = application.app.test_client()
    with client.session_transaction() as session:
        session["user_email"] = email

    response = client.get("/crm")

    assert response.status_code == 200
    assert f'"first_name": {json.dumps(first_name)}'.encode() in response.data
