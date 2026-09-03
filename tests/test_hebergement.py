import copy
import io
import zipfile

import app as application
import hebergement_contract as contract_generator


SESSION = "Du 9 novembre 2026 au 19 janvier 2027"


def _client(monkeypatch, data=None):
    data_store = data or copy.deepcopy(application.DEFAULT_DATA)
    saved = []
    deliveries = []
    monkeypatch.setattr(application, "load_data", lambda: data_store)
    monkeypatch.setattr(
        application,
        "save_data",
        lambda payload: saved.append(copy.deepcopy(payload)),
    )
    monkeypatch.setattr(
        application,
        "send_email_html",
        lambda *args: deliveries.append(args) or True,
    )
    application.app.config.update(TESTING=True)
    return application.app.test_client(), data_store, saved, deliveries


def _reservation(**overrides):
    reservation = {
        "id": "reservation-1",
        "nom": "Martin",
        "prenom": "Lina",
        "telephone": "06 00 00 00 00",
        "mail": "lina@example.com",
        "session": "Du 1er septembre au 27 octobre 2026",
        "paiement": "Non payé",
        "mode_paiement": "",
        "cle_numero": "",
        "cle_etat": "A donner",
        "date_paiement": "",
    }
    reservation.update(overrides)
    return reservation


def _complete_convention_form(action="save"):
    form = {
        "editor_action": action,
        "nom": "MARTIN",
        "prenom": "Lina",
        "telephone": "06 00 00 00 00",
        "mail": "lina@example.com",
        "session": "Du 1er septembre au 27 octobre 2026",
        "personal_address": "10 rue des Orangers",
        "postal_code": "83480",
        "city": "Puget-sur-Argens",
        "contract_date": "2026-08-31",
        "contract_time": "09:30",
        "arrival_date": "2026-08-31",
        "arrival_time": "09:15",
        "departure_date": "2026-10-27",
        "departure_time": "17:00",
        "room": "Dortoir A",
        "bed": "4",
        "key_number": "12",
        "center_representative": "Clément Vaillant",
        "center_role": "Direction Intégrale Academy",
        "payment_status": "Payé",
        "payment_method": "Espèces",
        "payment_date": "2026-08-31",
        "payment_cheque_number": "",
        "payment_bank": "",
        "payment_cheque_date": "",
        "receipt_issued": "Oui",
        "receipt_reference": "REC-2026-001",
        "deposit_received": "Oui",
        "deposit_holder": "Lina Martin",
        "deposit_bank": "Banque Exemple",
        "deposit_cheque_number": "CAU-123456",
        "deposit_cheque_date": "2026-08-31",
        "entry_photos_count": "2",
        "entry_observations": "État conforme lors de la remise des clés.",
    }
    for check_key, _label in application.HEBERGEMENT_HANDOVER_CHECKLIST:
        form[f"checklist_{check_key}"] = "on"
    for item_key, _label in application.HEBERGEMENT_INVENTORY_ITEMS:
        form[f"inventory_{item_key}_state"] = "B"
        form[f"inventory_{item_key}_observations"] = ""
    return form


def test_hebergement_page_presents_all_booking_conditions(monkeypatch):
    client, _, _, _ = _client(monkeypatch)

    response = client.get("/hebergement")

    assert response.status_code == 200
    body = response.data.decode()
    assert "Votre hébergement, directement sur place" in body
    assert "La veille du premier jour de formation, entre 08h00 et 17h00 impérativement" in body
    assert "Aucune remise de clés après 17h00" in body
    assert "54 chemin du Carreou, 83480 Puget-sur-Argens" in body
    assert "Participation à l’hébergement" in body
    assert "Chèque de caution" in body
    assert "Sac de couchage ou couverture" in body
    assert "aucun bruit après 22h00" in body
    assert "consommation d’alcool ou de drogue" in body
    assert "Aucune personne extérieure" in body
    assert "À verser impérativement dès votre arrivée, lors de la remise des clés et de la signature du contrat d’hébergement." in body
    assert "Chèque de caution à remettre impérativement dès votre arrivée, lors de la remise des clés et de la signature du contrat d’hébergement." in body
    assert "J’ai pris connaissance des sommes à verser, du matériel à apporter et de l’ensemble des règles de l’hébergement." in body
    assert 'data-arrival="dimanche 8 novembre 2026"' in body
    assert 'name="conditions_arrivee" value="acceptees" required' in body
    assert 'name="reglement_hebergement" value="accepte" required' in body


def test_hebergement_booking_sends_complete_confirmation_email(monkeypatch):
    client, data_store, saved, deliveries = _client(monkeypatch)

    response = client.post(
        "/hebergement",
        data={
            "nom": "Martin",
            "prenom": "Lina",
            "telephone": "06 00 00 00 00",
            "email": "lina@example.com",
            "session": SESSION,
            "conditions_arrivee": "acceptees",
            "reglement_hebergement": "accepte",
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/hebergement_confirmation?session=")
    assert len(data_store["hebergements"]) == 1
    assert saved[-1]["hebergements"][0]["session"] == SESSION
    assert len(deliveries) == 2

    recipient, subject, plain, html_body = deliveries[0]
    assert recipient == "lina@example.com"
    assert subject == "Confirmation de votre hébergement – Intégrale Academy"
    for expected in (
        "dimanche 8 novembre 2026",
        "entre 08h00 et 17h00",
        "Aucune remise de clés ne pourra être effectuée après 17h00",
        "par vos propres moyens une solution d’hébergement",
        "54 chemin du Carreou, 83480 Puget-sur-Argens",
        "Participation financière : 300 €, à verser impérativement dès votre arrivée",
        "chèque de caution distinct de 200 €, à remettre impérativement dès votre arrivée",
        "remise des clés et de la signature du contrat d’hébergement",
        "Sac de couchage ou une couverture",
        "aucun bruit après 22h00",
        "aucune personne extérieure",
    ):
        assert expected.lower() in plain.lower()

    for expected in (
        "Votre réservation est confirmée",
        "dimanche 8 novembre 2026",
        "Participation financière — 300 €",
        "Chèque de caution — 200 €",
        "Règles essentielles",
    ):
        assert expected in html_body

    admin_recipient, admin_subject, _, _ = deliveries[1]
    assert admin_recipient == "ecole@integraleacademy.com, clement@integraleacademy.com"
    assert admin_subject == "🏨 Nouvelle réservation hébergement – Lina Martin"

    confirmation = client.get(response.headers["Location"])
    assert confirmation.status_code == 200
    confirmation_body = confirmation.data.decode()
    assert SESSION in confirmation_body
    assert "dimanche 8 novembre 2026, entre 08h00 et 17h00" in confirmation_body
    assert "300 € à verser et un chèque de caution de 200 € à remettre" in confirmation_body
    assert "remise des clés et de la signature du contrat d’hébergement" in confirmation_body


def test_hebergement_email_escapes_user_content_in_html():
    _, _, html_body = application._hebergement_confirmation_email(
        "Lina <script>alert(1)</script>",
        "Du 9 novembre 2026 au 19 janvier 2027 <test>",
    )

    assert "<script>alert(1)</script>" not in html_body
    assert "Lina &lt;script&gt;alert(1)&lt;/script&gt;" in html_body
    assert "&lt;test&gt;" in html_body


def test_full_session_keeps_the_form_and_sends_no_email(monkeypatch):
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["hebergements"] = [
        {"id": str(index), "session": SESSION}
        for index in range(10)
    ]
    client, data_store, saved, deliveries = _client(monkeypatch, data)

    response = client.post(
        "/hebergement",
        data={
            "nom": "Martin",
            "prenom": "Lina",
            "telephone": "06 00 00 00 00",
            "email": "lina@example.com",
            "session": SESSION,
        },
    )

    assert response.status_code == 200
    body = response.data.decode()
    assert "complet pour cette session" in body
    assert 'value="Lina"' in body
    assert f'value="{SESSION}" data-arrival="dimanche 8 novembre 2026" selected' in body
    assert len(data_store["hebergements"]) == 10
    assert saved == []
    assert deliveries == []


def test_admin_hebergement_displays_modern_dashboard_and_row_actions(monkeypatch):
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["hebergements"] = [
        {
            "id": "reservation-1",
            "nom": "Martin",
            "prenom": "Lina",
            "telephone": "06 00 00 00 00",
            "mail": "lina@example.com",
            "session": "Du 1er septembre au 27 octobre 2026",
            "paiement": "Non payé",
            "mode_paiement": "",
            "cle_numero": "",
            "cle_etat": "A donner",
            "date_paiement": "",
        },
        {
            "id": "reservation-2",
            "nom": "Bernard",
            "prenom": "Noé",
            "telephone": "06 11 11 11 11",
            "mail": "noe@example.com",
            "session": SESSION,
            "paiement": "Payé",
            "mode_paiement": "Chèque",
            "cle_numero": "7",
            "cle_etat": "Donnee",
            "date_paiement": "08/11/2026 10:00",
        },
    ]
    client, _, _, _ = _client(monkeypatch, data)
    with client.session_transaction() as flask_session:
        flask_session["user_email"] = "clement@integraleacademy.com"

    response = client.get("/admin_hebergement")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Pilotage des hébergements" in body
    assert "Réservations affichées" in body
    assert "Paiements encaissés" in body
    assert "Paiements à encaisser" in body
    assert "Clés en circulation" in body
    assert 'class="stats-grid"' in body
    assert 'class="filters-panel no-print"' in body
    assert 'class="bookings-panel"' in body
    assert ".app-shell {" in body
    assert "width: 100%;" in body
    assert ".app-header, .hero, .stats-grid, .filters-panel" in body
    assert "width: min(100%, 1724px);" in body
    assert '@media (max-width: 720px)' in body
    assert 'data-label="Suivi de clé"' in body
    assert "Modifications enregistrées automatiquement" in body
    assert body.count("Préparer / signer") == 2
    assert body.count("À préparer") == 2
    assert 'href="/admin_hebergement/reservation-1/convention/preparer"' in body
    assert 'href="/admin_hebergement/reservation-2/convention/preparer"' in body
    assert 'href="/admin_hebergement/reservation-1/convention"' in body
    assert 'href="/admin_hebergement/reservation-2/convention"' in body
    assert "function updatePaiement" in body
    assert "function updateMode" in body
    assert "function updateCleNumero" in body
    assert "function updateCleEtat" in body
    assert "function updateField" in body


def test_admin_hebergement_generates_a_prefilled_pdf_convention(monkeypatch):
    reservation = {
        "id": "reservation-1",
        "nom": "Martin",
        "prenom": "Lina",
        "telephone": "06 00 00 00 00",
        "mail": "lina@example.com",
        "session": "Du 1er septembre au 27 octobre 2026",
        "paiement": "Payé",
        "mode_paiement": "Espèces",
        "cle_numero": "12",
        "cle_etat": "Donnee",
        "date_paiement": "31/08/2026 09:30",
    }
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["hebergements"] = [reservation]
    client, _, _, _ = _client(monkeypatch, data)
    with client.session_transaction() as flask_session:
        flask_session["user_email"] = "clement@integraleacademy.com"

    context = application._hebergement_convention_context(reservation)
    assert context["arrival_label"] == "lundi 31 août 2026"
    assert context["formation_start_label"] == "mardi 1 septembre 2026"
    assert context["formation_end_label"] == "mardi 27 octobre 2026"
    assert context["occupant"]["nom"] == "MARTIN"
    assert context["key_number"] == "12"

    assert "300 € doit être versée dès l'arrivée" in contract_generator.PARTICIPATION_COPY
    assert "chèque de caution distinct de 200 € doit être remis dès l'arrivée" in contract_generator.DEPOSIT_COPY
    assert "par ses propres moyens une solution d'hébergement" in contract_generator.ARRIVAL_LATE_COPY
    assert "sommes à verser" in contract_generator.FINAL_ACKNOWLEDGEMENT_COPY

    response = client.get("/admin_hebergement/reservation-1/convention")

    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert response.data.startswith(b"%PDF-")
    assert len(response.data) > 30_000
    assert "attachment;" in response.headers["Content-Disposition"]
    assert "Convention_hebergement_Martin_Lina.pdf" in response.headers["Content-Disposition"]
    assert response.headers["Cache-Control"] == "private, no-store, max-age=0"


def test_admin_hebergement_convention_returns_404_for_unknown_booking(monkeypatch):
    client, _, _, _ = _client(monkeypatch)
    with client.session_transaction() as flask_session:
        flask_session["user_email"] = "clement@integraleacademy.com"

    response = client.get("/admin_hebergement/missing/convention")

    assert response.status_code == 404


def test_admin_hebergement_convention_editor_is_prefilled_and_explains_yousign(
    monkeypatch,
):
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["hebergements"] = [_reservation()]
    monkeypatch.setattr(application, "is_yousign_configured", lambda: False)
    client, _, _, _ = _client(monkeypatch, data)
    with client.session_transaction() as flask_session:
        flask_session["user_email"] = "clement@integraleacademy.com"

    response = client.get(
        "/admin_hebergement/reservation-1/convention/preparer"
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Préparer la convention" in body
    assert "Connexion Yousign à configurer" in body
    assert "authentification OTP par SMS" in body
    assert 'value="2026-08-31"' in body
    assert 'name="inventory_access_state"' in body
    assert body.count('class="check-card"') == 6
    assert "Envoyer pour signature" in body
    assert "const sessionSchedule" in body
    assert '"arrival_date": "2026-08-31"' in body


def test_admin_hebergement_convention_editor_saves_all_arrival_information(
    monkeypatch,
):
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["hebergements"] = [_reservation()]
    client, data_store, saved, _ = _client(monkeypatch, data)
    with client.session_transaction() as flask_session:
        flask_session["user_email"] = "clement@integraleacademy.com"

    response = client.post(
        "/admin_hebergement/reservation-1/convention/preparer",
        data=_complete_convention_form(),
    )

    assert response.status_code == 302
    reservation = data_store["hebergements"][0]
    convention = reservation["convention_hebergement"]
    assert convention["fields"]["personal_address"] == "10 rue des Orangers"
    assert convention["fields"]["deposit_cheque_number"] == "CAU-123456"
    assert convention["inventory"]["access"]["entry_state"] == "B"
    assert all(convention["checklist"].values())
    assert reservation["paiement"] == "Payé"
    assert reservation["mode_paiement"] == "Espèces"
    assert reservation["cle_numero"] == "12"
    assert reservation["cle_etat"] == "Donnee"
    assert saved


def test_admin_hebergement_blocks_yousign_when_arrival_checks_are_incomplete(
    monkeypatch,
):
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["hebergements"] = [_reservation()]
    monkeypatch.setattr(application, "is_yousign_configured", lambda: True)

    class UnexpectedYousignClient:
        def __init__(self):
            raise AssertionError("Yousign ne doit pas être appelé")

    monkeypatch.setattr(application, "YousignClient", UnexpectedYousignClient)
    client, _, _, _ = _client(monkeypatch, data)
    with client.session_transaction() as flask_session:
        flask_session["user_email"] = "clement@integraleacademy.com"

    response = client.post(
        "/admin_hebergement/reservation-1/convention/preparer",
        data={"editor_action": "send_yousign", "payment_status": "Non payé"},
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "La convention n’a pas été envoyée" in body
    assert "participation de 300 € doit être enregistrée comme payée" in body
    assert "chèque de caution de 200 € doit être enregistré comme reçu" in body
    assert "Validez toutes les vérifications de remise" in body


def test_admin_hebergement_blocks_yousign_for_late_arrival_or_late_payment(
    monkeypatch,
):
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["hebergements"] = [_reservation()]
    monkeypatch.setattr(application, "is_yousign_configured", lambda: True)
    client, _, _, _ = _client(monkeypatch, data)
    with client.session_transaction() as flask_session:
        flask_session["user_email"] = "clement@integraleacademy.com"
    form = _complete_convention_form("send_yousign")
    form["arrival_time"] = "17:30"
    form["payment_date"] = "2026-09-01"

    response = client.post(
        "/admin_hebergement/reservation-1/convention/preparer",
        data=form,
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "comprise entre 08h00 et 17h00" in body
    assert "doit correspondre à la date d&#39;arrivée" in body


def test_admin_hebergement_sends_complete_convention_with_sms_otp(monkeypatch):
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["hebergements"] = [_reservation()]
    calls = []

    class FakeYousignClient:
        def create_signature_request(self, name, external_id=""):
            calls.append(("create", name, external_id))
            return {"id": "sr-123"}

        def upload_file(self, request_id, content, filename, parse_anchors=False):
            calls.append(("upload", request_id, filename, parse_anchors))
            assert content.startswith(b"%PDF-")
            return {"id": "doc-123"}

        def add_signer(self, request_id, first_name, last_name, email, phone):
            calls.append(
                ("signer", request_id, first_name, last_name, email, phone)
            )
            return {"id": "signer-123"}

        def add_signature_field(self, request_id, document_id, signer_id, **field):
            calls.append(("field", request_id, document_id, signer_id, field))
            return {"id": "field-123"}

        def activate_signature_request(self, request_id):
            calls.append(("activate", request_id))
            return {"status": "ongoing"}

    monkeypatch.setattr(application, "is_yousign_configured", lambda: True)
    monkeypatch.setattr(application, "YousignClient", FakeYousignClient)
    client, data_store, saved, _ = _client(monkeypatch, data)
    with client.session_transaction() as flask_session:
        flask_session["user_email"] = "clement@integraleacademy.com"

    response = client.post(
        "/admin_hebergement/reservation-1/convention/preparer",
        data=_complete_convention_form("send_yousign"),
    )

    assert response.status_code == 302
    state = data_store["hebergements"][0]["convention_hebergement"]["yousign"]
    assert state["signatureRequestId"] == "sr-123"
    assert state["documentId"] == "doc-123"
    assert state["signerId"] == "signer-123"
    assert state["fieldId"] == "field-123"
    assert state["status"] == "ongoing"
    assert state["recipientPhone"] == "+33600000000"
    assert next(call for call in calls if call[0] == "signer")[5] == "06 00 00 00 00"
    signature_field = next(call for call in calls if call[0] == "field")[4]
    assert signature_field == application.HEBERGEMENT_YOUSIGN_SIGNATURE_FIELD
    assert saved


def test_completed_convention_keeps_signature_field_on_page_eleven():
    reservation = _reservation(
        paiement="Payé",
        mode_paiement="Espèces",
        cle_numero="12",
        cle_etat="Donnee",
        date_paiement="31/08/2026 09:30",
    )
    record = application._hebergement_convention_record(
        reservation, "Clément Vaillant"
    )
    record = application._hebergement_convention_from_form(
        reservation,
        _complete_convention_form(),
        "Clément Vaillant",
    )
    for item in record["inventory"].values():
        item["observations"] = (
            "Observation contrôlée lors de l'arrivée." * 3
        )[:80]
    reservation["convention_hebergement"] = record

    pdf = application._build_hebergement_convention_pdf(reservation)

    assert application._hebergement_pdf_page_count(pdf) == 11


def test_hebergement_yousign_webhook_marks_the_convention_as_signed(monkeypatch):
    reservation = _reservation()
    record = application._hebergement_convention_record(reservation)
    record["yousign"].update({
        "signatureRequestId": "sr-123",
        "externalId": "hebergement-reservation-1",
        "status": "ongoing",
    })
    reservation["convention_hebergement"] = record
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["hebergements"] = [reservation]
    client, data_store, saved, _ = _client(monkeypatch, data)
    monkeypatch.setattr(
        application,
        "get_yousign_config",
        lambda: type("Config", (), {"webhook_secret": ""})(),
    )

    response = client.post(
        "/webhooks/yousign/hebergement",
        json={
            "event_name": "signature_request.done",
            "data": {"signature_request": {"id": "sr-123"}},
        },
    )

    assert response.status_code == 200
    assert response.get_json()["target"] == "hebergement"
    state = data_store["hebergements"][0]["convention_hebergement"]["yousign"]
    assert state["status"] == "done"
    assert state["signedAt"]
    assert saved


def test_admin_hebergement_syncs_yousign_and_downloads_signed_pdf(monkeypatch):
    reservation = _reservation()
    record = application._hebergement_convention_record(reservation)
    record["yousign"].update({
        "signatureRequestId": "sr-123",
        "signerId": "signer-123",
        "status": "ongoing",
    })
    reservation["convention_hebergement"] = record
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["hebergements"] = [reservation]

    signed_pdf = b"%PDF-1.4\n% signed convention\n%%EOF"
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("audit-trail.txt", "preuve")
        archive.writestr("convention-signee.pdf", signed_pdf)

    class FakeYousignClient:
        def get_signature_request(self, request_id):
            assert request_id == "sr-123"
            return {"status": "done", "done_at": "2026-09-03T10:00:00+02:00"}

        def get_signature_request_signers(self, request_id):
            assert request_id == "sr-123"
            return [{"id": "signer-123", "status": "done"}]

        def download_signed_documents(self, request_id):
            assert request_id == "sr-123"
            return archive_buffer.getvalue()

    monkeypatch.setattr(application, "is_yousign_configured", lambda: True)
    monkeypatch.setattr(application, "YousignClient", FakeYousignClient)
    client, data_store, saved, _ = _client(monkeypatch, data)
    with client.session_transaction() as flask_session:
        flask_session["user_email"] = "clement@integraleacademy.com"

    sync_response = client.post(
        "/admin_hebergement/reservation-1/convention/yousign/sync"
    )

    assert sync_response.status_code == 302
    state = data_store["hebergements"][0]["convention_hebergement"]["yousign"]
    assert state["status"] == "done"
    assert state["signedAt"] == "2026-09-03T10:00:00+02:00"

    download_response = client.get(
        "/admin_hebergement/reservation-1/convention/yousign/download"
    )

    assert download_response.status_code == 200
    assert download_response.data == signed_pdf
    assert download_response.mimetype == "application/pdf"
    assert "Convention_hebergement_signee_MARTIN_Lina.pdf" in (
        download_response.headers["Content-Disposition"]
    )
    assert download_response.headers["Cache-Control"] == (
        "private, no-store, max-age=0"
    )
    assert saved


def test_admin_hebergement_can_cancel_yousign_before_signature(monkeypatch):
    reservation = _reservation()
    record = application._hebergement_convention_record(reservation)
    record["yousign"].update({
        "signatureRequestId": "sr-123",
        "signerId": "signer-123",
        "status": "ongoing",
    })
    reservation["convention_hebergement"] = record
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["hebergements"] = [reservation]
    calls = []

    class FakeYousignClient:
        def cancel_signature_request(self, request_id, custom_note=""):
            calls.append((request_id, custom_note))
            return {"id": request_id, "status": "canceled"}

    monkeypatch.setattr(application, "is_yousign_configured", lambda: True)
    monkeypatch.setattr(application, "YousignClient", FakeYousignClient)
    client, data_store, saved, _ = _client(monkeypatch, data)
    with client.session_transaction() as flask_session:
        flask_session["user_email"] = "clement@integraleacademy.com"

    response = client.post(
        "/admin_hebergement/reservation-1/convention/yousign/cancel"
    )

    assert response.status_code == 302
    state = data_store["hebergements"][0]["convention_hebergement"]["yousign"]
    assert state["status"] == "canceled"
    assert state["canceledAt"]
    assert calls == [(
        "sr-123",
        "Convention d'hébergement annulée par Intégrale Academy.",
    )]
    assert saved
