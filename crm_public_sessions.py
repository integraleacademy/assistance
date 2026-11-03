"""Supplement CRM email dates with sessions advertised on the Academy's public site."""
import datetime
import logging
import re
import threading
import time

import requests

from crm_location_normalization import canonical_crm_location


PUBLIC_CALENDAR = "https://integraleacademyofficiel-1.onrender.com/api/sessions"
REFRESH_SECONDS = 900
_MONTHS = "janvier février mars avril mai juin juillet août septembre octobre novembre décembre".split()
_CENTRES = {"Côte d’Azur": "cote_azur", "Auvergne": "auvergne", "Paris": "paris"}
_lock = threading.Lock()
_next_refresh = 0
_calendar = {}


def _french_date(day):
    number = "1er" if day.day == 1 else str(day.day)
    return f"{number} {_MONTHS[day.month - 1]} {day.year}"


def _public_calendar(rows):
    calendar = {}
    for row in rows:
        formation = {"aps": "APS", "ssiap-1": "SSIAP"}.get((row.get("training") or {}).get("slug"))
        centre = _CENTRES.get(canonical_crm_location(row.get("location")))
        if not formation or not centre or row.get("status") == "HIDDEN":
            continue
        start = datetime.date.fromisoformat(row["startDate"][:10])
        end = datetime.date.fromisoformat(row["endDate"][:10])
        label = f"Du {_french_date(start)} au {_french_date(end)}"
        if row.get("examDate"):
            exam = datetime.date.fromisoformat(row["examDate"][:10])
            label += f" — examen le {_french_date(exam)}"
        variant = re.search(r"\b(avec|sans) SST\b", row.get("title") or "", re.I)
        if variant:
            label += f" ({variant[0]})"
        calendar.setdefault((centre, formation), []).append((start, end, label))
    return calendar


def _published_sessions():
    global _calendar, _next_refresh
    with _lock:
        now = time.monotonic()
        if now >= _next_refresh:
            try:
                response = requests.get(PUBLIC_CALENDAR, timeout=(2, 3))
                response.raise_for_status()
                rows = response.json()["sessions"]
                if not isinstance(rows, list):
                    raise ValueError("Invalid public session list")
                _calendar = _public_calendar(rows)
                _next_refresh = now + REFRESH_SECONDS
            except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as exc:
                logging.getLogger(__name__).warning("Planning public temporairement indisponible (%s)", type(exc).__name__)
                _next_refresh = now + 60
        return _calendar


def email_session_labels(centre, formation, local_labels, today, date_range):
    """Prefer published labels (including exam/SST), keeping CRM-only sessions."""
    sessions = [item for item in _published_sessions().get((centre, formation), []) if item[0] >= today]
    public_ranges = {(start, end) for start, end, _ in sessions}
    combined = [(start, label) for start, _, label in sessions]
    for label in local_labels:
        start, end = date_range(label)
        if (start, end) not in public_ranges:
            combined.append((start or datetime.date.max, label))
    return list(dict.fromkeys(label for _, label in sorted(combined)))
