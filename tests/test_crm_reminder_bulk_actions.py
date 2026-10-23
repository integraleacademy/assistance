"""Regression coverage for reminder selection, preview and bulk disqualification."""
import copy
import datetime as dt
import json
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

import pytest
import app as application

ROOT = Path(__file__).parents[1]


def starter_template(client):
    starter = client.get('/api/crm/templates').get_json()['email_free_starter']
    source = (ROOT / 'static/crm_workspace.js').read_text()
    helper = source[source.index('function reminderInterestTemplate'):source.index('function reminderDisqualifyModal')]
    script = helper + '\nconsole.log(JSON.stringify(reminderInterestTemplate({templates:{email_free_starter:' + json.dumps(starter) + '}})));'
    return json.loads(subprocess.check_output(['node', '-e', script], text=True))


@pytest.fixture
def crm_client(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DATA_FILE', str(tmp_path / 'crm.json'))
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    client = application.app.test_client()
    with client.session_transaction() as session:
        session['user_email'] = 'clement@integraleacademy.com'
    return client


def seed_contacts(count=266):
    today = dt.date.today()
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    rows = []
    for index in range(count):
        rows.append({
            'id': f'lead-{index}', 'prenom': f'Candidat {index:03}', 'nom': 'TEST',
            'mail': f'candidat-{index}@example.test', 'formation': 'A3P',
            'statut': 'A relancer', 'relance_date': yesterday, 'origine': 'META',
            'activities': [], 'relances': [{'id': f'r-{index}', 'status': 'scheduled', 'scheduled_date': yesterday}],
        })
    rows.extend([
        {**copy.deepcopy(rows[0]), 'id': 'today', 'relance_date': today.isoformat(), 'relances': []},
        {**copy.deepcopy(rows[0]), 'id': 'future', 'relance_date': (today + dt.timedelta(days=2)).isoformat(), 'relances': []},
        {**copy.deepcopy(rows[0]), 'id': 'archived', 'archived_at': '2026-01-01'},
        {**copy.deepcopy(rows[0]), 'id': 'converted', 'statut': 'Converti'},
        {**copy.deepcopy(rows[0]), 'id': 'disqualified', 'statut': 'Disqualifié'},
    ])
    data = application.load_data()
    data['crm_contacts'] = rows
    application.save_data(data)
    return rows


def test_interest_preview_is_personalized_matches_send_and_never_sends_on_preview(crm_client, monkeypatch):
    rows = seed_contacts(2)
    data = application.load_data()
    data['crm_contacts'][1].update(formation='APS', prenom='Lina & Ana')
    application.save_data(data)
    template = starter_template(crm_client)
    sent = []
    monkeypatch.setattr(application, '_crm_send_email_html', lambda *args, **kwargs: sent.append(args) or True)
    payload = {'type': 'email', 'sujet': template['sujet'], 'contenu': template['contenu']}
    for index, label, booking in [(0, 'Agent de protection physique des personnes (A3P)', '/apr'), (1, 'Agent de prévention et de sécurité (APS)', '/aps')]:
        endpoint = f'/api/crm/contacts/{rows[index]["id"]}'
        before = copy.deepcopy(application.load_data()['crm_contacts'])
        preview = crm_client.post(endpoint + '/message-preview', json=payload)
        assert preview.status_code == 200
        assert len(sent) == index
        html = preview.get_json()['html']
        assert label in html
        assert 'https://calendly.com/integraleacademy' + booking in html
        assert 'Prendre un rendez-vous téléphonique' in html
        assert 'Et si nous faisions le point ?' in html
        assert '{{' not in html
        assert html.count('<!doctype html>') == 1
        assert application.load_data()['crm_contacts'] == before
        response = crm_client.post(endpoint + '/message', json=payload)
        assert response.status_code == 200
        assert sent[-1][3] == html
        assert sent[-1][1] == preview.get_json()['sujet']


def test_disqualifying_266_keeps_records_cancels_only_selected_reminders(crm_client):
    rows = seed_contacts()
    ids = [row['id'] for row in rows[:266]]
    response = crm_client.patch('/api/crm/contacts/bulk', json={'ids': ids, 'action': 'disqualify', 'reason': 'Injoignable'})
    assert response.status_code == 200
    assert response.get_json()['count'] == 266
    saved = application.load_data()['crm_contacts']
    assert len(saved) == len(rows)
    for contact in saved[:266]:
        assert contact['statut'] == 'Disqualifié'
        assert not contact['relance_date']
        assert all(item['status'] != 'scheduled' for item in contact['relances'])
        assert contact['disqualification_reason'] == 'Injoignable'
        assert any(activity['title'] == 'Piste disqualifiée' for activity in contact['activities'])
    for original, saved_row in zip(rows[266:], saved[266:]):
        assert original == saved_row


@pytest.fixture
def reminder_browser(crm_client):
    sync_api = pytest.importorskip('playwright.sync_api')
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except sync_api.Error as exc:
            pytest.skip(f'Chromium unavailable: {exc}')
        page = browser.new_page(viewport={'width': 1440, 'height': 1000}, timezone_id='Europe/Paris')
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        def route_request(route):
            url = urlsplit(route.request.url)
            if url.netloc != 'crm.test':
                route.abort()
                return
            path = url.path + ('?' + url.query if url.query else '')
            response = crm_client.open(path, method=route.request.method, data=route.request.post_data, content_type=route.request.headers.get('content-type'))
            route.fulfill(status=response.status_code, headers={k: v for k, v in response.headers if k.lower() not in {'content-length', 'content-encoding', 'set-cookie'}}, body=response.get_data())
        page.route('**/*', route_request)
        yield page, errors
        browser.close()


def test_browser_selects_all_overdue_previews_and_confirms_disqualification(crm_client, reminder_browser, monkeypatch):
    rows = seed_contacts()
    page, errors = reminder_browser
    sent = []
    monkeypatch.setattr(application, '_crm_send_email_html', lambda *args, **kwargs: sent.append(args) or True)
    page.goto('https://crm.test/crm/relances')
    page.locator('#reminderOverdue').click()
    assert page.locator('[data-reminder-select]').count() == 266
    page.locator('#reminderSelectAll').check()
    assert page.locator('[data-reminder-select]:checked').count() == 266
    assert '266' in page.locator('#reminderSelectedCount').inner_text()
    # Narrowing the search must discard hidden selections.
    page.locator('#reminderSearch').fill('Candidat 000')
    assert page.locator('[data-reminder-select]:checked').count() == 1
    page.locator('#reminderSearch').fill('')
    page.locator('#reminderSelectAll').check()
    page.locator('#reminderBulkEmail').click()
    assert page.locator('#sendBulkMessage').is_disabled()
    page.locator('#previewBulkMessage').click()
    page.wait_for_function("!document.querySelector('#sendBulkMessage').disabled")
    assert sent == []
    frame = page.frame_locator('#bulkMessagePreview iframe')
    assert frame.get_by_role('link', name='Prendre un rendez-vous téléphonique').get_attribute('href') == 'https://calendly.com/integraleacademy/apr'
    assert frame.get_by_text('Agent de protection physique des personnes (A3P)', exact=True).count() >= 1
    page.locator('#bulkSubject').fill('Projet de formation {{ formation }}')
    assert page.locator('#sendBulkMessage').is_disabled()
    page.locator('#cancelBulkMessage').click()
    assert sent == []
    page.locator('#reminderBulkDisqualify').click()
    page.locator('#confirmReminderDisqualify').click()
    assert application.load_data()['crm_contacts'][0]['statut'] == 'A relancer'
    page.locator('#reminderDisqualifyReason').select_option(label='Injoignable')
    page.locator('#confirmReminderDisqualify').click()
    page.wait_for_function("document.querySelectorAll('[data-reminder-select]').length === 0")
    assert page.locator('#reminderOverdue b').inner_text() == '0'
    assert page.locator('#reminderSelectedCount').inner_text() == '0 piste sélectionnée'
    assert len(application.load_data()['crm_contacts']) == len(rows)
    assert not errors


def test_browser_send_retries_only_failures_and_skips_missing_email(crm_client, reminder_browser, monkeypatch):
    seed_contacts(3)
    data = application.load_data()
    data['crm_contacts'][2]['mail'] = ''
    application.save_data(data)
    page, errors = reminder_browser
    attempts = []
    def send(to, *args, **kwargs):
        attempts.append(to)
        return to != 'candidat-1@example.test' or attempts.count(to) > 1
    monkeypatch.setattr(application, '_crm_send_email_html', send)
    page.goto('https://crm.test/crm/relances')
    page.locator('#reminderOverdue').click()
    page.locator('#reminderSelectAll').check()
    page.locator('#reminderBulkEmail').click()
    assert '1 piste ignorée' in page.locator('.bulk-message-summary').inner_text()
    page.locator('#previewBulkMessage').click()
    page.wait_for_function("!document.querySelector('#sendBulkMessage').disabled")
    page.locator('#sendBulkMessage').click()
    page.wait_for_function("document.querySelector('#sendBulkMessage').textContent.includes('Réessayer')")
    assert attempts == ['candidat-0@example.test', 'candidat-1@example.test']
    page.locator('#sendBulkMessage').click()
    page.wait_for_function("!document.querySelector('#sendBulkMessage')")
    assert attempts == ['candidat-0@example.test', 'candidat-1@example.test', 'candidat-1@example.test']
    assert page.locator('[data-reminder-select]:checked').count() == 1
    assert not errors
