import base64
import copy
import datetime
import io
import shutil
import subprocess
import zipfile

import pytest

import app as application
import hebergement_contract as contract_generator


SESSION = "Du 9 novembre 2026 au 19 janvier 2027"


@pytest.fixture(autouse=True)
def fixed_booking_date(monkeypatch):
    class FixedDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 9, 9, 10, tzinfo=datetime.timezone.utc)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(application.datetime, "datetime", FixedDateTime)


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
        "key_number": "12",
        "reception_agent": "Clément Vaillant",
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
    }
    for check_key, _label in application.HEBERGEMENT_HANDOVER_CHECKLIST:
        form[f"checklist_{check_key}"] = "on"
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


def test_hebergement_uses_only_upcoming_on_site_a3p_admin_sessions(monkeypatch):
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["formation_sessions"] = {
        "cote_azur": {
            "A3P": [
                {"label": "Du 19 janvier au 16 mars 2027"},
                {"label": "Du 1er septembre au 27 octobre 2026"},
                {"label": "Du 8 juin au 4 août 2026"},
                {"label": "Du 10 novembre 2026 au 14 janvier 2027 - examen le 15 janvier 2027"},
                {"label": "Du 9 au 30 septembre 2026"},
                {"label": "Du 19 janvier au 16 mars 2027"},
                {"label": "Dates à confirmer"},
                {"label": "Du 31 février au 16 mars 2027"},
                {"label": "Du 20 mars au 16 mars 2027"},
            ],
            "APS": [{"label": "Du 1er au 30 décembre 2026"}],
        },
        "auvergne": {"A3P": [{"label": "Du 21 octobre au 17 décembre 2026"}]},
    }
    original_sessions = copy.deepcopy(data["formation_sessions"])
    client, _, saved, deliveries = _client(monkeypatch, data)

    body = client.get("/hebergement").get_data(as_text=True)

    assert 'value="Du 9 au 30 septembre 2026"' in body
    assert 'data-arrival="mardi 8 septembre 2026"' in body
    assert 'value="Du 10 novembre 2026 au 14 janvier 2027"' in body
    assert 'data-arrival="lundi 9 novembre 2026"' in body
    assert body.count('value="Du 19 janvier au 16 mars 2027"') == 1
    assert body.index('value="Du 9 au 30 septembre 2026"') < body.index(
        'value="Du 10 novembre 2026 au 14 janvier 2027"'
    ) < body.index('value="Du 19 janvier au 16 mars 2027"')
    for hidden in (
        "Du 1er septembre au 27 octobre 2026",
        "Du 8 juin au 4 août 2026",
        SESSION,
        "Du 1er au 30 décembre 2026",
        "Du 21 octobre au 17 décembre 2026",
        "Dates à confirmer",
        "Du 31 février au 16 mars 2027",
        "Du 20 mars au 16 mars 2027",
    ):
        assert hidden not in body
    assert data["formation_sessions"] == original_sessions
    assert saved == deliveries == []


def test_hebergement_follows_admin_add_update_and_delete_without_static_fallback(monkeypatch):
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["formation_sessions"] = {"cote_azur": {"A3P": []}}
    client, _, _, _ = _client(monkeypatch, data)
    with client.session_transaction() as flask_session:
        flask_session["user_email"] = "clement@integraleacademy.com"
    form = {
        "centre": "cote_azur", "formation": "A3P", "idx": "0",
        "label": "Du 19 janvier au 16 mars 2027",
    }

    assert client.post("/admin/formation-sessions", data={**form, "action": "add"}).status_code == 302
    assert form["label"] in client.get("/hebergement").get_data(as_text=True)

    updated_label = "Du 23 mars au 4 juin 2027"
    assert client.post("/admin/formation-sessions", data={
        **form, "action": "update", "label": updated_label,
    }).status_code == 302
    body = client.get("/hebergement").get_data(as_text=True)
    assert updated_label in body
    assert form["label"] not in body
    assert 'data-arrival="lundi 22 mars 2027"' in body

    assert client.post("/admin/formation-sessions", data={**form, "action": "delete"}).status_code == 302
    body = client.get("/hebergement").get_data(as_text=True)
    assert updated_label not in body
    assert SESSION not in body
    assert "Aucune session A3P à venir" in body
    assert 'class="submit-button" type="submit" disabled' in body


@pytest.mark.parametrize("unavailable_session", [
    "Du 1er septembre au 27 octobre 2026",
    "Du 8 juin au 4 août 2026",
    SESSION,
    "Du 21 octobre au 17 décembre 2026",
    "Du 1er au 30 décembre 2026",
    "Session inconnue",
    "",
])
def test_hebergement_rejects_outdated_or_unknown_form_submissions(monkeypatch, unavailable_session):
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["formation_sessions"] = {
        "cote_azur": {
            "A3P": [
                {"label": "Du 1er septembre au 27 octobre 2026"},
                {"label": "Du 10 novembre 2026 au 14 janvier 2027"},
            ],
            "APS": [{"label": "Du 1er au 30 décembre 2026"}],
        },
        "auvergne": {"A3P": [{"label": "Du 21 octobre au 17 décembre 2026"}]},
    }
    client, data_store, saved, deliveries = _client(monkeypatch, data)

    response = client.post("/hebergement", data={
        "prenom": "Lina", "nom": "Martin", "email": "lina@example.com",
        "session": unavailable_session,
    })

    assert response.status_code == 200
    assert "Cette session n’est plus disponible" in response.get_data(as_text=True)
    assert 'value="Lina"' in response.get_data(as_text=True)
    assert data_store["hebergements"] == []
    assert saved == deliveries == []


def test_new_admin_session_booking_uses_its_dates_in_email_confirmation_and_convention(monkeypatch):
    training_session = "Du 10 novembre 2026 au 14 janvier 2027"
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["formation_sessions"] = {"cote_azur": {"A3P": [{"label": training_session}]}}
    client, data_store, _, deliveries = _client(monkeypatch, data)

    response = client.post("/hebergement", data={
        "nom": "Martin", "prenom": "Lina", "telephone": "06 00 00 00 00",
        "email": "lina@example.com", "session": training_session,
    })

    assert response.status_code == 302
    assert len(deliveries) == 2
    assert "lundi 9 novembre 2026" in deliveries[0][2]
    assert training_session in deliveries[0][2]
    assert "lundi 9 novembre 2026" in client.get(response.headers["Location"]).get_data(as_text=True)
    reservation = data_store["hebergements"][0]
    fields = application._hebergement_convention_record(reservation)["fields"]
    assert fields["contract_date"] == "2026-11-09"
    assert fields["arrival_date"] == "2026-11-09"
    assert fields["departure_date"] == "2027-01-14"

    with client.session_transaction() as flask_session:
        flask_session["user_email"] = "clement@integraleacademy.com"
    body = client.get(f"/admin_hebergement/{reservation['id']}/convention/preparer").get_data(as_text=True)
    assert f'value="{training_session}"' in body
    assert '"arrival_date": "2026-11-09"' in body
    assert '"departure_date": "2027-01-14"' in body

    form = {
        **_complete_convention_form(),
        "session": training_session, "contract_date": "2026-11-09",
        "arrival_date": "2026-11-09", "departure_date": "2027-01-14",
        "payment_date": "2026-11-09", "deposit_cheque_date": "2026-11-09",
    }
    record = application._hebergement_convention_from_form(reservation, form)
    assert record["fields"]["arrival_date"] == form["arrival_date"]
    assert record["fields"]["departure_date"] == form["departure_date"]


def test_existing_booking_keeps_its_dates_after_admin_session_removal(monkeypatch):
    reservation = _reservation(session="Du 5 janvier au 16 mars 2026")
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["hebergements"] = [reservation]
    data["formation_sessions"] = {"cote_azur": {"A3P": []}}
    client, _, _, _ = _client(monkeypatch, data)
    with client.session_transaction() as flask_session:
        flask_session["user_email"] = "clement@integraleacademy.com"

    response = client.get("/admin_hebergement/reservation-1/convention/preparer")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert f'value="{reservation["session"]}"' in body
    assert '"arrival_date": "2026-01-04"' in body
    assert '"departure_date": "2026-03-16"' in body
    assert application._hebergement_arrival_label(reservation["session"]) == "dimanche 4 janvier 2026"


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
    assert body.count("Préparer la convention\n") == 2
    assert "Suivre la signature" not in body
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


def test_admin_hebergement_convention_editor_is_prefilled_for_paper_signature(
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
    assert "yousign" not in body.lower()
    assert "signature électronique" not in body.lower()
    assert 'value="2026-08-31"' in body
    assert "inventory_" not in body
    assert "état des lieux" not in body.lower()
    assert body.count('class="check-card"') == 5
    assert "Ouvrir le PDF à imprimer" in body
    assert 'name="room"' not in body and 'name="bed"' not in body
    assert "Agent d’accueil présent" in body
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
    assert "inventory" not in convention
    assert "yousign" not in convention
    assert all(convention["checklist"].values())
    assert reservation["paiement"] == "Payé"
    assert reservation["mode_paiement"] == "Espèces"
    assert reservation["cle_numero"] == "12"
    assert reservation["cle_etat"] == "Donnee"
    assert saved


def test_obsolete_signature_actions_do_not_send_or_modify_the_booking(monkeypatch):
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["hebergements"] = [_reservation()]
    client, _, saved, deliveries = _client(monkeypatch, data)
    with client.session_transaction() as flask_session:
        flask_session["user_email"] = "clement@integraleacademy.com"
    monkeypatch.setattr(application, "YousignClient", lambda: pytest.fail("unexpected Yousign call"))
    url = "/admin_hebergement/reservation-1/convention"
    assert client.post(url + "/preparer", data=_complete_convention_form("send_yousign")).status_code == 400
    assert client.post(url + "/yousign/remind").status_code == 404
    assert not saved
    assert not deliveries


@pytest.mark.parametrize("status", ["ongoing", "done"])
def test_print_from_legacy_booking_uses_current_fields_and_keeps_archives(monkeypatch, status):
    archive = {
        "version": 2,
        "yousign": {"signatureRequestId": "old-request", "status": status},
        "inventory": {"window": {"observations": "Ancienne observation"}},
        "fields": {"entry_observations": "Ancienne observation", "contract_date": "2026-09-02",
                   "center_representative": "Ancien représentant"},
        "checklist": {"inventory_completed": True, "key_handed_over": True},
    }
    data = copy.deepcopy(application.DEFAULT_DATA)
    data["hebergements"] = [_reservation(cle_etat="Restituee", convention_hebergement=copy.deepcopy(archive))]
    client, _, saved, deliveries = _client(monkeypatch, data)
    monkeypatch.setattr(application, "YousignClient", lambda: pytest.fail("unexpected Yousign call"))
    with client.session_transaction() as flask_session:
        flask_session["user_email"] = "clement@integraleacademy.com"
    url = "/admin_hebergement/reservation-1/convention/preparer"
    body = client.get(url).get_data(as_text=True)
    assert "old-request" not in body
    assert "Ancienne observation" not in body
    assert 'name="reception_agent" value=""' in body
    response = client.post(url, data={"editor_action": "preview", "key_number": "25", "checklist_key_handed_over": "on"},
                           follow_redirects=True)
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert response.headers["Content-Disposition"].startswith("inline;")
    updated = saved[-1]["hebergements"][0]["convention_hebergement"]
    assert saved[-1]["hebergements"][0]["cle_etat"] == "Restituee"
    assert updated["fields"]["contract_date"] == "2026-09-02"
    assert updated["fields"]["key_number"] == "25"
    assert updated["fields"]["center_representative"] == "Ancien représentant"
    assert updated["fields"]["reception_agent"] == ""
    assert updated["fields"]["entry_observations"] == "Ancienne observation"
    assert updated["inventory"] == archive["inventory"]
    assert updated["yousign"] == archive["yousign"]
    assert updated["checklist"]["inventory_completed"] is True
    assert not deliveries
    listing = client.get("/admin_hebergement").get_data(as_text=True)
    assert ("Télécharger l’original signé" in listing) == (status == "done")


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="Poppler requis pour lire le PDF")
def test_paper_pdf_contains_handwritten_signatures_and_no_inventory(monkeypatch):
    reservation = _reservation(convention_hebergement={
        "fields": {"room": "Chambre Sapin", "bed": "Lit violet", "center_representative": "Ancien représentant", "reception_agent": "Aurélie",
                   "entry_observations": "Ancienne observation"},
        "inventory": {"door": {"observations": "Ancienne observation"}},
    })
    # Exercise the production rendering path with a synthetic centre mark.
    from PIL import Image
    mark = io.BytesIO()
    Image.new("RGB", (80, 20), "blue").save(mark, format="PNG")
    monkeypatch.setenv("HEBERGEMENT_CENTRE_VISA_PNG_BASE64", base64.b64encode(mark.getvalue()).decode())
    pdf = application._build_hebergement_convention_pdf(reservation)
    output = subprocess.run(["pdftotext", "-enc", "UTF-8", "-", "-"],
                            input=pdf, capture_output=True, check=True)
    text = " ".join(output.stdout.decode().casefold().split())
    pages = [" ".join(page.casefold().split())
             for page in output.stdout.decode().split("\f") if page.strip()]
    def page_of(label):
        return next(i for i, page in enumerate(pages) if label in page)

    assert page_of("5. participation financière") < page_of("6. dépôt de garantie")
    assert page_of("a. participation financière") < page_of("d. au départ")
    assert page_of("chaque engagement ci-dessous") < page_of("déclaration finale")
    assert "suivi de la caution" in pages[page_of("d. au départ")]
    assert len(pages) > 9
    for removed in ("yousign", "signature électronique", "état des lieux", "inventaire",
                    "ancienne observation", "ancien représentant", "photographies annexées", "annexe 3", "chambre sapin", "lit violet",
                    "à à compléter", "date du non renseignée"):
        assert removed not in text
    assert text.count("clément vaillant") == 4  # Identification and the three centre signature panels.
    assert text.count("directeur général") == 4
    for retained in ("aurélie", "deux exemplaires", "clé d’entrée", "signature manuscrite", "pour le centre", "l'occupant",
                     "annexe 1", "annexe 2", "attestation de prise de connaissance des règles"):
        assert retained in text
    if shutil.which("pdfimages"):
        report = subprocess.run(["pdfimages", "-list", "-"], input=pdf, capture_output=True, check=True)
        mark_pages = [int(cols[0]) for line in report.stdout.decode().splitlines()
                      if len(cols := line.split()) > 4 and cols[2:5] == ["image", "80", "20"]]
        assert len(set(mark_pages)) == 3
    rules, acknowledgement = text.rsplit("attestation de prise de connaissance des règles", 1)
    assert "strictement interdit de brancher un véhicule électrique sur une prise du site" in rules
    assert "je ne branche aucun véhicule électrique sur les prises du site" in acknowledgement


def test_hebergement_yousign_webhook_marks_the_convention_as_signed(monkeypatch):
    reservation = _reservation()
    record = application._hebergement_convention_record(reservation)
    record["yousign"] = {
        "signatureRequestId": "sr-123",
        "externalId": "hebergement-reservation-1",
        "status": "ongoing",
    }
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
    record["yousign"] = {
        "signatureRequestId": "sr-123",
        "signerId": "signer-123",
        "status": "ongoing",
    }
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
    record["yousign"] = {
        "signatureRequestId": "sr-123",
        "signerId": "signer-123",
        "status": "ongoing",
    }
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


def test_roster_preserves_filters_and_lists_occupants_for_reception(monkeypatch):
    client, data, saved, deliveries = _client(monkeypatch)
    data["hebergements"] = [
        _reservation(session=SESSION, nom="ZIMMERMANN-LEGRAND", prenom="Anne-Marie"),
        _reservation(id="2", session=SESSION, nom="ALBERT"),
        _reservation(id="3", nom="HORS-SESSION"),
    ]
    params = {"session_filter": SESSION, "vue": "liste"}
    assert client.get("/admin_hebergement", query_string=params).status_code == 302
    with client.session_transaction() as auth:
        auth["user_email"] = "clement@integraleacademy.com"
    page = client.get("/admin_hebergement", query_string={"session_filter": SESSION})
    assert "Imprimer la liste" in page.text and "Aperçu du rappel J−2" in page.text
    roster = client.get("/admin_hebergement", query_string=params).text
    assert roster.index("ALBERT") < roster.index("ZIMMERMANN-LEGRAND")
    assert "Anne-Marie" in roster and "08/11/2026" in roster
    assert "HORS-SESSION" not in roster and "contenteditable" not in roster
    filtered = client.get("/admin_hebergement", query_string={**params, "q": "Anne-Marie"}).text
    assert "ZIMMERMANN-LEGRAND" in filtered and "ALBERT" not in filtered
    assert saved == deliveries == []


def test_reminder_preview_matches_email_without_sending(monkeypatch):
    import html
    client, data, saved, deliveries = _client(monkeypatch)
    booking = _reservation(prenom="Lina <Test>", session="Du 19 janvier au 16 mars 2027")
    data["hebergements"] = [booking]
    monkeypatch.setattr(application, "_load_data_snapshot", lambda: data)
    path = "/admin_hebergement/reservation-1/rappel"
    assert client.get(path).status_code == 302
    with client.session_transaction() as auth:
        auth["user_email"] = "clement@integraleacademy.com"
    page = client.get(path)
    assert page.status_code == 200 and "samedi 16 janvier 2027" in page.text
    arrival = application._hebergement_booking_arrival(booking)
    subject, plain, markup = application._hebergement_reminder_email(booking, arrival)
    assert "lundi 18 janvier 2027" in plain and "08h00 et 17h00" in plain
    assert application.HEBERGEMENT_ADDRESS in plain
    assert subject in page.text
    assert markup.replace(application.HEBERGEMENT_MAIL_LOGO, "/static/logo.png") in html.unescape(page.text)
    assert "Lina &lt;Test&gt;" in markup
    assert client.get("/admin_hebergement/inconnu/rappel").status_code == 404
    booking["session"] = "Dates à préciser"
    assert "date d’arrivée ne peut pas être déterminée" in client.get(path).text
    assert saved == deliveries == []


def test_confirmation_and_reminder_attach_the_actual_logo(monkeypatch):
    from pathlib import Path
    from unittest.mock import MagicMock
    smtp = MagicMock()
    monkeypatch.setattr(application.smtplib, "SMTP_SSL", lambda *a, **kw: smtp)
    mailer = smtp.__enter__.return_value
    for message in (
        application._hebergement_confirmation_email("Lina", SESSION),
        application._hebergement_reminder_email(_reservation(), datetime.date(2026, 11, 8)),
    ):
        assert application.HEBERGEMENT_MAIL_LOGO in message[2] and "force.com" not in message[2]
        assert application.send_email_html("lina@example.com", *message)
        mime = mailer.send_message.call_args.args[0]
        html_part = next(p for p in mime.walk() if p.get_content_type() == "text/html")
        assert b'src="cid:integrale-academy-logo"' in html_part.get_payload(decode=True)
        images = [part for part in mime.walk() if part.get_content_type() == "image/png"]
        assert len(images) == 1
        assert images[0]["Content-ID"] == "<integrale-academy-logo>"
        assert images[0].get_payload(decode=True) == (Path(application.app.static_folder) / "logo.png").read_bytes()


def test_reminder_j2_catchup_concurrency_and_retry(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import Mock
    from hebergement_reminders import deliver_due, DeliveryUncertain
    bookings = [
        _reservation(id="due", session="Du 10 novembre 2026 au 14 janvier 2027"),
        _reservation(id="catchup", session="Du 9 novembre 2026 au 14 janvier 2027"),
        _reservation(id="future", session="Du 11 novembre 2026 au 14 janvier 2027"),
        _reservation(id="arrived", session="Du 8 novembre 2026 au 14 janvier 2027"),
        _reservation(id="bad", session="Session inconnue"),
        _reservation(id="missing", mail="", session="Du 10 novembre 2026 au 14 janvier 2027"),
        _reservation(id="handed", cle_etat="Donnee", session="Du 10 novembre 2026 au 14 janvier 2027"),
    ]
    attempts = []
    logger = Mock()
    def send(booking, arrival, address):
        assert address == "lina@example.com"
        attempts.append(booking["id"])
        return True
    def run(hour=8, sender=send, records=bookings):
        deliver_due(records, datetime.datetime(2026, 11, 7, hour), tmp_path / "crm.json",
                    application._hebergement_booking_arrival, sender, logger)
    run(7)
    assert not attempts
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: run(), range(3)))
    run()
    assert sorted(attempts) == ["catchup", "due"]
    failing = [{**bookings[0], "id": "retry"}]
    run(sender=lambda *args: False, records=failing)
    run(records=failing)
    assert attempts.count("retry") == 1
    uncertain = [{**bookings[0], "id": "uncertain"}]
    run(sender=Mock(side_effect=DeliveryUncertain()), records=uncertain)
    run(records=uncertain)
    assert "uncertain" not in attempts


def test_reminder_uses_convention_arrival_only_for_current_session():
    booking = _reservation(session="Du 10 novembre 2026 au 14 janvier 2027")
    booking["convention_hebergement"] = {"fields": {
        "session": booking["session"], "arrival_date": "2026-11-10", "arrival_time": "09:30",
    }}
    arrival = application._hebergement_booking_arrival(booking)
    assert arrival == datetime.date(2026, 11, 10)
    assert "09h30" in application._hebergement_reminder_email(booking, arrival)[1]
    booking["session"] = "Du 19 janvier au 16 mars 2027"
    arrival = application._hebergement_booking_arrival(booking)
    assert arrival == datetime.date(2027, 1, 18)
    assert "09h30" not in application._hebergement_reminder_email(booking, arrival)[1]


@pytest.mark.parametrize("paid,deposit", [(False, False), (True, False), (False, True), (True, True)])
def test_arrival_reminder_introduces_host_and_only_requests_outstanding_items(paid, deposit):
    booking = _reservation(paiement="Payé" if paid else "Non payé")
    booking["convention_hebergement"] = {"fields": {"deposit_received": "Oui" if deposit else "Non"}}
    _, plain, markup = application._hebergement_reminder_email(booking, datetime.date(2026, 11, 9))
    for content in (plain, markup):
        assert "Je suis Aurélie BAYARD" in content and "vous accueillerai" in content
        assert "04 87 83 06 15" in content and "aurelie@integraleacademy.com" in content
        assert "sac de couchage" in content and "17 h" in content
        assert ("Préparez le montant" in content) == (not paid)
        assert ("chèque de caution distinct" in content) == (not deposit)
    assert 'href="tel:+33487830615"' in markup and 'href="mailto:aurelie@integraleacademy.com"' in markup
    assert 'static/messaging/aurelie.png' in markup and "Chargée des relations clients" in markup
    assert "maps/dir/?api=1&amp;destination=" in markup
