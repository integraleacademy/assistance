"""Empirical conversion rates for scheduled phone appointments."""
from bisect import bisect_left, bisect_right
from collections import defaultdict

from candidate_ai_analysis import parse_calendly_datetime
from secretariat_calls import folded, is_phone_appointment


OUTCOMES = {"confirmed", "pending", "declined"}
CANCELLED = {"canceled", "cancelled"}
DAY = 86400
MIN_CONTACTS = 20
LOOKBACK = 365 * DAY
FOLLOW_UP = 7 * DAY
PROJECT_FIELDS = {"dates_formation", "statut_demande_financement_ft", "reste_a_charge_perso", "origine", "desp_type"}


def _time(value):
    try:
        date = parse_calendly_datetime(value)
        return date.timestamp() if date else None
    except (OverflowError, OSError, ValueError):
        return None


def booking_facts(contact):
    """Only commercial facts known at the time of this snapshot."""
    funding = folded(contact.get("statut_demande_financement_ft"))
    personal = folded(contact.get("reste_a_charge_perso"))
    if funding == "acceptee":
        financing = "accordé"
    elif personal in {"oui", "yes", "true"}:
        financing = "reste accepté"
    elif funding in {"transmise", "en cours instruction"}:
        financing = "en instruction"
    else:
        financing = ""
    return [bool(contact.get("dates_formation")), financing, folded(contact.get("origine")),
            folded(contact.get("desp_type"))]


def remember_booking_facts(appointment, contact, now):
    start = _time(appointment.get("start_time"))
    if (contact and start and start > _time(now) and is_phone_appointment(appointment)
            and appointment.get("status") not in CANCELLED):
        snapshot = appointment.get("conversion_context") or {}
        facts = booking_facts(contact)
        if snapshot.get("contact_id") != contact.get("id") or snapshot.get("facts") != facts:
            appointment["conversion_context"] = {
                "contact_id": contact.get("id"), "as_of": now, "facts": facts,
            }


def validate_outcome(appointment, outcome, response, now):
    if not isinstance(outcome, str) or outcome not in OUTCOMES | {""}:
        return "Résultat commercial invalide."
    if response != "answered" or not appointment.get("contact_id"):
        return "Cette décision concerne un rendez-vous décroché et lié à une fiche."
    start = _time(appointment.get("start_time"))
    if (not start or start > _time(now) or appointment.get("status") in CANCELLED
            or not is_phone_appointment(appointment)):
        return "Ce rendez-vous téléphonique doit avoir commencé pour enregistrer sa décision."
    return ""


def record_outcome(appointment, outcome, now):
    if appointment.get("enrollment_outcome", "") != outcome:
        appointment["enrollment_outcome"] = outcome
        appointment["enrollment_outcome_at"] = now
        appointment["updated_at"] = now


def _events(contact):
    exchanges, reminders, quotes, conversions = [], [], [], []
    discussions = []
    def add_discussion(event, text, at):
        # Only the topic is retained. An edited note is first usable at edit time.
        topic = folded(str(text or "")[:600]).split()
        if any(word in {"cpf", "financement", "financer", "session", "acompte"} for word in topic):
            edited = _time(event.get("edited_at") or event.get("updated_at"))
            discussions.append(max(at, edited or at))
    for event in contact.get("activities") or ():
        if not isinstance(event, dict):
            continue
        kind = str(event.get("kind") or "")
        if kind not in {"statut", "appel", "email", "sms", "demande_rappel", "devis", "relance"}:
            continue
        at = _time(event.get("date"))
        if at is None:
            continue
        title = folded(event.get("title"))
        if kind == "statut" and title == "statut converti":
            conversions.append(at)
        if kind in {"appel", "email", "sms", "demande_rappel"}:
            exchanges.append(at)
            if kind in {"appel", "demande_rappel"}:
                add_discussion(event, event.get("detail"), at)
        if kind == "devis" or (kind == "email" and "devis" in title):
            quotes.append(at)
        if kind == "relance":
            reminders.append(at)
    for note in contact.get("publications") or ():
        if isinstance(note, dict):
            at = _time(note.get("date"))
            if at is not None:
                # Publication edits in old dossiers did not timestamp the edited text.
                exchanges.append(at)
    for reminder in contact.get("relances") or ():
        if isinstance(reminder, dict):
            at = _time(reminder.get("created_at"))
            if at is not None:
                reminders.append(at)
    return (sorted(exchanges), sorted(reminders), sorted(quotes), sorted(discussions)), min(conversions, default=None)


def _keys(appointment, contact, cutoff, prior, events, future, bookings):
    course = folded(appointment.get("name") or appointment.get("event_type_uri"))
    exchanges, reminders, quotes, discussions = events
    def recent(items):
        return bisect_left(items, cutoff) - bisect_left(items, cutoff - 30 * DAY)
    if recent(quotes):
        engagement = "devis"
    elif recent(reminders):
        engagement = "relance"
    elif recent(exchanges):
        engagement = "échanges"
    else:
        engagement = "sans échange récent"
    previous = bisect_left(prior, (cutoff,)) - 1
    history = prior[previous][1] if previous >= 0 else "premier"
    base = (course,)
    detailed = (course, history, engagement)
    opens, closes = bookings
    active = bisect_right(opens, cutoff) - bisect_left(closes, cutoff)
    booked_at = _time(appointment.get("calendly_created_at") or appointment.get("created_at"))
    other_planned = max(0, active - int(booked_at is not None and booked_at <= cutoff))
    context = (min(recent(exchanges), 3), min(recent(reminders), 2), bool(recent(discussions)),
               bool(other_planned), bool(appointment.get("old_invitee") or appointment.get("rescheduled")))
    keys = []
    snapshot = appointment.get("conversion_context") or {}
    as_of = _time(snapshot.get("as_of"))
    if (as_of and as_of <= cutoff and snapshot.get("contact_id") == contact.get("id")
            and isinstance(snapshot.get("facts"), (tuple, list)) and len(snapshot["facts"]) == 4):
        facts = tuple(snapshot["facts"])
    elif future:
        facts = tuple(booking_facts(contact))
    else:
        facts = None
    if facts is not None and all(isinstance(value, (str, bool)) for value in facts):
        keys.append((detailed + context + facts, "Même type de rendez-vous, parcours, échanges, session, financement et origine comparables"))
    if any(context):
        keys.append((detailed + context, "Appels, échanges, relances et réservations comparables pour ce type de rendez-vous"))
    keys.extend([(detailed, "Même type de rendez-vous, historique d’appels et d’échanges comparable"),
                 ((course, history), "Même type de rendez-vous et historique d’appels comparable"),
                 (base, "Même type de rendez-vous téléphonique")])
    return keys


class Sample:
    """Totals with a cheap leave-one-contact-out calculation for repeat appointments."""
    def __init__(self):
        self.total = self.known = self.positive = 0
        self.by_contact = defaultdict(lambda: [0, 0, 0])
        self.observed_contacts = 0

    def add(self, contact, outcome):
        entry = self.by_contact[contact]
        entry[0] += 1
        self.total += 1
        if outcome is not None:
            self.observed_contacts += int(entry[1] == 0)
            entry[1] += 1
            entry[2] += int(outcome)
            self.known += 1
            self.positive += int(outcome)

    def excluding(self, contact):
        total, known, positive = self.by_contact.get(contact, (0, 0, 0))
        return (self.total - total, self.known - known, self.positive - positive,
                self.observed_contacts - int(known > 0))


def _estimate(contact_id, keys, samples, recent_samples, mode):
    for key, label in keys:
        sample = samples.get(key)
        if not sample:
            continue
        total, known, positive, people = sample.excluding(contact_id)
        if people < MIN_CONTACTS or known < 0.8 * total:
            continue
        if mode == "during_call":
            # A later period must also have adequate coverage and a comparable rate.
            recent = recent_samples.get(key)
            r_total, r_known, r_positive, r_people = recent.excluding(contact_id) if recent else (0, 0, 0, 0)
            old_known = known - r_known
            if (r_people < 5 or old_known < 10 or r_known < 0.8 * r_total
                    or abs(r_positive / r_known - (positive - r_positive) / old_known) > 0.20):
                continue
        elif not positive:
            # No dated CRM conversion in this group: a zero rate would be misleading.
            continue
        return {"mode": mode, "percent": round(100 * positive / known), "observed": known,
                "positive": positive, "contacts": people, "basis": label}
    return None


def forecasts(data, now):
    """One pass over linked dossiers. Only future appointments receive predictions."""
    current = now.timestamp()
    groups = defaultdict(list)
    bookings = defaultdict(lambda: ([], []))
    seen = set()
    for appt in data.get("crm_calendly_appointments") or ():
        if not isinstance(appt, dict) or not appt.get("id") or not appt.get("contact_id"):
            continue
        start = _time(appt.get("start_time"))
        identity = (appt["contact_id"], appt.get("invitee_uri") or appt["id"])
        if start is None or identity in seen:
            continue
        seen.add(identity)
        opened = _time(appt.get("calendly_created_at") or appt.get("created_at"))
        cancelled = appt.get("status") in CANCELLED
        closed = start
        if cancelled:
            cancellation = appt.get("cancellation") or {}
            closed = _time(cancellation.get("created_at") if isinstance(cancellation, dict) else None)
        if opened is not None and closed is not None and opened <= min(start, closed):
            times = bookings[appt["contact_id"]]
            times[0].append(opened)
            times[1].append(min(start, closed))
        if cancelled or not is_phone_appointment(appt):
            continue
        groups[appt["contact_id"]].append((start, appt))
    exact = defaultdict(Sample)
    recent_exact = defaultdict(Sample)
    proxy = defaultdict(Sample)
    historical, targets = [], []
    first_label = {}
    result = {}
    for contact in data.get("crm_contacts") or ():
        if not isinstance(contact, dict):
            continue
        cid = contact.get("id")
        if cid not in groups:
            continue
        calls = sorted(groups[cid], key=lambda pair: pair[0])
        events, converted = _events(contact)
        booking_times = tuple(sorted(times) for times in bookings[cid])
        claimed = []
        prior = []
        confirmed_at = {}
        for start, appt in calls:
            answer = appt.get("response_status")
            if answer == "answered" and appt.get("enrollment_outcome") == "confirmed":
                confirmed_at.setdefault(folded(appt.get("name") or appt.get("event_type_uri")), start)
            recorded = _time(appt.get("response_status_updated_at"))
            if answer in {"answered", "no_answer"} and recorded is not None:
                prior.append((max(start, recorded), answer))
            if converted is not None and converted - FOLLOW_UP <= start <= converted and answer != "no_answer":
                claimed.append((start, appt))
        prior.sort()
        # An intervening unreported or simultaneous appointment makes ownership ambiguous.
        credit = None
        if claimed and claimed[-1][1].get("response_status") == "answered":
            if len(claimed) == 1 or claimed[-2][0] != claimed[-1][0]:
                credit = claimed[-1][1]["id"]
        undated_conversion = contact.get("statut") == "Converti" and converted is None
        for start, appt in calls:
            if start < current - LOOKBACK:
                continue
            future = start > current
            answer = appt.get("response_status")
            if future and (answer or contact.get("archived_at") or contact.get("statut") == "Converti"
                           or (converted is not None and converted <= current)):
                continue
            if converted is not None and start > converted:
                continue
            if start > confirmed_at.get(folded(appt.get("name") or appt.get("event_type_uri")), float("inf")):
                continue
            keys = _keys(appt, contact, min(current, start), prior, events, future, booking_times)
            if future:
                targets.append((appt["id"], cid, keys))
                continue
            recorded = appt.get("enrollment_outcome")
            label = None
            if answer == "answered" and recorded in OUTCOMES:
                label = recorded == "confirmed"
                course = keys[-1][0]
                first_label[course] = min(start, first_label.get(course, start))
            elif answer == "no_answer":
                label = False
            historical.append((cid, keys, start, label))
            if start > current - FOLLOW_UP:
                continue
            proxy_label = None
            if answer in {"answered", "no_answer"} and not undated_conversion:
                ambiguous = (converted is not None and converted - FOLLOW_UP <= start <= converted
                             and answer == "answered" and credit is None)
                if not ambiguous:
                    proxy_label = appt["id"] == credit
            for key, _ in keys:
                proxy[key].add(cid, proxy_label)

    validation_people = {(keys[-1][0], cid) for cid, keys, start, _ in historical
                         if start >= current - 14 * DAY}
    for cid, keys, start, label in historical:
        since = first_label.get(keys[-1][0])
        if since is None or start < since:
            continue
        recent = start >= current - 14 * DAY
        if not recent and (keys[-1][0], cid) in validation_people:
            continue
        for key, _ in keys:
            exact[key].add(cid, label)
            if recent:
                recent_exact[key].add(cid, label)
    for aid, cid, keys in targets:
        result[aid] = (_estimate(cid, keys, exact, recent_exact, "during_call")
                       or _estimate(cid, keys, proxy, {}, "seven_days")
                       or {"mode": "insufficient"})
    return result
