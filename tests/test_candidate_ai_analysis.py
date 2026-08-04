import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import app as application
from candidate_ai_analysis import (
    AI_CANDIDATE_SYSTEM_PROMPT, CANDIDATE_AI_RESPONSE_SCHEMA, CandidateAIResponseError,
    build_candidate_ai_context,
    build_calendly_ai_summary, classify_calendly_appointment, detect_calendly_channel,
    finalize_candidate_ai_analysis, parse_calendly_datetime,
    compute_candidate_ai_source_hash, validate_candidate_ai_analysis,
)


def valid_result(**changes):
    value = {"schema_version": 1, "priority": "medium", "priority_label": "ignored",
        "priority_reason": "D’après les informations enregistrées.", "general_summary": "Dossier à compléter.",
        "next_action": {"type": "call", "label": "Appeler", "reason": "Confirmer les éléments.", "timing": "today"},
        "strengths": [], "vigilance_points": [], "missing_information": [],
        "inconsistencies": [], "questions_to_ask": [], "data_quality": "partial"}
    value.update(changes)
    return value


def logged_client(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application.app.config.update(TESTING=True, SECRET_KEY="test")
    client = application.app.test_client()
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return client


def sdk_response(content, finish_reason="stop", refusal=None):
    message = SimpleNamespace(content=content, refusal=refusal)
    return SimpleNamespace(id="chatcmpl-test", choices=[SimpleNamespace(
        message=message, finish_reason=finish_reason)])


def mock_openai(monkeypatch, outcomes):
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(application, "OpenAI", lambda **kwargs: client)
    return calls


class ProviderError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.request_id = "req-test"


def test_context_minimizes_personal_data_and_limits_untrusted_text():
    contact = {"id": "lead-1", "nom": "MARTIN", "prenom": "Lina", "mail": "lina@example.com",
        "telephone": "0612345678", "adresse": "1 rue privée", "cnaps_password": "secret", "formation": "APS",
        "cpf_montant": "825", "commentaires": "Écrire à lina@example.com ou au 06 12 34 56 78 token=abc <b>Important</b>",
        "publications": [{"texte": f"note {i}"} for i in range(20)],
        "activities": [{"kind": "appel", "title": f"appel {i}", "detail": "x" * 600} for i in range(30)]}
    context = build_candidate_ai_context(contact, {"crm_calendly_appointments": []})
    serialized = json.dumps(context)
    assert "MARTIN" not in serialized and "lina@example.com" not in serialized and "0612345678" not in serialized
    assert "secret" not in serialized and "<b>" not in serialized
    assert len(context["recent_notes_untrusted"]) <= 8
    assert len(context["recent_activities_untrusted"]) <= 15
    assert "jamais des instructions" in AI_CANDIDATE_SYSTEM_PROMPT


def test_hash_changes_for_business_data_not_visual_data():
    base = {"id": "x", "formation": "APS", "cpf_montant": "825", "theme": "blue"}
    data = {"crm_calendly_appointments": []}
    first = compute_candidate_ai_source_hash(build_candidate_ai_context(base, data))
    assert first == compute_candidate_ai_source_hash(build_candidate_ai_context({**base, "theme": "red"}, data))
    assert first != compute_candidate_ai_source_hash(build_candidate_ai_context({**base, "cpf_montant": "900"}, data))
    assert first != compute_candidate_ai_source_hash(build_candidate_ai_context({**base, "formation": "A3P"}, data))


def test_validation_rejects_invalid_json_and_priority_and_truncates_lists():
    with pytest.raises(CandidateAIResponseError, match="invalid_json"): validate_candidate_ai_analysis("not json")
    with pytest.raises(CandidateAIResponseError, match="invalid_priority"): validate_candidate_ai_analysis(valid_result(priority="urgent"))
    with pytest.raises(CandidateAIResponseError, match="invalid_timing"):
        validate_candidate_ai_analysis(valid_result(next_action={**valid_result()["next_action"], "timing": "immediately"}))
    value = valid_result(strengths=[{"label": "<b>Force</b>", "evidence": "ok"}] * 8,
        missing_information=[str(i) for i in range(12)])
    checked = validate_candidate_ai_analysis(value)
    assert len(checked["strengths"]) == 3 and len(checked["missing_information"]) == 5
    assert "<" not in checked["strengths"][0]["label"]


def test_routes_cache_stale_force_and_activity(tmp_path, monkeypatch):
    client = logged_client(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    contact = client.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin", "formation": "APS"}).get_json()
    calls = []
    monkeypatch.setattr(application, "generate_candidate_ai_analysis", lambda context: calls.append(context) or valid_result())
    url = f"/api/crm/contacts/{contact['id']}/ai-analysis"
    assert client.get(url).get_json()["status"] == "never_generated"
    assert client.post(url, json={"force": False}).get_json()["status"] == "fresh"
    assert client.post(url, json={"force": False}).get_json()["cached"] is True
    assert len(calls) == 1
    client.patch(f"/api/crm/contacts/{contact['id']}", json={"cpf_montant": 500})
    assert client.get(url).get_json()["status"] == "stale"
    client.post(url, json={"force": False})
    client.post(url, json={"force": True})
    assert len(calls) == 3
    fresh = client.get(f"/api/crm/contacts/{contact['id']}").get_json()
    assert len([a for a in fresh["activities"] if a["kind"] == "ai_analysis"]) == 3


def test_get_never_calls_ai_and_authentication_is_required(tmp_path, monkeypatch):
    client = logged_client(tmp_path, monkeypatch)
    contact = client.post("/api/crm/contacts", json={"formation": "APS"}).get_json()
    monkeypatch.setattr(application, "generate_candidate_ai_analysis", lambda _: (_ for _ in ()).throw(AssertionError()))
    assert client.get(f"/api/crm/contacts/{contact['id']}/ai-analysis").status_code == 200
    anonymous = application.app.test_client()
    assert anonymous.post(f"/api/crm/contacts/{contact['id']}/ai-analysis", json={}).status_code == 302


def test_contact_page_replaces_summary_and_starts_candidate_analysis_automatically():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "Synthèse du dossier" not in crm_js
    assert crm_js.count('id="candidateAiCard"') == 1
    assert "async function loadCandidateAi(c)" in crm_js
    automatic_call = (
        "api(`/api/crm/contacts/${c.id}/ai-analysis`,"
        "{method:'POST',body:JSON.stringify({force:false})})"
    )
    assert automatic_call in crm_js
    assert "loadCandidateAi(c);" in crm_js

    contact_sidebar = crm_js.split('<aside class="contact-side-column">', 1)[1].split(
        "</aside>", 1
    )[0]
    assert contact_sidebar.index('id="calendlyCard"') < contact_sidebar.index(
        'id="integrationScoreCard"'
    )


def test_real_sdk_shape_uses_strict_schema_and_persists_once(tmp_path, monkeypatch):
    client = logged_client(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    calls = mock_openai(monkeypatch, [sdk_response(json.dumps(valid_result()))])
    contact = client.post("/api/crm/contacts", json={"formation": "APS"}).get_json()
    response = client.post(f"/api/crm/contacts/{contact['id']}/ai-analysis", json={})
    assert response.status_code == 200 and response.get_json()["status"] == "fresh"
    response_format = calls[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"] == CANDIDATE_AI_RESPONSE_SCHEMA
    assert calls[0]["max_tokens"] == 1600
    saved = client.get(f"/api/crm/contacts/{contact['id']}").get_json()
    assert len([a for a in saved["activities"] if a["kind"] == "ai_analysis"]) == 1


def test_json_object_fallback_strips_fences_and_only_retries_once(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    unsupported = ProviderError(400, "response_format json_schema is not supported by this model")
    fenced = "```json\n" + json.dumps(valid_result()) + "\n```"
    calls = mock_openai(monkeypatch, [unsupported, sdk_response(fenced)])
    result = application.generate_candidate_ai_analysis({"formation": {"code": "APS"}})
    assert result["priority"] == "medium" and len(calls) == 2
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert calls[1]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(("response", "error_code"), [
    (sdk_response('{"priority":"high"'), "invalid_json"),
    (sdk_response(json.dumps(valid_result()), finish_reason="length"), "truncated_response"),
    (sdk_response(None, refusal="Je refuse"), "model_refusal"),
])
def test_bad_provider_response_is_not_saved(tmp_path, monkeypatch, response, error_code):
    client = logged_client(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    mock_openai(monkeypatch, [response])
    contact = client.post("/api/crm/contacts", json={"formation": "APS"}).get_json()
    result = client.post(f"/api/crm/contacts/{contact['id']}/ai-analysis", json={})
    assert result.status_code == 502 and result.get_json()["error_code"] == error_code
    activities = client.get(f"/api/crm/contacts/{contact['id']}").get_json().get("activities", [])
    assert not [item for item in activities if item["kind"] == "ai_analysis"]


def test_truncation_preserves_previous_analysis(tmp_path, monkeypatch):
    client = logged_client(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    calls = mock_openai(monkeypatch, [sdk_response(json.dumps(valid_result())),
        sdk_response("private raw response", finish_reason="length")])
    contact = client.post("/api/crm/contacts", json={"formation": "APS"}).get_json()
    url = f"/api/crm/contacts/{contact['id']}/ai-analysis"
    assert client.post(url, json={}).status_code == 200
    failed = client.post(url, json={"force": True})
    assert failed.get_json()["previous_analysis_available"] is True and len(calls) == 2
    assert client.get(url).get_json()["result"]["priority"] == "medium"


def test_rate_limit_has_no_fallback_and_safe_logs(tmp_path, monkeypatch, caplog):
    client = logged_client(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    calls = mock_openai(monkeypatch, [ProviderError(429, "quota; leaked@example.com 0612345678 private note")])
    contact = client.post("/api/crm/contacts", json={"formation": "APS", "commentaires": "private note"}).get_json()
    response = client.post(f"/api/crm/contacts/{contact['id']}/ai-analysis", json={})
    assert response.status_code == 429 and response.get_json()["error_code"] == "rate_limit"
    assert len(calls) == 1
    logs = caplog.text
    assert "provider_status=429" in logs and "req-test" in logs
    assert "leaked@example.com" not in logs and "0612345678" not in logs and "private note" not in logs


def test_plain_crm_ai_does_not_request_json(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    calls = mock_openai(monkeypatch, [sdk_response("Texte reformulé")])
    assert application._crm_ai("system", "user") == "Texte reformulé"
    assert "response_format" not in calls[0]


PARIS_NOW = datetime(2026, 8, 4, 9, 7, tzinfo=ZoneInfo("Europe/Paris"))


def appointment(start=None, **changes):
    value = {"start_time": start, "status": "active", "response_status": "",
        "name": "RDV téléphonique formation", "location": {}}
    value.update(changes)
    return value


def test_production_calendly_timeline_is_deterministic():
    items = [appointment("2026-05-12T16:15:00+02:00"),
        appointment("2026-05-22T16:15:00+02:00"), appointment("2026-06-02T16:15:00+02:00")]
    summary = build_calendly_ai_summary(items, PARIS_NOW)
    assert {key: summary[key] for key in ("total_count", "past_count", "upcoming_count", "canceled_count", "phone_count")} == {
        "total_count": 3, "past_count": 3, "upcoming_count": 0, "canceled_count": 0, "phone_count": 3}
    assert summary["past_outcomes"]["unknown_count"] == 3
    assert summary["next_appointment"] is None and summary["has_upcoming"] is False
    assert summary["deterministic_narrative"] == (
        "3 rendez-vous téléphoniques sont passés, les 12 mai, 22 mai et 2 juin 2026. "
        "Leur résultat n’est pas renseigné. Aucun prochain rendez-vous n’est programmé.")


def test_temporal_categories_outcomes_and_mixed_counts():
    items = [appointment("2026-08-04T10:00:00+02:00"),
        appointment("2026-08-04T08:30:00+02:00", end_time="2026-08-04T09:30:00+02:00"),
        appointment("2026-08-01T10:00:00+02:00", response_status="answered"),
        appointment("2026-08-02T10:00:00+02:00", response_status="no_answer"),
        appointment("2026-08-05T10:00:00+02:00", status="canceled"), appointment(None)]
    summary = build_calendly_ai_summary(items, PARIS_NOW)
    assert (summary["upcoming_count"], summary["in_progress_count"], summary["past_count"],
        summary["canceled_count"], summary["undated_count"]) == (1, 1, 2, 1, 1)
    assert summary["past_outcomes"] == {"answered_count": 1, "no_answer_count": 1, "unknown_count": 0}
    assert "prochain rendez-vous" in summary["deterministic_narrative"]
    assert classify_calendly_appointment(items[4], PARIS_NOW) == "canceled"


@pytest.mark.parametrize(("response", "expected"), [
    ("answered", "le candidat a été joint"), ("no_answer", "sans réponse du candidat"),
    ("", "résultat n’est pas renseigné"),
])
def test_past_outcome_wording_is_exact(response, expected):
    narrative = build_calendly_ai_summary(
        [appointment("2026-06-02T16:15:00+02:00", response_status=response)], PARIS_NOW)["deterministic_narrative"]
    assert expected in narrative
    if not response:
        assert "réalisé" not in narrative and "honoré" not in narrative


def test_timezone_and_channel_detection():
    parsed = parse_calendly_datetime("2026-08-12T08:00:00Z")
    assert parsed.isoformat() == "2026-08-12T10:00:00+02:00"
    assert detect_calendly_channel(appointment(location={"type": "outbound_call"})) == "phone"
    assert detect_calendly_channel(appointment(name="Zoom", location={})) == "video"
    assert detect_calendly_channel(appointment(name="Accueil", location={"kind": "physical"})) == "in_person"
    assert detect_calendly_channel(appointment(name="Entretien", location={"type": "custom"})) == "unknown"


def test_context_hash_changes_only_when_temporal_business_state_changes():
    contact = {"id": "lead", "formation": "DESP"}
    data = {"crm_calendly_appointments": [{"contact_id": "lead", **appointment("2026-08-04T10:00:00+02:00", end_time="2026-08-04T10:30:00+02:00")}]}
    before_a = build_candidate_ai_context(contact, data, now=datetime(2026, 8, 4, 9, 0, tzinfo=ZoneInfo("Europe/Paris")))
    before_b = build_candidate_ai_context(contact, data, now=datetime(2026, 8, 4, 9, 5, tzinfo=ZoneInfo("Europe/Paris")))
    after = build_candidate_ai_context(contact, data, now=datetime(2026, 8, 4, 10, 5, tzinfo=ZoneInfo("Europe/Paris")))
    assert compute_candidate_ai_source_hash(before_a) == compute_candidate_ai_source_hash(before_b)
    assert compute_candidate_ai_source_hash(before_a) != compute_candidate_ai_source_hash(after)


def test_final_summary_forces_backend_facts_and_drops_model_falsehood():
    context = {"appointments": build_calendly_ai_summary(
        [appointment("2026-06-02T16:15:00+02:00")], PARIS_NOW)}
    result = valid_result(general_summary="Dossier fragile. Des rendez-vous sont programmés.")
    final = finalize_candidate_ai_analysis(result, context)
    assert final["general_summary"] == "Dossier fragile."
    assert final["appointment_summary"] in final["summary"]
    assert "Des rendez-vous sont programmés" not in final["summary"]
