import json
from types import SimpleNamespace

import pytest

import app as application
from candidate_ai_analysis import (
    AI_CANDIDATE_SYSTEM_PROMPT, CANDIDATE_AI_RESPONSE_SCHEMA, CandidateAIResponseError,
    build_candidate_ai_context,
    compute_candidate_ai_source_hash, validate_candidate_ai_analysis,
)


def valid_result(**changes):
    value = {"schema_version": 1, "priority": "medium", "priority_label": "ignored",
        "priority_reason": "D’après les informations enregistrées.", "summary": "Dossier à compléter.",
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
