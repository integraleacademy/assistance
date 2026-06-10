import unittest

import app as application


class SsiapInformationFormTestCase(unittest.TestCase):
    def setUp(self):
        self.client = application.app.test_client()

    def test_form_exposes_ssiap_training_location_pricing_and_session(self):
        response = self.client.get("/demande-informations-formations")

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('<option value="SSIAP">Agent de sécurité incendie SSIAP 1</option>', page)
        self.assertIn("Formation organisée uniquement à Puget-sur-Argens", page)
        self.assertIn("980 € TTC", page)
        self.assertIn("1 200 € TTC", page)
        self.assertIn("Du 12 au 27 octobre 2026 - examen le 28 octobre 2026", page)

    def test_ssiap_session_is_only_available_at_cote_azur_centre(self):
        sessions = application.get_formation_sessions({})

        self.assertEqual(
            sessions["cote_azur"]["SSIAP"],
            [
                {
                    "label": "Du 12 au 27 octobre 2026 - examen le 28 octobre 2026",
                    "badge": "",
                    "date_examen": "2026-10-28",
                }
            ],
        )
        self.assertNotIn("SSIAP", sessions["auvergne"])
        self.assertNotIn("SSIAP", sessions["paris"])

    def test_ssiap_quote_uses_base_price_with_valid_first_aid_certificate(self):
        context = application.build_devis_context(
            "SSIAP",
            application.PLAN_FORMATIONS["SSIAP"],
            "Du 12 au 27 octobre 2026 - examen le 28 octobre 2026",
            formation_details={"ssiap_secourisme_valide": "OUI"},
        )

        self.assertEqual(context["devis_total"], "980 €")
        self.assertEqual(context["devis_lignes"][0]["prix_unitaire"], "980 €")
        self.assertNotIn("SST inclus", context["devis_lignes"][0]["intitule"])

    def test_ssiap_quote_includes_sst_when_certificate_is_not_valid(self):
        context = application.build_devis_context(
            "SSIAP",
            application.PLAN_FORMATIONS["SSIAP"],
            "Du 12 au 27 octobre 2026 - examen le 28 octobre 2026",
            formation_details={"ssiap_secourisme_valide": "NON"},
        )

        self.assertEqual(context["devis_total"], "1200 €")
        self.assertEqual(context["devis_lignes"][0]["prix_unitaire"], "1200 €")
        self.assertIn("SST inclus", context["devis_lignes"][0]["intitule"])


if __name__ == "__main__":
    unittest.main()
