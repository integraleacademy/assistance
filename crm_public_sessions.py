"""Sessions du calendrier public utiles aux e-mails CRM et à la prise d’appel."""
import calendar
import datetime
import logging
import re
import threading
import time
from dataclasses import dataclass

import requests

from crm_location_normalization import canonical_crm_location


PUBLIC_CALENDAR = "https://integraleacademyofficiel-1.onrender.com/api/sessions"
REFRESH_SECONDS = 900
_MONTHS = "janvier février mars avril mai juin juillet août septembre octobre novembre décembre".split()
_CENTRES = {"Côte d’Azur": "cote_azur", "Auvergne": "auvergne", "Paris": "paris"}
_SLUGS = {"aps": "APS", "a3p-apr": "A3P", "desp-dssp": "DESP_INIT", "ssiap-1": "SSIAP"}
_lock = threading.Lock()
_next_refresh = 0
_calendar = {}


def _french_date(day):
    number = "1er" if day.day == 1 else str(day.day)
    return f"{number} {_MONTHS[day.month - 1]} {day.year}"


def _iso_day(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _sst_variant(text):
    match = re.search(r"\b(avec|sans) SST\b", text or "", re.I)
    return match[1].lower() + " SST" if match else ""


def _delivery_periods(row):
    periods = []
    for prefix, label in (("remote", "À distance"), ("inPerson", "En centre")):
        start = _iso_day(row.get(prefix + "StartDate"))
        end = _iso_day(row.get(prefix + "EndDate"))
        if start and end and start <= end:
            periods.append(f"{label} du {start:%d/%m} au {end:%d/%m/%Y}")
    return " · ".join(periods)


@dataclass(frozen=True)
class PublishedSession:
    start: datetime.date
    end: datetime.date
    exam: datetime.date | None
    variant: str
    periods: str
    badge: str

    @property
    def label(self):
        return f"Du {_french_date(self.start)} au {_french_date(self.end)}"

    @property
    def email_label(self):
        exam = f" — examen le {_french_date(self.exam)}" if self.exam else ""
        variant = f" ({self.variant})" if self.variant else ""
        return self.label + exam + variant


def _public_calendar(rows):
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        training = row.get("training")
        if not isinstance(training, dict) or training.get("isActive") is False or row.get("status") == "HIDDEN":
            continue
        code = _SLUGS.get(training.get("slug"))
        centre = _CENTRES.get(canonical_crm_location(row.get("location")))
        start, end = _iso_day(row.get("startDate")), _iso_day(row.get("endDate"))
        if not (code and centre and start and end and start <= end):
            continue
        seats = row.get("seatsLeft")
        badge = ""
        if row.get("status") == "FULL" or (row.get("showSeatsLeft") and type(seats) is int and seats == 0):
            badge = "Complet"
        elif row.get("showSeatsLeft") and type(seats) is int and seats > 0:
            badge = f"{seats} place disponible" if seats == 1 else f"{seats} places disponibles"
        session = PublishedSession(start, end, _iso_day(row.get("examDate")), _sst_variant(row.get("title")), _delivery_periods(row), badge)
        result.setdefault((centre, code), []).append(session)
    return result


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
    sessions = [item for item in _published_sessions().get((centre, formation), []) if item.start >= today]
    public_ranges = {(item.start, item.end) for item in sessions}
    combined = [(item.start, item.email_label) for item in sessions]
    for label in local_labels:
        start, end = date_range(label)
        if (start, end) not in public_ranges:
            combined.append((start or datetime.date.max, label))
    return list(dict.fromkeys(label for _, label in sorted(combined)))


def desk_calendar(local, *, today, date_range, use_public):
    """Keep CRM dates. Enrich exact matches, and add public sessions where the CRM has no equivalent."""
    public = _published_sessions() if use_public else {}
    result = {}
    keys = {(centre, code) for centre, courses in local.items() for code in courses} | set(public)
    for centre, code in keys:
        rows = []
        local_ranges = []
        for original in local.get(centre, {}).get(code, []):
            label = str(original.get("label") or "").strip()
            start, end = date_range(label)
            if not label:
                continue
            row = dict(original)
            row["start_date"] = start.isoformat() if start else ""
            if (code in {"DESP_VAE", "VTC"} and start and end and not row.get("date_examen")
                    and start.day == 1 and start.replace(day=calendar.monthrange(start.year, start.month)[1]) == end):
                row["month_preference"] = f"{_MONTHS[start.month - 1].capitalize()} {start.year}"
            rows.append(row)
            if start and end:
                local_ranges.append((start, end, row))
        for session in public.get((centre, code), []):
            if session.start < today:
                continue
            compatible = [(start, end, row) for start, end, row in local_ranges
                          if not session.variant or _sst_variant(row["label"]) in ("", session.variant)]
            match = next((row for start, end, row in compatible if (start, end) == (session.start, session.end)), None)
            if match is None:
                # A slightly rescheduled cohort appears only at the dates retained by the secretariat.
                if code != "SSIAP" and any(abs((start - session.start).days) <= 7 and start <= session.end
                                           and session.start <= end for start, end, _ in compatible):
                    continue
                label = session.label + (f" ({session.variant})" if session.variant else "")
                if any(row["label"] == label for row in rows):
                    continue
                match = {"label": label, "start_date": session.start.isoformat()}
                rows.append(match)
            if not match.get("date_examen") and session.exam:
                match["date_examen"] = session.exam.isoformat()
            if not match.get("badge") and session.badge:
                match["badge"] = session.badge
            if session.variant and not _sst_variant(match["label"]):
                match["label"] += f" ({session.variant})"
            if session.periods:
                match["periods"] = session.periods
        result.setdefault(centre, {})[code] = sorted(rows, key=lambda row: row["start_date"] or "9999")
    return result
