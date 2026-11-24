"""Accès indépendants au secrétariat, sans changer l'API de rappel."""
from crm_app import legacy_app as application


def test_message_link_displays_contact_fields_first_then_routing_fields():
    app = application.app
    app.config.update(TESTING=True)
    response = app.test_client().get("/secretariat/message")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    page = response.get_data(as_text=True)
    fields = ['id="lastName"', 'id="firstName"', 'id="email"',
              'id="telephone"', 'id="recipient"', 'id="callReason"',
              'id="messageText"']
    assert [page.index(field) for field in fields] == sorted(page.index(field) for field in fields)
    for field in fields:
        assert field in page
    assert 'id="messageForm"' in page
    assert 'id="messageSuccess"' in page
    assert 'href="/secretariat/gestion"' not in page
    assert 'Autres demandes' not in page and 'Accéder aux autres demandes' not in page
    for name, function in [('Cassandre MENARD', 'Responsable commerciale'), ('Aurélie CHAUSSEZ', 'Responsable BTS et relation clients'),
                           ('Elsa DUQUESNE', 'Comptabilité'), ('Clément VAILLANT', 'Direction générale')]:
        assert f'value="{name}">{name} — {function}</option>' in page


def test_other_demands_page_keeps_full_flow_without_message_choice(monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))
    application.app.config.update(TESTING=True)
    response = application.app.test_client().get("/secretariat/gestion")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'data-step="3"><h2>Quelle formation' in page
    assert 'data-step="1"' not in page
    assert all(f'data-formation-code="{code}"' in page for code in application.SECRETARIAT_FORMATIONS)
    assert all(item not in page for item in ['Nouveau prospect', 'Suivre un dossier', 'Les formations en détails', 'Transmettre un message'])
    assert 'href="/secretariat/message"' not in page
    assert 'Solde à confirmer avec le conseiller' not in page
    assert 'value="A_CONFIRMER"' not in page


def test_historical_link_stays_usable_with_original_choices(monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: dict(application.DEFAULT_DATA))
    response = application.app.test_client().get("/secretariat")
    assert response.status_code == 200
    assert 'data-request="autre"' in response.get_data(as_text=True)


def test_quick_message_reuses_crm_callback_api_and_idempotency_key():
    script = application.app.test_client().get("/static/secretariat_message.js")
    assert script.status_code == 200
    content = script.get_data(as_text=True)
    assert "fetch('/api/secretariat/demandes'" in content
    assert "callback_motif: 'message'" in content
    assert "call_key: callKey" in content
    assert "type: 'autre'" in content
    assert "callback_recipient:" in content
    assert "Motif de l’appel :" in content


def test_quick_message_styles_cancel_historical_sibling_margin():
    """Right-column fields must not inherit the 18px offset from secretariat.css."""
    response = application.app.test_client().get("/static/secretariat_message.css")
    assert response.status_code == 200
    css = response.get_data(as_text=True)
    assert ".message-fields > .field + .field:not(.grid .field) { margin-top: 0; }" in css
    assert ".message-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));" in css


def test_quick_message_saves_all_caller_details_and_reason_in_crm_queue(monkeypatch):
    """The real CRM endpoint must retain all seven fields without generating a sales lead."""
    from copy import deepcopy

    data = deepcopy(application.DEFAULT_DATA)
    data.update(crm_contacts=[], secretariat_demandes=[], demandes=[])
    saved = []
    monkeypatch.setattr(application, "load_data", lambda: data)
    monkeypatch.setattr(application, "_load_data_snapshot", lambda: data)
    monkeypatch.setattr(application, "save_data", lambda *_: saved.append(True))
    monkeypatch.setattr(application, "_crm_send_email_html", lambda *_: True)
    salesforce = []
    monkeypatch.setattr(application, "creer_piste_salesforce", salesforce.append)
    application.app.config.update(TESTING=True)
    client = application.app.test_client()
    recipient = next(member["name"] for member in application.USERS.values()
                     if member["first_name"] == "Cassandre")
    payload = {
        "call_key": "0123456789abcdef0123456789abcdef",
        "type": "autre",
        "callback_motif": "message",
        "share_with_desk": False,
        "prenom": "Camille",
        "nom_famille": "Martin",
        "nom": "Camille Martin",
        "email": "camille@example.com",
        "telephone": "0601020304",
        "callback_recipient": recipient,
        "notes": "Motif de l’appel : Facturation ou paiement\\n"
                 "Message : Merci de rappeler au sujet d’une facture.",
        "rdv": "Non abordé",
    }
    result = client.post("/api/secretariat/demandes", json=payload)
    assert result.status_code == 201
    assert saved
    entry, = data["secretariat_demandes"]
    assert entry["prenom"] == payload["prenom"]
    assert entry["nom_famille"] == payload["nom_famille"]
    assert entry["nom"] == payload["nom"]
    assert entry["email"] == payload["email"]
    assert entry["telephone"] == payload["telephone"]
    assert entry["callback_recipient"] == recipient
    assert entry["notes"] == payload["notes"]
    assert entry["callback_status"] == "pending"
    row, = application._crm_callback_requests_payload(data)
    assert row["display_name"] == payload["nom"]
    assert row["email"] == payload["email"]
    assert row["telephone"] == payload["telephone"]
    assert row["recipient"] == recipient
    assert row["notes"] == payload["notes"]
    assert row["status"] == "pending"
    assert not data["crm_contacts"]
    assert not salesforce

    duplicate = client.post("/api/secretariat/demandes", json=payload)
    assert duplicate.status_code == 200
    assert len(data["secretariat_demandes"]) == 1
