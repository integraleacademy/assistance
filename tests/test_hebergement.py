import copy

import app as application


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
