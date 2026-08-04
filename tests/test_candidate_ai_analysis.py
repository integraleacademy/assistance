import json

import app as application
from candidate_ai_analysis import (
    AI_CANDIDATE_SYSTEM_PROMPT, build_candidate_ai_context,
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
    import pytest
    with pytest.raises(ValueError): validate_candidate_ai_analysis("not json")
    with pytest.raises(ValueError): validate_candidate_ai_analysis(valid_result(priority="urgent"))
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
