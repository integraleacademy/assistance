import copy

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
    assert '@media (max-width: 720px)' in body
    assert 'data-label="Suivi de clé"' in body
    assert "Modifications enregistrées automatiquement" in body
    assert body.count("Convention PDF") == 2
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
