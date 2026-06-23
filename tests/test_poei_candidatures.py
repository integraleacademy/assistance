import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app as application


class PoeiCandidaturesTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_file = os.path.join(self.temp_dir.name, "data.json")
        self.client = application.app.test_client()
        admin_email = next(
            email for email, user in application.USERS.items() if user.get("role") == "admin"
        )
        with self.client.session_transaction() as session:
            session["user_email"] = admin_email

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_data(self, demandes):
        with open(self.data_file, "w", encoding="utf-8") as data_file:
            json.dump({"demandes": demandes}, data_file)

    def test_admin_devis_exposes_poei_button(self):
        self.write_data([])

        with patch.object(application, "DATA_FILE", self.data_file):
            response = self.client.get("/admin-devis")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/admin-devis/poei", response.data)
        self.assertIn("POEI".encode("utf-8"), response.data)

    def test_admin_devis_poei_lists_submitted_applications(self):
        self.write_data([
            {
                "id": "poei-1",
                "source": "poei_agent_securite_cannes",
                "date": "23/06/2026 09:15",
                "nom": "Martin",
                "prenom": "Nadia",
                "mail": "nadia@example.com",
                "telephone": "0612345678",
                "statut": "Non traité",
                "mail_confirme": "23/06/2026 09:16",
                "details": json.dumps({
                    "Ville de résidence": "Cannes",
                    "Permis B": "Oui",
                    "Disponible formation": "Oui",
                    "Mobilité Cannes": "Oui",
                    "Inscrit France Travail": "Oui",
                    "Identifiant France Travail": "1234567A",
                    "Message / motivation": "Très motivée",
                }),
            },
            {"id": "other", "motif": "Demande de devis détaillé", "nom": "Ignore"},
        ])

        with patch.object(application, "DATA_FILE", self.data_file):
            response = self.client.get("/admin-devis/poei")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Nadia Martin".encode("utf-8"), response.data)
        self.assertIn(b"nadia@example.com", response.data)
        self.assertIn("1234567A".encode("utf-8"), response.data)
        self.assertNotIn(b"Ignore", response.data)

    def test_poei_submission_sends_admin_email_to_aurelie(self):
        self.write_data([])
        form_data = {
            "nom": "Martin",
            "prenom": "Nadia",
            "mail": "nadia@example.com",
            "telephone": "0612345678",
            "ville": "Cannes",
            "permis_b": "Oui",
            "disponible_formation": "Oui",
            "mobilite_cannes": "Oui",
            "france_travail": "Oui",
            "identifiant_france_travail": "1234567A",
            "message": "Très motivée",
            "confirm_disponibilite": "on",
            "confirm_cannes": "on",
            "confirm_cnaps": "on",
            "consentement": "on",
        }

        with (
            patch.object(application, "DATA_FILE", self.data_file),
            patch.object(application, "send_email_html", return_value=True) as send_email_html,
        ):
            response = self.client.post("/poei-agent-securite-cannes", data=form_data)

        self.assertEqual(response.status_code, 302)
        send_email_html.assert_called_once()
        self.assertEqual(send_email_html.call_args.args[0], "aurelie@integraleacademy.com")
        self.assertIn("Très motivée", send_email_html.call_args.args[2])
        self.assertIn("1234567A", send_email_html.call_args.args[3])


if __name__ == "__main__":
    unittest.main()
