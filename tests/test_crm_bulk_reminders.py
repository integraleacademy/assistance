"""Exercise the reminder selection and preview/send flow with synthetic contacts."""
from collections import Counter
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

import pytest

import app as application

ROOT = Path(__file__).parents[1]


def test_recipient_validation_has_no_100_contact_limit_and_deduplicates_addresses():
    javascript = (ROOT / 'static/crm.js').read_text()
    helper = javascript.split('function bulkMessageRecipients', 1)[1].split(
        'const reminderInterestDraft=', 1
    )[0]
    script = 'function bulkMessageRecipients' + helper + r'''
const assert=require('node:assert/strict');
const selected=Array.from({length:266},(_,id)=>({id,mail:`p${id}@example.test`}));
selected.push({id:'duplicate',mail:' P0@EXAMPLE.TEST '},{id:'missing',mail:''},{id:'invalid',mail:'bad-address'});
const result=bulkMessageRecipients('email',selected,true);
assert.equal(result.eligible.length,266);
assert.equal(result.duplicates.length,1);
assert.equal(result.missing.length,2);
assert.equal(bulkMessageRecipients('sms',[{telephone:'0600000000'}]).eligible.length,1);
'''
    subprocess.run(['node', '-e', script], cwd=ROOT, check=True, capture_output=True)


@pytest.fixture
def crm_browser(tmp_path, monkeypatch):
    browser_api = pytest.importorskip('playwright.sync_api')
    monkeypatch.setattr(application, 'DATA_FILE', str(tmp_path / 'data.json'))
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    client = application.app.test_client()
    with client.session_transaction() as session:
        session['user_email'] = 'clement@integraleacademy.com'
    data = application.load_data()
    contacts = [dict(
        id=f'overdue-{index:03}', prenom=f'Prénom {index:03}', nom='TEST',
        mail=f'prospect{index}@example.test', telephone='',
        formation='A3P' if index < 264 else 'APS', lieu='Puget-sur-Argens',
        statut='A relancer', relance_date='2020-01-06',
        activities=[], relances=[], created_at='2020-01-01T12:00:00',
    ) for index in range(266)]
    for key, overrides in [
        ('future', {'relance_date': '2099-01-06'}),
        ('missing', {'relance_date': ''}),
        ('converted', {'statut': 'Converti'}),
        ('archived', {'archived_at': '2020-01-07'}),
    ]:
        contacts.append({**contacts[0], 'id': key, 'mail': f'{key}@example.test', **overrides})
    data['crm_contacts'] = contacts
    application.save_data(data)
    sent = []
    monkeypatch.setattr(application, 'send_email_html', lambda *args, **kwargs: sent.append((args, kwargs)) or True)
    monkeypatch.setattr(application, 'send_sms', lambda *args, **kwargs: pytest.fail('No SMS expected'))
    with browser_api.sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).exists():
            pytest.skip('Playwright Chromium is not installed')
        browser = playwright.chromium.launch(args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors, requests, attempts, payloads = [], [], Counter(), []
        failures = set()
        page.on('pageerror', lambda error: errors.append(str(error)))

        def dispatch(route):
            request = route.request
            url = urlsplit(request.url)
            if url.netloc != 'crm.test':
                route.abort()
                return
            requests.append((request.method, url.path))
            # Mock delivery at the HTTP boundary; exercise 266 serial requests
            # without repeatedly recalculating unrelated integrations.
            match = re.fullmatch(r'/api/crm/contacts/([^/]+)/message', url.path)
            if match:
                contact_id = match[1]
                attempts[contact_id] += 1
                payloads.append(request.post_data_json)
                if contact_id in failures and attempts[contact_id] == 1:
                    route.fulfill(status=502, json={'error': 'Échec simulé'})
                else:
                    contact = next(c for c in contacts if c['id'] == contact_id)
                    route.fulfill(json=contact)
                return
            response = client.open(
                url.path + (f'?{url.query}' if url.query else ''),
                method=request.method, data=request.post_data,
                content_type=request.headers.get('content-type'),
            )
            route.fulfill(status=response.status_code, body=response.data,
                          content_type=response.content_type)

        page.route('**/*', dispatch)
        page.goto('http://crm.test/crm/relances')
        browser_api.expect(page.locator('#reminderOverdue b')).to_have_text('266')
        yield page, browser_api.expect, requests, attempts, payloads, failures, errors, sent, tmp_path
        browser.close()


def test_all_266_reminders_preview_edit_send_and_retry_only_failures(crm_browser):
    page, expect, requests, attempts, payloads, failures, errors, sent, tmp_path = crm_browser
    page.locator('#reminderOverdue').click()
    expect(page.locator('[data-select-reminder]')).to_have_count(266)
    page.locator('#reminderSelectAll').check()
    expect(page.locator('#reminderSelectionCount')).to_have_text('266 relances sélectionnées')
    page.locator('#reminderBulkEmail').click()
    expect(page.locator('#sendBulkMessage')).to_be_disabled()
    expect(page.locator('#bulkMsg')).to_contain_text('Êtes-vous toujours intéressé(e)')
    assert not attempts and not sent
    page.locator('#previewBulkMessage').click()
    expect(page.locator('#sendBulkMessage')).to_be_enabled()
    expect(page.locator('#bulkMessagePreviewLabel')).to_contain_text('Agent de protection physique des personnes (A3P)')
    expect(page.frame_locator('#bulkMessagePreview iframe').locator('body')).to_contain_text('Prénom 000')
    expect(page.frame_locator('#bulkMessagePreview iframe').locator('body')).to_contain_text('Et votre projet de formation ?')
    page.locator('#bulkPreviewRecipient').select_option('overdue-264')
    expect(page.locator('#sendBulkMessage')).to_be_disabled()
    page.locator('#previewBulkMessage').click()
    expect(page.locator('#bulkMessagePreviewLabel')).to_contain_text('Agent de prévention et de sécurité (APS)')
    page.locator('#bulkSubject').fill('Votre projet {{ formation }}')
    expect(page.locator('#sendBulkMessage')).to_be_disabled()
    page.locator('#previewBulkMessage').click()
    expect(page.locator('#sendBulkMessage')).to_be_enabled()
    assert not attempts and not sent
    failures.update({'overdue-003', 'overdue-150'})
    page.locator('#sendBulkMessage').click()
    expect(page.locator('#bulkMessageProgressText')).to_contain_text('264 envois réussis, 2 échecs', timeout=60000)
    assert len(attempts) == 266 and sum(attempts.values()) == 266
    assert all(payload['sujet'] == 'Votre projet {{ formation }}' for payload in payloads)
    assert len({payload['contenu'] for payload in payloads}) == 1
    expect(page.locator('#reminderSelectionCount')).to_have_text('2 relances sélectionnées')
    page.locator('#sendBulkMessage').click()
    expect(page.locator('#modalRoot')).to_be_empty(timeout=10000)
    assert sum(attempts.values()) == 268
    assert {key for key, count in attempts.items() if count == 2} == failures
    expect(page.locator('#reminderSelectionCount')).to_have_text('Aucune sélection')
    assert not errors and not sent


def test_selection_tracks_filters_and_checkbox_does_not_open_contact(crm_browser):
    page, expect, requests, attempts, payloads, failures, errors, sent, tmp_path = crm_browser
    page.locator('#reminderOverdue').click()
    first = page.locator('[data-select-reminder]').first
    first.check()
    expect(page.locator('#reminderSelectionCount')).to_have_text('1 relance sélectionnée')
    assert page.locator('#reminderSelectAll').evaluate('(el)=>el.indeterminate')
    expect(page.locator('#reminderBulkEmail')).to_be_enabled()
    page.locator('#reminderSelectAll').check()
    page.locator('#reminderFormation').select_option('APS')
    expect(page.locator('[data-select-reminder]')).to_have_count(2)
    expect(page.locator('#reminderSelectionCount')).to_have_text('2 relances sélectionnées')
    page.locator('#reminderFormation').select_option('')
    expect(page.locator('#reminderSelectionCount')).to_have_text('2 relances sélectionnées')
    page.locator('#reminderClearSelection').click()
    expect(page.locator('#reminderBulkEmail')).to_be_disabled()
    page.locator('#reminderSelectAll').check()
    page.locator('#reminderTodayMetric').click()
    expect(page.locator('#reminderSelectionCount')).to_have_text('Aucune sélection')
    expect(page.locator('#reminderSelectAll')).to_be_disabled()
    assert not attempts and not errors and not sent


def test_reminder_assets_use_a_new_browser_cache_key():
    template = (ROOT / 'templates/crm.html').read_text()
    for filename in ('crm.js', 'crm_workspace.js', 'crm_workspace.css'):
        tag = next(tag for tag in template.split('{{') if f"filename='{filename}'" in tag)
        assert "relances_bulk_version='20261023-1'" in tag


def test_stale_or_failed_preview_cannot_enable_sending(crm_browser):
    page, expect, requests, attempts, payloads, failures, errors, sent, tmp_path = crm_browser
    page.locator('#reminderOverdue').click()
    page.locator('[data-select-reminder]').first.check()
    page.locator('#reminderBulkEmail').click()
    page.evaluate('''() => {
      window.realFetch=window.fetch;
      window.fetch=(url,options)=>String(url).endsWith('/message-preview')
        ?new Promise(resolve=>{window.releasePreview=()=>resolve(new Response(
          JSON.stringify({html:'<p>Ancien contenu</p>',sujet:'Ancien objet'}),
          {status:200,headers:{'Content-Type':'application/json'}}))})
        :window.realFetch(url,options);
    }''')
    page.locator('#previewBulkMessage').click()
    page.locator('#bulkMsg').fill('Un nouveau message pour {{ prenom }}')
    page.evaluate('window.releasePreview()')
    expect(page.locator('#previewBulkMessage')).to_be_enabled()
    expect(page.locator('#sendBulkMessage')).to_be_disabled()
    expect(page.locator('#bulkMessagePreview')).to_be_hidden()
    page.evaluate('window.fetch=window.realFetch')
    page.route('**/message-preview', lambda route: route.fulfill(status=503, json={'error': 'Aperçu indisponible'}))
    page.locator('#previewBulkMessage').click()
    expect(page.locator('#toast')).to_contain_text('Aperçu indisponible')
    expect(page.locator('#sendBulkMessage')).to_be_disabled()
    assert not attempts and not errors and not sent
