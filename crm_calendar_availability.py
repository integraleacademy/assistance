"""Read Cassandre's bookable start times without altering the booking calendar."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
from threading import Lock
from time import monotonic
from zoneinfo import ZoneInfo

from secretariat_calls import folded

PARIS = ZoneInfo('Europe/Paris')
DAY = timedelta(days=1)
WEEKDAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
HOST_EMAIL = 'cassandre@integraleacademy.com'


def instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('A Calendly timestamp must include its timezone.')
    return result.astimezone(UTC)


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _is_cassandre(member):
    return (str(member.get('email') or '').strip().casefold() == HOST_EMAIL
            or folded(member.get('name')) in {'cassandre', 'cassandre menard'})


def _calendar_event(event, collection):
    uri = event['uri']
    memberships = collection('/event_type_memberships', {'event_type': uri, 'count': 100})
    hosts = [row['member'] for row in memberships]
    matching = [host for host in hosts if _is_cassandre(host)]
    if not matching:
        return None
    if len(matching) != 1:
        raise RuntimeError('Plusieurs comptes Calendly correspondent à Cassandre.')
    if len(hosts) == 1 or event.get('pooling_type') == 'collective':
        # Calendly can only offer these times when Cassandre herself is available.
        return {'uri': uri}

    host_uri = matching[0]['uri']
    rows = collection('/event_type_availability_schedules', {'event_type': uri})
    schedules = [row['availability_rule'] for row in rows]
    personal = [rule for rule in schedules if rule.get('user') == host_uri]
    # Event-wide custom hours have no user. Do not borrow a colleague's schedule.
    applicable = personal or [rule for rule in schedules if not rule.get('user')]
    if len(applicable) != 1 or not isinstance(applicable[0].get('rules'), list):
        raise RuntimeError('Calendly ne permet pas de vérifier les horaires de Cassandre sur un agenda partagé.')
    duration = int(event['duration'])
    if duration <= 0:
        raise ValueError('Calendly meeting duration must be positive.')
    return {'uri': uri, 'host': host_uri, 'schedule': applicable[0],
            'duration': timedelta(minutes=duration),
            'before': timedelta(minutes=event.get('before_buffer') or 0),
            'after': timedelta(minutes=event.get('after_buffer') or 0)}


def _working_intervals(schedule, start, end):
    zone = ZoneInfo(schedule['timezone'])
    day = start.astimezone(zone).date() - DAY
    last = end.astimezone(zone).date()
    intervals = []
    while day <= last:
        overrides = [rule for rule in schedule['rules'] if rule['type'] == 'date' and rule['date'] == day.isoformat()]
        rules = overrides or [rule for rule in schedule['rules']
                              if rule['type'] == 'wday' and rule['wday'] == WEEKDAYS[day.weekday()]]
        for rule in rules:
            for interval in rule['intervals']:
                opening = datetime.combine(day, time.fromisoformat(interval['from']), zone)
                closing_time = interval['to']
                if closing_time == '24:00':
                    closing = datetime.combine(day + DAY, time.min, zone)
                else:
                    closing = datetime.combine(day, time.fromisoformat(closing_time), zone)
                    if closing < opening:
                        closing += DAY
                intervals.append((opening.astimezone(UTC), closing.astimezone(UTC)))
        day += DAY
    merged = []
    for opening, closing in sorted(intervals):
        if merged and opening <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(closing, merged[-1][1]))
        else:
            merged.append((opening, closing))
    return merged


def _busy_intervals(host, start, end, collection):
    intervals = []
    while start < end:
        boundary = min(end, start + 7 * DAY)
        rows = collection('/user_busy_times', {'user': host, 'start_time': stamp(start), 'end_time': stamp(boundary)})
        for row in rows:
            intervals.append((instant(row.get('buffered_start_time') or row['start_time']),
                              instant(row.get('buffered_end_time') or row['end_time'])))
        start = boundary
    return intervals


def _available_starts(events, start, end, now, collection):
    shared = [event for event in events if 'host' in event]
    busy = {}
    for host in {event['host'] for event in shared}:
        earliest = max(now + timedelta(minutes=1), start - max(event['before'] for event in shared))
        latest = end + max(event['duration'] + event['after'] for event in shared)
        busy[host] = _busy_intervals(host, earliest, latest, collection)

    def times(event):
        rows = collection('/event_type_available_times', {
            'event_type': event['uri'], 'start_time': stamp(start), 'end_time': stamp(end),
        })
        openings = {instant(row['start_time']) for row in rows
                    if row['status'] == 'available' and row.get('invitees_remaining', 1) > 0}
        openings = {opening for opening in openings if start <= opening < end}
        if 'host' not in event:
            return openings
        hours = _working_intervals(event['schedule'], start, end + event['duration'])
        eligible = set()
        for opening in openings:
            closing = opening + event['duration']
            inside_hours = any(a <= opening and closing <= b for a, b in hours)
            conflict = any(a < closing + event['after'] and opening - event['before'] < b
                           for a, b in busy[event['host']])
            if inside_hours and not conflict:
                eligible.add(opening)
        return eligible

    with ThreadPoolExecutor(max_workers=4) as pool:
        return set().union(*pool.map(times, events))


class CassandreCalendar:
    """Share short-lived reads between CRM users. Failed reads never become counts."""

    def __init__(self):
        self.lock = Lock()
        self.events = ()
        self.catalog_key = None
        self.catalog_until = 0
        self.windows = {}

    def counts(self, first, length, now, account, list_types, collection, refresh=False):
        days = {(first + i * DAY).isoformat(): 0 for i in range(length)}
        end = datetime.combine(first + length * DAY, time.min, PARIS).astimezone(UTC)
        start = max(now + timedelta(minutes=1), datetime.combine(first, time.min, PARIS).astimezone(UTC))
        if start >= end:
            return days
        # Serialize cache misses. Navigating between the five tiles shares this window.
        with self.lock:
            if account != self.catalog_key or monotonic() >= self.catalog_until:
                types = [event for event in list_types() if event.get('active') and event.get('booking_method') != 'poll']
                with ThreadPoolExecutor(max_workers=4) as pool:
                    configs = list(pool.map(lambda event: _calendar_event(event, collection), types))
                events = tuple(config for config in configs if config)
                if not events:
                    raise RuntimeError('Aucun agenda actif pour Cassandre n’a été identifié dans Calendly.')
                self.events = events
                self.catalog_key, self.catalog_until = account, monotonic() + 300
                self.windows.clear()
            key = (first, length)
            cached = self.windows.get(key)
            if refresh or cached is None or monotonic() >= cached[0]:
                self.windows.pop(key, None)
                starts = _available_starts(self.events, start, end, now, collection)
                if len(self.windows) >= 32 and key not in self.windows:
                    self.windows.pop(next(iter(self.windows)))
                self.windows[key] = (monotonic() + 45, starts)
            for opening in self.windows[key][1]:
                if opening > now:
                    days[opening.astimezone(PARIS).date().isoformat()] += 1
        return days
