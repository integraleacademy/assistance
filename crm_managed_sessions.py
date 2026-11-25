"""Project the shared catalogue onto existing CRM session labels."""
from datetime import date
import logging
import os
import re
import threading
import time

import requests

URL = "https://gestionstagiaires-r5no.onrender.com/api/integrations/session-availability"
CODES = {"aps":"APS", "a3p-apr":"A3P", "ssiap-1":"SSIAP", "ssiap-2":"SSIAP2", "ssiap-3":"SSIAP3", "desp-dssp":"DESP_INIT", "desp-vae":"DESP_VAE", "vtc":"VTC"}
CENTRES = {"paris":"paris", "puget":"cote_azur", "auvergne":"auvergne"}
PLANNING_COURSES = frozenset({"APS", "A3P", "SSIAP", "SSIAP2", "SSIAP3", "DESP_INIT"})
MONTHS = "janvier février mars avril mai juin juillet août septembre octobre novembre décembre".split()
_lock = threading.Lock()
_next = _success = 0
_rows = []


def read():
    global _next, _success, _rows
    token = os.environ.get("SESSION_CATALOG_TOKEN")
    if not token:
        return []
    with _lock:
        now = time.monotonic()
        if now >= _next:
            try:
                response = requests.get(URL, headers={"Authorization": "Bearer " + token}, timeout=(2, 5), allow_redirects=False)
                response.raise_for_status()
                body = response.json()
                if body.get("version") != 1 or not isinstance(body.get("sessions"), list):
                    raise ValueError("Format du catalogue invalide")
                _rows = body["sessions"]
                _success, _next = now, now + 30
            except (ValueError, requests.RequestException) as exc:
                logging.getLogger(__name__).warning("Calendrier des sessions indisponible (%s)", type(exc).__name__)
                _next = now + 10
        return _rows if _success and time.monotonic() - _success <= 120 else None


def french(value):
    day = date.fromisoformat(value)
    return f"{'1er' if day.day == 1 else day.day} {MONTHS[day.month-1]} {day.year}"


def match_score(context, key):
    centre, course, start, end, exam = context
    if CENTRES.get(key.get("centre")) != centre or CODES.get(key.get("slug")) != course:
        return 0
    starts = (key.get("start"), key.get("standard_start")) if course == "SSIAP" else (key.get("start"),)
    return sum((bool(start) and start in starts, bool(end) and end == key.get("end"), bool(exam) and exam == key.get("exam")))


def same(context, key):
    return match_score(context, key) >= 2


def best_key(context, row):
    keys = [key for key in [row, *row.get("aliases", [])] if same(context, key)]
    if not keys:
        return None
    def rank(key):
        delta = abs((date.fromisoformat(context[2]) - date.fromisoformat(key["start"])).days) if context[2] and key.get("start") else 99999
        return match_score(context, key), -delta
    return max(keys, key=rank)


def matching_rows(catalogue, context, identifier="", *, handoff=False):
    # A stable catalogue ID must never transfer an old enrolment link to a replacement.
    if identifier:
        direct = [row for row in catalogue if identifier == row["id"] or
                  row["slug"] == "ssiap-1" and identifier == row["id"] + "-sans-sst"]
        if direct:
            return [(row, best_key(context, row)) for row in direct]
        if identifier.startswith("platform-"):
            return []
    matches = [(row, key) for row in catalogue if (key := best_key(context, row))]
    managed_matches = [(row, key) for row, key in matches if row.get("managed")]
    if managed_matches:
        matches = managed_matches
    if handoff and any(row.get("deleted") or row["visibility"] in ("closed", "internal") for row, _ in matches):
        return [(row, key) for row, key in matches if row.get("deleted") or row["visibility"] in ("closed", "internal")]
    current = [(row, key) for row, key in matches if not row.get("deleted") and same(context, row)]
    return current or matches


def details(row):
    return {"session_name": row.get("name") or "", "venue": row.get("venue") or ""}


def withdrawn_contact_filters(contacts, catalogue, date_range):
    if catalogue == []:
        return []
    centres = {"Paris": "paris", "Côte d’Azur": "cote_azur", "Auvergne": "auvergne"}
    courses = {"APS": "APS", "A3P": "A3P", "SSIAP 1": "SSIAP", "SSIAP 2": "SSIAP2",
               "SSIAP 3": "SSIAP3", "DESP": "DESP_INIT", "VTC": "VTC"}
    choices = {(c.get("formation"), c.get("lieu"), c.get("dates_formation")) for c in contacts if c.get("dates_formation")}
    if catalogue is None:
        return sorted([course, centre, label] for course, centre, label in choices
                      if courses.get(course) in PLANNING_COURSES and centre in centres)
    groups = [r for r in catalogue if r.get("managed")]
    retired = []
    for course, centre, label in choices:
        if course not in courses or centre not in centres:
            continue
        start, end = date_range(label)
        if not start or not end:
            continue
        ctx = (centres[centre], courses[course], start.isoformat(), end.isoformat(), "")
        hits = matching_rows(groups, ctx)
        if hits and not any(r["visibility"] != "closed" and same(ctx, r) for r, _ in hits):
            retired.append([course, centre, label])
    return sorted(retired)


def availability(row):
    sold = row.get("sold")
    left = max(0, (row.get("capacity") or 12) - sold) if isinstance(sold, int) else None
    label = "Disponibilité à confirmer" if left is None else "Complet" if left == 0 else "1 place restante" if left == 1 else f"{left} places restantes"
    return {"full": left == 0, "badge": label}


def website_selection(choice, catalogue, date_range):
    if not choice or catalogue is None and choice["formation"] in PLANNING_COURSES:
        return None
    if catalogue is None:
        return choice
    start, end = date_range(choice["label"])
    context = ("cote_azur", choice["formation"], start.isoformat() if start else "",
               end.isoformat() if end else "", choice.get("date_examen"))
    hits = matching_rows(catalogue, context, choice["id"], handoff=True)
    if not hits:
        return None if choice["id"].startswith("platform-") else choice
    if len(hits) != 1:
        return None
    row, key = hits[0]
    if (row["visibility"] not in ("public", "match") or key != row
            or (choice.get("date_examen") and row["exam"] and choice["date_examen"] != row["exam"])
            or isinstance(row.get("sold"), int) and row["sold"] >= (row.get("capacity") or 12)):
        return None
    if row.get("standard_start") and choice.get("formula") in ("ssiap-1", "ssiap-1-sst"):
        expected = row["standard_start"] if choice["formula"] == "ssiap-1" else row["start"]
        if context[2] != expected:
            return None
    return {**choice, **availability(row), **details(row), "catalogue_id": row["id"]}


def requested_date_available(catalogue, form, date_range, projected_choices=None):
    course, label = form.get("formation"), form.get("dates")
    if course not in PLANNING_COURSES or not label or catalogue == []:
        return True
    if catalogue is None:
        return False
    start, end = date_range(label)
    if not start or not end:
        return not form.get("catalogue_session_id")
    context = (form.get("centre"), course, start.isoformat(), end.isoformat(), form.get("date_examen"))
    identifier = form.get("catalogue_session_id") or form.get("ssiap_session_id") or ""
    matches = matching_rows(catalogue, context, identifier, handoff=True)
    if not matches:
        return not identifier.startswith("platform-")
    if len(matches) != 1:
        return False
    row, _ = matches[0]
    if (row["visibility"] not in ("public", "match") or availability(row)["full"] or
            CENTRES.get(row["centre"]) != context[0] or CODES.get(row["slug"]) != course):
        return False
    if course == "SSIAP" and projected_choices is not None:
        # A legacy course can have two offered starts even without a stored standard_start.
        return any(option.get("catalogue_id") == row["id"] and not option.get("full")
                   and date_range(option["label"]) == (start, end)
                   and (not form.get("ssiap_formule") or option.get("formula") == form["ssiap_formule"])
                   and (not context[4] or context[4] == option.get("date_examen"))
                   for option in projected_choices())
    return (context[2] in (row["start"], row.get("standard_start")) and context[3] == row["end"]
            and (not context[4] or row["exam"] == context[4]))


def options(catalogue, local, date_range, *, include_internal=False):
    if catalogue is None:
        # Cached website or CRM calendars can still contain a withdrawn or private group.
        return {centre: {course: [] if course in PLANNING_COURSES else values for course, values in courses.items()}
                for centre, courses in local.items()}
    if not catalogue:
        return local
    handled = set()
    output = {}
    def put(centre, code, entry):
        output.setdefault(centre, {}).setdefault(code, []).append(entry)
    def managed(row, base=None, alias=None, formula=None):
        base = dict(base or {})
        start, end = row["start"], row["end"]
        variant = re.search(r"\b(avec|sans) SST\b", base.get("label",""), re.I)
        if variant and row["slug"] == "ssiap-1":
            base["formula"] = "ssiap-1" if variant[1].lower() == "sans" else "ssiap-1-sst"
        if formula:
            start = row["standard_start"] if formula == "ssiap-1" else row["start"]
            base["formula"] = formula
        elif variant and alias and alias.get("start"):
            old, _ = date_range(base["label"])
            if old:
                start = (date.fromisoformat(start) + (old - date.fromisoformat(alias["start"]))).isoformat()
        if row["visibility"] != "match" or row.get("edited") or not base:
            form_name = ("sans SST" if formula == "ssiap-1" else "avec SST") if formula else (variant[0] if variant else "")
            base["label"] = f"Du {french(start)} au {french(end)}" + (f" ({form_name})" if form_name else "")
            base["date_examen"] = row.get("exam") or ""
            base["start_date"] = start
        base.update(catalogue_id=row["id"], **availability(row), **details(row))
        periods = []
        for key, label in (("remote", "À distance"), ("onsite", "Au centre de formation")):
            lo, hi = row.get(key+"_start"), row.get(key+"_end")
            if formula == "ssiap-1" and key == "onsite" and lo:
                lo = start
            if lo and hi:
                periods.append(f"{label} du {french(lo)} au {french(hi)}")
        if periods:
            base["periods"] = " · ".join(periods)
        elif row.get("edited"):
            base.pop("periods", None)
        return base

    def add_row(centre, course, row, base=None, alias=None):
        if course == "SSIAP" and row.get("standard_start") and (row["visibility"] != "match" or row.get("edited")):
            for formula in ("ssiap-1-sst", "ssiap-1"):
                put(centre, course, managed(row, base, alias, formula))
        else:
            put(centre, course, managed(row, base, alias))

    for centre, courses in local.items():
        output.setdefault(centre, {})
        for code, values in courses.items():
            output[centre].setdefault(code, [])
            for value in values:
                start, end = date_range(value.get("label", ""))
                ctx = (centre, code, start.isoformat() if start else "", end.isoformat() if end else "", value.get("date_examen"))
                identifier = value.get("catalogue_id") or ""
                hits = matching_rows(catalogue, ctx, identifier)
                if not hits:
                    if not identifier.startswith("platform-"):
                        put(centre, code, value)
                    continue
                if len(hits) > 1 and any(r.get("managed") for r, _ in hits):
                    # Drop the ambiguous historical choice. Each explicitly offered group
                    # is added below under its own source ID, with its own visibility.
                    continue
                handled.update(r["id"] for r,k in hits)
                if len(hits) == 1:
                    row, key = hits[0]
                    if row["visibility"] == "closed" or row["visibility"] == "internal" and not include_internal:
                        continue
                    dest, course = CENTRES.get(row["centre"]), CODES.get(row["slug"])
                    if dest and course:
                        add_row(dest, course, row, value, key)
                elif all(r["visibility"] not in ("closed", "internal") for r,k in hits):
                    put(centre, code, {**value, "badge":"Disponibilité à confirmer"})
    for row in catalogue:
        code, place = CODES.get(row["slug"]), CENTRES.get(row["centre"])
        if code and place and row["id"] not in handled and (row["visibility"] == "public" or include_internal and row["visibility"] == "internal"):
            add_row(place, code, row)
    for courses in output.values():
        for code, values in courses.items():
            # A moved local entry and an already updated one can describe the same offer.
            seen = set()
            unique = []
            for item in values:
                key = (item.get("catalogue_id"), item.get("label"))
                if not key[0] or key not in seen:
                    unique.append(item)
                    seen.add(key)
            courses[code] = values = unique
            values.sort(key=lambda v: date_range(v.get("label", ""))[0] or date.max)
    return output
