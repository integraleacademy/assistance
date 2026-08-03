import app as application
def setup_function():
    application._LOGIN_ATTEMPTS.clear()
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)


def test_anonymous_crm_redirects_to_login():
    response = application.app.test_client().get('/crm/contacts')
    assert response.status_code == 302
    assert '/login?next=/crm/contacts' in response.location


def test_uppercase_crm_redirects_to_lowercase_when_authenticated():
    client = application.app.test_client()
    with client.session_transaction() as session:
        session['user_email'] = 'clement@integraleacademy.com'
    response = client.get('/CRM')
    assert response.status_code == 302
    assert response.location.endswith('/crm')


def test_configured_user_can_login(monkeypatch):
    monkeypatch.setenv('CRM_CLEMENT_PASSWORD', 'correct-password')
    response = application.app.test_client().post('/login', data={'email': 'clement@integraleacademy.com', 'password': 'correct-password'})
    assert response.status_code == 302
    assert response.location.endswith('/crm')


def test_wrong_password_and_missing_password_are_refused(monkeypatch):
    monkeypatch.setenv('CRM_CASSANDRE_PASSWORD', 'correct-password')
    client = application.app.test_client()
    assert client.post('/login', data={'email': 'cassandre@integraleacademy.com', 'password': 'wrong'}).status_code == 401
    monkeypatch.delenv('CRM_AURELIE_PASSWORD', raising=False)
    assert client.post('/login', data={'email': 'aurelie@integraleacademy.com', 'password': 'anything'}).status_code == 401


def test_empty_password_variable_does_not_enable_account(monkeypatch):
    monkeypatch.setenv('CRM_AURELIE_PASSWORD', '')
    response = application.app.test_client().post(
        '/login', data={'email': 'aurelie@integraleacademy.com', 'password': ''}
    )
    assert response.status_code == 401


def test_accounts_and_roles_are_exact():
    assert set(application.USERS) == {'clement@integraleacademy.com', 'cassandre@integraleacademy.com', 'aurelie@integraleacademy.com', 'elsa@integraleacademy.com'}
    assert application.USERS['clement@integraleacademy.com']['role'] == 'admin'
    assert {u['role'] for email, u in application.USERS.items() if not email.startswith('clement@')} == {'user'}
    assert all('mohamed' not in email for email in application.USERS)


def test_external_next_is_ignored(monkeypatch):
    monkeypatch.setenv('CRM_ELSA_PASSWORD', 'correct-password')
    response = application.app.test_client().post('/login?next=https://evil.example', data={'email': 'elsa@integraleacademy.com', 'password': 'correct-password'})
    assert response.location.endswith('/crm')


def test_logout_clears_session():
    client = application.app.test_client()
    with client.session_transaction() as session:
        session['user_email'] = 'clement@integraleacademy.com'
        session['user_name'] = 'Clément VAILLANT'
    assert client.post('/logout').status_code == 302
    with client.session_transaction() as session:
        assert 'user_email' not in session
        assert 'user_name' not in session


def test_internal_crm_api_requires_authentication():
    response = application.app.test_client().get('/api/crm/contacts')
    assert response.status_code == 302
    assert '/login' in response.location
