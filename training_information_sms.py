"""Accusés de réception des SMS d’information, partagés entre les parcours formation."""

from contextlib import closing
from datetime import datetime
from hashlib import sha256
from itertools import chain
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo
import fcntl
import json
import logging
import re
import sqlite3
import time


WINDOW_SECONDS = 24 * 60 * 60
PARIS = ZoneInfo("Europe/Paris")


class Receipt(NamedTuple):
    status: str
    timestamp: float

    @property
    def date(self):
        return datetime.fromtimestamp(self.timestamp, PARIS)


def _key(phone, course):
    code = str(course or "").strip().upper()
    if code == "SSIAP" or re.fullmatch(r"SSIAP[ _-]?1", code):
        code = "SSIAP1"
    elif re.fullmatch(r"SSIAP[ _-]?3", code):
        code = "SSIAP3"
    return sha256(f"{phone}:{code}".encode()).hexdigest()


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=PARIS)
        return parsed.timestamp()
    except (ValueError, TypeError):
        try:
            return datetime.strptime(value, "%d/%m/%Y %H:%M").replace(tzinfo=PARIS).timestamp()
        except (ValueError, TypeError):
            return 0


def _historical_rows(data):
    for row in data.get("secretariat_demandes", []):
        if not isinstance(row, dict):
            continue
        stamp = row.get("sms_summary_sent_at") or row.get("information_sms_sent_at")
        if stamp:
            yield row, stamp
    for row in data.get("formulaires_abandonnes", []):
        if isinstance(row, dict) and isinstance(row.get("fields"), dict) and row.get("abandoned_sms_sent_at"):
            yield row["fields"], row["abandoned_sms_sent_at"]
    for row in chain(data.get("demandes", []), data.get("archives", [])):
        if not isinstance(row, dict):
            continue
        stamp = row.get("sms_sent_at") or row.get("abandoned_sms_sent_at")
        if stamp:
            fields = row.get("details") or {}
            if isinstance(fields, str):
                try:
                    fields = json.loads(fields)
                except ValueError:
                    continue
            if isinstance(fields, dict):
                yield {**fields, "telephone": row.get("telephone") or fields.get("telephone")}, stamp


def _journal_path(data_file):
    journal = Path(str(data_file) + ".formation-sms.sqlite3")
    journal.parent.mkdir(parents=True, exist_ok=True)
    return journal


def _initialize(db, now, load_history, normalize):
    if db.execute("PRAGMA user_version").fetchone()[0]:
        return
    db.execute("CREATE TABLE receipt (key TEXT PRIMARY KEY, state TEXT NOT NULL, stamp REAL NOT NULL)")
    # Seed the rolling window with confirmed messages sent by the previous version.
    for fields, value in _historical_rows(load_history()):
        stamp = _timestamp(value)
        if now - WINDOW_SECONDS < stamp <= now:
            phone = normalize(fields.get("telephone"))
            if phone:
                db.execute("""INSERT INTO receipt VALUES (?, 'sent', ?)
                              ON CONFLICT(key) DO UPDATE SET stamp = max(stamp, excluded.stamp)""",
                           (_key(phone, fields.get("formation")), stamp))
    db.execute("PRAGMA user_version = 1")


def preserve_history_before_cleanup(data_file, data, normalize):
    # A submitted form can remove its old draft before its first SMS check on upgrade.
    if not any(isinstance(draft, dict) and draft.get("abandoned_sms_sent_at") and not draft.get("abandoned_sms_status")
               for draft in data.get("formulaires_abandonnes", [])):
        return
    with closing(sqlite3.connect(_journal_path(data_file), timeout=15)) as db, db:
        if not db.execute("PRAGMA user_version").fetchone()[0]:
            db.execute("BEGIN IMMEDIATE")
            _initialize(db, time.time(), lambda: data, normalize)


def _claim(db, key, generic_key, now, load_history, normalize):
    cutoff = now - WINDOW_SECONDS
    with db:
        db.execute("BEGIN IMMEDIATE")
        _initialize(db, now, load_history, normalize)
        db.execute("DELETE FROM receipt WHERE stamp <= ?", (cutoff,))
        previous = db.execute("SELECT state, stamp FROM receipt WHERE key = ?", (key,)).fetchone()
        if previous is None and key != generic_key:
            # Older abandoned forms could send a generic SMS before the prospect chose a course.
            # Attribute that receipt to the first course requested, leaving other courses independent.
            previous = db.execute("SELECT state, stamp FROM receipt WHERE key = ?", (generic_key,)).fetchone()
            if previous:
                db.execute("UPDATE receipt SET key = ? WHERE key = ?", (key, generic_key))
        if previous:
            return Receipt("already_sent" if previous[0] == "sent" else "unconfirmed", previous[1])
        # Reserve before the network call. A crash after a possible delivery cannot trigger a second SMS.
        db.execute("INSERT INTO receipt VALUES (?, 'unconfirmed', ?)", (key, now))
    return None


def deliver(data_file, phone, course, send, load_history, normalize):
    number = normalize(phone)
    if not number:
        return Receipt("failed", time.time())
    key = _key(number, course)
    generic_key = _key(number, "")
    journal = _journal_path(data_file)
    # Sixteen stable lock files coordinate workers without holding the database write lock over HTTP.
    with open(f"{journal}.{generic_key[0]}.lock", "a") as lock, closing(sqlite3.connect(journal, timeout=15)) as db:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = _claim(db, key, generic_key, time.time(), load_history, normalize)
        if previous:
            return previous
        try:
            accepted = send()
        except Exception:
            logging.getLogger(__name__).exception("Envoi du SMS d’information à vérifier")
            accepted = None
        now = time.time()
        with db:
            if accepted:
                db.execute("UPDATE receipt SET state = 'sent', stamp = ? WHERE key = ?", (now, key))
                return Receipt("sent", now)
            if accepted is False:
                db.execute("DELETE FROM receipt WHERE key = ?", (key,))
                return Receipt("failed", now)
        return Receipt("unconfirmed", now)
