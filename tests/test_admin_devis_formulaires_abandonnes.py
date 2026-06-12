import unittest

import app as application


class AbandonedFormsTransmissionFeedbackTestCase(unittest.TestCase):
    def setUp(self):
        self.client = application.app.test_client()
        admin_email = next(
            email
            for email, user in application.USERS.items()
            if user.get("role") == "admin"
        )
        with self.client.session_transaction() as session:
            session["user_email"] = admin_email

    def test_relance_displays_ypareo_transmission_feedback(self):
        with application.app.test_request_context():
            page = application.render_template(
                "admin_devis_formulaires_abandonnes.html",
                formulaires=[
                    {
                        "id": "form-1",
                        "date": "12/06/2026 10:30",
                        "nom": "MARTIN",
                        "prenom": "Nadia",
                        "mail": "nadia@example.com",
                        "telephone": "0612345678",
                        "statut": "Abandonné",
                        "infos": {"formation": "SSIAP", "centre": "cote_azur"},
                    }
                ],
                stats={
                    "today": 1,
                    "yesterday": 0,
                    "week": 1,
                    "month": 1,
                    "treated": 0,
                    "to_process": 1,
                    "total": 1,
                },
            )

        self.assertIn('class="relance-form"', page)
        self.assertIn("En cours de transmission Yparéo…", page)
        self.assertIn("Création de la personne et transmission des informations", page)
        self.assertIn("requestAnimationFrame", page)
        self.assertIn("form.submit()", page)
        relance_form_start = page.index('class="relance-form"')
        relance_form_end = page.index("</form>", relance_form_start)
        self.assertNotIn("onsubmit=", page[relance_form_start:relance_form_end])


if __name__ == "__main__":
    unittest.main()
