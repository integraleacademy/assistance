import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app as application


class DeleteTreatedRappelsTestCase(unittest.TestCase):
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

    def read_demandes(self):
        with open(self.data_file, "r", encoding="utf-8") as data_file:
            return json.load(data_file)["demandes"]

    def test_admin_page_exposes_the_bulk_delete_button(self):
        self.write_data([])

        with patch.object(application, "DATA_FILE", self.data_file):
            response = self.client.get("/admin-devis")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="delete-treated-rappels"', response.data)
        self.assertIn("Supprimer les traités".encode("utf-8"), response.data)

    def test_deletes_only_treated_phone_callbacks(self):
        self.write_data([
            {"id": "treated-bool", "motif": "Formulaire à rappeler", "traite": True},
            {"id": "treated-status", "motif": "Formulaire à rappeler", "statut": "Traité"},
            {
                "id": "pending",
                "motif": "Formulaire à rappeler",
                "traite": False,
                "statut": "A rappeler",
            },
            {"id": "other", "motif": "Demande de devis détaillé", "traite": True},
        ])

        with patch.object(application, "DATA_FILE", self.data_file):
            response = self.client.delete("/admin-devis/rappels/traites")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "deleted_count": 2})
        self.assertEqual([item["id"] for item in self.read_demandes()], ["pending", "other"])

    def test_returns_zero_without_rewriting_when_no_treated_callback_exists(self):
        demandes = [
            {"id": "pending", "motif": "Formulaire à rappeler", "traite": False},
            {"id": "other", "motif": "Autre"},
        ]
        self.write_data(demandes)

        with (
            patch.object(application, "DATA_FILE", self.data_file),
            patch.object(application, "save_data") as save_data,
        ):
            response = self.client.delete("/admin-devis/rappels/traites")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "deleted_count": 0})
        save_data.assert_not_called()

    def test_requires_authentication(self):
        anonymous_client = application.app.test_client()
        response = anonymous_client.delete("/admin-devis/rappels/traites")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_rejects_users_without_callback_management_permission(self):
        regular_email = next(
            email
            for email, user in application.USERS.items()
            if user.get("role") != "admin" and user.get("name") != "Mohamed"
        )
        with self.client.session_transaction() as session:
            session["user_email"] = regular_email

        response = self.client.delete("/admin-devis/rappels/traites")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json(), {"ok": False, "error": "forbidden"})


if __name__ == "__main__":
    unittest.main()
