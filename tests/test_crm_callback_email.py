import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
import os
import shutil
import subprocess

import pytest
from flask import render_template

import app as legacy
import crm_callback_email as emails


@pytest.fixture
def callback(tmp_path, monkeypatch):
    monkeypatch.setattr(legacy, "DATA_FILE", str(tmp_path / "data.json"))
    legacy.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    data = copy.deepcopy(legacy.DEFAULT_DATA)
    data["crm_contacts"] = [{"id": "contact", "prenom": "Camille", "nom": "Exemple",
        "mail": "camille@example.test", "telephone": "0601020304", "activities": [],
        "commentaires": "PRIVATE INTERNAL NOTE"}]
    data["secretariat_demandes"] = [{"id": "request", "type": "autre", "prenom": "Camille",
        "nom": "Camille Exemple", "email": "camille@example.test", "crm_contact_id": "contact",
        "notes": "Je souhaite des informations sur mon dossier.", "callback_status": "pending",
        "callback_status_updated_at": "2026-11-17T10:00:00+01:00", "callback_comment": "PRIVATE TEAM NOTE"}]
    legacy.save_data(data)
    deliveries = []
    monkeypatch.setattr(legacy, "_crm_send_email_html", lambda *args: deliveries.append(args) or True)
    monkeypatch.setattr(legacy, "creer_piste_salesforce", lambda *_: None)
    monkeypatch.setattr(legacy, "_send_secretariat_information_messages", lambda *_: {})
    monkeypatch.setattr(legacy, "_secretariat_refresh_calendly_appointments", lambda *_: None)
    monkeypatch.setattr(legacy, "_ensure_secretariat_quote", lambda *_: None)
    client = staff_client()
    return client, deliveries


def staff_client():
    client = legacy.app.test_client()
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return client


def reply(**changes):
    return {"recipient": "camille@example.test", "subject": "Votre dossier", "body": "Bonjour Camille,\n\nVoici la réponse relue.\n\nBien cordialement,",
            "operation_id": "a" * 32, "revision": "2026-11-17T10:00:00+01:00", **changes}


BASE = "/api/crm/callback-requests/request"


@pytest.mark.parametrize("training", [False, True])
def test_intake_acknowledges_once_without_closing_request(callback, training):
    client, sent = callback
    payload = {"type": "formation" if training else "autre", "prenom": "Camille", "nom": "Camille Exemple",
        "telephone": "0601020304", "email": "camille@example.test", "notes": "Merci de me rappeler.",
        "formation": "APS" if training else "", "prospect_callback": "oui", "call_key": "b" * 32}
    response = client.post("/api/secretariat/demandes", json=payload)
    assert response.status_code == 201
    assert client.post("/api/secretariat/demandes", json=payload).status_code == 200
    assert len(sent) == 1
    address, subject, plain, html = sent[0]
    assert address == "camille@example.test" and subject == emails.ACK_SUBJECT
    assert subject == "Nous avons bien reçu votre demande"
    assert "Je fais suite à notre conversation téléphonique." in plain
    assert "meilleurs délais" in plain and "Le secrétariat" in html
    assert "Le secrétariat – Intégrale Academy" in plain
    assert "Cassandre" not in plain and "Cassandre" not in html
    assert "EMAIL_FOOTER_DETAILS_START" in html and "font-style:italic" in html
    assert 'align="center"' in html and "Faites le" in html
    data = legacy.load_data()
    entry = data["secretariat_demandes"][-1]
    assert entry["callback_status"] == "pending"
    assert entry["callback_email"]["ack"]["status"] == "sent"


@pytest.mark.parametrize("acknowledgement", [False, True])
def test_callback_uses_the_email_models_footer_and_matching_signature(callback, acknowledgement):
    with legacy.app.test_request_context():
        model = render_template("crm_email_wrapper.html", prenom="Camille", formation="VTC", contenu="<p>Réponse</p>")
        html = emails.render_mail("Votre demande", "Bonjour Camille,", acknowledgement=acknowledgement)
        footer = render_template("emails/_crm_email_footer.html").strip()
        cassandre = render_template("emails/_cassandre_signature.html").strip()
        secretariat = render_template("emails/_secretariat_signature.html").strip()
    assert model.count(footer) == html.count(footer) == 1
    assert model.count(cassandre) == 1
    assert "Certification Qualiopi" in footer and "UAI Paris" in footer
    if acknowledgement:
        assert html.count(secretariat) == 1 and "Cassandre" not in html
    else:
        assert html.count(cassandre) == 1 and secretariat not in html


def test_reading_historical_callbacks_never_sends_ack(callback):
    client, sent = callback
    for path in ("/api/crm/callback-requests", "/api/crm/bootstrap?section=demandes-rappel"):
        assert client.get(path).status_code == 200
    assert sent == []


def test_ack_uses_linked_email_and_records_missing_or_failed_delivery(callback, monkeypatch):
    _, sent = callback
    data = legacy.load_data()
    entry = data["secretariat_demandes"][0]
    entry["email"] = ""
    legacy.save_data(data)
    with legacy.app.app_context():
        emails.acknowledge(legacy, entry["id"])
        emails.acknowledge(legacy, entry["id"])
    assert len(sent) == 1 and sent[0][0] == "camille@example.test"
    for address, expected in (("", "missing_email"), ("other@example.test", "failed")):
        data = legacy.load_data()
        entry = {"id": expected, "type": "autre", "email": address}
        data["secretariat_demandes"].append(entry)
        legacy.save_data(data)
        monkeypatch.setattr(legacy, "_crm_send_email_html", lambda *_: False)
        with legacy.app.app_context():
            emails.acknowledge(legacy, entry["id"])
        entry = next(e for e in legacy.load_data()["secretariat_demandes"] if e["id"] == expected)
        assert entry["callback_email"]["ack"]["status"] == expected
        assert legacy._crm_callback_request_status(entry) == "pending"


@pytest.mark.parametrize("instructions", ["", "Confirmer que les documents seront examinés demain."])
def test_generation_uses_request_and_optional_notes_without_side_effects(callback, monkeypatch, instructions):
    client, sent = callback
    before = legacy.load_data()
    calls = []
    monkeypatch.setattr(legacy, "_crm_ai", lambda system, prompt, **kw:
        calls.append((system, prompt)) or json.dumps({"subject": "Votre demande", "body": "Bonjour Camille,\n\nNous revenons vers vous.\n\nBien cordialement,"}))
    response = client.post(BASE + "/generate-reply", json={"instructions": instructions})
    assert response.status_code == 200
    assert response.json["subject"] == "Votre demande" and "Cassandre MENARD" in response.json["html"]
    body = response.json["body"]
    assert body.startswith("Bonjour Camille,\n\n" + emails.REPLY_INTRO)
    assert body.endswith(emails.REPLY_CLOSING)
    assert body.count("Bien cordialement") == 1
    context = json.loads(calls[0][1])
    assert context["elements_de_reponse_equipe"] == instructions
    assert context["appel"]["demande"] == before["secretariat_demandes"][0]["notes"]
    assert "PRIVATE" not in calls[0][1] and "camille@example.test" not in calls[0][1]
    assert "deux ou trois paragraphes utiles" in calls[0][0]
    assert "pas de remplissage" in calls[0][0] and "N’invente ni tarif, date" in calls[0][0]
    assert legacy.load_data() == before and not sent


def test_generated_reply_replaces_inappropriate_greeting_and_signoff(callback, monkeypatch):
    client, sent = callback
    monkeypatch.setattr(legacy, "_crm_ai", lambda *_args, **_kw: json.dumps({
        "subject": "Votre dossier", "body": "Bonjour,\n\nJe vous remercie pour votre message. Votre dossier sera étudié demain.\n\nCordialement."}))
    result = client.post(BASE + "/generate-reply", json={})
    assert result.status_code == 200
    body = result.json["body"]
    assert "remercie pour votre message" not in body
    assert body.count("Bonjour") == 1 and body.count(emails.REPLY_INTRO) == 1
    assert "Votre dossier sera étudié demain." in body
    assert body.endswith(emails.REPLY_CLOSING) and not sent


def test_booking_options_and_matching_preview_and_delivery(callback):
    client, sent = callback
    options = client.get(BASE + "/booking-options")
    assert options.status_code == 200
    vtc = next(c for c in options.json["courses"] if c["code"] == "VTC")
    assert vtc["url"] == "https://calendly.com/integraleacademy/chauffeurvtc"
    payload = reply(booking_formation="VTC")
    preview = client.post(BASE + "/reply-preview", json=payload)
    assert preview.status_code == 200
    assert vtc["url"] in preview.json["html"] and vtc["label"] in preview.json["html"]
    assert "Prendre un rendez-vous téléphonique" in preview.json["html"]
    assert "Je vous invite à réserver un rendez-vous téléphonique avec notre équipe." in preview.json["html"]
    assert "ÉCHANGEONS SUR VOTRE PROJET" not in preview.json["html"]
    assert "Un moment" not in preview.json["html"]
    without = client.post(BASE + "/reply-preview", json=reply())
    assert "calendly.com" not in without.json["html"]
    assert client.post(BASE + "/reply", json=payload).status_code == 200
    assert sent[0][3] == preview.json["html"] and vtc["url"] in sent[0][2]
    assert legacy.load_data()["secretariat_demandes"][0]["callback_email"]["replies"][0]["booking"] == vtc


@pytest.mark.parametrize("closing", [
    "Je vous souhaite une agréable journée.\n\nBien cordialement,",
    "Je vous souhaite une excellente journée.\nBien cordialement,",
    "Bien cordialement,",
])
@pytest.mark.parametrize("separator", ["\n\n", "\n"])
def test_booking_precedes_edited_closing_in_html_and_plain_text(callback, closing, separator):
    client, sent = callback
    body = "Bonjour Camille,\n\nVotre examen est prévu le 17 novembre à 8 h." + separator + closing
    payload = reply(body=body, booking_formation="VTC")
    preview = client.post(BASE + "/reply-preview", json=payload).json["html"]
    assert preview.index("Votre examen") < preview.index("https://calendly.com/") < preview.index(closing.splitlines()[0])
    assert client.post(BASE + "/reply", json=payload).status_code == 200
    plain, html = sent[0][2:4]
    assert html == preview
    assert plain.index("Votre examen") < plain.index("https://calendly.com/") < plain.index(closing.splitlines()[0])
    assert plain.count(closing) == 1
    assert legacy.load_data()["secretariat_demandes"][0]["callback_email"]["replies"][0]["body"] == body


def test_booking_preserves_a_message_without_a_closing(callback):
    client, _ = callback
    body = "Voici les éléments vérifiés.\n\nUne précision supplémentaire."
    html = client.post(BASE + "/reply-preview", json=reply(body=body, booking_formation="VTC")).json["html"]
    assert html.index("Une précision supplémentaire.") < html.index("https://calendly.com/")
    assert "Je vous souhaite" not in html


@pytest.mark.parametrize("linked", [False, True])
def test_history_keeps_exact_sent_emails_and_is_read_only(callback, monkeypatch, linked):
    client, sent = callback
    if not linked:
        data = legacy.load_data()
        data["crm_contacts"] = []
        data["secretariat_demandes"][0].pop("crm_contact_id")
        legacy.save_data(data)
    with legacy.app.app_context():
        emails.acknowledge(legacy, "request")
    assert client.post(BASE + "/reply", json=reply(booking_formation="VTC")).status_code == 200
    before = legacy.load_data()
    monkeypatch.setattr(emails, "render_mail", lambda *_args, **_kw: pytest.fail("History must not re-render a saved mail"))
    messages = client.get(BASE + "/emails").json["messages"]
    assert [item["id"] for item in messages] == ["a" * 32, "ack"]
    assert [item["kind"] for item in messages] == ["reply", "acknowledgement"]
    assert all(item["recipient"] == "camille@example.test" and item["sent_at"] for item in messages)
    assert all("html" not in item and "body" not in item for item in messages)
    for key, delivery in (("ack", sent[0]), ("a" * 32, sent[1])):
        result = client.get(BASE + "/emails/" + key + "/preview")
        assert result.status_code == 200 and result.json == {"html": delivery[3], "original": True}
    row = client.get("/api/crm/callback-requests").json["callback_requests"][0]
    assert row["email_delivery"]["sent_count"] == 2
    assert "html" not in row["email_delivery"]
    assert legacy.load_data() == before and len(sent) == 2


def test_history_recovers_legacy_original_from_contact_then_labels_text_fallback(callback):
    client, sent = callback
    assert client.post(BASE + "/reply", json=reply()).status_code == 200
    data = legacy.load_data()
    delivery = data["secretariat_demandes"][0]["callback_email"]["replies"][0]
    delivery.pop("html")
    activity = next(a for a in data["crm_contacts"][0]["activities"] if a["kind"] == "email")
    # The original activity uses second precision; retain a deterministic historical timestamp.
    delivery["sent_at"] = activity["date"]
    legacy.save_data(data)
    endpoint = BASE + "/emails/" + delivery["id"] + "/preview"
    result = client.get(endpoint)
    assert result.json == {"html": sent[0][3], "original": True}
    data["crm_contacts"][0]["activities"] = []
    legacy.save_data(data)
    before = legacy.load_data()
    result = client.get(endpoint)
    assert result.status_code == 200 and result.json["original"] is False
    assert "Voici la réponse relue." in result.json["html"]
    assert legacy.load_data() == before and len(sent) == 1


def test_history_never_lists_failed_pending_or_uncertain_mail(callback):
    client, sent = callback
    data = legacy.load_data()
    data["secretariat_demandes"][0]["callback_email"] = {
        "ack": {"status": "missing_email"},
        "replies": [{"id": status, "status": status, "body": "private draft"}
                    for status in ("sending", "failed", "unknown")]}
    legacy.save_data(data)
    assert client.get(BASE + "/emails").json == {"messages": []}
    for key in ("ack", "sending", "failed", "unknown", "missing"):
        assert client.get(BASE + "/emails/" + key + "/preview").status_code == 404
    assert not sent


def test_history_requires_internal_access_and_existing_callback(callback, monkeypatch):
    client, _ = callback
    for route in ("/emails", "/emails/ack/preview"):
        assert legacy.app.test_client().get(BASE + route).status_code in (302, 401)
        assert client.get("/api/crm/callback-requests/missing" + route).status_code == 404
    monkeypatch.setattr(legacy, "current_user", lambda: {"email": "external@example.test"})
    for route in ("/emails", "/emails/ack/preview"):
        assert client.get(BASE + route).status_code == 403


@pytest.mark.parametrize("code", ["javascript:alert(1)", "https://example.test", "INCONNUE", ["VTC"]])
def test_invalid_booking_selection_never_sends(callback, code):
    client, sent = callback
    for route in ("/reply-preview", "/reply"):
        assert client.post(BASE + route, json=reply(booking_formation=code)).status_code == 400
    assert not sent


def test_booking_options_require_internal_access_and_existing_callback(callback, monkeypatch):
    client, _ = callback
    assert legacy.app.test_client().get(BASE + "/booking-options").status_code in (302, 401)
    assert client.get("/api/crm/callback-requests/missing/booking-options").status_code == 404
    monkeypatch.setattr(legacy, "current_user", lambda: {"email": "external@example.test"})
    assert client.get(BASE + "/booking-options").status_code == 403


@pytest.mark.parametrize("raw", ["not JSON", '{"subject":"ok","body":""}', '{"subject":"hello\\nBcc: x@y.test","body":"Hi"}', '[]'])
def test_malformed_ai_response_is_not_offered_or_sent(callback, monkeypatch, raw):
    client, sent = callback
    monkeypatch.setattr(legacy, "_crm_ai", lambda *_args, **_kwargs: raw)
    assert client.post(BASE + "/generate-reply", json={}).status_code == 502
    assert not sent


def test_edited_reply_matches_preview_is_archived_and_closes_once(callback):
    client, sent = callback
    payload = reply(body="Bonjour,\n\nTexte corrigé <script>alert(1)</script> & vérifié.")
    stale = legacy.load_data()
    preview = client.post(BASE + "/reply-preview", json=payload)
    assert preview.status_code == 200 and sent == []
    result = client.post(BASE + "/reply", json=payload)
    assert result.status_code == 200
    assert result.json["request"]["status"] == "processed" and result.json["callback_pending_count"] == 0
    assert sent[0][3] == preview.json["html"]
    assert "<script>" not in sent[0][3] and "&lt;script&gt;" in sent[0][3]
    assert "Texte corrigé" in sent[0][2] and "Cassandre MENARD" in sent[0][2]
    assert client.post(BASE + "/reply", json=payload).status_code == 200
    assert len(sent) == 1
    # A concurrent unrelated save must retain delivery evidence and completion.
    legacy.save_data(stale)
    stored = legacy.load_data()
    entry = stored["secretariat_demandes"][0]
    assert entry["callback_email"]["replies"][0]["status"] == "sent"
    assert entry["callback_status"] == "processed"
    activity = next(a for a in stored["crm_contacts"][0]["activities"] if a["kind"] == "email")
    assert activity["preview"] == sent[0][3] and activity["callback_request_id"] == "request"
    assert entry["callback_events"][-1]["kind"] == "email_sent"


@pytest.mark.parametrize("uncertain", [False, True])
def test_delivery_failure_keeps_request_open_and_blocks_uncertain_retry(callback, monkeypatch, uncertain):
    client, _ = callback
    attempts = []
    def send(*_):
        attempts.append(True)
        if uncertain:
            raise TimeoutError("private provider information")
        return False
    monkeypatch.setattr(legacy, "_crm_send_email_html", send)
    response = client.post(BASE + "/reply", json=reply())
    assert response.status_code == 502 and "private" not in response.text
    data = legacy.load_data()
    assert data["secretariat_demandes"][0]["callback_status"] == "pending"
    assert not any(a["kind"] == "email" for a in data["crm_contacts"][0]["activities"])
    retry = client.post(BASE + "/reply", json=reply())
    assert retry.status_code == (409 if uncertain else 502)
    assert len(attempts) == (1 if uncertain else 2)


def test_concurrent_sends_make_one_delivery_and_do_not_hold_crm_lock(callback, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def send(*_):
        calls.append(True)
        entered.set()
        assert release.wait(5)
        return True
    monkeypatch.setattr(legacy, "_crm_send_email_html", send)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: staff_client().post(BASE + "/reply", json=reply()))
        try:
            assert entered.wait(5)
            assert legacy._CRM_RECONCILIATION_LOCK.acquire(timeout=1)
            legacy._CRM_RECONCILIATION_LOCK.release()
            second = staff_client().post(BASE + "/reply", json=reply(operation_id="c" * 32))
            assert second.status_code == 409
        finally:
            release.set()
        assert future.result().status_code == 200
    assert len(calls) == 1


def test_acknowledgement_releases_locks_and_preserves_parallel_processing(callback, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def send(*_):
        entered.set()
        assert release.wait(5)
        return True
    monkeypatch.setattr(legacy, "_crm_send_email_html", send)
    payload = {"type": "autre", "email": "camille@example.test", "notes": "Question", "nom": "Camille"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: staff_client().post("/api/secretariat/demandes", json=payload))
        try:
            assert entered.wait(5)
            for lock in (legacy._SECRETARIAT_DELIVERY_LOCK, legacy._CRM_RECONCILIATION_LOCK):
                assert lock.acquire(timeout=1)
                lock.release()
            request_id = legacy.load_data()["secretariat_demandes"][-1]["id"]
            assert staff_client().patch("/api/crm/callback-requests/" + request_id,
                                        json={"status": "processed"}).status_code == 200
        finally:
            release.set()
        assert future.result().status_code == 201
    data = legacy.load_data()
    entry = data["secretariat_demandes"][-1]
    assert entry["callback_status"] == "processed"
    assert entry["callback_email"]["ack"]["status"] == "sent"
    assert any(a["title"] == "Demande de rappel traitée" for a in data["crm_contacts"][0]["activities"])


def test_smtp_timeout_never_falls_back_to_second_delivery(callback, monkeypatch):
    class SMTP:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def login(self, *_): pass
        def send_message(self, *_): raise TimeoutError("Submission response lost")
    monkeypatch.setattr(legacy.smtplib, "SMTP_SSL", lambda *_args, **_kwargs: SMTP())
    monkeypatch.setattr(legacy, "_crm_send_email_html", legacy.send_email_html)
    monkeypatch.setattr(legacy, "_send_email_brevo", lambda *_args, **_kwargs: pytest.fail("Ambiguous SMTP must not fall back"))
    with legacy.app.app_context():
        assert emails._send(legacy, "person@example.test", "Subject", "Body", "<p>Body</p>") == "unknown"


@pytest.mark.parametrize("changes", [{"recipient": ""}, {"recipient": "a@x.test,b@y.test"},
    {"subject": "one\nBcc: bad@example.test"}, {"body": " "}, {"body": "x" * 12001},
    {"operation_id": "invalid"}])
def test_invalid_email_payload_never_sends(callback, changes):
    client, sent = callback
    assert client.post(BASE + "/reply", json=reply(**changes)).status_code == 400
    assert not sent


def test_stale_or_closed_request_does_not_send(callback):
    client, sent = callback
    assert client.post(BASE + "/reply", json=reply(revision="old")).status_code == 409
    assert client.patch(BASE, json={"status": "processed"}).status_code == 200
    assert client.post(BASE + "/reply", json=reply()).status_code == 409
    assert not sent


@pytest.mark.parametrize("route", ["/generate-reply", "/reply-preview", "/reply"])
def test_reply_endpoints_require_internal_login(callback, monkeypatch, route):
    assert legacy.app.test_client().post(BASE + route, json=reply()).status_code in (302, 401)
    client, sent = callback
    monkeypatch.setattr(legacy, "current_user", lambda: {"email": "external@example.test"})
    assert client.post(BASE + route, json=reply()).status_code == 403
    assert not sent


def test_email_preservation_does_not_revert_a_newer_callback_status():
    current = {"id": "request", "type": "autre", "callback_status": "pending",
        "callback_status_updated_at": "2026-11-17T10:00:00+01:00",
        "callback_email": {"updated_at": "2026-11-17T10:02:00+01:00", "ack": {"status": "sent"}}}
    outgoing = {"id": "request", "type": "autre", "callback_status": "processed",
        "callback_status_updated_at": "2026-11-17T10:03:00+01:00"}
    old_receipt = {"id": "receipt", "callback_request_id": "request", "callback_status": "pending", "kind": "demande_rappel"}
    new_receipt = {**old_receipt, "callback_status": "processed"}
    handled = {"id": "handled", "callback_request_id": "request", "kind": "demande_rappel"}
    mail = {"id": "mail", "callback_request_id": "request", "kind": "email"}
    data = {"secretariat_demandes": [outgoing], "crm_contacts": [{"id": "c", "activities": [handled, new_receipt]}]}
    persisted = {"secretariat_demandes": [current], "crm_contacts": [{"id": "c", "activities": [mail, old_receipt]}]}
    legacy._preserve_newer_callback_lifecycle(data, persisted)
    assert outgoing["callback_status"] == "processed" and outgoing["callback_email"] == current["callback_email"]
    assert {a["id"] for a in data["crm_contacts"][0]["activities"]} == {"mail", "receipt", "handled"}
    assert next(a for a in data["crm_contacts"][0]["activities"] if a["id"] == "receipt")["callback_status"] == "processed"


def test_history_dom_lazy_loading_selection_race_and_safe_preview():
    if not shutil.which("node") or subprocess.run(["node", "-e", "require('jsdom')"], capture_output=True).returncode:
        pytest.skip("Optional DOM runner requires node and jsdom")
    source = (Path(__file__).parents[1] / "static/crm.js").read_text()
    functions = "\n".join([
        source[source.index("function callbackRequestActions("):source.index("const callbackReplyDrafts=")],
        source[source.index("function modal("):source.index("\nfunction ", source.index("function modal(") + 1)],
    ])
    script = r"""
const {JSDOM}=require('jsdom'), assert=require('node:assert/strict');
const source=JSON.parse(require('fs').readFileSync(0,'utf8'));
const dom=new JSDOM('<div id="modalRoot"></div>',{url:'https://crm.test',runScripts:'outside-only'}),w=dom.window;
const calls=[],pending={};let failList=false;
const messages=[{id:'new',kind:'reply',subject:'Réponse <img src=x onerror=alert(1)>',recipient:'camille@example.test',sent_at:'2026-11-17',actor:'Équipe'},
 {id:'ack',kind:'acknowledgement',subject:'Nous avons bien reçu votre demande',recipient:'camille@example.test',sent_at:'2026-11-16'}];
w.api=async url=>{calls.push(url);if(url.endsWith('/emails')){if(failList)throw Error('Chargement indisponible');return {messages}}
 const id=url.split('/').at(-2);return new Promise(resolve=>pending[id]=resolve)};
w.eval(`const esc=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const fmt=value=>value;function stopActiveVoiceDictation(){}
${source}
window.openHistory=callbackEmailHistoryModal;window.actions=callbackRequestActions;`);
const $=id=>w.document.getElementById(id),buttons=()=>w.document.querySelectorAll('[data-email-index]');
const until=async fn=>{for(let i=0;i<100&&!fn();i++)await new Promise(r=>setTimeout(r,5));assert.ok(fn())};
const row={id:'request',display_name:'Camille',crm_contact_id:'contact',email_delivery:{sent_count:2},status:'processed'};
(async()=>{
 assert.ok(w.actions(row).includes('Historique des mails (2)'));assert.equal(calls.length,0);
 const opening=w.openHistory(row);await until(()=>pending.new);
 assert.equal(buttons().length,2);assert.equal(w.document.querySelector('img'),null);
 assert.equal(w.document.querySelector('a').getAttribute('href'),'/crm/contacts?fiche=contact');
 buttons()[1].click();await until(()=>pending.ack);
 pending.ack({html:'<p>Accusé archivé</p>',original:true});await until(()=>$('callbackHistoryFrame').srcdoc.includes('Accusé archivé'));
 pending.new({html:'<p>Réponse plus récente</p>',original:true});await opening;
 assert.ok($('callbackHistoryFrame').srcdoc.includes('Accusé archivé'));
 assert.equal(buttons()[1].getAttribute('aria-pressed'),'true');
 assert.equal($('callbackHistoryFrame').getAttribute('sandbox'),'');
 buttons()[0].click();await until(()=>calls.length===4);
 pending.new({html:'<p>Texte ancien</p>',original:false});await until(()=>$('callbackHistoryFeedback').textContent.includes('reconstituée'));
 $('callbackHistoryClose').click();assert.equal($('callbackHistoryFrame'),null);
 failList=true;await w.openHistory(row);assert.ok($('callbackHistoryRetry'));
 failList=false;$('callbackHistoryRetry').click();await until(()=>buttons().length===2);
 $('callbackHistoryClose').click();pending.new({html:'<p>Réponse tardive</p>',original:true});
 await new Promise(r=>setTimeout(r,10));assert.equal($('callbackHistoryFrame'),null);
 dom.window.close();
})().catch(e=>{console.error(e);process.exitCode=1;dom.window.close()});
"""
    result = subprocess.run(["node", "-e", script], input=json.dumps(functions), text=True,
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_reply_dom_dictation_generation_preview_retry_and_cleanup():
    """Run actual UI functions with simulated speech and mail transport, offline."""
    if not shutil.which("node") or subprocess.run(["node", "-e", "require('jsdom')"], capture_output=True).returncode:
        pytest.skip("Optional DOM runner requires node and jsdom")
    source = (Path(__file__).parents[1] / "static/crm.js").read_text()
    functions = "\n".join([
        source[source.index("function callbackRequestBrief("):source.index("function callbackRequestsTable(")],
        source[source.index("function modal("):source.index("\nfunction ", source.index("function modal(") + 1)],
        source[source.index("function formatVoiceDictation("):source.index("const CRM_CALL_DRAFT_PREFIX=")],
    ])
    script = r"""
const {JSDOM}=require('jsdom'), assert=require('node:assert/strict');
const source=JSON.parse(require('fs').readFileSync(0,'utf8'));
const dom=new JSDOM('<div id="modalRoot"></div>',{url:'https://crm.test',runScripts:'outside-only'}),w=dom.window;
const calls=[],notices=[];let failSend=true,recognizer;
w.confirm=()=>true;
w.SpeechRecognition=class {
 start(){recognizer=this;this.onstart?.()}
 stop(){setTimeout(()=>{const r=[{transcript:'Documents vérifiés point'}];r.isFinal=true;this.onresult({resultIndex:0,results:[r]});this.onend?.()},5)}
 abort(){this.aborted=true;this.onend?.()}
};
w.api=async (url,options)=>{const data=JSON.parse(options?.body||'{}');calls.push({url,data});
 if(url.endsWith('/reformuler'))return {texte:data.texte};
 if(url.endsWith('/booking-options'))return {courses:[{code:'VTC',label:'Chauffeur VTC'}],selected:'VTC'};
 if(url.endsWith('/generate-reply'))return {subject:'Votre demande',body:'Bonjour,\n\nUne réponse complète.'};
 if(url.endsWith('/reply-preview'))return {html:'<html><body>'+data.body+' Cassandre MENARD</body></html>'};
 if(failSend)throw Error('Envoi échoué, texte conservé');
 return {request:{id:'request',status:'processed'},callback_pending_count:0};
};
w.toast=(...args)=>notices.push(args);
w.eval(`const C={};let callbackRequests=[{id:'request'}],callbackRequestPendingCount=1;
const esc=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const fmt=value=>value;function updateCallbackRequestNavCount(){}function drawCallbackRequests(){}
${source}
window.openReply=callbackReplyModal;`);
const row={id:'request',display_name:'Camille Exemple',notes:'Question concernant mon dossier',email:'camille@example.test',status:'pending',revision:'rev'};
const $=id=>w.document.getElementById(id);
const until=async fn=>{for(let i=0;i<100&&!fn();i++)await new Promise(r=>setTimeout(r,5));assert.ok(fn())};
(async()=>{
 w.openReply(row);$('dictateCallbackReply').click();$('callbackReplyGenerate').click();
 await until(()=>$('callbackReplyBody').value.includes('réponse complète'));
 assert.equal(calls.find(c=>c.url.endsWith('/generate-reply')).data.instructions,'Documents vérifiés.');assert.equal(calls.length,2);
 $('callbackReplyBookingToggle').click();await until(()=>$('callbackReplyBookingFields').hidden===false);
 assert.equal($('callbackReplyBookingCourse').value,'VTC');
 $('callbackReplyBody').value='Bonjour,\n\nRéponse corrigée.';$('callbackReplyBody').dispatchEvent(new w.Event('input'));
 $('callbackReplyClose').click();assert.equal($('callbackReplyBody'),null);
 w.openReply(row);assert.equal($('callbackReplyBody').value,'Bonjour,\n\nRéponse corrigée.');
 $('callbackReplyPreview').click();await until(()=>$('callbackReplyFrame').hidden===false);
 assert.ok($('callbackReplyFrame').srcdoc.includes('Réponse corrigée.'));
 assert.ok($('callbackReplyFrame').hasAttribute('sandbox'));
 assert.equal($('callbackReplyFrame').getAttribute('sandbox'),'allow-same-origin');
 assert.equal(calls.at(-1).data.booking_formation,'VTC');
 $('callbackReplyBookingToggle').click();assert.equal($('callbackReplyFrame').hidden,true);
 assert.equal($('callbackReplyBookingFields').hidden,true);
 $('callbackReplyBookingToggle').click();await until(()=>$('callbackReplyBookingFields').hidden===false);
 $('callbackReplySend').click();$('callbackReplySend').click();
 await until(()=>$('callbackReplyFeedback').textContent.includes('Envoi échoué'));
 assert.equal(calls.filter(c=>c.url.endsWith('/reply')).length,1);
 assert.equal(calls.at(-1).data.booking_formation,'VTC');
 assert.equal($('callbackReplyBody').value,'Bonjour,\n\nRéponse corrigée.');
 const key=calls.at(-1).data.operation_id;failSend=false;$('callbackReplySend').click();
 await until(()=>!$('callbackReplyBody'));
 assert.equal(calls.at(-1).data.operation_id,key);assert.equal(notices.at(-1)[0],'Réponse envoyée. La demande est maintenant traitée.');
 w.openReply({...row,email:''});assert.equal($('callbackReplySend').disabled,true);
 $('dictateCallbackReply').click();$('callbackReplyClose').click();assert.equal(recognizer.aborted,true);
 dom.window.close();
})().catch(e=>{console.error(e);process.exitCode=1;dom.window.close()});
"""
    result = subprocess.run(["node", "-e", script], input=json.dumps(functions), text=True,
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("width", [1440, 390])
def test_callback_reply_browser_dictation_preview_edit_and_send(callback, monkeypatch, width):
    playwright = pytest.importorskip("playwright.sync_api")
    client, sent = callback
    prompts = []
    def generate(_system, prompt, **_):
        prompts.append(json.loads(prompt))
        return json.dumps({"subject": "Votre demande de documents", "body":
            "Bonjour Camille,\n\nNous vous confirmons que votre demande a bien été transmise à notre équipe.\n\nNous reviendrons vers vous dès que les documents auront été vérifiés.\n\nBien cordialement,"})
    monkeypatch.setattr(legacy, "_crm_ai", generate)
    with playwright.sync_playwright() as engine:
        try:
            browser = engine.chromium.launch(executable_path=os.getenv("CHROMIUM_EXECUTABLE") or None)
        except playwright.Error as exc:
            pytest.skip(f"Chromium indisponible : {exc}")
        try:
            page = browser.new_page(viewport={"width": width, "height": 980}, locale="fr-FR")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            def serve(route):
                req, url = route.request, urlsplit(route.request.url)
                if url.netloc != "callback.test":
                    return route.abort()
                response = client.open(url.path, query_string=url.query, method=req.method,
                    json=req.post_data_json if req.method in {"POST", "PATCH"} else None)
                route.fulfill(status=response.status_code, content_type=response.content_type, body=response.data)
            page.route("**/*", serve)
            page.add_init_script("""
                window.SpeechRecognition=class {
                    start(){this.onstart?.();const r=[{transcript:'Confirmer la vérification des documents point'}];r.isFinal=true;this.onresult({resultIndex:0,results:[r]})}
                    stop(){setTimeout(()=>this.onend?.(),20)}
                    abort(){this.onend?.()}
                };
            """)
            page.goto("https://callback.test/crm/demandes-rappel")
            page.locator(".callback-reply-open").first.click()
            page.locator("#dictateCallbackReply").click()
            page.locator("#callbackReplyGenerate").click()
            page.wait_for_function("document.querySelector('#callbackReplyBody').value.includes('Bonjour Camille')")
            assert prompts[0]["elements_de_reponse_equipe"] == "Confirmer la vérification des documents."
            assert sent == []
            # The generated message is editable and preserved after closing/reopening.
            page.locator("#callbackReplyBody").fill("Bonjour Camille,\n\nVersion relue et corrigée.\n\nBien cordialement,")
            page.locator("#callbackReplyClose").click()
            page.locator(".callback-reply-open").first.click()
            assert "Version relue" in page.locator("#callbackReplyBody").input_value()
            dialog = page.locator(".callback-reply-modal")
            assert dialog.evaluate("e=>e.scrollWidth<=e.clientWidth+1")
            if os.getenv("CALLBACK_SCREENSHOT_DIR"):
                target = Path(os.environ["CALLBACK_SCREENSHOT_DIR"])
                target.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(target / f"callback-editor-{width}.png"))
            page.locator("#callbackReplyPreview").click()
            page.locator("#callbackReplyFrame").wait_for(state="visible")
            preview = page.locator("#callbackReplyFrame").get_attribute("srcdoc")
            assert "Cassandre MENARD" in preview and "Version relue" in preview
            if os.getenv("CALLBACK_SCREENSHOT_DIR"):
                page.screenshot(path=str(target / f"callback-preview-{width}.png"))
            page.locator("#callbackReplySend").click()
            page.locator(".callback-reply-modal").wait_for(state="detached")
            assert len(sent) == 1 and sent[0][3] == preview
            assert legacy.load_data()["secretariat_demandes"][0]["callback_status"] == "processed"
            assert page.locator("#callbackRequestPendingStat").inner_text() == "0"
            assert errors == []
        finally:
            browser.close()
