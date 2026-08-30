from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch
import gzip
import json
import subprocess
import threading
import time

import pytest

import app as application


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    test_client = application.app.test_client()
    with test_client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"
    return test_client


def test_data_file_is_decoded_once_and_mutable_callers_are_isolated(
        tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application._DATA_CACHE_PAYLOAD = None
    application._DATA_CACHE_SIGNATURE = None
    application.save_data({**application.DEFAULT_DATA, "crm_contacts": [{"id": "one"}]})
    application._DATA_CACHE_PAYLOAD = None
    application._DATA_CACHE_SIGNATURE = None
    calls = 0
    real_load = json.load

    def counted_load(stream, *args, **kwargs):
        nonlocal calls
        calls += 1
        return real_load(stream, *args, **kwargs)

    monkeypatch.setattr(application.json, "load", counted_load)
    first = application.load_data()
    first["crm_contacts"][0]["id"] = "mutated"
    second = application.load_data()

    assert calls == 1
    assert second["crm_contacts"][0]["id"] == "one"


def test_contact_detail_read_does_not_wait_for_crm_write_lock(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    locked = threading.Event()
    release = threading.Event()

    def hold_write_lock():
        with application._CRM_RECONCILIATION_LOCK:
            locked.set()
            release.wait(2)

    worker = threading.Thread(target=hold_write_lock)
    worker.start()
    assert locked.wait(1)
    started = time.perf_counter()
    try:
        response = c.get(f"/api/crm/contacts/{contact['id']}")
    finally:
        release.set()
        worker.join(2)

    assert response.status_code == 200
    assert time.perf_counter() - started < 0.5


def test_large_crm_json_responses_are_gzipped_for_mobile_clients(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    data = application.load_data()
    data["crm_contacts"] = [
        {
            "id": f"contact-{index}", "prenom": "Lina", "nom": "MARTIN",
            "statut": "Nouveaux", "formation": "APS", "commentaires": "x" * 200,
            "activities": [], "relances": [],
        }
        for index in range(20)
    ]
    application.save_data(data)

    response = c.get(
        "/api/crm/bootstrap?section=pistes",
        headers={"Accept-Encoding": "gzip"},
    )

    assert response.status_code == 200
    assert response.headers["Content-Encoding"] == "gzip"
    decoded = json.loads(gzip.decompress(response.data))
    assert len(decoded["contacts"]) == 20


def test_crm_is_private(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application.app.config.update(TESTING=True)
    response = application.app.test_client().get("/CRM")
    assert response.status_code == 302
    assert "/login" in response.location


def test_versioned_static_assets_are_immutable_and_navigation_is_canonical():
    response = application.app.test_client().get("/static/crm.js?v=test-version")
    template = open(
        application.app.root_path + "/templates/crm.html", encoding="utf-8"
    ).read()

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert 'href="/CRM' not in template
    assert "iaconnectcrm.png',v=asset_version" in template


def test_admin_can_read_live_brevo_sms_credits(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setenv("BREVO_API_KEY", "test-key")
    monkeypatch.setattr(application, "send_email_html", lambda *args: True)
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {"plan": [{"type": "sms", "credits": 125.5}]},
    )
    with patch.object(application.requests, "get", return_value=response) as get:
        result = c.get("/api/crm/brevo/sms-credits")

    assert result.status_code == 200
    assert result.get_json() == {"credits": 125.5}
    get.assert_called_once_with(
        "https://api.brevo.com/v3/account",
        headers={"accept": "application/json", "api-key": "test-key"},
        timeout=10,
    )


def test_low_brevo_sms_balance_alerts_both_recipients_only_once(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setattr(application, "_brevo_sms_credits", lambda: 220.5)
    deliveries = []
    monkeypatch.setattr(
        application,
        "send_email_html",
        lambda *args: deliveries.append(args) or True,
    )

    assert c.get("/api/crm/brevo/sms-credits").status_code == 200
    assert c.get("/api/crm/brevo/sms-credits").status_code == 200

    assert len(deliveries) == 1
    recipients, subject, plain_text, html_body = deliveries[0]
    assert recipients == (
        "clement@integraleacademy.com",
        "cassandre@integraleacademy.com",
    )
    assert "moins de 50 SMS" in subject
    assert "49 SMS restants" in plain_text
    assert "49 SMS restants" in html_body


def test_brevo_sms_balance_alert_rearms_after_top_up(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    balances = iter((220.5, 225, 220.5))
    monkeypatch.setattr(application, "_brevo_sms_credits", lambda: next(balances))
    deliveries = []
    monkeypatch.setattr(
        application,
        "send_email_html",
        lambda *args: deliveries.append(args) or True,
    )

    for _ in range(3):
        assert c.get("/api/crm/brevo/sms-credits").status_code == 200

    assert len(deliveries) == 2


def test_failed_brevo_sms_balance_alert_is_retried(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setattr(application, "_brevo_sms_credits", lambda: 0)
    attempts = []
    monkeypatch.setattr(
        application,
        "send_email_html",
        lambda *args: attempts.append(args) or len(attempts) > 1,
    )

    assert c.get("/api/crm/brevo/sms-credits").status_code == 200
    assert c.get("/api/crm/brevo/sms-credits").status_code == 200

    assert len(attempts) == 2


def test_team_user_can_read_brevo_sms_credits(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    with c.session_transaction() as session:
        session["user_email"] = "elsa@integraleacademy.com"
    monkeypatch.setattr(application, "_brevo_sms_credits", lambda: 500)

    response = c.get("/api/crm/brevo/sms-credits")

    assert response.status_code == 200
    assert response.get_json() == {"credits": 500}


def test_administration_menu_displays_brevo_sms_balance():
    template = open(application.app.root_path + "/templates/crm.html", encoding="utf-8").read()
    crm_js = open(application.app.root_path + "/static/crm.js", encoding="utf-8").read()

    assert 'id="brevoSmsCredits"' in template
    assert "api('/api/crm/brevo/sms-credits')" in crm_js
    assert "if(opening)loadBrevoSmsCredits()" in crm_js
    assert "Estimation calculée sur une consommation moyenne de 4,5 crédits par SMS." in template
    assert "const brevoCreditsToSms=" in crm_js
    assert "Math.floor(numericCredits/4.5)" in crm_js
    assert "Solde Brevo : ${formattedCredits}" in crm_js
    assert 'id="developmentSupportBtn"' in template
    assert "function developmentSupportModal()" in crm_js
    assert "api('/api/crm/development-support'" in crm_js
    assert 'name="platform"' in crm_js
    assert 'name="page_url"' in crm_js
    assert 'name="actions"' in crm_js
    assert 'name="attachment" type="file"' in crm_js
    assert 'body:new FormData(form)' in crm_js
    assert "headers:{}" in crm_js
    assert "attachment_uploaded" in crm_js
    assert "20260825-development-support-inline-images-2" in template


def test_team_user_can_open_development_support(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    with c.session_transaction() as session:
        session["user_email"] = "elsa@integraleacademy.com"

    response = c.post("/api/crm/development-support", json={
        "platform": "Autre",
        "page_url": "javascript:alert(1)",
        "actions": "Trop court",
    })

    assert response.status_code == 400
    assert "plateforme" in response.get_json()["error"].lower()


def test_development_support_validates_platform_url_and_description(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)

    response = c.post("/api/crm/development-support", json={
        "platform": "Autre",
        "page_url": "javascript:alert(1)",
        "actions": "Trop court",
    })

    assert response.status_code == 400
    assert "plateforme" in response.get_json()["error"].lower()


def test_development_support_rewrites_and_creates_notion_page(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setenv("NOTION_API_TOKEN", "notion-test")
    monkeypatch.setattr(
        application, "_crm_ai",
        lambda system, prompt, max_tokens=500: (
            "Objectif : faciliter la qualification.\n"
            "Modifications demandées : ajouter le contrôle.\n"
            "Critères observables : le bouton est visible."
        ),
    )
    calls = {}

    def fake_post(url, **kwargs):
        calls["url"] = url
        calls.update(kwargs)
        return SimpleNamespace(
            status_code=201,
            json=lambda: {
                "id": "new-page",
                "url": "https://www.notion.so/new-page",
            },
        )

    monkeypatch.setattr(application.requests, "post", fake_post)
    response = c.post("/api/crm/development-support", json={
        "platform": "Gestion stagiaires",
        "page_url": "https://example.com/stagiaires/42",
        "actions": "Ajouter un bouton de validation et afficher un message de confirmation.",
    })

    assert response.status_code == 201
    assert response.get_json()["url"] == "https://www.notion.so/new-page"
    assert calls["url"] == "https://api.notion.com/v1/pages"
    assert calls["headers"]["Authorization"] == "Bearer notion-test"
    notion = calls["json"]
    assert notion["parent"]["data_source_id"] == application.CRM_NOTION_DATA_SOURCE_ID
    properties = notion["properties"]
    assert properties["Domaine"]["select"]["name"] == "Développement web"
    assert properties["Plateforme"]["select"]["name"] == "Gestion stagiaires"
    assert properties["Statut"]["select"]["name"] == "À traiter"
    assert properties["Type"]["select"]["name"] == "À faire"
    children_text = json.dumps(notion["children"], ensure_ascii=False)
    assert "faciliter la qualification" in children_text
    assert "Ajouter un bouton de validation" in children_text
    assert "https://example.com/stagiaires/42" in children_text
    assert "Clément VAILLANT" in children_text


def test_development_support_uploads_attachment_to_notion(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setenv("NOTION_API_TOKEN", "notion-test")
    monkeypatch.setattr(application, "_crm_ai", lambda *args, **kwargs: "Demande reformulée")
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url == "https://api.notion.com/v1/file_uploads":
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"id": "upload-123", "status": "pending"},
            )
        if url == "https://api.notion.com/v1/file_uploads/upload-123/send":
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"id": "upload-123", "status": "uploaded"},
            )
        if url == "https://api.notion.com/v1/pages":
            return SimpleNamespace(
                status_code=201,
                json=lambda: {"id": "new-page", "url": "https://www.notion.so/new-page"},
            )
        raise AssertionError(f"URL Notion inattendue : {url}")

    monkeypatch.setattr(application.requests, "post", fake_post)
    response = c.post(
        "/api/crm/development-support",
        data={
            "platform": "CRM",
            "page_url": "https://example.com/crm/pistes",
            "actions": "Ajouter la capture à la demande de développement envoyée dans Notion.",
            "attachment": (BytesIO(b"fake-png-content"), "capture écran.png"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 201
    assert response.get_json()["attachment_uploaded"] is True
    assert [url for url, _ in calls] == [
        "https://api.notion.com/v1/file_uploads",
        "https://api.notion.com/v1/file_uploads/upload-123/send",
        "https://api.notion.com/v1/pages",
    ]
    create_upload = calls[0][1]
    assert create_upload["json"]["mode"] == "single_part"
    assert create_upload["json"]["filename"] == "capture_ecran.png"
    assert create_upload["json"]["content_type"] == "image/png"
    send_upload = calls[1][1]
    assert "Content-Type" not in send_upload["headers"]
    assert send_upload["files"]["file"] == (
        "capture_ecran.png", b"fake-png-content", "image/png")
    notion = calls[2][1]["json"]
    assert application.CRM_NOTION_ATTACHMENT_PROPERTY not in notion["properties"]
    file_blocks = [child for child in notion["children"] if child["type"] == "file"]
    assert file_blocks == []
    image_blocks = [child for child in notion["children"] if child["type"] == "image"]
    assert image_blocks[0]["image"]["type"] == "file_upload"
    assert image_blocks[0]["image"]["file_upload"]["id"] == "upload-123"


def test_development_support_keeps_documents_as_notion_files():
    with application.app.test_request_context("/"):
        application.session["user_email"] = "clement@integraleacademy.com"
        notion = application._crm_development_support_page(
            "CRM",
            "https://example.com/crm/pistes",
            "Ajouter le document à la demande de développement.",
            "Demande reformulée",
            attachment_upload_id="upload-pdf",
            attachment_filename="cahier-des-charges.pdf",
            attachment_content_type="application/pdf",
        )

    attached_file = notion["properties"][application.CRM_NOTION_ATTACHMENT_PROPERTY]["files"][0]
    assert attached_file["name"] == "cahier-des-charges.pdf"
    assert attached_file["file_upload"]["id"] == "upload-pdf"
    file_blocks = [child for child in notion["children"] if child["type"] == "file"]
    assert file_blocks[0]["file"]["file_upload"]["id"] == "upload-pdf"
    assert not [child for child in notion["children"] if child["type"] == "image"]


def test_development_support_rejects_unsupported_attachment(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        application, "_crm_ai",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("AI should not run")),
    )

    response = c.post(
        "/api/crm/development-support",
        data={
            "platform": "CRM",
            "page_url": "https://example.com/crm/pistes",
            "actions": "Ajouter une pièce jointe à cette demande de développement.",
            "attachment": (BytesIO(b"binary"), "programme.exe"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "format" in response.get_json()["error"].lower()


def test_development_support_rejects_oversized_attachment(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setattr(application, "CRM_DEVELOPMENT_SUPPORT_MAX_ATTACHMENT_BYTES", 5)

    response = c.post(
        "/api/crm/development-support",
        data={
            "platform": "CRM",
            "page_url": "https://example.com/crm/pistes",
            "actions": "Ajouter une pièce jointe à cette demande de développement.",
            "attachment": (BytesIO(b"123456"), "capture.png"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 413
    assert "20 Mo" in response.get_json()["error"]


def test_development_support_does_not_create_page_when_attachment_upload_fails(
        tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setenv("NOTION_API_TOKEN", "notion-test")
    monkeypatch.setattr(application, "_crm_ai", lambda *args, **kwargs: "Demande reformulée")
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=503, json=lambda: {})

    monkeypatch.setattr(application.requests, "post", fake_post)
    response = c.post(
        "/api/crm/development-support",
        data={
            "platform": "CRM",
            "page_url": "https://example.com/crm/pistes",
            "actions": "Ajouter une pièce jointe à cette demande de développement.",
            "attachment": (BytesIO(b"fake-png-content"), "capture.png"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 503
    assert calls == ["https://api.notion.com/v1/file_uploads"]
    assert "pièce jointe" in response.get_json()["error"].lower()


def test_development_support_creates_notion_page_when_ai_is_unavailable(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setenv("NOTION_API_TOKEN", "notion-test")
    monkeypatch.setattr(
        application, "_crm_ai",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("service indisponible")),
    )
    calls = {}

    def fake_post(url, **kwargs):
        calls.update(kwargs)
        return SimpleNamespace(
            status_code=201,
            json=lambda: {"url": "https://www.notion.so/fallback-page"},
        )

    monkeypatch.setattr(application.requests, "post", fake_post)
    actions = "Ajouter un bouton de validation et conserver toute la demande originale."
    response = c.post("/api/crm/development-support", json={
        "platform": "CRM",
        "page_url": "https://example.com/crm/pistes",
        "actions": actions,
    })

    assert response.status_code == 201
    assert response.get_json()["ai_rewritten"] is False
    children_text = json.dumps(calls["json"]["children"], ensure_ascii=False)
    assert "Demande à reformuler" in children_text
    assert actions in children_text


def test_development_support_reports_missing_notion_connection(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.delenv("NOTION_API_TOKEN", raising=False)
    monkeypatch.setattr(application, "_crm_ai", lambda *args, **kwargs: "Demande reformulée")

    response = c.post("/api/crm/development-support", json={
        "platform": "CRM",
        "page_url": "https://example.com/crm/pistes",
        "actions": "Ajouter un bouton clairement décrit dans la liste.",
    })

    assert response.status_code == 503
    assert "Notion" in response.get_json()["error"]


def test_global_search_closes_when_clicking_outside():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "document.addEventListener('click'" in crm_js
    assert "if(!searchBox.contains(e.target))globalResults.classList.remove('open')" in crm_js


def test_sidebar_lead_count_only_includes_new_leads():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "const count=contacts.filter(c=>c.statut==='Nouveaux').length" in crm_js
    assert "contacts.filter(isActiveLead).length;if(leadCount)leadCount.textContent=count" not in crm_js


def test_dashboard_reports_meta_and_builds_origins_from_live_contacts():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "function dashboardOrigin(contact)" in crm_js
    assert "return'META'" in crm_js
    assert "primaryOrigins=dashboardOriginGroups(current)" in crm_js
    assert "secondaryOrigins=dashboardOriginGroups(current,true)" in crm_js
    assert "function dashboardOrigin(contact){return canonicalCrmOrigin(contact)}" in crm_js
    assert "campaign_name" in crm_js
    assert "ad_name" in crm_js
    assert "sourceCounts=['Google','Site internet','Simulateur VAE'" not in crm_js


def test_dashboard_compares_periods_and_remains_compact_on_mobile():
    root = application.app.root_path
    with open(root + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()
    with open(root + "/static/crm.css", encoding="utf-8") as source:
        crm_css = source.read()

    assert "today:'Aujourd’hui',week:'Semaine',month:'Mois',quarter:'Trimestre',year:'Année'" in crm_js
    assert "lastYear.start.setFullYear" in crm_js
    assert "data-dashboard-shift" in crm_js
    assert "bindDashboard()" in crm_js
    assert "@media(max-width:650px)" in crm_css
    assert ".analytics-kpis{grid-template-columns:1fr 1fr" in crm_css
    assert ".analytics-table-wrap{width:100%;overflow:auto}" in crm_css


def test_dashboard_today_period_uses_local_calendar_day():
    root = application.app.root_path
    crm_js = open(root + "/static/crm.js", encoding="utf-8").read()
    period_code = crm_js[
        crm_js.index("const dashboardPeriods="):
        crm_js.index("const dashboardInRange=")
    ]
    node_script = f"""
let dashboardPeriod='today',dashboardOffset=0;
{period_code}
const assert=(condition,message)=>{{if(!condition)throw new Error(message)}};
const current=dashboardFullRange('today',0);
const previous=dashboardFullRange('today',-1);
const nextDay=new Date(current.start);nextDay.setDate(nextDay.getDate()+1);
const nextPreviousDay=new Date(previous.start);nextPreviousDay.setDate(nextPreviousDay.getDate()+1);
assert(current.start.getHours()===0&&current.start.getMinutes()===0,'today starts at local midnight');
assert(current.end.getTime()===nextDay.getTime(),'today ends at the next local midnight');
assert(previous.end.getTime()===nextPreviousDay.getTime(),'past days cover the whole local day');
assert(current.start.getDate()!==previous.start.getDate()||current.start.getMonth()!==previous.start.getMonth(),'offset moves to the previous calendar day');
const ranges=dashboardRanges();
assert(ranges.current.start.getTime()===current.start.getTime(),'the dashboard uses today as its current range');
assert(ranges.current.end.getTime()<=Date.now()+1000,'the current day never includes future data');
assert(Object.keys(dashboardPeriods)[0]==='today','today is the first period tab');
console.log('CRM dashboard today period: OK');
"""
    completed = subprocess.run(
        ["node", "-e", node_script],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM dashboard today period: OK" in completed.stdout


def test_complete_workspace_assets_are_loaded_before_the_crm_script():
    root = application.app.root_path
    template = open(root + "/templates/crm.html", encoding="utf-8").read()
    workspace_js = open(root + "/static/crm_workspace.js", encoding="utf-8").read()
    workspace_css = open(root + "/static/crm_workspace.css", encoding="utf-8").read()

    assert template.index("crm_workspace.js") < template.index("crm.js")
    assert "crm_workspace.css" in template
    for marker in (
        "pipeline-board", "workspace-bulk", "calendar_default_view",
        "Disqualifier la piste", "CENTRE D’ACTIVITÉ", "Espace Direction",
        "Coûts moyens par formation", "createContactModal",
        "calendar-filterbar", "directionComparison",
    ):
        assert marker in workspace_js
    assert "@media(max-width:700px)" in workspace_css
    assert ".pipeline-board" in workspace_css
    assert ".direction-comparison" in workspace_css


def test_pistes_restore_the_clickable_status_overview():
    crm_js = open(
        application.app.root_path + "/static/crm.js", encoding="utf-8"
    ).read()
    render = crm_js[
        crm_js.index("function render(){"):crm_js.index("async function init()")
    ]

    assert "pipelineOverviewStatuses().map" in crm_js
    assert 'data-status="${esc(s)}"' in crm_js
    assert "document.querySelectorAll('[data-status]')" in crm_js
    legacy_pistes = (
        "if(C.section==='pistes'){page.innerHTML=listPage(C.section);"
        "bindList(C.section);bindRows();return}"
    )
    assert legacy_pistes in render
    assert render.index(legacy_pistes) < render.index("if(window.CRMWorkspace)")


def test_pistes_can_filter_by_origin_and_sort_by_score():
    root = application.app.root_path
    crm_js = open(root + "/static/crm.js", encoding="utf-8").read()

    assert 'id="originFilter"' in crm_js
    assert 'aria-label="Filtrer selon l’origine"' in crm_js
    assert "crmOriginLabels(c).includes(origin)" in crm_js
    assert 'data-score-sort' in crm_js
    assert "Trier les scores du plus grand au plus petit" in crm_js
    assert "Trier les scores du plus petit au plus grand" in crm_js
    assert "function contactScoreValue(contact)" in crm_js
    assert "function sortLeadsByScore(list,direction)" in crm_js
    assert "if(first===null)return 1" in crm_js
    assert "direction==='asc'?first-second:second-first" in crm_js


def test_pistes_score_badge_keeps_numeric_blocked_score_and_special_markers():
    crm_js = open(
        application.app.root_path + "/static/crm.js", encoding="utf-8"
    ).read()
    scoring_code = crm_js[
        crm_js.index("const shortScoreLabel="):crm_js.index("const vaeEligibilityQuestions=")
    ]
    node_script = f"""
const esc=value=>String(value??'').replace(/[&<>'"]/g,character=>character);
{scoring_code}
const score=(value,status='blocked',extra={{}})=>({{
  integration_score:{{score:value,level:'fragile',operational_status:status,...extra}}
}});
const rendered={{
  blocked:scoreBadge(score(25)),
  zero:scoreBadge(score(0)),
  missing:[null,undefined,'','invalide'].map(value=>scoreBadge(score(value))),
  ready:scoreBadge(score(75,'ready')),
  provisional:scoreBadge(score(28,'action_required',{{score_estimated:true,score_complete:false}})),
  regulatory:scoreBadge(score(25,'blocked',{{
    regulatory_applicable:true,regulatory_status:'accepted',regulatory_label:'Accepté'
  }})),
  vae:scoreBadge({{vae_eligibility:{{score:80}},...score(25)}}),
  sorted:sortLeadsByScore([score(null),score(10),score(25)],'desc')
    .map(contact=>contact.integration_score.score)
}};
process.stdout.write(JSON.stringify(rendered));
"""
    completed = subprocess.run(
        ["node", "-e", node_script], check=True, capture_output=True, text=True
    )
    rendered = json.loads(completed.stdout)

    assert "25 — Bloqué" in rendered["blocked"]
    assert "0 — Bloqué" in rendered["zero"]
    assert all("Non calculable" in badge for badge in rendered["missing"])
    assert "75 — Fragile" in rendered["ready"]
    assert "28 ~ — Provisoire" in rendered["provisional"]
    assert "score-badge fragile provisional" in rendered["provisional"]
    assert "Score provisoire fondé sur les informations actuellement connues" in rendered["provisional"]
    assert "25 — Bloqué" in rendered["regulatory"] and "score-shield accepted" in rendered["regulatory"]
    assert "VAE 80 %" in rendered["vae"] and "Bloqué" not in rendered["vae"]
    assert rendered["sorted"] == [25, 10, None]


def test_flag_badges_render_in_business_order_for_search_and_lead_score():
    crm_js = open(
        application.app.root_path + "/static/crm.js", encoding="utf-8"
    ).read()
    flag_code = crm_js[
        crm_js.index("function listQualificationFlag(contact)"):
        crm_js.index("const dashboardPeriods=")
    ]
    node_script = f"""
const esc=value=>String(value??'').replace(/[&<>'"]/g,character=>character);
const displayName=contact=>[contact.prenom,contact.nom].filter(Boolean).join(' ')||'Sans nom';
const scoreBadge=contact=>`<span class="score-badge">${{contact.score??'Non calculable'}}</span>`;
{flag_code}
const rendered={{
  green:listQualificationFlag({{qualification_flag:'green'}}),
  redName:globalContactName({{qualification_flag:'red',prenom:'Lina',nom:'Martin'}}),
  greenScore:leadScoreCell({{qualification_flag:'green',score:72}}),
  emptyScore:leadScoreCell({{qualification_flag:'',score:null}}),
  legacyScore:leadScoreCell({{score:25}})
}};
process.stdout.write(JSON.stringify(rendered));
"""
    completed = subprocess.run(
        ["node", "-e", node_script], check=True, capture_output=True, text=True
    )
    rendered = json.loads(completed.stdout)

    assert "Green Flag" in rendered["green"]
    assert 'role="img"' in rendered["green"]
    assert rendered["redName"].index("Red Flag") < rendered["redName"].index("Lina Martin")
    assert rendered["greenScore"].index("Green Flag") < rendered["greenScore"].index("72")
    assert "contact-flag-badge" not in rendered["emptyScore"]
    assert "Non calculable" in rendered["emptyScore"]
    assert "contact-flag-badge" not in rendered["legacyScore"]
    assert "25" in rendered["legacyScore"]


def test_pistes_support_selection_and_individualized_bulk_messages():
    root = application.app.root_path
    crm_js = open(root + "/static/crm.js", encoding="utf-8").read()
    crm_css = open(root + "/static/crm.css", encoding="utf-8").read()

    for marker in (
        'id="leadSelectAll"', "data-lead-select", 'id="selectAllLeads"',
        'id="leadBulkBar"', 'data-bulk-message="email"',
        'data-bulk-message="sms"', 'id="changeSelectedLeadStatus"',
        'id="deleteSelectedLeads"', "function bulkMessageModal(type)",
        "function bulkLeadStatusModal()", "function bulkLeadDeleteModal()",
    ):
        assert marker in crm_js
    assert "selectedLeadIds.add(id)" in crm_js
    assert "selectedLeadIds.delete(id)" in crm_js
    assert "Chaque piste recevra un message individuel" in crm_js
    assert "for(let index=0;index<batch.length;index++)" in crm_js
    assert "`/api/crm/contacts/${contact.id}/message`" in crm_js
    assert "mergeContactInStore(contact.id,updated)" in crm_js
    assert "pending=failures.map(item=>item.contact)" in crm_js
    assert ".lead-bulk-bar" in crm_css
    assert ".bulk-message-progress" in crm_css


def test_calendar_restores_training_colours_without_touching_contact_sheets():
    crm_js = open(
        application.app.root_path + "/static/crm.js", encoding="utf-8"
    ).read()
    crm_css = open(
        application.app.root_path + "/static/crm.css", encoding="utf-8"
    ).read()
    render = crm_js[
        crm_js.index("function render(){"):crm_js.index("async function init()")
    ]

    assert "if(C.section==='calendrier')return calendarPage()" in render
    assert "CRMWorkspace.calendarPage" not in render
    assert "calendar-training-${calendarFormationTone(a,c)}" in crm_js
    for tone in ("aps", "a3p", "vtc", "ssiap", "desp"):
        assert f".calendar-training-{tone}" in crm_css
    assert "CRMWorkspace.enhanceContact(c,workspaceContext())" in crm_js
    assert "CRMWorkspace.bindContactActions(c,workspaceContext())" in crm_js


def test_contact_completeness_uses_the_real_conditional_requirements():
    workspace_js = open(
        application.app.root_path + "/static/crm_workspace.js", encoding="utf-8"
    ).read()

    assert "function contactCompletenessDetails(contact)" in workspace_js
    for marker in (
        "dates_formation",
        "next_action",
        "cpf_montant",
        "identite_creation",
        "statut_demande_financement_ft",
        "reste_a_charge_perso",
        "carte_pro",
        "titre_sejour_cnaps",
        "integration_dracar",
    ):
        assert marker in workspace_js
    assert "enhanceContact:enhanceContactWithCompleteness" in workspace_js
    assert "refreshContactCompleteness(draft)" in workspace_js
    assert "details.missing.join(', ')" in workspace_js
    assert "contactCompletenessDetails(contact).percent" in workspace_js


def test_crm_displays_and_saves_the_meta_cpf_tier_next_to_the_amount(tmp_path, monkeypatch):
    crm_js = open(
        application.app.root_path + "/static/crm.js", encoding="utf-8"
    ).read()
    crm_css = open(
        application.app.root_path + "/static/crm.css", encoding="utf-8"
    ).read()

    assert 'name="cpf_montant"' in crm_js
    assert 'name="cpf_palier"' in crm_js
    assert "Palier CPF déclaré sur META" in crm_js
    assert "c.cpf_montant||c.cpf_palier" in crm_js
    assert ".cpf-declared-values" in crm_css

    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    updated = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"cpf_palier": "2000-3000 euros"},
    )

    assert updated.status_code == 200
    assert updated.get_json()["cpf_palier"] == "2000-3000 euros"


def test_crm_saves_universal_personal_capacity_and_confirmed_ft_amount(
        tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={
        "prenom": "Lina", "formation": "SSIAP 1",
    }).get_json()

    updated = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={
            "cpf": "NON", "financement_ft": "OUI",
            "statut_demande_financement_ft": "acceptee",
            "montant_accorde_ft": "980",
            "financement_perso_possible": "OUI",
        },
    )

    assert updated.status_code == 200
    body = updated.get_json()
    assert body["montant_accorde_ft"] == "980.00"
    assert body["financement_perso_possible"] == "OUI"
    assert body["integration_score"]["financial_score"] == 100
    assert body["integration_score"]["funding_solution_status"] == (
        "secured_france_travail"
    )

    invalid = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"montant_accorde_ft": "12.345"},
    )
    assert invalid.status_code == 400


def test_manual_contact_creation_keeps_workspace_fields(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    created = c.post("/api/crm/contacts", json={
        "prenom": "Lina", "nom": "Martin", "formation": "A3P",
        "lieu": "Côte d’Azur", "origine": "Google", "commercial": "Cassandre",
    }).get_json()

    assert created["lieu"] == "Côte d’Azur"
    assert created["origine"] == "Google"
    assert created["commercial"] == "Cassandre"
    assert created["integration_score"]["score"] == 0
    assert created["integration_score"]["score_estimated"] is True
    assert created["integration_score"]["score_complete"] is False
    assert created["integration_score"]["operational_status"] == "action_required"


def test_workspace_bulk_actions_require_disqualification_reason_and_are_audited(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    first = c.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin"}).get_json()
    second = c.post("/api/crm/contacts", json={"prenom": "Nora", "nom": "Petit"}).get_json()
    ids = [first["id"], second["id"]]

    missing = c.patch("/api/crm/contacts/bulk", json={"ids": ids, "action": "disqualify"})
    assert missing.status_code == 400

    result = c.patch("/api/crm/contacts/bulk", json={
        "ids": ids, "action": "disqualify", "reason": "Projet reporté",
        "detail": "Recontacter après la rentrée", "reactivation_date": "2027-01-15",
    })
    assert result.status_code == 200
    assert result.get_json()["count"] == 2
    for contact in result.get_json()["updated"]:
        assert contact["statut"] == "Disqualifié"
        assert contact["disqualification_reason"] == "Projet reporté"
        assert contact["disqualification_detail"] == "Recontacter après la rentrée"
        assert contact["reactivation_date"] == "2027-01-15"
        activity = next(
            item for item in contact["activities"]
            if item.get("title") == "Piste disqualifiée"
        )
        assert "Ancien statut : Nouveaux" in activity["detail"]
        assert "Motif : Projet reporté" in activity["detail"]
        assert "Précisions : Recontacter après la rentrée" in activity["detail"]
        assert "Réactivation prévue : 2027-01-15" in activity["detail"]

    restored = c.patch("/api/crm/contacts/bulk", json={
        "ids": [first["id"]], "action": "status", "value": "Nouveaux",
    }).get_json()["updated"][0]
    assert restored["disqualification_reason"] == ""
    assert restored["disqualification_detail"] == ""
    assert any(
        item.get("title") == "Piste disqualifiée"
        and "Recontacter après la rentrée" in item.get("detail", "")
        for item in restored["activities"]
    )


def test_bulk_delete_removes_selected_contacts_and_linked_appointments(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    first = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    second = c.post("/api/crm/contacts", json={"prenom": "Nora"}).get_json()
    kept = c.post("/api/crm/contacts", json={"prenom": "Yanis"}).get_json()
    data = application.load_data()
    data["crm_calendly_appointments"] = [
        {"id": "rdv-first", "contact_id": first["id"]},
        {"id": "rdv-second", "contact_id": second["id"]},
        {"id": "rdv-kept", "contact_id": kept["id"]},
    ]
    application.save_data(data)

    response = c.delete("/api/crm/contacts/bulk", json={
        "ids": [first["id"], second["id"]],
    })

    assert response.status_code == 200
    assert response.get_json()["count"] == 2
    assert set(response.get_json()["deleted_ids"]) == {first["id"], second["id"]}
    saved = application.load_data()
    assert {contact["id"] for contact in saved["crm_contacts"]} == {kept["id"]}
    assert saved["crm_calendly_appointments"] == [
        {"id": "rdv-kept", "contact_id": kept["id"]},
    ]


def test_workspace_archive_assignment_and_financial_fields_persist(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()

    updated = c.patch(f"/api/crm/contacts/{contact['id']}", json={
        "commercial": "Aurélie", "tags": "urgent, entreprise",
        "prix_vente": "4200", "cout_estime": "2600",
    }).get_json()
    assert updated["commercial"] == "Aurélie"
    assert updated["prix_vente"] == "4200"
    assert updated["cout_estime"] == "2600"

    archived = c.patch("/api/crm/contacts/bulk", json={
        "ids": [contact["id"]], "action": "archive",
    }).get_json()["updated"][0]
    assert archived["archived_at"]
    restored = c.patch("/api/crm/contacts/bulk", json={
        "ids": [contact["id"]], "action": "restore",
    }).get_json()["updated"][0]
    assert restored["archived_at"] == ""


def test_workspace_can_merge_duplicates_without_losing_history(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    target = c.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin", "mail": "lina@example.com"}).get_json()
    source = c.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin", "telephone": "0611223344", "force_create": True}).get_json()
    c.post(f"/api/crm/contacts/{source['id']}/publications", json={"texte": "À conserver"})

    result = c.post("/api/crm/contacts/merge", json={
        "target_id": target["id"], "source_id": source["id"],
    })
    assert result.status_code == 200
    merged = result.get_json()["contact"]
    assert merged["telephone"] == "0611223344"
    assert any(item.get("texte") == "À conserver" for item in merged["publications"])
    assert c.get(f"/api/crm/contacts/{source['id']}").status_code == 404


def test_crm_settings_persist_and_direction_costs_are_admin_only(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    updated = c.patch("/api/crm/settings", json={
        "calendar_default_view": "month", "calendar_workday_start": "07:30",
        "direction_costs": {"A3P": "2500"},
    })
    assert updated.status_code == 200
    assert updated.get_json()["calendar_default_view"] == "month"
    assert updated.get_json()["direction_costs"]["A3P"] == 2500

    with c.session_transaction() as session:
        session["user_email"] = "cassandre@integraleacademy.com"
    denied = c.patch("/api/crm/settings", json={"direction_costs": {"APS": 900}})
    assert denied.status_code == 403


def test_template_updates_keep_a_version_history(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    created = c.post("/api/crm/templates", json={
        "type": "sms", "nom": "Relance", "contenu": "Premier texte", "categorie": "Relance",
    }).get_json()
    updated = c.patch(f"/api/crm/templates/{created['id']}", json={
        "nom": "Relance", "contenu": "Deuxième texte", "categorie": "Relance",
    }).get_json()

    assert updated["categorie"] == "Relance"
    assert updated["versions"][0]["contenu"] == "Premier texte"


def test_pipeline_financing_stages_follow_real_funding_request_status():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "if(fundingStatus==='en_cours_instruction')statuses.add('Financement FT en cours')" in crm_js
    assert "if(fundingStatus==='refusee')statuses.add('Financement FT refusé')" in crm_js
    assert "const contactHasPipelineStatus=(c,status)=>contactPipelineStatuses(c).includes(status)" in crm_js
    assert "activeContacts.filter(c=>contactHasPipelineStatus(c,s)).length" in crm_js
    assert "list.filter(c=>contactHasPipelineStatus(c,statusFilter))" in crm_js
    assert "!statusFilter||contactHasPipelineStatus(c,statusFilter)" in crm_js


def test_crm_collaborative_refresh_is_lightweight_and_never_overlaps():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "CRM_REFRESH_INTERVAL_MS=60000" in crm_js
    assert "crmRefreshInFlight" in crm_js
    assert "document.hidden||crmRefreshInFlight" in crm_js
    assert "api(`/api/crm/contacts/updates${suffix}`)" in crm_js
    assert "setInterval(async()=>{try{const fresh=await api('/api/crm/contacts')" not in crm_js


def test_crm_bootstrap_reads_the_shared_data_file_once(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    original_load_data = application.load_data
    calls = []

    def counted_load_data():
        calls.append(True)
        return original_load_data()

    monkeypatch.setattr(application, "load_data", counted_load_data)
    response = c.get("/api/crm/bootstrap")

    assert response.status_code == 200
    assert len(calls) == 1
    assert set(response.get_json()) == {
        "contacts", "templates", "formation_sessions", "notifications",
        "appointments", "calendly_integration", "settings",
        "callback_requests", "callback_pending_count",
    }


def test_crm_frontend_uses_the_single_bootstrap_endpoint():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    init = crm_js[crm_js.index("async function init()"):
                  crm_js.index("const newContactButton")]
    assert "api(`/api/crm/bootstrap?section=${encodeURIComponent(C.section)}`)" in init
    assert "Promise.all" not in init


def test_crm_bootstrap_is_compact_and_contact_details_are_loaded_on_demand(
        tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    created = c.post(
        "/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin"}
    ).get_json()
    data = application.load_data()
    contact = next(
        row for row in data["crm_contacts"] if row["id"] == created["id"]
    )
    contact["formulaire"] = {"document": "x" * 250_000}
    contact["activities"] = [{
        "id": "activity-heavy",
        "date": "2026-08-17T12:00:00+02:00",
        "kind": "email",
        "title": "E-mail envoyé",
        "detail": "Informations transmises",
        "preview": "<html>" + "y" * 250_000 + "</html>",
    }]
    contact["publications"] = [{
        "id": "publication-1", "date": "2026-08-17T12:05:00+02:00",
        "texte": "Publication visible", "comments": [], "likes": [],
    }]
    application.save_data(data)

    compact_response = c.get("/api/crm/bootstrap?section=pistes")
    compact = compact_response.get_json()["contacts"][0]

    assert compact["_summary"] is True
    assert "formulaire" not in compact
    assert compact["activities"] == [{
        "id": "activity-heavy", "kind": "email",
        "date": "2026-08-17T12:00:00+02:00",
    }]
    assert compact["publications"] == []
    assert len(compact_response.data) < 150_000

    activity_response = c.get("/api/crm/bootstrap?section=fil-actu")
    activity_contact = activity_response.get_json()["contacts"][0]
    assert activity_contact["activities"][0]["title"] == "E-mail envoyé"
    assert "preview" not in activity_contact["activities"][0]
    assert activity_contact["publications"][0]["texte"] == "Publication visible"
    assert len(activity_response.data) < 150_000

    detail_response = c.get(f"/api/crm/contacts/{created['id']}")
    detail = detail_response.get_json()
    assert "formulaire" not in detail
    assert detail["activities"][0]["has_preview"] is True
    assert "preview" not in detail["activities"][0]
    assert len(detail_response.data) < 150_000

    preview = c.get(
        f"/api/crm/contacts/{created['id']}/activities/activity-heavy/preview"
    ).get_json()
    assert len(preview["preview"]) > 250_000


def test_contact_summaries_count_messages_relances_and_active_appointments(
        tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    created = c.post(
        "/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin"}
    ).get_json()
    data = application.load_data()
    contact = next(
        row for row in data["crm_contacts"] if row["id"] == created["id"]
    )
    contact["origine"] = "Google Ads"
    contact["activities"] = [
        {"id": "mail-1", "date": "2026-08-10T09:00:00+02:00", "kind": "email"},
        {"id": "mail-2", "date": "2026-08-11T09:00:00+02:00", "kind": "email"},
        {"id": "sms-1", "date": "2026-08-12T09:00:00+02:00", "kind": "sms"},
        {"id": "failed", "date": "2026-08-13T09:00:00+02:00", "kind": "erreur"},
    ]
    contact["relances"] = [
        {"id": "scheduled", "scheduled_date": "2026-08-20", "status": "scheduled"},
        {"id": "answered", "scheduled_date": "2026-08-12", "status": "answered"},
        {"id": "no-answer", "scheduled_date": "2026-08-13", "status": "no_answer"},
        {"id": "reprogrammed", "scheduled_date": "2026-08-14", "status": "reprogrammed"},
        {"id": "cancelled", "scheduled_date": "2026-08-15", "status": "cancelled"},
    ]
    data["crm_calendly_appointments"] = [
        {
            "id": "past", "contact_id": created["id"], "status": "active",
            "start_time": "2026-08-10T08:00:00Z",
        },
        {
            "id": "future", "contact_id": created["id"], "status": "active",
            "start_time": "2026-08-25T08:00:00Z",
        },
        {
            "id": "canceled", "contact_id": created["id"], "status": "canceled",
            "start_time": "2026-08-22T08:00:00Z",
        },
        {
            "id": "undated", "contact_id": created["id"], "status": "active",
            "start_time": "",
        },
    ]
    application.save_data(data)

    bootstrap = c.get("/api/crm/bootstrap?section=contacts").get_json()
    summary = next(row for row in bootstrap["contacts"] if row["id"] == created["id"])

    assert summary["origine"] == "Google Ads"
    assert summary["activity_counts"] == {
        "appointments": 2,
        "relances": 3,
        "emails": 2,
        "sms": 1,
    }

    updates = c.get("/api/crm/contacts/updates").get_json()["contacts"]
    update = next(row for row in updates if row["id"] == created["id"])
    assert update["activity_counts"] == summary["activity_counts"]


def test_contacts_table_displays_origin_and_activity_badges():
    root = application.app.root_path
    workspace_js = open(root + "/static/crm_workspace.js", encoding="utf-8").read()
    workspace_css = open(root + "/static/crm_workspace.css", encoding="utf-8").read()

    assert "if(type==='contacts')return'<th>CONTACT</th><th>ORIGINE</th><th>ACTIVITÉS</th>" in workspace_js
    assert "function contactActivityBadges" in workspace_js
    for label in ("RDV téléphonique", "relance", "mail", "SMS"):
        assert label in workspace_js
    assert "function originBadge" in workspace_js
    assert "google-ads" in workspace_js
    assert ".workspace-activity-tag.rdv" in workspace_css
    assert ".workspace-origin-badge.google-ads" in workspace_css


def test_pistes_table_displays_origin_and_activity_badges_in_both_renderers():
    root = application.app.root_path
    crm_js = open(root + "/static/crm.js", encoding="utf-8").read()
    workspace_js = open(root + "/static/crm_workspace.js", encoding="utf-8").read()
    workspace_css = open(root + "/static/crm_workspace.css", encoding="utf-8").read()

    assert "<th>CONTACT</th><th>ORIGINE</th><th>ACTIVITÉS</th><th>FORMATION</th>" in crm_js
    assert "function listOriginBadge" in crm_js
    assert "function listActivityBadges" in crm_js
    assert "'Google Ads'" in crm_js and "'Ajout manuel'" in crm_js
    assert "function leadOriginFilterOptions" in crm_js
    assert "<th>PROJET</th><th>ORIGINE</th><th>ACTIVITÉS</th>" in workspace_js
    assert "${originBadge(contact,ctx)}</td><td>${contactActivityBadges(contact,appointmentCount)}" in workspace_js
    assert "workspaceOriginOptions" in workspace_js
    assert "table-layout:fixed" in workspace_css
    assert "col.crm-col-contact{width:310px}" in workspace_css


def test_contact_sheet_fetches_full_record_only_when_a_summary_is_opened():
    crm_js = open(
        application.app.root_path + "/static/crm.js", encoding="utf-8"
    ).read()

    assert "async function showContact(id,initialTab='contactInfoTab')" in crm_js
    assert "if(c._summary)" in crm_js
    assert "api(`/api/crm/contacts/${encodeURIComponent(id)}`,{timeout:10000})" in crm_js
    assert "delete c._summary" in crm_js


def test_activity_preferences_are_bound_only_after_the_modal_exists():
    workspace_js = open(
        application.app.root_path + "/static/crm_workspace.js", encoding="utf-8"
    ).read()
    activity = workspace_js[
        workspace_js.index("function enhancedActivityPage"):
        workspace_js.index("function createContactModal")
    ]

    assert "settings.onclick=()=>{" in activity
    assert "document.querySelector('#cancelNotificationSettings')" in activity
    assert "document.querySelector('#saveNotificationSettings')" in activity
    assert "cancelNotificationSettings.onclick" not in activity


def test_save_data_streams_the_previous_file_to_backup(tmp_path, monkeypatch):
    data_file = tmp_path / "data.json"
    monkeypatch.setattr(application, "DATA_FILE", str(data_file))
    application.save_data({"version": "avant"})
    application.save_data({"version": "apres"})

    assert application.json.loads((tmp_path / "data.json.bak").read_text(
        encoding="utf-8"
    )) == {"version": "avant"}


def test_wedof_open_refresh_is_targeted_and_manual_refresh_remains_available():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert 'id="wedofRefresh"' in crm_js
    assert "refresh.onclick=()=>refreshWedof(c,status)" in crm_js
    assert "void refreshWedofOnOpen(c,status)" in crm_js
    assert "/wedof/refresh-on-open" in crm_js
    assert "status.configured!==false&&resources.length" in crm_js
    assert "cached.sync?.last_sync_at||status.last_sync_at});if(status.configured!==false)" not in crm_js
    assert "loadWedofTabCount(c,contactWedofTab);wedofLoaded=true;loadWedof(c)" not in crm_js


def test_gunicorn_recycles_the_single_worker():
    root = application.app.root_path
    procfile = open(root + "/Procfile", encoding="utf-8").read()
    config = open(root + "/gunicorn.conf.py", encoding="utf-8").read()

    assert "--max-requests ${GUNICORN_MAX_REQUESTS:-5000}" in procfile
    assert "--max-requests-jitter ${GUNICORN_MAX_REQUESTS_JITTER:-500}" in procfile
    assert 'max_requests = int(os.getenv("GUNICORN_MAX_REQUESTS", "5000"))' in config
    assert 'max_requests_jitter = int(os.getenv("GUNICORN_MAX_REQUESTS_JITTER", "500"))' in config
    assert "max_requests = 0" not in config


def test_funding_request_status_automatically_updates_secondary_timeline(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    created = c.post("/api/crm/contacts", json={"prenom": "Auto", "nom": "FT"}).get_json()

    in_progress = c.patch(
        f"/api/crm/contacts/{created['id']}",
        json={"statut_demande_financement_ft": "en_cours_instruction"},
    )
    assert in_progress.status_code == 200
    assert in_progress.get_json()["statut_secondaire"] == "Financement FT en cours"

    refused = c.patch(
        f"/api/crm/contacts/{created['id']}",
        json={"statut_demande_financement_ft": "refusee"},
    )
    assert refused.status_code == 200
    refused_contact = refused.get_json()
    today = application.datetime.datetime.now(
        application.pytz.timezone("Europe/Paris")
    ).date()
    if today.weekday() >= 5:
        today += application.datetime.timedelta(days=7 - today.weekday())
    today = today.isoformat()
    assert refused_contact["statut_secondaire"] == "Financement FT refusé"
    assert refused_contact["statut"] == "A relancer"
    assert refused_contact["relance_date"] == today
    scheduled = [
        item for item in refused_contact["relances"]
        if item.get("status") == "scheduled"
    ]
    assert len(scheduled) == 1
    assert scheduled[0]["scheduled_date"] == today
    assert scheduled[0]["source"] == "manual_ft_refusal"
    assert scheduled[0]["motif"] == "Suite refus FT"
    assert any(
        item.get("title") == "Relance France Travail planifiée"
        for item in refused_contact["activities"]
    )

    replayed = c.patch(
        f"/api/crm/contacts/{created['id']}",
        json={"statut_demande_financement_ft": "refusee"},
    ).get_json()
    assert len([
        item for item in replayed["relances"]
        if item.get("status") == "scheduled"
    ]) == 1
    assert len([
        item for item in replayed["activities"]
        if item.get("title") == "Relance France Travail planifiée"
    ]) == 1


def test_primary_pipeline_places_in_progress_after_scheduled_appointment():
    statuses = application._crm_statuses({})

    scheduled_index = statuses.index("RDV programmé")
    assert statuses[scheduled_index:scheduled_index + 3] == [
        "RDV programmé", "En cours", "A relancer",
    ]


def test_custom_primary_pipeline_repositions_in_progress_without_duplicate():
    statuses = application._crm_statuses({
        "crm_statuses": [
            "Nouveaux", "En cours", "Blocage", "RDV programmé",
            "Prochain RDV inscription", "En cours",
        ],
    })

    assert statuses.count("En cours") == 1
    assert statuses.index("En cours") == statuses.index("RDV programmé") + 1


@pytest.mark.parametrize(
    "status",
    [status for status in application.CRM_STATUSES if status != "RDV programmé"],
)
def test_each_manually_selected_primary_status_survives_refresh(
        tmp_path, monkeypatch, status):
    c = client(tmp_path, monkeypatch)
    created = c.post(
        "/api/crm/contacts", json={"prenom": "Persistance", "nom": "Primaire"}
    ).get_json()

    saved = c.patch(
        f"/api/crm/contacts/{created['id']}", json={"statut": status}
    )
    assert saved.status_code == 200
    assert saved.get_json()["statut"] == status

    refreshed = next(
        contact for contact in c.get("/api/crm/contacts").get_json()
        if contact["id"] == created["id"]
    )
    assert refreshed["statut"] == status


@pytest.mark.parametrize("status", application.CRM_SECONDARY_STATUSES)
def test_each_manually_selected_secondary_status_survives_refresh(
        tmp_path, monkeypatch, status):
    c = client(tmp_path, monkeypatch)
    created = c.post(
        "/api/crm/contacts", json={"prenom": "Persistance", "nom": "Secondaire"}
    ).get_json()

    saved = c.patch(
        f"/api/crm/contacts/{created['id']}",
        json={"statut_secondaire": status},
    )
    assert saved.status_code == 200
    assert saved.get_json()["statut_secondaire"] == status

    refreshed = next(
        contact for contact in c.get("/api/crm/contacts").get_json()
        if contact["id"] == created["id"]
    )
    assert refreshed["statut_secondaire"] == status


def test_manual_ft_refusal_updates_real_status_and_beats_stale_wedof_state(
        tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    created = c.post(
        "/api/crm/contacts", json={"prenom": "Persistance", "nom": "FT"}
    ).get_json()
    monkeypatch.setattr(
        application,
        "_wedof_funding_statuses_by_contact",
        lambda data: {created["id"]: "en_cours_instruction"},
    )

    saved = c.patch(
        f"/api/crm/contacts/{created['id']}",
        json={"statut_secondaire": "Financement FT refusé"},
    ).get_json()
    assert saved["statut_secondaire"] == "Financement FT refusé"
    assert saved["statut_demande_financement_ft"] == "refusee"
    assert saved["statut"] == "A relancer"
    expected_date = application.datetime.datetime.now(
        application.pytz.timezone("Europe/Paris")
    ).date()
    if expected_date.weekday() >= 5:
        expected_date += application.datetime.timedelta(
            days=7 - expected_date.weekday()
        )
    assert saved["relance_date"] == expected_date.isoformat()
    assert len([
        item for item in saved["relances"]
        if item.get("status") == "scheduled"
    ]) == 1

    refreshed = next(
        contact for contact in c.get("/api/crm/contacts").get_json()
        if contact["id"] == created["id"]
    )
    assert refreshed["statut_secondaire"] == "Financement FT refusé"
    assert refreshed["statut_demande_financement_ft"] == "refusee"
    assert refreshed["statut"] == "A relancer"
    assert refreshed["relance_date"] == saved["relance_date"]


@pytest.mark.parametrize("final_status", ["Converti", "Disqualifié"])
def test_ft_refusal_does_not_reopen_a_finalized_contact(
        tmp_path, monkeypatch, final_status):
    c = client(tmp_path, monkeypatch)
    created = c.post(
        "/api/crm/contacts",
        json={"prenom": "Final", "nom": final_status},
    ).get_json()
    finalized = c.patch(
        f"/api/crm/contacts/{created['id']}",
        json={"statut": final_status},
    )
    assert finalized.status_code == 200

    refused = c.patch(
        f"/api/crm/contacts/{created['id']}",
        json={"statut_demande_financement_ft": "refusee"},
    )
    assert refused.status_code == 200
    contact = refused.get_json()
    assert contact["statut"] == final_status
    assert contact["statut_secondaire"] == "Financement FT refusé"
    assert not [
        item for item in contact["relances"]
        if item.get("status") == "scheduled"
    ]
    assert not [
        item for item in contact["activities"]
        if item.get("title") == "Relance France Travail planifiée"
    ]


def test_ft_refusal_keeps_relance_priority_with_an_active_calendly_appointment(
        tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    created = c.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin"},
    ).get_json()
    data = application.load_data()
    data["crm_calendly_appointments"] = [{
        "id": "future-ft-appointment",
        "contact_id": created["id"],
        "status": "active",
        "start_time": "2099-09-02T10:00:00+02:00",
        "end_time": "2099-09-02T10:30:00+02:00",
    }]
    application.save_data(data)

    programmed = c.patch(
        f"/api/crm/contacts/{created['id']}",
        json={"statut": "RDV programmé"},
    )
    assert programmed.status_code == 200
    assert programmed.get_json()["statut"] == "RDV programmé"

    refused = c.patch(
        f"/api/crm/contacts/{created['id']}",
        json={"statut_demande_financement_ft": "refusee"},
    )
    assert refused.status_code == 200
    contact = refused.get_json()
    assert contact["statut"] == "A relancer"
    assert contact["statut_secondaire"] == "Financement FT refusé"
    assert len([
        item for item in contact["relances"]
        if item.get("status") == "scheduled"
    ]) == 1


def test_a_new_ft_refusal_on_another_day_reprograms_the_follow_up():
    paris = application.pytz.timezone("Europe/Paris")
    contact = {
        "id": "ft-cycle",
        "prenom": "Cycle",
        "nom": "FT",
        "statut": "Nouveaux",
        "relances": [],
        "activities": [],
    }

    first, first_changed = application._crm_schedule_ft_refusal_relance(
        contact,
        source="wedof_ft_refusal",
        stable_id="folder-cycle",
        now=paris.localize(application.datetime.datetime(2026, 8, 21, 10)),
    )
    second, second_changed = application._crm_schedule_ft_refusal_relance(
        contact,
        source="wedof_ft_refusal",
        stable_id="folder-cycle",
        now=paris.localize(application.datetime.datetime(2026, 8, 24, 10)),
    )

    assert first_changed is True
    assert second_changed is True
    relances_by_date = {
        item["scheduled_date"]: item for item in contact["relances"]
    }
    assert relances_by_date["2026-08-21"]["status"] == "reprogrammed"
    assert relances_by_date["2026-08-24"]["status"] == "scheduled"
    assert second["scheduled_date"] == "2026-08-24"
    assert contact["relance_date"] == "2026-08-24"
    assert len([
        item for item in contact["activities"]
        if item.get("title") == "Relance France Travail planifiée"
    ]) == 2


@pytest.mark.parametrize("received_at", [(2026, 8, 22), (2026, 8, 23)])
def test_ft_refusal_received_on_weekend_is_scheduled_for_monday(received_at):
    paris = application.pytz.timezone("Europe/Paris")
    contact = {
        "id": "ft-weekend",
        "prenom": "Weekend",
        "nom": "FT",
        "statut": "Nouveaux",
        "relances": [],
        "activities": [],
    }

    relance, changed = application._crm_schedule_ft_refusal_relance(
        contact,
        source="wedof_ft_refusal",
        stable_id="folder-weekend",
        now=paris.localize(application.datetime.datetime(*received_at, 10)),
    )
    replayed, replayed_changed = application._crm_schedule_ft_refusal_relance(
        contact,
        source="wedof_ft_refusal",
        stable_id="folder-weekend",
        now=paris.localize(application.datetime.datetime(
            2026, 8, 23 if received_at[-1] == 22 else 22, 10
        )),
    )

    active = [
        item for item in contact["relances"]
        if item.get("status") == "scheduled"
    ]
    assert changed is True
    assert replayed_changed is False
    assert replayed["id"] == relance["id"]
    assert relance["scheduled_date"] == "2026-08-24"
    assert contact["relance_date"] == "2026-08-24"
    assert active == [relance]
    assert contact["activities"][0]["detail"] == (
        "Financement France Travail refusé. Relance prévue le 24/08/2026. "
        "Dossier WEDOF : folder-weekend."
    )
    assert len(contact["activities"]) == 1


def test_pipeline_table_displays_every_status_held_by_a_contact():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "contactPipelineStatuses(c).map(badge).join(' ')" in crm_js


def test_tracking_card_can_expand_and_displays_secretariat_origin():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert 'id="trackingExpand"' in crm_js
    assert "trackingCard.showPopover()" in crm_js
    assert ".tracking-card:popover-open" in open(
        application.app.root_path + "/static/crm.css", encoding="utf-8"
    ).read()
    assert "'Secrétariat','Formulaire abandonné','Ajout manuel','Autre'" in crm_js


def test_tracking_card_precedes_the_activity_tab_and_publications_stay_inside_it():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert crm_js.index('id="trackingCard"') < crm_js.index(
        'id="contactActivityPanel"'
    )
    activity_panel = crm_js.split('id="contactActivityPanel"', 1)[1].split(
        'id="contactRelancePanel"', 1
    )[0]
    assert 'class="card publications-card"' in activity_panel


def test_contact_lifecycle_and_activity(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    created = c.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin", "formation": "APS"})
    assert created.status_code == 201
    contact = created.get_json()
    assert contact["statut"] == "Nouveaux"
    assert contact["origine"] == "Ajout manuel"

    updated = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"statut": "A relancer", "relance_date": "2026-08-10", "carte_pro": "NON"},
    )
    assert updated.status_code == 200
    assert updated.get_json()["activities"][0]["title"] == "Statut : A relancer"

    reloaded = c.get(f"/api/crm/contacts/{contact['id']}").get_json()
    assert reloaded["statut"] == "A relancer"
    assert reloaded["relance_date"] == "2026-08-10"

    call = c.post(f"/api/crm/contacts/{contact['id']}/appel", json={"commentaire": "Échange financement."})
    assert call.status_code == 200
    assert call.get_json()["activities"][0]["kind"] == "appel"


def test_manual_next_action_is_trimmed_persisted_and_clearable(
        tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin", "formation": "APS"},
    ).get_json()

    assert contact["prochaine_action_manuelle"] == ""

    updated = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={
            "prochaine_action_manuelle":
                "  Rappeler pour confirmer le financement  "
        },
    )

    assert updated.status_code == 200
    assert updated.get_json()["prochaine_action_manuelle"] == (
        "Rappeler pour confirmer le financement"
    )
    reloaded = c.get(f"/api/crm/contacts/{contact['id']}").get_json()
    assert reloaded["prochaine_action_manuelle"] == (
        "Rappeler pour confirmer le financement"
    )
    summaries = c.get("/api/crm/contacts?section=contacts").get_json()
    summary = next(item for item in summaries if item["id"] == contact["id"])
    assert summary["prochaine_action_manuelle"] == (
        "Rappeler pour confirmer le financement"
    )

    too_long = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"prochaine_action_manuelle": "x" * 301},
    )
    assert too_long.status_code == 400
    assert "300 caractères" in too_long.get_json()["error"]

    cleared = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"prochaine_action_manuelle": "   "},
    )
    assert cleared.status_code == 200
    assert cleared.get_json()["prochaine_action_manuelle"] == ""


def test_contact_followup_comment_is_logged_only_when_it_changes(
        tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin"},
    ).get_json()

    updated = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"commentaires": "Rappeler après réception du justificatif."},
    ).get_json()
    assert updated["commentaires"] == "Rappeler après réception du justificatif."
    assert updated["activities"][0]["kind"] == "suivi"
    assert updated["activities"][0]["title"] == "Suivi mis à jour"
    assert updated["activities"][0]["detail"] == (
        "Rappeler après réception du justificatif."
    )
    activity_count = len(updated["activities"])

    unchanged = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={
            "commentaires": "Rappeler après réception du justificatif.",
            "prenom": "Lina",
        },
    ).get_json()
    assert len(unchanged["activities"]) == activity_count

    cleared = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"commentaires": ""},
    ).get_json()
    assert cleared["activities"][0]["kind"] == "suivi"
    assert cleared["activities"][0]["detail"] == "Commentaire retiré du suivi."


def test_vae_eligibility_simulation_creates_detailed_crm_lead(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    monkeypatch.setattr(application, "creer_piste_salesforce", lambda payload: None)
    application.app.config.update(TESTING=True)

    response = application.app.test_client().post(
        "/simulateur-eligibilite-vae-desp",
        json={
            "nom": "martin", "prenom": "lina", "mail": "lina@example.com",
            "telephone": "06 12 34 56 78",
            "reponses": {"q1": "oui", "q2": "oui", "q3": "non", "q4": "oui", "q5": "oui"},
        },
    )

    assert response.status_code == 200
    assert response.get_json()["score"] == 75
    contact = application.load_data()["crm_contacts"][0]
    assert response.get_json()["crm_contact_id"] == contact["id"]
    assert (contact["prenom"], contact["nom"]) == ("Lina", "MARTIN")
    assert (contact["formation"], contact["desp_type"], contact["statut"]) == ("DESP", "VAE", "Nouveaux")
    assert contact["source"] == "simulateur_vae_desp"
    assert contact["origine"] == "Simulateur VAE"
    assert contact["vae_eligibility"] == {
        "completed_at": contact["created_at"], "score": 75,
        "resultat": "Profil favorable",
        "reponses": {"q1": "oui", "q2": "oui", "q3": "non", "q4": "oui", "q5": "oui"},
    }
    assert contact["activities"][0]["title"] == "Test d’éligibilité VAE DESP complété"
    assert "Score : 75%" in contact["activities"][0]["detail"]


def test_crm_displays_vae_eligibility_score_and_clickable_details():
    crm_js = open(application.app.root_path + "/static/crm.js", encoding="utf-8").read()

    assert "vaeEligibilityCard(c)" in crm_js
    assert "VAE ${Number(eligibility.score)} %" in crm_js
    assert "Voir le détail des réponses" in crm_js
    assert "vaeEligibilityQuestions.map" in crm_js
    assert crm_js.index('id="calendlyCard"') < crm_js.index('${vaeEligibilityCard(c)}')
    assert "Simulateur VAE" in crm_js


def test_crm_templates_include_automatic_training_emails(tmp_path, monkeypatch):
    response = client(tmp_path, monkeypatch).get("/api/crm/templates")

    assert response.status_code == 200
    automatic = response.get_json()["automatic_email"]
    assert [template["formation"] for template in automatic] == [
        "DESP_VAE", "A3P", "APS", "SSIAP", "VTC", "DESP_INIT", "DESP_INIT",
    ]
    aps = next(template for template in automatic if template["formation"] == "APS")
    assert aps["sujet"] == "👮‍♂️ Formation Agent de Sécurité Privée (APS)"
    assert "{{ prenom }}" in aps["contenu"]
    assert "1 650" in aps["contenu"]
    a3p = next(template for template in automatic if template["formation"] == "A3P")
    assert "youtube" not in a3p["contenu"].lower()
    assert "<iframe" not in a3p["contenu"].lower()
    desp_initial = [
        template for template in automatic if template["formation"] == "DESP_INIT"
    ]
    assert [(template["id"], template["nom"]) for template in desp_initial] == [
        ("automatic-desp-initial", "DESP initial – Côte d’Azur"),
        ("automatic-desp-initial-paris", "DESP initial – Paris"),
    ]
    assert "Centre de formation : <strong>Côte d’Azur</strong>" in desp_initial[0]["contenu"]
    assert "Centre de formation : <strong>Paris</strong>" in desp_initial[1]["contenu"]


def test_crm_templates_include_meta_a3p_email_and_sms(tmp_path, monkeypatch):
    response = client(tmp_path, monkeypatch).get("/api/crm/templates")

    assert response.status_code == 200
    automatic_meta = response.get_json()["automatic_meta"]
    assert [(template["type"], template["formation"]) for template in automatic_meta] == [
        ("email", "A3P"), ("sms", "A3P"),
    ]
    email, sms = automatic_meta
    assert email["sujet"] == "👮‍♂️ Formation Agent de Protection Physique des Personnes (A3P)"
    assert "{{ prenom }}" in email["contenu"]
    assert "Télécharger mon devis détaillé" not in email["contenu"]
    assert sms["contenu"] == application.build_training_information_sms_text("A3P")
    assert "https://calendly.com/integraleacademy/apr" in sms["contenu"]


def test_crm_templates_page_displays_automatic_emails_as_read_only():
    crm_js = open(application.app.root_path + "/static/crm.js", encoding="utf-8").read()

    assert "E-mails automatiques du formulaire" in crm_js
    assert '<details class="card automatic-template-card">' in crm_js
    assert '<summary class="card-head">' in crm_js
    assert '</div>`+automaticSection' in crm_js
    assert "data-preview-automatic-template" in crm_js
    assert "Envoi automatique" in crm_js
    assert "templates.automatic_email||[]" in crm_js


def test_crm_templates_page_displays_meta_a3p_messages_as_read_only():
    crm_js = open(application.app.root_path + "/static/crm.js", encoding="utf-8").read()

    assert "Messages automatiques META A3P" in crm_js
    assert "templates.automatic_meta||[]" in crm_js
    assert "data-preview-automatic-meta" in crm_js
    assert "t.type==='email'?'E-mail':'SMS'" in crm_js
    assert "automatique · META · A3P" in crm_js


def test_admin_can_reset_only_crm_prospect_data(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    c.post("/api/crm/contacts", json={"prenom": "Lina"})
    data = application.load_data()
    data.update({
        "demandes": [{"id": "outside-crm"}],
        "crm_calendly_appointments": [{"id": "rdv-1", "contact_id": "lead-1"}],
        "crm_notifications": [{"id": "notification-1", "contact_id": "lead-1"}],
        "crm_ai_candidate_analyses": {"lead-1": {"score": 80}},
        "crm_cnaps_scoring_snapshots": {"lead-1": {"score": 70}},
        "crm_email_templates": [{"id": "template-1"}],
    })
    application.save_data(data)

    response = c.delete("/api/crm/database")

    assert response.status_code == 200
    assert response.get_json()["deleted_count"] == 1
    saved = application.load_data()
    assert saved["crm_contacts"] == []
    assert saved["crm_calendly_appointments"] == []
    assert saved["crm_notifications"] == []
    assert saved["crm_ai_candidate_analyses"] == {}
    assert saved["crm_cnaps_scoring_snapshots"] == {}
    assert saved["crm_email_templates"] == [{"id": "template-1"}]
    assert saved["demandes"] == [{"id": "outside-crm"}]


def test_non_admin_cannot_reset_crm_database(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    with c.session_transaction() as session:
        session["user_email"] = "cassandre@integraleacademy.com"

    response = c.delete("/api/crm/database")

    assert response.status_code == 403
    assert application.load_data()["crm_contacts"][0]["id"] == contact["id"]


def test_database_reset_button_is_only_rendered_for_admin(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    admin_page = c.get("/crm").data
    assert b'id="adminToolsBtn"' in admin_page
    assert b'id="deleteCrmDatabase"' in admin_page

    for email in (
        "cassandre@integraleacademy.com",
        "aurelie@integraleacademy.com",
        "elsa@integraleacademy.com",
    ):
        with c.session_transaction() as session:
            session["user_email"] = email
        team_page = c.get("/crm").data
        assert b'id="adminToolsBtn"' in team_page
        assert b'id="developmentSupportBtn"' in team_page
        assert b'id="deleteCrmDatabase"' not in team_page


def test_contact_publication_is_signed_and_persisted(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin"}).get_json()

    response = c.post(
        f"/api/crm/contacts/{contact['id']}/publications",
        json={"texte": "Reviens vers nous prochainement"},
    )

    assert response.status_code == 201
    publication = response.get_json()["publication"]
    assert publication["texte"] == "Reviens vers nous prochainement"
    assert publication["author"] == "Clément VAILLANT"
    assert publication["date"]
    saved = c.get(f"/api/crm/contacts/{contact['id']}").get_json()
    assert saved["publications"] == [publication]


def test_empty_contact_publication_is_rejected(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    response = c.post(f"/api/crm/contacts/{contact['id']}/publications", json={"texte": "  "})
    assert response.status_code == 400


def test_publication_social_actions_and_owner_deletion(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    publication = c.post(
        f"/api/crm/contacts/{contact['id']}/publications", json={"texte": "Une actualité"}
    ).get_json()["publication"]

    liked = c.post(f"/api/crm/contacts/{contact['id']}/publications/{publication['id']}/like")
    assert liked.status_code == 200
    assert liked.get_json()["publication"]["likes"] == ["clement@integraleacademy.com"]

    commented = c.post(
        f"/api/crm/contacts/{contact['id']}/publications/{publication['id']}/comments",
        json={"texte": "Merci pour l'information"},
    )
    assert commented.status_code == 201
    comment = commented.get_json()["comment"]
    assert comment["author"] == "Clément VAILLANT"
    assert c.delete(
        f"/api/crm/contacts/{contact['id']}/publications/{publication['id']}/comments/{comment['id']}"
    ).status_code == 204
    assert c.delete(
        f"/api/crm/contacts/{contact['id']}/publications/{publication['id']}"
    ).status_code == 204
    assert c.get(f"/api/crm/contacts/{contact['id']}").get_json()["publications"] == []


def test_news_feed_page_is_available(tmp_path, monkeypatch):
    response = client(tmp_path, monkeypatch).get("/crm/fil-actu")
    assert response.status_code == 200
    assert b"Fil actu" in response.data
    assert b'"team"' in response.data
    assert b'Elsa DUQUESNE' in response.data


def test_news_feed_section_renders_publications_instead_of_contacts():
    crm_js = open(application.app.root_path + "/static/crm.js", encoding="utf-8").read()
    workspace_js = open(application.app.root_path + "/static/crm_workspace.js", encoding="utf-8").read()

    assert "CRMWorkspace.activityPage(workspaceContext(),'publications')" in crm_js
    assert "state.activityTab==='publications'" in workspace_js
    assert "ctx.publicationCard(row.item,row.contact,true)" in workspace_js


def test_information_form_creates_complete_crm_contact_and_activity_log(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application.app.config.update(TESTING=True, SERVER_NAME="localhost")
    public_client = application.app.test_client()

    with (
        patch.object(application, "creer_piste_salesforce"),
        patch.object(application, "send_email_html", return_value=True),
        patch.object(application, "envoyer_sms_demande_infos_formation", return_value=True),
    ):
        response = public_client.post("/demande-informations-formations", data={
            "nom": "Martin", "prenom": "Lina", "mail": "lina@example.com",
            "telephone": "0612345678", "formation": "SSIAP", "centre": "cote_azur",
            "dates": "Du 12 au 27 octobre 2026", "cpf_consulte": "OUI",
            "cpf_montant": "1200", "france_travail": "NON",
            "financement_perso": "OUI", "identite_numerique": "OUI",
            "cnaps_ok": "OUI", "garde_vue": "NON", "titre_sejour": "OUI",
            "ssiap_secourisme_valide": "OUI", "souhaite_devis": "OUI",
            "commentaires_secretariat": "Ne doit pas être copié dans la fiche CRM.",
        })

    assert response.status_code == 302
    contact = application.load_data()["crm_contacts"][0]
    assert contact["statut"] == "Nouveaux"
    assert contact["prenom"] == "Lina"
    assert contact["nom"] == "MARTIN"
    assert contact["mail"] == "lina@example.com"
    assert contact["telephone"] == "0612345678"
    assert contact["formation"] == "SSIAP 1"
    assert contact["lieu"] == "Côte d’Azur"
    assert contact["dates_formation"] == "Du 12 au 27 octobre 2026"
    assert contact["cpf"] == "OUI"
    assert contact["cpf_montant"] == "1200.00"
    assert contact["financement_ft"] == "NON"
    assert contact["garde_vue"] == "NON"
    assert contact["titre_sejour"] == "OUI"
    assert contact["antecedents"] == "NON"
    assert contact["origine"] == "Site internet"
    assert contact["gclid"] == ""
    assert contact["commentaires"] == ""
    assert contact["formulaire"]["cpf_montant"] == "1200"
    assert application._crm_contact_response(contact)["integration_score"]["cpf_amount_eur"] == 1200
    assert contact["source_demande_id"]
    assert contact["source_devis_id"]
    activities = {activity["title"]: activity for activity in contact["activities"]}
    assert "Formulaire de demande d’informations complété" in activities
    assert "Devis détaillé créé" in activities
    assert "Ouvrir le devis" in activities["Devis détaillé créé"]["preview"]
    assert "E-mail automatique envoyé" in activities
    assert "SMS automatique envoyé" in activities
    assert activities["E-mail automatique envoyé"]["preview"]


def test_information_form_attributes_google_ads_and_exposes_gclid(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application.app.config.update(TESTING=True, SERVER_NAME="localhost")
    public_client = application.app.test_client()

    with (
        patch.object(application, "creer_piste_salesforce"),
        patch.object(application, "send_email_html", return_value=True),
        patch.object(application, "envoyer_sms_demande_infos_formation", return_value=True),
    ):
        response = public_client.post("/demande-informations-formations", data={
            "nom": "Durand", "prenom": "Emma", "mail": "emma@example.com",
            "telephone": "0698765432", "formation": "A3P", "centre": "cote_azur",
            "dates": "Du 1er septembre au 27 octobre 2026", "cpf_consulte": "NON",
            "france_travail": "NON", "financement_perso": "OUI",
            "identite_numerique": "NON", "cnaps_ok": "NON", "garde_vue": "NON",
            "titre_sejour": "NON", "souhaite_devis": "OUI",
            "gclid": "  CjwKCA-test_123  ",
        })

    assert response.status_code == 302
    contact = application.load_data()["crm_contacts"][0]
    assert contact["origine"] == "Google Ads"
    assert contact["gclid"] == "CjwKCA-test_123"
    assert contact["formulaire"]["gclid"] == "CjwKCA-test_123"


@pytest.mark.parametrize("identifier_key", ["wbraid", "gbraid"])
def test_information_form_accepts_google_ads_privacy_identifiers(
    identifier_key, tmp_path, monkeypatch
):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application.app.config.update(TESTING=True, SERVER_NAME="localhost")
    public_client = application.app.test_client()

    with (
        patch.object(application, "creer_piste_salesforce"),
        patch.object(application, "send_email_html", return_value=True),
        patch.object(application, "envoyer_sms_demande_infos_formation", return_value=True),
    ):
        response = public_client.post("/demande-informations-formations", data={
            "nom": "Durand", "prenom": "Emma", "mail": "emma@example.com",
            "telephone": "0698765432", "formation": "A3P", "centre": "cote_azur",
            "dates": "Du 1er septembre au 27 octobre 2026", "cpf_consulte": "NON",
            "france_travail": "NON", "financement_perso": "OUI",
            "identite_numerique": "NON", "cnaps_ok": "NON", "garde_vue": "NON",
            "titre_sejour": "NON", "souhaite_devis": "OUI",
            identifier_key: f"{identifier_key}-test-123",
        })

    assert response.status_code == 302
    contact = application.load_data()["crm_contacts"][0]
    assert contact["origine"] == "Google Ads"
    assert contact[identifier_key] == f"{identifier_key}-test-123"
    assert contact["google_ads_identifier"] == f"{identifier_key}-test-123"
    assert contact["google_ads_identifier_type"] == identifier_key.upper()


def test_information_form_recognizes_google_paid_utm_without_click_id():
    fields = {"utm_source": "google", "utm_medium": "cpc"}

    assert application._crm_information_request_origin(fields) == "Google Ads"
    assert application._crm_information_request_google_ads_identifier(fields) == ("", "")


def test_crm_backfills_google_ads_attribution_from_legacy_information_form(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    data = application.load_data()
    data["crm_contacts"] = [{
        "id": "legacy-google-form-lead",
        "source": "demande_infos_formations",
        "prenom": "Emma",
        "nom": "DURAND",
        "origine": "Site internet",
        "formulaire": {"gclid": "legacy-click-id"},
    }]
    application.save_data(data)

    response = test_client.get("/api/crm/contacts/legacy-google-form-lead")

    assert response.status_code == 200
    contact = response.get_json()
    assert contact["origine"] == "Google Ads"
    assert contact["gclid"] == "legacy-click-id"
    stored = application.load_data()["crm_contacts"][0]
    assert stored["origine"] == "Google Ads"
    assert stored["gclid"] == "legacy-click-id"


def test_crm_contact_page_displays_google_ads_gclid():
    crm_js = open(application.app.root_path + "/static/crm.js", encoding="utf-8").read()

    assert "'Google Ads','Google','Site internet'" in crm_js
    assert 'GCLID Google Ads' in crm_js
    assert 'value="${esc(c.gclid)}" readonly' in crm_js
    assert "c.google_ads_identifier||c.gclid||c.wbraid||c.gbraid" in crm_js
    assert "googleAdsIdentifierType" in crm_js
    assert "Non transmis par Google" in crm_js


def test_information_form_recovers_gclid_from_google_ads_attribution_sources():
    form_html = open(
        application.app.root_path + "/templates/demande_informations_formations.html",
        encoding="utf-8",
    ).read()

    assert "'gclid', 'wbraid', 'gbraid'" in form_html
    assert "params.get(name)" in form_html
    assert "document.referrer" in form_html
    assert "cookieValue('_gcl_aw')" in form_html
    assert "integrale_google_ads_attribution_v2" in form_html
    assert "window.setTimeout(syncGoogleAdsAttribution, 1500)" in form_html


def test_crm_backfills_regulatory_answers_from_older_information_form_contacts(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    data = application.load_data()
    data["crm_contacts"] = [{
        "id": "legacy-form-lead",
        "source": "demande_infos_formations",
        "prenom": "Lina",
        "nom": "MARTIN",
        "garde_vue": "",
        "titre_sejour": "",
        "formulaire": {"garde_vue": "NON", "titre_sejour": "OUI"},
    }]
    application.save_data(data)

    response = test_client.get("/api/crm/contacts/legacy-form-lead")

    assert response.status_code == 200
    assert response.get_json()["garde_vue"] == "NON"
    assert response.get_json()["titre_sejour"] == "OUI"


def test_crm_pages_and_templates(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    page = c.get("/CRM/pistes", follow_redirects=True)
    assert page.status_code == 200
    assert b"iaconnectcrm.png" in page.data
    assert b"favicon_32x32.png" in page.data
    assert b'id="manageStatusesTop"' in page.data
    assert application.CRM_ASSET_VERSION.encode() in page.data
    response = c.post("/api/crm/templates", json={"type": "email", "nom": "Bienvenue", "sujet": "Bonjour", "contenu": "<p>Bienvenue</p>"})
    assert response.status_code == 201
    assert c.get("/api/crm/templates").get_json()["email"][0]["nom"] == "Bienvenue"


def test_crm_email_starter_exposes_editable_complete_html(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)

    starter = c.get("/api/crm/templates").get_json()["email_starter"]

    assert starter.lower().startswith("<!doctype html>")
    assert "Faites le premier pas vers votre futur métier" in starter
    assert "Le résumé de notre échange" in starter
    assert "Écrivez ici le contenu de votre e-mail." in starter
    assert "<!-- EMAIL_CONTENT_START -->" in starter
    assert "<!-- EMAIL_CONTENT_END -->" in starter
    assert "Cassandre MENARD" in starter
    assert "cassandre@integraleacademy.com" in starter
    assert "SIREN 840 899 884" in starter
    assert "Autorisation d’exercice CNAPS" in starter
    assert "<!-- EMAIL_HEADER_TITLE_START -->" in starter
    assert "<!-- EMAIL_HEADER_TAGLINE_END -->" in starter
    assert "{{ contenu|safe }}" not in starter


def test_crm_template_library_lists_all_supported_variables():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()
    for variable in ("prenom", "nom", "email", "telephone", "formation", "lieu", "statut", "dates_formation", "prochaines_dates", "date_rdv_du_jour", "heure_rdv_du_jour", "date_heure_rdv_du_jour", "lien_rdv_calendly"):
        assert "{{ " + variable + " }}" in crm_js
    assert "Variables disponibles" in crm_js


def test_crm_unanswered_appointment_variables_use_its_paris_date_and_time(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    paris_now = application.datetime.datetime.now(
        application.pytz.timezone("Europe/Paris")
    )
    appointment_at = (paris_now - application.datetime.timedelta(days=7)).replace(
        hour=14, minute=30, second=0, microsecond=0
    )
    data = application.load_data()
    data["crm_calendly_appointments"] = [{
        "id": "rdv-today",
        "contact_id": contact["id"],
        "status": "active",
        "response_status": "no_answer",
        "response_status_updated_at": paris_now.isoformat(),
        "start_time": appointment_at.astimezone(application.pytz.UTC).isoformat(),
    }]
    application.save_data(data)

    response = c.post(
        f"/api/crm/contacts/{contact['id']}/message-preview",
        json={"contenu": "RDV le {{ date_rdv_du_jour }} à {{ heure_rdv_du_jour }} ({{ date_heure_rdv_du_jour }})"},
    )

    assert response.status_code == 200
    html = response.get_json()["html"]
    assert "à 14h30" in html
    assert f"{appointment_at.day} " in html
    assert "{{ date_rdv_du_jour }}" not in html
    assert "{{ heure_rdv_du_jour }}" not in html


def test_crm_formation_variable_uses_complete_customer_facing_name(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post(
        "/api/crm/contacts", json={"prenom": "Lina", "formation": "A3P"}
    ).get_json()

    response = c.post(
        f"/api/crm/contacts/{contact['id']}/message-preview",
        json={"contenu": "Votre formation {{ formation }}"},
    )

    assert response.status_code == 200
    html = response.get_json()["html"]
    assert "Agent de protection physique des personnes (A3P)" in html
    assert "Votre formation A3P<" not in html


def test_complete_crm_email_html_is_not_wrapped_twice(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    complete_html = "<!doctype html><html><body><h1>Bonjour {{ prenom }}</h1></body></html>"

    response = c.post(
        f"/api/crm/contacts/{contact['id']}/message-preview",
        json={"contenu": complete_html},
    )

    assert response.status_code == 200
    html = response.get_json()["html"]
    assert html == "<!doctype html><html><body><h1>Bonjour Lina</h1></body></html>"
    assert "Faites le premier pas" not in html


def test_crm_templates_can_be_edited_and_deleted(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    created = c.post("/api/crm/templates", json={"type": "sms", "nom": "Rappel", "contenu": "Ancien texte"}).get_json()
    updated = c.patch(f"/api/crm/templates/{created['id']}", json={"nom": "Rappel rendez-vous", "contenu": "Nouveau texte"})
    assert updated.status_code == 200
    assert updated.get_json()["nom"] == "Rappel rendez-vous"
    assert updated.get_json()["contenu"] == "Nouveau texte"
    assert updated.get_json()["updated_at"]
    assert c.patch(f"/api/crm/templates/{created['id']}", json={"nom": " "}).status_code == 400
    assert c.delete(f"/api/crm/templates/{created['id']}").status_code == 204
    assert c.get("/api/crm/templates").get_json()["sms"] == []
    assert c.delete(f"/api/crm/templates/{created['id']}").status_code == 404


def test_message_template_picker_prioritizes_the_contact_formation():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "messageTemplateOptions(list,c.formation)" in crm_js
    assert "includes(needle)" in crm_js
    assert 'value="__other_templates__">Autres modèles…' in crm_js
    assert 'label="Autres modèles"' in crm_js


def test_meta_a3p_templates_are_available_in_manual_message_picker():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    picker = crm_js.split("function messageTemplateOptions", 1)[1].split(
        "function smsPreviewHtml", 1,
    )[0]
    message_modal = crm_js.split("function messageModal", 1)[1].split(
        "function previewModal", 1,
    )[0]
    assert "automatic-meta-a3p-" in picker
    assert 'optgroup label="Messages META A3P"' in picker
    assert "templates.automatic_meta||[]" in message_modal
    assert ".filter(t=>t.type===type)" in message_modal
    assert "c.origine,c.source" in message_modal
    assert "isMeta||isA3p" in message_modal
    assert "template_id:templateSelect.value" in message_modal


def test_meta_a3p_templates_can_be_sent_manually_and_logged(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Nancoro"}).get_json()
    contact = c.patch(f"/api/crm/contacts/{contact['id']}", json={
        "origine": "META", "mail": "nancoro@example.com",
        "telephone": "+33641574512",
    }).get_json()
    automatic_meta = c.get("/api/crm/templates").get_json()["automatic_meta"]
    email = next(template for template in automatic_meta if template["type"] == "email")
    sms = next(template for template in automatic_meta if template["type"] == "sms")
    sent_email, sent_sms = {}, {}
    monkeypatch.setattr(
        application, "send_email_html",
        lambda to, subject, plain, html: sent_email.update(
            to=to, subject=subject, plain=plain, html=html,
        ) or True,
    )
    monkeypatch.setattr(
        application, "send_sms",
        lambda to, body: sent_sms.update(to=to, body=body) or True,
    )

    email_response = c.post(f"/api/crm/contacts/{contact['id']}/message", json={
        "type": "email", "template_id": email["id"],
        "sujet": email["sujet"], "contenu": email["contenu"],
    })
    sms_response = c.post(f"/api/crm/contacts/{contact['id']}/message", json={
        "type": "sms", "template_id": sms["id"], "contenu": sms["contenu"],
    })

    assert email_response.status_code == 200
    assert sms_response.status_code == 200
    assert sent_email["to"] == "nancoro@example.com"
    assert "Bonjour Nancoro" in sent_email["html"]
    assert "{{ prenom }}" not in sent_email["html"]
    assert sent_sms == {"to": "+33641574512", "body": sms["contenu"]}
    titles = {activity["title"] for activity in sms_response.get_json()["activities"]}
    assert titles >= {
        "E-mail META A3P envoyé manuellement",
        "SMS META A3P envoyé manuellement",
    }


def test_message_modal_does_not_rely_on_named_window_properties():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    message_modal = crm_js.split("function messageModal", 1)[1].split("function previewModal", 1)[0]
    for selector in ("#generateMessage", "#tpl", "#msg", "#subject", "#messagePreview", "#sendMessage"):
        assert f"document.querySelector('{selector}')" in message_modal
    assert "messagePreview.onclick" not in message_modal


def test_crm_email_preview_uses_the_sent_mail_wrapper(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()

    response = c.post(
        f"/api/crm/contacts/{contact['id']}/message-preview",
        json={"contenu": "<p>Mon texte libre</p>"},
    )

    assert response.status_code == 200
    html = response.get_json()["html"]
    assert "Bonjour <strong>Lina</strong>" in html
    assert "Mon texte libre" in html
    assert "Faites le premier pas vers votre futur métier" in html
    assert "integraleacademy.com" in html


def test_crm_plain_text_email_preserves_line_breaks_in_preview_and_delivery(
        tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "mail": "lina@example.com"},
    ).get_json()
    content = (
        "Première ligne\r\nDeuxième ligne\r\n\r\n"
        "Nouveau paragraphe : 2 < 3 & 5 > 4"
    )
    sent = {}
    monkeypatch.setattr(
        application,
        "send_email_html",
        lambda to, subject, plain, html: sent.update(
            to=to, subject=subject, plain=plain, html=html,
        ) or True,
    )

    preview = c.post(
        f"/api/crm/contacts/{contact['id']}/message-preview",
        json={"type": "email", "sujet": "Suivi", "contenu": content},
    )
    delivery = c.post(
        f"/api/crm/contacts/{contact['id']}/message",
        json={"type": "email", "sujet": "Suivi", "contenu": content},
    )

    assert preview.status_code == 200
    assert delivery.status_code == 200
    html = preview.get_json()["html"]
    assert "Première ligne<br>Deuxième ligne" in html
    assert "Nouveau paragraphe : 2 &lt; 3 &amp; 5 &gt; 4" in html
    assert html.count('<p style="margin:0 0 16px">') == 2
    assert sent["html"] == html



def test_grouped_preview_resolves_email_subject_and_sms_without_side_effects(
    tmp_path, monkeypatch
):
    c = client(tmp_path, monkeypatch)
    contact = c.post(
        "/api/crm/contacts",
        json={
            "prenom": "Lina",
            "nom": "Martin",
            "formation": "A3P",
            "mail": "lina@example.com",
            "telephone": "0612345678",
        },
    ).get_json()
    template = c.post(
        "/api/crm/templates",
        json={
            "type": "sms",
            "nom": "Relance groupée",
            "contenu": "Bonjour {{ prenom }}, formation {{ formation }}.",
        },
    ).get_json()
    unexpected_delivery = lambda *args, **kwargs: pytest.fail(
        "La prévisualisation ne doit appeler aucun fournisseur d’envoi"
    )
    monkeypatch.setattr(application, "send_email_html", unexpected_delivery)
    monkeypatch.setattr(application, "send_sms", unexpected_delivery)
    before = application.load_data()
    before_contact = application._crm_contact(before, contact["id"])
    before_activities = list(before_contact.get("activities", []))
    before_templates = list(before.get("crm_sms_templates", []))

    email = c.post(
        f"/api/crm/contacts/{contact['id']}/message-preview",
        json={
            "type": "email",
            "sujet": "Dossier de {{ prenom }} — {{ formation }}",
            "contenu": "<p>Bonjour {{ prenom }} {{ nom }}</p>",
        },
    )
    sms = c.post(
        f"/api/crm/contacts/{contact['id']}/message-preview",
        json={
            "type": "sms",
            "contenu": template["contenu"],
        },
    )

    assert email.status_code == 200
    email_preview = email.get_json()
    assert email_preview["type"] == "email"
    assert email_preview["sujet"] == (
        "Dossier de Lina — Agent de protection physique des personnes (A3P)"
    )
    assert email_preview["contenu"] == "<p>Bonjour Lina MARTIN</p>"
    assert "Bonjour <strong>Lina</strong>" in email_preview["html"]
    assert "{{ prenom }}" not in email_preview["html"]
    assert sms.status_code == 200
    sms_preview = sms.get_json()
    assert sms_preview == {
        "type": "sms",
        "sujet": "",
        "contenu": (
            "Bonjour Lina, formation "
            "Agent de protection physique des personnes (A3P)."
        ),
    }
    assert c.post(
        f"/api/crm/contacts/{contact['id']}/message-preview",
        json={"type": "push", "contenu": "Test"},
    ).status_code == 400

    after = application.load_data()
    after_contact = application._crm_contact(after, contact["id"])
    assert after_contact.get("activities", []) == before_activities
    assert after.get("crm_sms_templates", []) == before_templates

def test_crm_email_dynamic_dates_come_from_upcoming_admin_sessions(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    data = application.load_data()
    data["formation_sessions"] = {"cote_azur": {"APS": [
        {"label": "Du 1 janvier au 2 janvier 2020", "badge": ""},
        {"label": "Du 3 septembre au 30 septembre 2099 - examen le 1 octobre 2099", "badge": ""},
    ]}}
    application.save_data(data)
    contact = c.post("/api/crm/contacts", json={
        "prenom": "Lina", "formation": "APS", "lieu": "Côte d’Azur",
    }).get_json()

    response = c.post(
        f"/api/crm/contacts/{contact['id']}/message-preview",
        json={"contenu": "<p>Nos prochaines dates :</p>{{prochaines_dates}}"},
    )

    assert response.status_code == 200
    html = response.get_json()["html"]
    assert "3 septembre au 30 septembre 2099" in html
    assert "1 janvier au 2 janvier 2020" not in html
    assert "{{prochaines_dates}}" not in html


@pytest.mark.parametrize(("formation", "expected_url"), [
    ("APS", "https://calendly.com/integraleacademy/aps"),
    ("A3P", "https://calendly.com/integraleacademy/apr"),
    ("SSIAP 1", "https://calendly.com/integraleacademy/ssiap1"),
    ("DESP", "https://calendly.com/integraleacademy/dirigeant"),
    ("Chauffeur VTC", "https://calendly.com/integraleacademy/chauffeurvtc"),
    ("Formation personnalisée", "https://calendly.com/integraleacademy/formation"),
])
def test_crm_calendly_variable_matches_contact_training(
    tmp_path, monkeypatch, formation, expected_url
):
    c = client(tmp_path, monkeypatch)
    contact = c.post(
        "/api/crm/contacts", json={"prenom": "Lina", "formation": formation}
    ).get_json()

    response = c.post(
        f"/api/crm/contacts/{contact['id']}/message-preview",
        json={"contenu": "<a href=\"{{ lien_rdv_calendly }}\">Prendre rendez-vous</a>"},
    )

    assert response.status_code == 200
    html = response.get_json()["html"]
    assert f'href="{expected_url}"' in html
    assert "{{ lien_rdv_calendly }}" not in html


def test_crm_can_send_a_template_test_email(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    sent = {}
    monkeypatch.setattr(application, "send_email_html", lambda to, subject, plain, html: sent.update(to=to, subject=subject, plain=plain, html=html) or True)

    response = c.post("/api/crm/test-email", json={
        "destinataire": "equipe@example.com",
        "sujet": "Aperçu formation",
        "contenu": "<h1>Bonjour</h1><p>Voici le programme.</p>",
    })

    assert response.status_code == 200
    assert sent == {"to": "equipe@example.com", "subject": "Aperçu formation", "plain": "Bonjour Voici le programme.", "html": "<h1>Bonjour</h1><p>Voici le programme.</p>"}
    assert c.post("/api/crm/test-email", json={"destinataire": "invalide", "contenu": "test"}).status_code == 400


def test_crm_can_send_a_template_test_sms(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    sent = {}
    monkeypatch.setattr(
        application,
        "send_sms",
        lambda to, body: sent.update(to=to, body=body) or True,
    )

    response = c.post("/api/crm/test-sms", json={
        "destinataire": "06 12 34 56 78",
        "contenu": "Bonjour, voici votre confirmation.",
    })

    assert response.status_code == 200
    assert sent == {"to": "06 12 34 56 78", "body": "Bonjour, voici votre confirmation."}
    assert c.post("/api/crm/test-sms", json={"destinataire": "123", "contenu": "test"}).status_code == 400
    assert c.post("/api/crm/test-sms", json={"destinataire": "0612345678", "contenu": "  "}).status_code == 400


def test_sms_templates_can_be_previewed_and_sent_as_tests_from_the_library():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert 'data-preview-type="${type}"' in crm_js
    assert "if(type==='sms')previewModal(smsPreviewHtml(t.contenu)" in crm_js
    assert 'id="sendTestSms"' in crm_js
    assert "api('/api/crm/test-sms'" in crm_js
    assert "À quel numéro envoyer ce SMS de test ?" in crm_js


def test_email_template_preview_does_not_render_the_subject_in_the_message_body():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "previewModal(t.contenu,true,{sujet:t.sujet,contenu:t.contenu})" in crm_js
    assert '<h3>${esc(t.sujet)}</h3>' not in crm_js


def test_starter_email_has_a_simple_content_editor():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert 'for="tplContent">Contenu du mail' in crm_js
    assert "la mise en page du modèle est conservée automatiquement" in crm_js
    assert "Modifier le code HTML (avancé)" in crm_js
    assert "emailContentToHtml(tplContent.value)" in crm_js
    assert "Header du mail" in crm_js
    assert 'for="tplHeaderTitle">Titre' in crm_js
    assert 'for="tplHeaderSubtitle">Sous-titre' in crm_js
    assert 'for="tplHeaderTagline">Accroche' in crm_js
    assert "EMAIL_HEADER_TITLE_START" in crm_js


def test_crm_pipeline_statuses_can_be_added_renamed_and_deleted(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()

    added = c.post("/api/crm/statuses", json={"label": "Dossier incomplet"})
    assert added.status_code == 201
    assert "Dossier incomplet" in added.get_json()

    renamed = c.patch("/api/crm/statuses/Nouveaux", json={"label": "À qualifier"})
    assert renamed.status_code == 200
    assert c.get(f"/api/crm/contacts/{contact['id']}").get_json()["statut"] == "À qualifier"

    deleted = c.delete("/api/crm/statuses/%C3%80%20qualifier")
    assert deleted.status_code == 200
    assert "À qualifier" not in deleted.get_json()["statuses"]
    assert c.get(f"/api/crm/contacts/{contact['id']}").get_json()["statut"] == "Blocage"

    assert c.patch("/api/crm/statuses/Converti", json={"label": "Gagné"}).status_code == 400


def test_relances_page_uses_a_daily_calendar_view(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)

    page = c.get("/CRM/relances", follow_redirects=True)
    assert page.status_code == 200

    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()
    assert "function remindersPage()" in crm_js
    assert 'id="reminderDate" type="date"' in crm_js
    assert "c.relance_date===reminderSelectedDate" in crm_js
    assert "changeReminderDate(-1)" in crm_js
    assert "changeReminderDate(1)" in crm_js
    assert "Aucune relance ce jour-là" in crm_js
    assert 'id="reminderShowAll"' in crm_js
    assert "Voir toutes les relances" in crm_js
    assert "all.reduce((dates,c)" in crm_js

    # CRMWorkspace remplace la vue historique lorsque la page est chargée :
    # il doit donc conserver lui aussi la navigation quotidienne.
    with open(
        application.app.root_path + "/static/crm_workspace.js", encoding="utf-8"
    ) as source:
        workspace_js = source.read()
    assert "function remindersPage(ctx)" in workspace_js
    assert 'id="workspaceReminderDate" type="date"' in workspace_js
    assert (
        "function reminderPeriodMatches(contact,mode,selectedDate)"
        "{const date=reminderDate(contact)" in workspace_js
    )
    assert "selectedDate=today" in workspace_js
    assert "moveDate(-1)" in workspace_js
    assert "moveDate(1)" in workspace_js
    assert "Voir toutes les relances" in workspace_js
    assert "Aucune relance prévue le" in workspace_js


def test_planning_a_reminder_waits_for_server_persistence_before_updating_contact(tmp_path, monkeypatch):
    client(tmp_path, monkeypatch)

    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    handler_start = crm_js.index("function relaunchModal")
    api_update = "api(`/api/crm/contacts/${id}`"
    confirmed_update = "mergeContactInStore(id,updated);closeModal();showContact(id,options.returnTab||'contactInfoTab')"
    optimistic_update = "Object.assign(c,next);mergeContactInStore(id,next);closeModal();showContact(id)"
    assert api_update in crm_js
    assert confirmed_update in crm_js
    assert crm_js.index(api_update, handler_start) < crm_js.index(confirmed_update, handler_start)
    assert optimistic_update not in crm_js
    assert "saveRelaunch.textContent='Enregistrement…'" in crm_js
    assert "saveRelaunch.disabled=false" in crm_js


def test_relance_motif_is_saved_normalized_updated_and_validated(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post(
        "/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin"},
    ).get_json()

    planned = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={
            "statut": "A relancer",
            "relance_date": "2099-09-03",
            "relance_motif": "  Dossier   de financement à compléter  ",
        },
    )

    assert planned.status_code == 200
    saved = planned.get_json()
    assert saved["relances"][0]["motif"] == "Dossier de financement à compléter"
    assert any(
        "Motif : Dossier de financement à compléter" in item.get("detail", "")
        for item in saved["activities"]
    )

    updated = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={
            "relance_date": "2099-09-03",
            "relance_motif": "Rappeler après réception des pièces",
        },
    )
    assert updated.status_code == 200
    relances = [
        item for item in updated.get_json()["relances"]
        if item.get("status") == "scheduled"
    ]
    assert len(relances) == 1
    assert relances[0]["motif"] == "Rappeler après réception des pièces"

    without_motif = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={
            "relance_date": "2099-09-03",
            "relance_motif": "",
        },
    )
    assert without_motif.status_code == 200
    relances = [
        item for item in without_motif.get_json()["relances"]
        if item.get("status") == "scheduled"
    ]
    assert len(relances) == 1
    assert "motif" not in relances[0]

    too_long = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={
            "relance_date": "2099-09-04",
            "relance_motif": "x" * 161,
        },
    )
    assert too_long.status_code == 400
    assert "160 caractères" in too_long.get_json()["error"]


def test_legacy_relance_without_motif_remains_compatible(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    data = application.load_data()
    stored = next(item for item in data["crm_contacts"] if item["id"] == contact["id"])
    stored["relance_date"] = "2099-09-03"
    stored["relances"] = [{
        "id": "legacy-follow-up",
        "scheduled_date": "2099-09-03",
        "status": "scheduled",
        "source": "legacy",
    }]
    application.save_data(data)

    response = c.get(f"/api/crm/contacts/{contact['id']}")

    assert response.status_code == 200
    assert response.get_json()["relances"][0].get("motif") is None


def test_relance_no_answer_reprograms_and_sends_named_templates_only_once(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    deliveries = {"sms": [], "email": []}
    monkeypatch.setattr(
        application,
        "send_sms",
        lambda phone, body: deliveries["sms"].append((phone, body)) or True,
    )
    monkeypatch.setattr(
        application,
        "send_email_html",
        lambda mail, subject, plain, html: deliveries["email"].append(
            (mail, subject, plain, html)
        ) or True,
    )
    contact = c.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin", "formation": "APS"},
    ).get_json()
    c.post(
        "/api/crm/templates",
        json={
            "type": "sms",
            "nom": "Pas de réponse relance",
            "contenu": "Bonjour {{ prenom }}, nous avons tenté de vous joindre.",
        },
    )
    c.post(
        "/api/crm/templates",
        json={
            "type": "email",
            "nom": "Pas de réponse relance",
            "sujet": "Votre relance {{ formation }}",
            "contenu": "<p>Bonjour {{ prenom }}, nous avons tenté de vous joindre.</p>",
        },
    )
    planned = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={
            "telephone": "+33612345678",
            "mail": "lina@example.com",
            "statut": "A relancer",
            "relance_date": "2026-08-13",
        },
    ).get_json()
    first_relance = planned["relances"][0]
    assert first_relance["status"] == "scheduled"

    response = c.post(
        f"/api/crm/contacts/{contact['id']}/relances/{first_relance['id']}/sans-reponse",
        json={"next_date": "2026-08-15"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["delivery"] == {"email": True, "sms": True}
    assert payload["relance"]["status"] == "no_answer"
    assert payload["relance"]["message_template"] == "Pas de réponse relance"
    assert payload["next_relance"]["status"] == "scheduled"
    assert payload["contact"]["relance_date"] == "2026-08-15"
    assert len(deliveries["sms"]) == len(deliveries["email"]) == 1
    assert "Lina" in deliveries["sms"][0][1]
    assert "Agent de prévention et de sécurité" in deliveries["email"][0][1]

    duplicate = c.post(
        f"/api/crm/contacts/{contact['id']}/relances/{first_relance['id']}/sans-reponse",
        json={"next_date": "2026-08-16"},
    )
    assert duplicate.status_code == 200
    assert duplicate.get_json()["duplicate"] is True
    assert duplicate.get_json()["contact"]["relance_date"] == "2026-08-15"
    assert len(deliveries["sms"]) == len(deliveries["email"]) == 1


def test_answered_relance_requires_and_records_a_call_once(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin", "formation": "APS"},
    ).get_json()
    planned = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"statut": "A relancer", "relance_date": "2026-08-14"},
    ).get_json()
    relance = planned["relances"][0]

    missing_note = c.post(
        f"/api/crm/contacts/{contact['id']}/appel",
        json={"commentaire": "", "relance_id": relance["id"]},
    )
    assert missing_note.status_code == 400

    answered = c.post(
        f"/api/crm/contacts/{contact['id']}/appel",
        json={"commentaire": "La candidate a répondu et souhaite recevoir le devis.", "relance_id": relance["id"]},
    )
    assert answered.status_code == 200
    payload = answered.get_json()
    assert payload["relance"]["status"] == "answered"
    assert payload["contact"]["relance_date"] == ""
    assert any(
        activity["kind"] == "appel"
        and activity["detail"] == "La candidate a répondu et souhaite recevoir le devis."
        for activity in payload["contact"]["activities"]
    )

    activity_count = len(payload["contact"]["activities"])
    duplicate = c.post(
        f"/api/crm/contacts/{contact['id']}/appel",
        json={"commentaire": "Double clic", "relance_id": relance["id"]},
    ).get_json()
    assert duplicate["duplicate"] is True
    assert len(duplicate["contact"]["activities"]) == activity_count


def test_legacy_relance_date_is_migrated_without_being_duplicated(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    data = application.load_data()
    stored = next(item for item in data["crm_contacts"] if item["id"] == contact["id"])
    stored["relances"] = []
    stored["relance_date"] = "2026-08-20"
    application.save_data(data)

    first = c.get("/api/crm/contacts").get_json()[0]
    second = c.get("/api/crm/contacts").get_json()[0]

    assert first["relance_date"] == "2026-08-20"
    assert len(first["relances"]) == 1
    assert len(second["relances"]) == 1
    assert second["relances"][0]["source"] == "legacy"


def test_scheduling_a_relance_without_active_appointment_repairs_pipeline_status(
        tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"statut": "RDV programmé"},
    )

    response = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"relance_date": "2099-09-03"},
    )

    assert response.status_code == 200
    updated = response.get_json()
    assert updated["statut"] == "A relancer"
    assert updated["relance_date"] == "2099-09-03"
    assert updated["relances"][0]["status"] == "scheduled"
    assert updated["activities"][0]["title"] == "Statut : A relancer"
    assert updated["activities"][0]["detail"] == "Ancien statut : En cours"


def test_calendly_appointment_eligibility_uses_the_paris_calendar_day():
    paris = application.pytz.timezone("Europe/Paris")
    now = paris.localize(application.datetime.datetime(2026, 8, 23, 12, 0))

    assert application._crm_calendly_appointment_is_today_or_future(
        {"status": "active", "start_time": "2026-08-23T09:00:00+02:00"},
        now,
    )
    assert not application._crm_calendly_appointment_is_today_or_future(
        {"status": "active", "start_time": "2026-08-22T23:59:59+02:00"},
        now,
    )
    assert not application._crm_calendly_appointment_is_today_or_future(
        {"status": "canceled", "start_time": "2026-08-24T09:00:00+02:00"},
        now,
    )
    assert not application._crm_calendly_appointment_is_today_or_future(
        {"status": "active", "start_time": ""},
        now,
    )


def test_programmed_status_keeps_an_appointment_earlier_today_and_repairs_yesterday():
    paris = application.pytz.timezone("Europe/Paris")
    now = paris.localize(application.datetime.datetime(2026, 8, 23, 12, 0))
    contact = {"id": "contact-1", "statut": "RDV programmé"}

    today = {"crm_calendly_appointments": [{
        "contact_id": "contact-1",
        "status": "active",
        "start_time": "2026-08-23T09:00:00+02:00",
    }]}
    assert not application._crm_sync_contact_calendly_status(today, contact, now)
    assert contact["statut"] == "RDV programmé"

    yesterday = {"crm_calendly_appointments": [{
        "contact_id": "contact-1",
        "status": "active",
        "start_time": "2026-08-22T18:00:00+02:00",
    }]}
    assert application._crm_sync_contact_calendly_status(yesterday, contact, now)
    assert contact["statut"] == "En cours"


def test_contact_detail_persists_stale_programmed_status_as_in_progress(
        tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "statut": "RDV programmé"},
    ).get_json()
    data = application.load_data()
    stored = next(row for row in data["crm_contacts"] if row["id"] == contact["id"])
    stored["statut"] = "RDV programmé"
    stored["relance_date"] = ""
    stored["relances"] = []
    data["crm_calendly_appointments"] = [{
        "id": "past-rdv",
        "contact_id": contact["id"],
        "status": "active",
        "start_time": "2020-09-02T10:00:00+02:00",
        "end_time": "2020-09-02T10:30:00+02:00",
    }]
    application.save_data(data)

    prepared = c.get(f"/api/crm/contacts/{contact['id']}")

    assert prepared.status_code == 200
    assert prepared.get_json()["statut"] == "En cours"
    persisted = application.load_data()
    repaired = next(row for row in persisted["crm_contacts"] if row["id"] == contact["id"])
    assert repaired["statut"] == "En cours"


def test_active_calendly_appointment_keeps_programmed_status_with_a_relance(
        tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "statut": "En cours"},
    ).get_json()
    data = application.load_data()
    data["crm_calendly_appointments"] = [{
        "id": "future-rdv",
        "contact_id": contact["id"],
        "status": "active",
        "start_time": "2099-09-02T10:00:00+02:00",
        "end_time": "2099-09-02T10:30:00+02:00",
    }]
    application.save_data(data)

    response = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"relance_date": "2099-09-03"},
    )

    assert response.status_code == 200
    assert response.get_json()["statut"] == "RDV programmé"


@pytest.mark.parametrize("appointment", [
    {
        "id": "past-rdv", "status": "active",
        "start_time": "2020-09-02T10:00:00+02:00",
        "end_time": "2020-09-02T10:30:00+02:00",
    },
    {
        "id": "cancelled-rdv", "status": "canceled",
        "start_time": "2099-09-02T10:00:00+02:00",
        "end_time": "2099-09-02T10:30:00+02:00",
    },
    {"id": "undated-rdv", "status": "active", "start_time": ""},
])
def test_non_active_appointment_does_not_hide_a_scheduled_relance(
        tmp_path, monkeypatch, appointment):
    c = client(tmp_path, monkeypatch)
    contact = c.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "statut": "RDV programmé"},
    ).get_json()
    data = application.load_data()
    stored = next(row for row in data["crm_contacts"] if row["id"] == contact["id"])
    stored["statut"] = "RDV programmé"
    stored["relance_date"] = "2099-09-03"
    stored["relances"] = []
    data["crm_calendly_appointments"] = [{**appointment, "contact_id": contact["id"]}]
    application.save_data(data)

    prepared = c.get(f"/api/crm/contacts/{contact['id']}")

    assert prepared.status_code == 200
    assert prepared.get_json()["statut"] == "A relancer"
    persisted = application.load_data()
    repaired = next(row for row in persisted["crm_contacts"] if row["id"] == contact["id"])
    assert repaired["statut"] == "A relancer"
    assert repaired["relance_date"] == "2099-09-03"


@pytest.mark.parametrize("final_status", ["Disqualifié", "Converti"])
def test_open_relance_never_overwrites_a_final_pipeline_status(
        tmp_path, monkeypatch, final_status):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"statut": final_status},
    )

    response = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"relance_date": "2099-09-03"},
    )

    assert response.status_code == 200
    assert response.get_json()["statut"] == final_status


def test_crm_uses_admin_formation_sessions(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    data = application.load_data()
    data["formation_sessions"] = {
        "paris": {"APS": [{"label": "Du 1 au 5 septembre 2026", "badge": "", "date_examen": "2026-09-05"}]}
    }
    application.save_data(data)

    sessions = c.get("/api/formation-sessions").get_json()
    assert sessions["paris"]["APS"][0]["label"] == "Du 1 au 5 septembre 2026"

    javascript = (application.app.root_path + "/static/crm.js")
    with open(javascript, encoding="utf-8") as source:
        crm_js = source.read()
    assert "Programmer un rappel" in crm_js
    assert "formationSessions=snapshot.formation_sessions||{}" in crm_js
    assert "<h3>${crmIcon('book')}<span>Formation</span></h3>" in crm_js
    assert "<h3>${crmIcon('shield')}<span>Réglementaire</span></h3>" in crm_js
    assert "<h3>${crmIcon('wallet')}<span>Financement</span></h3>" in crm_js


def test_contact_activity_log_and_vae_tracking_are_displayed_in_tabs():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    tabs = crm_js.index('role="tablist" aria-label="Sections de la fiche"')
    information = crm_js.index('id="contactInfoTab"', tabs)
    wedof = crm_js.index('id="contactWedofTab"', tabs)
    vae = crm_js.index('id="contactVaeTab"', tabs)
    activity = crm_js.index('id="contactActivityTab"', tabs)
    relance = crm_js.index('id="contactRelanceTab"', tabs)

    assert information < activity < wedof < vae < relance
    assert 'id="contactVaePanel" role="tabpanel" aria-labelledby="contactVaeTab" hidden' in crm_js
    assert 'id="contactActivityPanel" role="tabpanel" aria-labelledby="contactActivityTab" hidden' in crm_js
    assert 'id="contactRelancePanel" role="tabpanel" aria-labelledby="contactRelanceTab" hidden' in crm_js
    assert crm_js.index('id="vaeTrackingPanel"') > crm_js.index('id="contactVaePanel"')
    assert crm_js.index('id="activityFeed"') > crm_js.index('id="contactActivityPanel"')
    assert crm_js.index('${relanceTracking(c)}') > crm_js.index('id="contactRelancePanel"')


def test_crm_rephrase_uses_chat_completion(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    create = lambda **kwargs: SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content="Compte-rendu reformulé."),
    )])
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(application, "OpenAI", lambda **kwargs: fake_client)

    response = c.post("/api/crm/reformuler", json={"texte": "note brute"})

    assert response.status_code == 200
    assert response.get_json() == {"texte": "Compte-rendu reformulé."}


def test_crm_rephrase_has_a_strict_voice_correction_mode(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = {}

    def fake_ai(system_prompt, user_prompt, max_tokens=500):
        captured.update(system=system_prompt, user=user_prompt)
        return "Monsieur part à la retraite le 1er octobre."

    monkeypatch.setattr(application, "_crm_ai", fake_ai)

    response = c.post("/api/crm/reformuler", json={
        "texte": "Monsieur par la retraite le 1er octobre",
        "mode": "correction_dictee",
    })

    assert response.status_code == 200
    assert response.get_json() == {
        "texte": "Monsieur part à la retraite le 1er octobre.",
    }
    assert captured["user"] == "Monsieur par la retraite le 1er octobre"
    for protected_fact in ("faits", "noms", "dates", "montants", "acronymes"):
        assert protected_fact in captured["system"]
    assert "N’ajoute aucune information" in captured["system"]


def test_france_travail_request_generator_uses_explicit_profile_facts(
        tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={
        "prenom": "Mehdy", "nom": "Moumen", "formation": "A3P",
        "lieu": "Côte d’Azur", "dates_formation": "Du 1er septembre au 27 octobre 2026",
    }).get_json()
    contact = c.patch(f"/api/crm/contacts/{contact['id']}", json={
        "cpf": "OUI", "cpf_montant": "1800", "financement_ft": "OUI",
        "carte_pro": "OUI",
    }).get_json()
    captured = {}

    def fake_ai(system, user, max_tokens=500):
        captured.update(system=system, user=user, max_tokens=max_tokens)
        return "Bonjour,\n\nJe sollicite le financement de ma formation.\n\nBien cordialement,\nMEHDY MOUMEN"

    monkeypatch.setattr(application, "_crm_ai", fake_ai)
    response = c.post(
        f"/api/crm/contacts/{contact['id']}/generer-demande-ft",
        json={
            "ancien_militaire": True,
            "carte_professionnelle": True,
            "experience_securite": True,
            "reconversion": False,
            "permis_b_mobilite": True,
            "perspectives_embauche": True,
            "parcours": "Ancien militaire et trois ans dans la sécurité privée.",
            "projet_professionnel": "Exercer dans la protection rapprochée.",
            "choix_formation_centre": "Centre recommandé par des professionnels.",
            "perspectives_emploi": "Deux entreprises ont accepté un entretien après la formation.",
            "motivation": "Disponible immédiatement et déterminé à réussir.",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["texte"].startswith("Bonjour,")
    context = json.loads(captured["user"])["informations_factuelles_autorisees"]
    assert context["candidat"]["nom_complet"] == "Mehdy MOUMEN"
    assert context["formation"]["nom"] == "Agent de protection physique des personnes (A3P)"
    assert context["formation"]["centre"] == "Intégrale Sécurité Formations à Puget-sur-Argens (Var)"
    assert context["profil"]["ancien_militaire"] is True
    assert context["profil"]["carte_professionnelle_cnaps"] is True
    assert context["profil"]["en_reconversion_professionnelle"] is False
    assert context["profil"]["parcours_et_experience"].startswith("Ancien militaire")
    assert context["financement"]["montant_cpf_disponible_euros"] == "1800.00"
    assert "N’invente aucun fait" in captured["system"]
    assert "première personne" in captured["system"]
    assert captured["max_tokens"] == 1100


def test_france_travail_request_generator_is_conditional_and_copyable():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        javascript = source.read()
    with open(application.app.root_path + "/static/crm.css", encoding="utf-8") as source:
        stylesheet = source.read()

    assert 'id="ftRequestGeneratorButton"' in javascript
    assert 'data-show="ft-yes"' in javascript
    assert "franceTravailRequestModal(c)" in javascript
    assert "generer-demande-ft" in javascript
    assert 'id="ftFormerMilitary"' in javascript
    assert 'id="ftProfessionalCard"' in javascript
    assert 'id="copyFtRequest"' in javascript
    assert "copyContactCoordinate(output.value)" in javascript
    assert ".ft-request-modal" in stylesheet
    assert ".ft-request-modal{display:flex;flex-direction:column" in stylesheet
    assert ".ft-request-modal .modal-body{min-height:0;overflow-y:auto" in stylesheet


def test_crm_ai_summary_and_message_generation(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={
        "prenom": "Lina", "nom": "Martin", "formation": "APS"
    }).get_json()
    prompts = []

    def fake_ai(system, user, max_tokens=500):
        prompts.append((system, user, max_tokens))
        return "Texte généré."

    monkeypatch.setattr(application, "_crm_ai", fake_ai)

    summary = c.post(f"/api/crm/contacts/{contact['id']}/synthese", json={})
    message = c.post(f"/api/crm/contacts/{contact['id']}/generer-message", json={
        "type": "sms", "instructions": "Confirmer le prochain rendez-vous"
    })

    assert summary.get_json() == {"texte": "Texte généré."}
    assert message.get_json() == {"texte": "Texte généré."}
    assert "6 à 10 phrases" in prompts[0][0]
    assert "prochain rendez-vous prévu" in prompts[0][0]
    assert "sérieux" in prompts[0][0]
    assert "320 caractères maximum" in prompts[1][0]
    assert "Confirmer le prochain rendez-vous" in prompts[1][1]


def test_crm_normalizes_contact_names_on_create_update_and_existing_data(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={
        "prenom": "  jEAN-pIERRE ", "nom": " de la tour "
    }).get_json()
    assert contact["prenom"] == "Jean-Pierre"
    assert contact["nom"] == "DE LA TOUR"

    updated = c.patch(f"/api/crm/contacts/{contact['id']}", json={
        "prenom": "mARIE cLAIRE", "nom": "d'angelo"
    }).get_json()
    assert updated["prenom"] == "Marie Claire"
    assert updated["nom"] == "D'ANGELO"

    data = application.load_data()
    data["crm_contacts"][0]["prenom"] = "lINA"
    data["crm_contacts"][0]["nom"] = "martin"
    application.save_data(data)
    existing = c.get("/api/crm/contacts").get_json()[0]
    assert existing["prenom"] == "Lina"
    assert existing["nom"] == "MARTIN"


def test_crm_email_has_branding_and_legal_footer(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    created = c.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin"}).get_json()
    c.patch(f"/api/crm/contacts/{created['id']}", json={"mail": "lina@example.com"})
    captured = {}

    def fake_send(recipient, subject, plain_text, html_body):
        captured["html"] = html_body
        return True

    monkeypatch.setattr(application, "send_email_html", fake_send)
    response = c.post(
        f"/api/crm/contacts/{created['id']}/message",
        json={"type": "email", "sujet": "Bienvenue", "contenu": "<p>Message</p>"},
    )

    assert response.status_code == 200
    assert "Logo_Integrale_Academy_officielpdf" in captured["html"]
    assert "Faites le premier pas vers votre futur métier" in captured["html"]
    assert "SIREN 840 899 884" in captured["html"]
    assert "Votre avenir, notre engagement" not in captured["html"]


def test_crm_conversion_prefills_remote_registration_before_changing_status(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin", "formation": "APS"}).get_json()
    c.patch(f"/api/crm/contacts/{contact['id']}", json={
        "mail": "lina@example.com", "telephone": "0600000000", "lieu": "Paris",
        "dates_formation": "Du 1 au 5 septembre 2026", "desp_type": "Initial",
        "commentaires": "Financement validé.",
    })
    monkeypatch.setenv("GESTION_STAGIAIRES_API_URL", "https://gestion.example/api/preremplissage")
    monkeypatch.setenv("GESTION_STAGIAIRES_API_TOKEN", "secret")
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return SimpleNamespace(status_code=201, content=b'{}', json=lambda: {
            "url": "https://gestion.example/inscriptions/nouveau?jeton=temporary",
        })

    monkeypatch.setattr(application.requests, "post", fake_post)
    response = c.post(f"/api/crm/contacts/{contact['id']}/convertir")

    assert response.status_code == 200
    result = response.get_json()
    converted = result["contact"]
    assert converted["statut"] == "Converti"
    assert result["url"] == "https://gestion.example/inscriptions/nouveau?jeton=temporary"
    assert converted["activities"][0]["kind"] == "conversion"
    assert converted["activities"][0]["title"] == "Dossier d’inscription ouvert dans Gestion stagiaires"
    assert "gestion_stagiaire_id" not in converted
    assert captured["json"]["email"] == "lina@example.com"
    assert captured["json"] == {
        "source": "integrale-connect-crm", "crm_contact_id": contact["id"],
        "prenom": "Lina", "nom": "MARTIN", "email": "lina@example.com",
        "telephone": "0600000000", "formation": "APS", "parcours": "Initial",
        "centre": "Paris", "session": "Du 1 au 5 septembre 2026",
        "commentaires": "Financement validé.",
    }
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert "Idempotency-Key" not in captured["headers"]


def test_crm_conversion_does_not_change_status_when_remote_rejects(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin"}).get_json()
    c.patch(f"/api/crm/contacts/{contact['id']}", json={
        "mail": "lina@example.com", "lieu": "Paris", "dates_formation": "Septembre 2026",
    })
    monkeypatch.setenv("GESTION_STAGIAIRES_API_URL", "https://gestion.example/api/preremplissage")
    monkeypatch.setenv("GESTION_STAGIAIRES_API_TOKEN", "secret")
    monkeypatch.setattr(application.requests, "post", lambda *args, **kwargs: SimpleNamespace(
        status_code=422, content=b'{}', json=lambda: {"error": "Session inconnue"}))

    response = c.post(f"/api/crm/contacts/{contact['id']}/convertir")

    assert response.status_code == 502
    assert response.get_json()["error"] == "Session inconnue"
    assert c.get(f"/api/crm/contacts/{contact['id']}").get_json()["statut"] == "Nouveaux"


def test_crm_conversion_javascript_opens_and_closes_registration_tab():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert "function conversionModal" not in crm_js
    assert "Inscrire dans Gestion stagiaires" not in crm_js
    open_tab = "const registrationTab=window.open('','_blank')"
    backend_call = "await api(`/api/crm/contacts/${c.id}/convertir`"
    assert open_tab in crm_js
    assert crm_js.index(open_tab) < crm_js.index(backend_call)
    assert "registrationTab.location.href=result.url" in crm_js
    assert "catch(e){registrationTab.close();toast(e.message,true)}" in crm_js


def test_crm_persists_reglementaire_answers(tmp_path, monkeypatch):
    test_client = client(tmp_path, monkeypatch)
    contact = test_client.post("/api/crm/contacts", json={"prenom": "Lina", "nom": "Martin", "formation": "APS"}).get_json()
    answers = {
        "carte_pro": "NON",
        "antecedents": "NON",
        "titre_sejour": "OUI",
        "compte_cnaps": "OUI",
        "cnaps_username": "lina.cnaps",
        "cnaps_password": "secret",
        "integration_dracar": "OUI",
    }

    response = test_client.patch(f"/api/crm/contacts/{contact['id']}", json=answers)

    assert response.status_code == 200
    assert all(response.get_json()[key] == value for key, value in answers.items())


def test_crm_mentions_are_private_and_replyable(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post('/api/crm/contacts', json={'prenom': 'Lina'}).get_json()
    publication = c.post(f"/api/crm/contacts/{contact['id']}/publications", json={'texte': '@aurelie peux-tu vérifier ?'}).get_json()['publication']
    assert c.get('/api/crm/notifications').get_json() == []
    with c.session_transaction() as session:
        session['user_email'] = 'aurelie@integraleacademy.com'
    notifications = c.get('/api/crm/notifications').get_json()
    assert len(notifications) == 1
    assert notifications[0]['publication_id'] == publication['id']
    assert c.post(f"/api/crm/contacts/{contact['id']}/publications/{publication['id']}/comments", json={'texte': 'Oui, je regarde.'}).status_code == 201
    assert c.patch('/api/crm/notifications', json={'id': notifications[0]['id']}).get_json()[0]['read'] is True


def test_crm_uppercase_refresh_preserves_contact_query(tmp_path, monkeypatch):
    response = client(tmp_path, monkeypatch).get('/CRM/contacts?fiche=contact-123')
    assert response.status_code == 302
    assert response.location.endswith('/crm/contacts?fiche=contact-123')


def test_crm_regulatory_and_funding_dependencies_are_present():
    with open(application.app.root_path + '/static/crm.js', encoding='utf-8') as source:
        script = source.read()
    assert "selectHtml('garde_vue','Garde à vue ou prise d’empreintes ?'" in script
    assert 'data-show="cpf-yes"' in script
    assert 'data-show="identity-created"' in script
    assert 'data-show="ft-yes"' in script
    assert "loadWedof(c);" in script
    assert "loadWedof(c,{refresh:true})" not in script


def test_leads_can_be_filtered_by_an_exact_training_session():
    with open(application.app.root_path + "/static/crm.js", encoding="utf-8") as source:
        crm_js = source.read()

    assert 'id="sessionFilter"' in crm_js
    assert 'Toutes les sessions' in crm_js
    assert 'sessionFilterRows()' in crm_js
    assert "c.dates_formation===session[2]" in crm_js
    assert "c.formation===session[0]" in crm_js
    assert "(c.lieu||'')===session[1]" in crm_js
    assert 'id="filterResultCount"' in crm_js


def test_contact_qualification_flag_is_validated_persisted_and_logged(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    contact = c.post(
        "/api/crm/contacts",
        json={"prenom": "Lina", "nom": "Martin", "formation": "APS"},
    ).get_json()
    original_status = contact["statut"]

    initial = c.get(f"/api/crm/contacts/{contact['id']}").get_json()
    assert initial["qualification_flag"] == ""

    updated = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"qualification_flag": "green"},
    )
    assert updated.status_code == 200
    payload = updated.get_json()
    assert payload["qualification_flag"] == "green"
    assert payload["statut"] == original_status
    assert not payload.get("relance_date")
    assert any(
        item["title"] == "Qualification : Green Flag"
        for item in payload["activities"]
    )

    invalid = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"qualification_flag": "blue"},
    )
    assert invalid.status_code == 400
    assert c.get(f"/api/crm/contacts/{contact['id']}").get_json()["qualification_flag"] == "green"

    removed = c.patch(
        f"/api/crm/contacts/{contact['id']}",
        json={"qualification_flag": ""},
    )
    assert removed.get_json()["qualification_flag"] == ""


def test_contact_merge_only_inherits_source_flag_when_target_is_empty(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    target = c.post("/api/crm/contacts", json={"prenom": "Cible"}).get_json()
    source = c.post("/api/crm/contacts", json={"prenom": "Source"}).get_json()
    c.patch(
        f"/api/crm/contacts/{source['id']}",
        json={"qualification_flag": "red"},
    )

    merged = c.post(
        "/api/crm/contacts/merge",
        json={"target_id": target["id"], "source_id": source["id"]},
    )
    assert merged.status_code == 200
    assert merged.get_json()["contact"]["qualification_flag"] == "red"


def test_abandoned_information_form_creates_one_internal_crm_lead(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setattr(application, "creer_piste_salesforce", lambda fields: None)
    monkeypatch.setattr(
        application, "envoyer_mail_formulaire_formation_abandonne",
        lambda record, fields: True,
    )
    monkeypatch.setattr(
        application, "envoyer_sms_formulaire_formation_abandonne",
        lambda record, fields: True,
    )
    payload = {
        "form_id": "draft-abandoned-1",
        "status": "abandoned",
        "fields": {
            "prenom": "Lina", "nom": "Martin",
            "mail": "lina@example.com", "telephone": "06 12 34 56 78",
            "formation": "APS", "centre": "paris",
            "dates": "Session septembre", "gclid": "gclid-test",
        },
    }

    assert c.post("/api/demande-informations-formations/autosave", json=payload).status_code == 204
    assert c.post("/api/demande-informations-formations/autosave", json=payload).status_code == 204

    data = application.load_data()
    assert len(data["crm_contacts"]) == 1
    contact = data["crm_contacts"][0]
    assert contact["statut"] == "Nouveaux"
    assert contact["origine"] == "Formulaire abandonné"
    assert contact["gclid"] == "gclid-test"
    assert contact["dates_formation"] == "Session septembre"
    assert [item["title"] for item in contact["activities"]].count(
        "Formulaire abandonné détecté"
    ) == 1
    draft = data["formulaires_abandonnes"][0]
    assert draft["crm_abandoned_contact_id"] == contact["id"]


def test_completed_form_enriches_abandoned_contact_without_changing_origin(tmp_path, monkeypatch):
    client(tmp_path, monkeypatch)
    monkeypatch.setattr(application, "current_user", lambda: {"name": "Test"})
    data = application.load_data()
    fields = {
        "prenom": "Lina", "nom": "Martin",
        "mail": "lina@example.com", "telephone": "06 12 34 56 78",
        "formation": "APS", "centre": "paris",
    }
    record = {"form_id": "draft-abandoned-2", "fields": fields}
    abandoned = application._crm_create_or_match_abandoned_form_contact(
        data, record, fields, "22/08/2026 14:00",
    )
    completed_fields = {
        **fields, "dates": "Session octobre",
        "cpf_consulte": "OUI", "cpf_montant": "1200",
        "gclid": "gclid-completed",
    }
    matched = application._crm_create_contact_from_information_request(
        data, completed_fields, "request-completed-2", "DEVIS-2", "/devis/2",
    )

    assert matched["id"] == abandoned["id"]
    assert matched["origine"] == "Formulaire abandonné"
    assert matched["dates_formation"] == "Session octobre"
    assert matched["cpf"] == "OUI"
    assert matched["gclid"] == "gclid-completed"
    assert len(data["crm_contacts"]) == 1


def test_crm_creation_survives_salesforce_failure_for_abandoned_form(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        application, "creer_piste_salesforce",
        lambda fields: (_ for _ in ()).throw(RuntimeError("Salesforce indisponible")),
    )
    monkeypatch.setattr(
        application, "envoyer_mail_formulaire_formation_abandonne",
        lambda record, fields: False,
    )
    monkeypatch.setattr(
        application, "envoyer_sms_formulaire_formation_abandonne",
        lambda record, fields: False,
    )

    response = c.post(
        "/api/demande-informations-formations/autosave",
        json={
            "form_id": "draft-abandoned-3", "status": "abandoned",
            "fields": {
                "prenom": "Nadia", "nom": "Durand",
                "mail": "nadia@example.com", "telephone": "0611223344",
            },
        },
    )

    assert response.status_code == 204
    data = application.load_data()
    assert len(data["crm_contacts"]) == 1
    assert data["crm_contacts"][0]["origine"] == "Formulaire abandonné"
    assert "Salesforce indisponible" in data["formulaires_abandonnes"][0]["salesforce_abandoned_error"]
