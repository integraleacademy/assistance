import unittest

import app as application


class SsiapInformationFormTestCase(unittest.TestCase):
    def setUp(self):
        self.client = application.app.test_client()

    def test_form_uses_the_same_step_order_as_aps_for_ssiap(self):
        response = self.client.get("/demande-informations-formations")

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        formation_option = '<option value="SSIAP">Agent de sécurité incendie SSIAP 1</option>'
        self.assertIn(formation_option, page)
        step2_end = page.index("</section>", page.index(formation_option))
        self.assertNotIn("ssiap_secourisme_valide", page[page.index(formation_option):step2_end])

        location_step = page.index("Lieu de formation souhaité")
        puget_location = page.index("Intégrale Academy Côte d’Azur (Puget-sur-Argens, Var)")
        dates_step = page.index("Dates de formation souhaitées")
        question = page.index("Je possède un certificat SST ou PSC1 de moins de 2 ans")
        financing_step = page.index("Financement de votre formation")

        self.assertLess(location_step, puget_location)
        self.assertLess(puget_location, dates_step)
        self.assertLess(financing_step, question)
        self.assertIn("980 € TTC", page)
        self.assertIn("1 200 € TTC", page)
        self.assertIn("Du 12 au 27 octobre 2026 - examen le 28 octobre 2026", page)
        self.assertIn("['VTC', 'APS', 'SSIAP', 'DESP_VAE']", page)

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

    def test_navigation_is_bound_before_optional_field_initialization(self):
        response = self.client.get("/demande-informations-formations")

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        navigation_binding = page.index(
            "nextBtns.forEach((nextBtn) => nextBtn.addEventListener('click'"
        )
        first_initialization_event = page.index(
            "cpfConsulte.dispatchEvent(new Event('change'))"
        )

        self.assertLess(navigation_binding, first_initialization_event)
        self.assertIn("if (blocSsiap)", page)
        self.assertIn("if (ssiapSecourismeValide)", page)

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
