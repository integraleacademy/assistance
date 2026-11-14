from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock

import pytest

import app as application
import crm_calendar_availability as calendar


TODAY = date(2026, 11, 14)
NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)
CASSANDRE = {'uri': 'users/c', 'name': 'Cassandre Ménard', 'email': calendar.HOST_EMAIL}
OTHER = {'uri': 'users/a', 'name': 'Aurélie Chaussez', 'email': 'other@example.test'}


def event(uri, **fields):
    return dict(uri=uri, active=True, duration=15, **fields)


def slot(start, **values):
    return dict({'status': 'available', 'invitees_remaining': 1, 'start_time': start}, **values)


def test_team_hours_busy_buffers_overrides_and_other_hosts_are_filtered():
    events = [event('shared', pooling_type='round_robin', after_buffer=5), event('personal'), event('other'),
              event('collective', pooling_type='collective'), event('poll', booking_method='poll')]
    hosts = {'shared': [OTHER, CASSANDRE], 'personal': [CASSANDRE], 'collective': [OTHER, CASSANDRE], 'other': [OTHER]}
    monday = [{'from': '09:00', 'to': '13:00'}]
    rules = [{'type': 'wday', 'wday': 'monday', 'intervals': monday},
             {'type': 'wday', 'wday': 'tuesday', 'intervals': monday},
             {'type': 'date', 'date': '2026-11-17', 'intervals': []}]
    starts = [slot('2026-11-16T07:00:00Z'), slot('2026-11-16T08:00:00Z'), slot('2026-11-16T08:15:00Z'),
              slot('2026-11-16T08:45:00Z'), slot('2026-11-16T09:00:00Z'), slot('2026-11-16T09:15:00Z'),
              slot('2026-11-16T09:30:00Z'), slot('2026-11-16T10:00:00Z', status='unavailable'),
              slot('2026-11-16T10:30:00Z', invitees_remaining=0), slot('2026-11-16T11:55:00Z'),
              slot('2026-11-17T09:30:00Z')]

    def read(path, params):
        uri = params.get('event_type')
        if path == '/event_type_memberships':
            return [{'member': member} for member in hosts[uri]]
        if path == '/event_type_availability_schedules':
            assert uri == 'shared'
            return [{'availability_rule': {'user': OTHER['uri'], 'timezone': 'UTC', 'rules': []}},
                    {'availability_rule': {'user': CASSANDRE['uri'], 'timezone': 'Europe/Paris', 'rules': rules}}]
        if path == '/user_busy_times':
            assert params['user'] == CASSANDRE['uri']
            return [{'start_time': '2026-11-16T08:30:00Z', 'end_time': '2026-11-16T08:45:00Z',
                     'buffered_start_time': '2026-11-16T08:25:00Z', 'buffered_end_time': '2026-11-16T08:50:00Z'},
                    {'type': 'external', 'start_time': '2026-11-16T09:16:00Z', 'end_time': '2026-11-16T09:25:00Z'}]
        assert path == '/event_type_available_times'
        if uri == 'shared':
            return starts
        if uri == 'personal':
            # Same instant in two formats, an elapsed slot, and the exclusive end of the five-day window.
            return [slot('2026-11-16T09:00:00+01:00'), slot('2026-11-14T10:00:00Z'), slot('2026-11-18T23:00:00Z')]
        assert uri == 'collective'
        return [slot('2026-11-16T23:15:00Z')]

    crm = calendar.CassandreCalendar()
    counts = crm.counts(TODAY, 5, NOW, 'org', lambda: events, read)
    assert list(counts.values()) == [0, 0, 2, 1, 0]
    assert crm.counts(TODAY, 5, NOW, 'org', Mock(side_effect=AssertionError), Mock(side_effect=AssertionError)) == counts


def test_paris_clock_change_midnight_and_cache_refresh():
    first = date(2026, 10, 24)
    now = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)
    starts = ['2026-10-23T22:15:00Z', '2026-10-25T00:30:00Z', '2026-10-25T01:30:00Z',
              '2026-10-28T22:45:00Z', '2026-10-28T23:00:00Z']

    def read(path, params):
        if path == '/event_type_memberships':
            return [{'member': CASSANDRE}]
        assert params['end_time'] == '2026-10-28T23:00:00Z'
        return [slot(t) for t in starts]

    crm = calendar.CassandreCalendar()
    read = Mock(side_effect=read)
    args = ('org', lambda: [event('solo')], read)
    assert list(crm.counts(first, 5, now, *args).values()) == [1, 2, 0, 0, 1]
    assert crm.counts(first, 5, now + timedelta(minutes=16), *args)[first.isoformat()] == 0
    assert read.call_count == 2
    starts.clear()
    assert sum(crm.counts(first, 5, now, *args, refresh=True).values()) == 0
    assert read.call_count == 3


def test_provider_error_never_returns_a_partial_total_and_can_retry():
    crm = calendar.CassandreCalendar()
    fail = True

    def read(path, params):
        if path == '/event_type_memberships':
            return [{'member': CASSANDRE}]
        if params['event_type'] == 'second' and fail:
            raise RuntimeError('Unavailable')
        return [slot('2026-11-15T12:00:00Z')]

    events = lambda: [event('first'), event('second')]
    with pytest.raises(RuntimeError, match='Unavailable'):
        crm.counts(TODAY, 5, NOW, 'org', events, read)
    fail = False
    assert sum(crm.counts(TODAY, 5, NOW, 'org', events, read).values()) == 1


def test_cannot_attribute_another_hosts_agenda_or_hours():
    def read(path, params):
        if path == '/event_type_memberships':
            return [{'member': CASSANDRE}, {'member': OTHER}] if params['event_type'] == 'shared' else [{'member': OTHER}]
        assert path == '/event_type_availability_schedules'
        return [{'availability_rule': {'user': OTHER['uri'], 'timezone': 'Europe/Paris', 'rules': []}}]

    for uri in ['other', 'shared']:
        with pytest.raises(RuntimeError, match='Cassandre'):
            calendar.CassandreCalendar().counts(TODAY, 5, NOW, 'org', lambda: [event(uri)], read)


def test_endpoint_defaults_validation_past_dates_and_read_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DATA_FILE', str(tmp_path / 'calendar.json'))
    monkeypatch.setattr(application, '_CASSANDRE_CALENDAR', calendar.CassandreCalendar())
    monkeypatch.setenv('CALENDLY_ACCESS_TOKEN', 'test')
    monkeypatch.setattr(application, '_calendly_event_types_for_context', lambda data: [event('personal')])
    monkeypatch.setattr(application, '_calendly_request', lambda method, path, **kw: {
        'collection': [{'member': CASSANDRE}] if path == '/event_type_memberships' else []})
    client = application.app.test_client()
    path = '/api/crm/calendly/cassandre-availability'
    assert client.get(path).status_code in (302, 401)
    with client.session_transaction() as session:
        session['user_email'] = calendar.HOST_EMAIL
    response = client.get(path)
    assert response.status_code == 200
    assert 'no-store' in response.headers['Cache-Control']
    assert list(response.json['days'].values()) == [0] * 5
    for invalid in ['wrong', '2026-02-30', '20261118', '9999-12-31']:
        assert client.get(path, query_string={'date': invalid}).status_code == 400
    monkeypatch.setattr(application, '_calendly_request', lambda *args, **kwargs: {})
    assert client.get(path + '?date=2000-03-01').json == {'days': {'2000-03-01': 0}}
    error = client.get(path + '?date=2099-03-01')
    assert error.status_code == 503 and 'days' not in error.json


def test_remaining_places_include_only_cassandres_booked_meetings(monkeypatch):
    first = datetime.now(calendar.PARIS).date()
    dates = [(first + n * calendar.DAY).isoformat() for n in range(5)]
    openings = dict(zip(dates, [0, 1, 12, 14, 17]))
    appointments = []

    def book(day, hour, minute=0, **fields):
        row = dict(host_email=calendar.HOST_EMAIL, start_time=f'{day}T{hour:02}:{minute:02}:00',
                   event_uri=f'events/{len(appointments)}', status='active')
        row.update(fields)
        appointments.append(row)
        return row

    for day, number in zip(dates, [1, 4, 10, 12, 13]):
        for i in range(number):
            book(day, 8 + i // 4, (i % 4) * 15)
    first_booking = next(row for row in appointments if row['start_time'].startswith(dates[2]))
    first_booking.update(host_email='', host_name='Cassandre Ménard', response_status='answered')
    appointments.append(dict(first_booking, invitee_uri='invitees/second'))
    book(dates[2], 15, status='cancelled')
    book(dates[2], 16, host_email='aurelie@integraleacademy.com')
    book(dates[2], 17, new_invitee='invitees/replacement')

    # Two historical invitee rows without an event URI at the same instant.
    # Local midnight in Paris falls on the previous UTC date.
    midnight = datetime.fromisoformat(dates[2]).replace(tzinfo=calendar.PARIS)
    legacy = next(row for row in appointments if row['start_time'].startswith(dates[2]) and row is not first_booking)
    legacy.update(event_uri='', start_time=midnight.astimezone(UTC).isoformat(), host_email='',
                  host_name='Cassandre')
    appointments.append(dict(legacy, start_time=midnight.replace(tzinfo=None).isoformat()))

    def provider(day, length, *args, **kwargs):
        return {str(day + n * calendar.DAY): openings[str(day + n * calendar.DAY)] for n in range(length)}

    monkeypatch.setattr(application, '_load_data_snapshot', lambda: {'crm_calendly_appointments': appointments})
    monkeypatch.setattr(application._CASSANDRE_CALENDAR, 'counts', provider)
    monkeypatch.setenv('CALENDLY_ACCESS_TOKEN', 'test')
    client = application.app.test_client()
    with client.session_transaction() as session:
        session['user_email'] = calendar.HOST_EMAIL
    path = '/api/crm/calendly/cassandre-availability'
    assert client.get(path).get_json()['days'] == dict(zip(dates, [0, 1, 2, 0, 0]))
    selected = {'date': dates[2]}
    assert client.get(path, query_string=selected).get_json()['days'] == {dates[2]: 2}
    booking = next(row for row in appointments if row['start_time'] == f'{dates[2]}T08:30:00')
    booking['status'] = 'canceled'
    assert client.get(path, query_string=selected).get_json()['days'] == {dates[2]: 3}
    outside = str(first + 8 * calendar.DAY)
    openings[outside] = 28
    assert client.get(path, query_string={'date': outside}).get_json()['days'] == {outside: 12}


def test_calendar_tiles_selected_day_refresh_and_failure_in_browser(tmp_path, monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    playwright = pytest.importorskip('playwright.sync_api')
    monkeypatch.setattr(application, 'DATA_FILE', str(tmp_path / 'calendar.json'))
    client = application.app.test_client()
    with client.session_transaction() as session:
        session['user_email'] = calendar.HOST_EMAIL
    calls = []
    failed = False
    counts = {str(TODAY + n * calendar.DAY): count for n, count in enumerate([0, 0, 2, 0, 2])}

    def serve(route):
        url = urlsplit(route.request.url)
        if url.netloc != 'calendar.test' or route.request.method != 'GET':
            return route.abort()
        if url.path.endswith('/cassandre-availability'):
            params = parse_qs(url.query)
            calls.append(params)
            if failed:
                return route.fulfill(status=503, json={'error': 'Calendly est indisponible.'})
            day = params.get('date', [''])[0]
            return route.fulfill(json={'days': {day: 4} if day else counts})
        if url.path.startswith('/api/'):
            return route.fulfill(json={})
        response = client.get(url.path, query_string=url.query)
        return route.fulfill(status=response.status_code, body=response.data, content_type=response.content_type)

    with playwright.sync_playwright() as engine:
        try:
            browser = engine.chromium.launch()
        except playwright.Error as exc:
            pytest.skip(f'Chromium indisponible : {exc}')
        try:
            page = browser.new_page(locale='fr-FR', timezone_id='America/Los_Angeles', viewport={'width': 390, 'height': 800})
            page.clock.set_fixed_time('2026-11-13T23:15:00Z')  # Already Saturday in Paris.
            page.route('**/*', serve)
            page.goto('https://calendar.test/crm/calendrier')
            badge = page.locator('#calendarDayFree')
            tiles = page.locator('.cassandre-day')
            total = page.locator('.cassandre-total strong')
            playwright.expect(tiles).to_have_count(5)
            playwright.expect(total).to_have_text('4')
            playwright.expect(badge).to_have_text('0 place restante avec Cassandre')
            tiles.nth(2).click()
            playwright.expect(badge).to_have_text('2 places restantes avec Cassandre')
            playwright.expect(tiles.nth(2)).to_have_attribute('aria-pressed', 'true')
            assert len(calls) == 1
            page.locator('#calendarDate').fill('2026-11-25')
            playwright.expect(badge).to_have_text('4 places restantes avec Cassandre')
            playwright.expect(total).to_have_text('4')
            assert calls[-1] == {'date': ['2026-11-25']}
            failed = True
            tiles.first.click()
            page.locator('#calendarRefresh').click()
            playwright.expect(total).to_have_text('—')
            playwright.expect(badge).to_contain_text('inconnue')
            failed = False
            counts['2026-11-16'] = 1
            page.locator('.cassandre-notice button').click()
            playwright.expect(total).to_have_text('3')
            playwright.expect(page.locator('.cassandre-notice')).to_be_hidden()
            assert page.locator('.cassandre-panel').evaluate('el => el.scrollWidth <= el.clientWidth')
        finally:
            browser.close()
