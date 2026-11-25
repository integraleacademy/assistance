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
        return _rows if now - _success <= 120 else [{**row, "sold": None} for row in _rows]


def french(value):
    day = date.fromisoformat(value)
    return f"{'1er' if day.day == 1 else day.day} {MONTHS[day.month-1]} {day.year}"


def same(context, key):
    centre, course, start, end, exam = context
    if CENTRES.get(key.get("centre")) != centre or CODES.get(key.get("slug")) != course:
        return False
    return sum(bool(day) and day == key.get(field) for day, field in ((start,"start"),(end,"end"),(exam,"exam"))) >= 2


def options(catalogue, local, date_range, *, include_internal=False):
    if not catalogue:
        return local
    handled = set()
    output = {}
    def put(centre, code, entry):
        output.setdefault(centre, {}).setdefault(code, []).append(entry)
    def managed(row, base=None, alias=None):
        base = dict(base or {})
        start, end = row["start"], row["end"]
        variant = re.search(r"\b(avec|sans) SST\b", base.get("label",""), re.I)
        if variant and alias and alias.get("start"):
            old, _ = date_range(base["label"])
            if old:
                start = (date.fromisoformat(start) + (old - date.fromisoformat(alias["start"]))).isoformat()
        if row["visibility"] != "match" or not base:
            base["label"] = f"Du {french(start)} au {french(end)}" + (f" ({variant[0]})" if variant else "")
            base["date_examen"] = row.get("exam") or ""
            base["start_date"] = start
        sold = row.get("sold")
        left = max(0, (row.get("capacity") or 12) - sold) if isinstance(sold, int) else None
        base.update(catalogue_id=row["id"], full=left == 0,
                    badge=("Complet" if left == 0 else "1 place restante" if left == 1 else f"{left} places restantes" if left is not None else "Disponibilité à confirmer"))
        periods = []
        for key, label in (("remote", "À distance"), ("onsite", "En centre")):
            lo, hi = row.get(key+"_start"), row.get(key+"_end")
            if lo and hi:
                periods.append(f"{label} du {french(lo)} au {french(hi)}")
        if periods:
            base["periods"] = " · ".join(periods)
        return base

    for centre, courses in local.items():
        output.setdefault(centre, {})
        for code, values in courses.items():
            output[centre].setdefault(code, [])
            for value in values:
                start, end = date_range(value.get("label", ""))
                ctx = (centre, code, start.isoformat() if start else "", end.isoformat() if end else "", value.get("date_examen"))
                hits = [(r, next((k for k in [r, *r.get("aliases", [])] if same(ctx, k)), None)) for r in catalogue]
                hits = [(r,k) for r,k in hits if k or (value.get("catalogue_id") and value["catalogue_id"] == r["id"])]
                if not hits:
                    put(centre, code, value)
                    continue
                handled.update(r["id"] for r,k in hits)
                if len(hits) == 1:
                    row, key = hits[0]
                    if row["visibility"] == "closed" or row["visibility"] == "internal" and not include_internal:
                        continue
                    dest, course = CENTRES.get(row["centre"]), CODES.get(row["slug"])
                    if dest and course:
                        put(dest, course, managed(row, value, key))
                elif all(r["visibility"] not in ("closed", "internal") for r,k in hits):
                    put(centre, code, {**value, "badge":"Disponibilité à confirmer"})
    for row in catalogue:
        code, place = CODES.get(row["slug"]), CENTRES.get(row["centre"])
        if code and place and row["id"] not in handled and (row["visibility"] == "public" or include_internal and row["visibility"] == "internal"):
            put(place, code, managed(row))
    for courses in output.values():
        for values in courses.values():
            values.sort(key=lambda v: date_range(v.get("label", ""))[0] or date.max)
    return output
