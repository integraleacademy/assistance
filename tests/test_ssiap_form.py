import os
import unittest
from unittest.mock import Mock, patch

import app as application


class SsiapInformationFormTestCase(unittest.TestCase):
    def setUp(self):
        self.client = application.app.test_client()

    @patch.dict(os.environ, {"BREVO_API_KEY": "test-key", "BREVO_SMS_SENDER": "ACADEMY"})
    @patch("app.requests.post")
    def test_sms_enables_brevo_unicode_encoding_for_emojis(self, post):
        post.return_value = Mock(status_code=201, text='{"messageId":"test"}')

        self.assertTrue(application.send_sms("06 12 34 56 78", "Bonjour 👨‍🎓 👉"))

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["content"], "Bonjour 👨‍🎓 👉")
        self.assertIs(payload["unicodeEnabled"], True)
        self.assertNotIn("unicode", payload)

    @patch.dict(os.environ, {"BREVO_API_KEY": "test-key", "BREVO_SMS_SENDER": "ACADEMY"})
    @patch("app.requests.post")
    def test_sms_keeps_gsm_encoding_for_compatible_text(self, post):
        post.return_value = Mock(status_code=201, text='{"messageId":"test"}')

        self.assertTrue(application.send_sms("0612345678", "Bonjour, a bientot !"))

        self.assertIs(post.call_args.kwargs["json"]["unicodeEnabled"], False)

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
        self.assertIn("1 230 € TTC", page)
        self.assertNotIn("980 € TTC", page)
        self.assertNotIn("1 200 € TTC", page)
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

    def test_navigation_is_bound_before_all_business_initialization(self):
        response = self.client.get("/demande-informations-formations")

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        navigation_binding = page.index(
            "nextBtns.forEach((nextBtn) => nextBtn.addEventListener('click'"
        )
        first_business_initialization = page.index(
            "const step5Cards = Array.from(document.querySelectorAll('#step5 .step5-card'))"
        )

        self.assertLess(navigation_binding, first_business_initialization)
        self.assertIn("if (blocSsiap)", page)
        self.assertIn("if (ssiapSecourismeValide)", page)

    def test_removed_ssiap_details_reference_cannot_break_javascript(self):
        response = self.client.get("/demande-informations-formations")

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertNotIn("function updateSsiapDetails", page)
        self.assertIn(
            "const ssiapDetails = document.getElementById('ssiapDetails') || blocSsiap;",
            page,
        )

    def test_ssiap_quote_uses_requested_price_with_valid_first_aid_certificate(self):
        context = application.build_devis_context(
            "SSIAP",
            application.PLAN_FORMATIONS["SSIAP"],
            "Du 12 au 27 octobre 2026 - examen le 28 octobre 2026",
            formation_details={"ssiap_secourisme_valide": "OUI"},
        )

        self.assertEqual(context["devis_total"], "1230 €")
        self.assertEqual(context["devis_lignes"][0]["prix_unitaire"], "1230 €")
        self.assertNotIn("SST inclus", context["devis_lignes"][0]["intitule"])

    def test_ssiap_quote_keeps_requested_price_when_certificate_is_not_valid(self):
        context = application.build_devis_context(
            "SSIAP",
            application.PLAN_FORMATIONS["SSIAP"],
            "Du 12 au 27 octobre 2026 - examen le 28 octobre 2026",
            formation_details={"ssiap_secourisme_valide": "NON"},
        )

        self.assertEqual(context["devis_total"], "1230 €")
        self.assertEqual(context["devis_lignes"][0]["prix_unitaire"], "1230 €")
        self.assertNotIn("SST inclus", context["devis_lignes"][0]["intitule"])

    def test_ssiap_email_uses_dedicated_aps_style_template(self):
        email_html = application.build_ssiap1_email_html(
            "Clément",
            "Du 12 au 27 octobre 2026 - examen le 28 octobre 2026",
            "cote_azur",
            "https://example.com/devis/ssiap",
            "OUI",
        )

        self.assertIn("Devenez Agent de Sécurité Incendie SSIAP 1", email_html)
        self.assertIn("Du 12 au 27 octobre 2026 — examen le 28 octobre 2026", email_html)
        self.assertIn("67 heures de formation", email_html)
        self.assertIn("Puget-sur-Argens", email_html)
        self.assertIn("1 230 € TTC", email_html)
        self.assertIn("certificat SST valide ou un PSC1 de moins de 2 ans", email_html)
        self.assertIn("https://example.com/devis/ssiap", email_html)
        self.assertIn("https://calendly.com/integraleacademy/ssiap1", email_html)
        self.assertNotIn("{session_html}", email_html)
        self.assertNotIn("formation APS", email_html)

    def test_ssiap_email_keeps_requested_tariff_when_first_aid_is_missing(self):
        email_html = application.build_ssiap1_email_html(
            "Nadia",
            "Du 12 au 27 octobre 2026 - examen le 28 octobre 2026",
            "cote_azur",
            "https://example.com/devis/ssiap-sst",
            "NON",
        )

        self.assertIn("1 230 € TTC", email_html)
        self.assertIn("Un certificat SST valide ou un PSC1 de moins de 2 ans reste requis", email_html)

    def test_email_uses_brevo_fallback_when_gmail_smtp_fails(self):
        brevo_response = Mock(status_code=201, text='{"messageId":"test"}')

        with (
            patch.dict(
                os.environ,
                {
                    "SMTP_USER": "ecole@integraleacademy.com",
                    "SMTP_PASS": "invalid",
                    "BREVO_API_KEY": "brevo-test-key",
                },
                clear=False,
            ),
            patch.object(
                application.smtplib,
                "SMTP_SSL",
                side_effect=OSError("SMTP indisponible"),
            ),
            patch.object(application.requests, "post", return_value=brevo_response) as post,
        ):
            sent = application.send_email_html(
                "stagiaire@example.com",
                "Informations SSIAP 1",
                "Contenu texte",
                "<p>Contenu HTML</p>",
            )

        self.assertTrue(sent)
        post.assert_called_once()
        self.assertEqual(
            post.call_args.args[0],
            "https://api.brevo.com/v3/smtp/email",
        )
        self.assertEqual(
            post.call_args.kwargs["json"]["to"],
            [{"email": "stagiaire@example.com"}],
        )

    def test_ssiap_submission_records_automatic_email_failure(self):
        data_store = {
            "demandes": [],
            "archives": [],
            "compteur_traitees": 0,
            "hebergements": [],
            "formation_sessions": {},
            "plans_simulation": {},
        }

        with (
            patch.object(application, "load_data", return_value=data_store),
            patch.object(application, "save_data") as save_data,
            patch.object(application, "creer_piste_salesforce"),
            patch.object(application, "send_email_html", return_value=False),
            patch.object(
                application,
                "envoyer_sms_demande_infos_formation",
                return_value=True,
            ),
        ):
            response = self.client.post(
                "/demande-informations-formations",
                data={
                    "nom": "Martin",
                    "prenom": "Nadia",
                    "mail": "nadia@example.com",
                    "telephone": "0612345678",
                    "formation": "SSIAP",
                    "centre": "cote_azur",
                    "dates": "Du 12 au 27 octobre 2026 - examen le 28 octobre 2026",
                    "ssiap_secourisme_valide": "OUI",
                    "souhaite_devis": "OUI",
                },
            )

        self.assertEqual(response.status_code, 302)
        demande = next(
            entry
            for entry in data_store["demandes"]
            if entry.get("source") == "demande_infos_formations"
        )
        self.assertEqual(
            demande["mail_erreur"],
            "❌ Erreur lors de l'envoi automatique du mail",
        )
        self.assertEqual(demande["mail_confirme"], "")
        self.assertIn(
            "formation Agent de sécurité incendie SSIAP 1",
            demande["mail_contenu"],
        )
        self.assertIn(
            "Devenez Agent de Sécurité Incendie SSIAP 1",
            demande["mail_html"],
        )
        save_data.assert_called()

    def test_ssiap_confirmation_uses_ssiap_calendly(self):
        response = self.client.get(
            "/confirmation-demande-informations?formation=SSIAP&hot=1"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "https://calendly.com/integraleacademy/ssiap1",
            response.get_data(as_text=True),
        )

    def test_ssiap_sms_uses_ssiap_calendly(self):
        sms = application.build_training_information_sms_text("SSIAP")

        self.assertIn(
            "SSIAP 1 – Agent de Service de Sécurité Incendie et d’Assistance à Personnes",
            sms,
        )
        self.assertIn("https://calendly.com/integraleacademy/ssiap1", sms)
        self.assertNotIn("https://calendly.com/integraleacademy/apr", sms)
        self.assertNotIn("https://calendly.com/integraleacademy/aps", sms)


if __name__ == "__main__":
    unittest.main()
