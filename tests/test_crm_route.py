import app as application


def test_crm_legacy_url_redirects_to_admin():
    application.app.config.update(TESTING=True)
    client = application.app.test_client()

    response = client.get("/CRM")

    assert response.status_code == 302
    assert response.headers["Location"] == "/admin"


def test_crm_legacy_url_is_case_tolerant():
    application.app.config.update(TESTING=True)
    client = application.app.test_client()

    response = client.get("/crm")

    assert response.status_code == 302
    assert response.headers["Location"] == "/admin"
