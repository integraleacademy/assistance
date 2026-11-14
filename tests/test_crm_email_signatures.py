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
    assert result.count(avatar) == 1
    assert '>CM</div>' not in result
    assert template['contenu'] == content


def test_library_training_dates_and_branding(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DATA_FILE', str(tmp_path / 'data.json'))
    data = application.load_data()
    data['formation_sessions'] = {'cote_azur': {
        code: [{'label': f'Du {day} au 28 février {year} - examen le 1er mars {year}'}
               for day, year in [(1, 2000), (2, 2099), (3, 2099)]]
        for code in ['APS', 'SSIAP']
    }}
    with application.app.test_request_context():
        templates = application._crm_templates_payload(data)
    for template in templates['automatic_email'] + [templates['automatic_meta'][0]]:
        body = template['contenu']
        assert body.count('Memoji de Cassandre MENARD') == 1
        assert 'Clément VAILLANT' not in body and 'force.com' not in body
        assert 'class="academy-wordmark"' in body and application.HEBERGEMENT_MAIL_LOGO not in body
        if template['formation'] in ('APS', 'SSIAP'):
            assert 'Du 1 au' not in body
            for day in [2, 3]:
                assert f'Du {day} au 28 février 2099 — examen le 1er mars 2099' in body


def test_legacy_saved_html_and_snippets_preview_with_one_signature(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DATA_FILE', str(tmp_path / 'data.json'))
    app = application.app
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_email'] = 'clement@integraleacademy.com'
    old_logo = 'https://integraleacademy.file.force.com/file-asset-public/Logo_Integrale_Academy_officielpdf?oid=old'
    bodies = [
        "<html><body><img src='" + old_logo + "'><p>Mon message</p>"
        '<p><strong>Clément VAILLANT</strong><br>Directeur Intégrale Group</p></body></html>',
        '<html><body><p>Mon message</p></body></html>',
        '<p>Mon message</p>',
    ]
    original = application.load_data()
    for idx, body in enumerate(bodies):
        original['crm_email_templates'].append({'id': str(idx), 'nom': 'Mon modèle', 'contenu': body})
    application.save_data(original)
    for idx in range(len(bodies)):
        response = client.get(f'/api/crm/templates/{idx}/email-preview')
        assert response.status_code == 200
        body = response.get_json()['html']
        assert body.count('Memoji de Cassandre MENARD') == 1
        assert 'Mon message' in body and 'Clément VAILLANT' not in body
        assert body.count(application.HEBERGEMENT_MAIL_LOGO) == 1 and old_logo not in body
        assert application._crm_refresh_email_signature(body, saved=True) == body
    assert application.load_data()['crm_email_templates'] == original['crm_email_templates']


def test_unanswered_call_uses_chosen_email_and_original_sms_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DATA_FILE', str(tmp_path / 'data.json'))
    data = application.load_data()
    data['crm_contacts'] = [{
        'id': 'prospect', 'prenom': 'Lina', 'formation': 'APS', 'mail': 'test@example.test',
        'telephone': '+33600000000', 'relance_date': '2026-08-13', 'statut': 'A relancer',
        'relances': [{'id': 'call', 'status': 'scheduled', 'scheduled_date': '2026-08-13'}],
    }]
    data['crm_sms_templates'] = [{'id': 'sms', 'nom': 'Pas de réponse relance', 'contenu': 'SMS habituel'}]
    data['crm_email_templates'] = [{'id': 'mail', 'nom': 'Pas de réponse relance', 'sujet': 'Ancien modèle', 'contenu': 'Ancien texte'}]
    application.save_data(data)
    emails, sms = [], []
    monkeypatch.setattr(application, 'send_email_html', lambda *args: emails.append(args) or True)
    monkeypatch.setattr(application, 'send_sms', lambda number, body: sms.append(body) or True)
    client = application.app.test_client()
    with client.session_transaction() as session:
        session['user_email'] = 'clement@integraleacademy.com'
    endpoint = '/api/crm/contacts/prospect/relances/call/sans-reponse'
    assert client.post(endpoint, json={'next_date': '2026-08-17', 'email_model': 'unknown'}).status_code == 400
    assert not sms and not emails
    payload = {'next_date': '2026-08-17', 'email_model': 'appointment'}
    response = client.post(endpoint, json=payload)
    assert response.status_code == 200 and response.get_json()['delivery'] == {'sms': True, 'email': True}
    assert client.post(endpoint, json=payload).get_json()['duplicate']
    assert sms == ['SMS habituel'] and len(emails) == 1
    _, subject, _, body = emails[0]
    assert 'faisons le point' in subject and 'Ancien texte' not in body
    assert 'Bonjour <strong>Lina</strong>' in body and 'Agent de prévention et de sécurité (APS)' in body
    assert 'href="https://calendly.com/integraleacademy/aps"' in body
    assert body.count('Memoji de Cassandre MENARD') == 1
    history = application.load_data()['crm_contacts'][0]['relances']
    assert next(x for x in history if x['id'] == 'call')['email_model'] == 'appointment'


def test_modeles_combine_public_sst_variants_and_crm_only_dates(tmp_path, monkeypatch):
    from datetime import date
    from crm_public_sessions import _public_calendar
    monkeypatch.setattr(application, 'DATA_FILE', str(tmp_path / 'data.json'))
    monkeypatch.setitem(application.app.config, 'CRM_USE_PUBLIC_CALENDAR', True)
    data = {'formation_sessions': {'cote_azur': {'SSIAP': [
        {'label': 'Du 15 au 26 février 2027'}, {'label': 'Du 4 au 15 mars 2027'},
    ]}}}
    common = {'training': {'slug': 'ssiap-1'}, 'status': 'OPEN', 'location': 'Puget-sur-Argens / Côte d’Azur',
              'examDate': '2027-03-01', 'endDate': '2027-02-26'}
    rows = [{**common, 'title': 'SSIAP 1 avec SST', 'startDate': '2027-02-11'},
            {**common, 'title': 'SSIAP 1 sans SST', 'startDate': '2027-02-15'},
            {**common, 'title': 'Autre centre', 'startDate': '2027-02-19', 'location': 'Paris'},
            {**common, 'title': 'Non publié', 'startDate': '2027-02-22', 'status': 'HIDDEN'}]
    monkeypatch.setattr(application.crm_public_sessions, '_published_sessions', lambda: _public_calendar(rows))
    html = application._format_upcoming_sessions_for_email('cote_azur', 'SSIAP', data, today=date(2026, 11, 3),
                                                          selected='Du 15 au 26 février 2027')
    assert html.count('<li ') == 3
    assert 'examen le 1er mars 2027 (avec SST)' in html
    assert 'examen le 1er mars 2027 (sans SST) (session demandée)' in html
    assert 'Du 4 au 15 mars 2027' in html


def test_dossier_template_is_editable_and_shared_by_library_and_relances(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DATA_FILE', str(tmp_path / 'data.json'))
    client = application.app.test_client()
    with client.session_transaction() as session:
        session['user_email'] = 'clement@integraleacademy.com'
    application._crm_register_dossier_email_once()
    application._crm_register_dossier_email_once()
    templates = client.get('/api/crm/templates').get_json()['email']
    dossier, = [row for row in templates if row['nom'] == 'Faire le point sur votre dossier']
    assert dossier['categorie'] == 'Relance' and dossier['relance_choice'] == 'appointment'
    path = '/api/crm/templates/' + dossier['id']
    preview = client.get(path + '/email-preview').get_json()['html']
    assert 'href="{{ lien_rdv_calendly }}"' in preview and preview.count('Memoji de Cassandre MENARD') == 1
    assert '<!-- EMAIL_CONTENT_START -->' in preview
    edit = client.patch(path, json={'nom': 'Suivi personnalisé', 'sujet': 'Votre rendez-vous {{ formation }}'})
    assert edit.status_code == 200 and edit.get_json()['relance_choice'] == 'appointment'
    application._crm_register_dossier_email_once()
    data = application.load_data()
    assert application._crm_relance_email_template(data, 'appointment')['sujet'] == 'Votre rendez-vous {{ formation }}'
    assert client.delete(path).status_code == 204
    application._crm_register_dossier_email_once()
    assert not client.get('/api/crm/templates').get_json()['email']


def test_dossier_registration_reuses_existing_model_without_overwriting_text(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DATA_FILE', str(tmp_path / 'data.json'))
    data = application.load_data()
    data['crm_email_templates'] = [{'id': 'custom', 'nom': 'Faire le point sur votre dossier',
                                    'sujet': 'Mon objet', 'contenu': '<p>Mon texte</p>'}]
    application.save_data(data)
    application._crm_register_dossier_email_once()
    data = application.load_data()
    assert data['crm_dossier_email_template_id'] == 'custom'
    template, = data['crm_email_templates']
    assert template['sujet'] == 'Mon objet' and template['contenu'] == '<p>Mon texte</p>'
