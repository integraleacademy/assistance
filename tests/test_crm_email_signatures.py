import copy

import pytest

import app as application
from crm_email_signatures import with_cassandre_memoji


LEGACY_SIGNATURE = '''<table role="presentation"><tr>
<td width="48"><div style="border-radius:50%">CM</div></td>
<td><strong>Cassandre MENARD</strong><br>Responsable commerciale – Intégrale Academy<br>
<a href="mailto:cassandre@integraleacademy.com">cassandre@integraleacademy.com</a></td>
</tr></table>'''


@pytest.fixture
def avatar():
    with application.app.app_context():
        return application.render_template("emails/_cassandre_memoji.html").strip()


@pytest.mark.parametrize("signature", [
    LEGACY_SIGNATURE,
    '<p><strong>Cassandre MENARD</strong><br>Responsable commerciale Intégrale Academy</p>',
    '<p><b>Cassandre&nbsp;MENARD</b><br>Responsable commerciale</p>',
    '<p><span>Cassandre</span><br>Responsable commerciale</p>',
    '<p>Cassandre MENARD<br>Responsable commerciale</p>',
    LEGACY_SIGNATURE.replace('<div style="border-radius:50%">CM</div>',
                             '<img src="https://example.com/cassandre.png">'),
])
def test_saved_signature_gets_one_round_memoji_without_changing_message(signature, avatar):
    message = '<p>Bonjour {{ prenom }}, voici votre dossier.</p>'
    original = '<!doctype html><html><body>' + message + signature + '</body></html>'
    result = with_cassandre_memoji(original, avatar)

    assert result.count(avatar) == 1
    assert 'width="56" height="56"' in result
    assert 'border-radius:50%' in result
    assert '>CM</div>' not in result
    assert message in result
    assert 'Responsable commerciale' in result
    assert with_cassandre_memoji(result, avatar) == result


@pytest.mark.parametrize("body", [
    '<p>Votre interlocutrice est <strong>Cassandre MENARD</strong>.</p>',
    '<p><strong>Clément VAILLANT</strong><br>Directeur Intégrale Academy</p>',
    '<p><strong>Aurélie CHAUSSEZ</strong><br>Responsable commerciale</p>',
    '<!-- EMAIL_SIGNATURE_START -->L’équipe commerciale<!-- EMAIL_SIGNATURE_END -->',
    'Cassandre MENARD\nResponsable commerciale Intégrale Academy',
])
def test_other_signatures_mentions_and_plain_text_are_preserved(body, avatar):
    assert with_cassandre_memoji(body, avatar) == body


def test_template_library_refreshes_copies_and_both_starters(tmp_path, monkeypatch, avatar):
    monkeypatch.setattr(application, 'DATA_FILE', str(tmp_path / 'data.json'))
    data = application.load_data()
    data['crm_email_templates'] = [{
        'id': 'saved', 'nom': 'Modèle existant', 'contenu': LEGACY_SIGNATURE,
        'versions': [{'contenu': LEGACY_SIGNATURE}],
    }]
    original = copy.deepcopy(data)
    with application.app.test_request_context():
        payload = application._crm_templates_payload(data)
    assert payload['email'][0]['contenu'].count(avatar) == 1
    for key in ('email_starter', 'email_free_starter'):
        assert payload[key].count(avatar) == 1
        assert "include 'emails/_cassandre_memoji.html'" not in payload[key]
    assert data == original


def test_saved_template_preview_and_delivery_match(tmp_path, monkeypatch, avatar):
    monkeypatch.setattr(application, 'DATA_FILE', str(tmp_path / 'data.json'))
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    client = application.app.test_client()
    with client.session_transaction() as session:
        session['user_email'] = 'clement@integraleacademy.com'
    contact = client.post('/api/crm/contacts', json={
        'prenom': 'Lina', 'formation': 'APS', 'mail': 'lina@example.com',
    }).get_json()
    content = '<!doctype html><html><body><p>Bonjour {{ prenom }}</p>' + LEGACY_SIGNATURE + '</body></html>'
    payload = {'type': 'email', 'sujet': 'Votre dossier', 'contenu': content}
    delivered = []
    monkeypatch.setattr(application, 'send_email_html', lambda *args, **kwargs: delivered.append(args[3]) or True)
    endpoint = '/api/crm/contacts/' + contact['id']
    preview = client.post(endpoint + '/message-preview', json=payload)
    assert preview.status_code == 200
    assert delivered == []
    response = client.post(endpoint + '/message', json=payload)
    assert response.status_code == 200
    assert delivered == [preview.get_json()['html']]
    assert delivered[0].count(avatar) == 1
    assert 'Bonjour Lina' in delivered[0]
    assert any(activity.get('preview') == delivered[0] for activity in response.get_json()['activities'])


def test_secretariat_summary_contains_round_memoji(monkeypatch, avatar):
    monkeypatch.setattr(application, '_crm_ai', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('offline')))
    entry, contact = application._secretariat_preview_data(False)
    with application.app.test_request_context():
        _, plain, rendered = application._build_secretariat_followup_email(entry, contact)
    assert rendered.count(avatar) == 1
    assert '<img' not in plain


@pytest.mark.parametrize('complete_document', [False, True])
def test_secretariat_saved_information_template_is_refreshed(monkeypatch, avatar, complete_document):
    content = LEGACY_SIGNATURE
    if complete_document:
        content = '<!doctype html><html><body>' + content + '</body></html>'
    template = {'id': 'saved-aps', 'sujet': 'Informations APS', 'contenu': content}
    monkeypatch.setattr(application, '_secretariat_information_template', lambda *args: template)
    with application.app.test_request_context():
        _, _, _, result = application._secretariat_information_email(
            {}, {'formation': 'APS'}, {'prenom': 'Lina', 'formation': 'APS'},
        )
    # A fragment receives the existing wrapper signature as well; both are updated.
    assert result.count(avatar) == (1 if complete_document else 2)
    assert '>CM</div>' not in result
    assert template['contenu'] == content
