import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app as application


class DiplomePhotoTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_file = os.path.join(self.temp_dir.name, "data.json")
        self.upload_dir = os.path.join(self.temp_dir.name, "uploads")
        os.makedirs(self.upload_dir, exist_ok=True)
        self.client = application.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_diplome_submission_stores_identity_photo(self):
        with (
            patch.object(application, "DATA_FILE", self.data_file),
            patch.object(application, "UPLOAD_FOLDER", self.upload_dir),
            patch.object(application, "envoyer_mail_admin"),
            patch.object(application, "envoyer_mail_accuse"),
        ):
            response = self.client.post(
                "/",
                data={
                    "nom": "Dupont",
                    "prenom": "Jean",
                    "telephone": "0600000000",
                    "mail": "jean@example.com",
                    "motif": "Diplome",
                    "details": "Diplôme SSIAP",
                    "photo_identite": (io.BytesIO(b"fake image"), "photo-identite.jpg"),
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 302)
        with open(self.data_file, encoding="utf-8") as data_file:
            demande = json.load(data_file)["demandes"][0]

        self.assertEqual(demande["photo_identite"], "photo-identite.jpg")
        self.assertTrue(os.path.exists(os.path.join(self.upload_dir, "photo-identite.jpg")))

    def test_imprimer_displays_identity_photo_at_official_size(self):
        demande = {
            "id": "demande-photo",
            "nom": "Dupont",
            "prenom": "Jean",
            "telephone": "0600000000",
            "mail": "jean@example.com",
            "motif": "Diplome",
            "details": "Diplôme SSIAP",
            "justificatif": "",
            "photo_identite": "photo-identite.jpg",
            "date": "23/06/2026 10:00",
            "attribution": "",
            "statut": "Non traité",
            "commentaire": "",
        }
        with open(self.data_file, "w", encoding="utf-8") as data_file:
            json.dump({"demandes": [demande], "archives": []}, data_file)

        with patch.object(application, "DATA_FILE", self.data_file):
            response = self.client.get("/imprimer/demande-photo")

        self.assertEqual(response.status_code, 200)
        page = response.data.decode("utf-8")
        self.assertIn('class="photo-identite"', page)
        self.assertIn("width: 35mm;", page)
        self.assertIn("height: 45mm;", page)
        self.assertIn("/uploads/photo-identite.jpg", page)


if __name__ == "__main__":
    unittest.main()
