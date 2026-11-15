import datetime
import json
from unittest.mock import patch

import app as application
import crm_public_sessions as calendar


def published():
    def row(sid, start, variant, status="OPEN"):
        return {"id": sid, "training": {"slug": "ssiap-1"}, "location": "Puget-sur-Argens",
                "title": "SSIAP 1 " + variant, "status": status,
                "startDate": start, "endDate": "2030-02-28", "examDate": "2030-03-01"}
    return calendar._public_calendar([row("avec", "2030-02-11", "avec SST"),
                                      row("sans", "2030-02-15", "sans SST"),
                                      row("clos", "2030-02-11", "avec SST", "FULL")])


def test_form_uses_published_dates_and_formula():
    client = application.app.test_client()
    with patch.object(calendar, "_published_sessions", return_value=published()), patch.object(application, "load_data", return_value={}):
        for sid, label, fee in [("avec", "SSIAP 1 + SST", "1 230 € TTC"), ("sans", "SSIAP 1 sans SST", "980 € TTC")]:
            result = client.get("/demande-informations-formations?formation=SSIAP&session=" + sid + "&formule=ssiap-1")
            page = result.get_data(as_text=True)
            assert result.status_code == 200 and label in page and fee in page and 'id="websiteSessionId"' in page
        assert "n’a pas pu être confirmée" in client.get("/demande-informations-formations?formation=SSIAP&session=clos").get_data(as_text=True)
        assert calendar.website_ssiap_selection("sans", None, datetime.date(2031,1,1)) is None


def test_submitted_session_reaches_contact_quote_and_email():
    client = application.app.test_client()
    store = {"demandes": [], "formation_sessions": {}}
    with (patch.object(application, "load_data", return_value=store), patch.object(application, "save_data"),
          patch.object(application, "creer_piste_salesforce"), patch.object(application, "send_email_html", return_value=True),
          patch.object(application, "envoyer_sms_demande_infos_formation", return_value=True),
          patch.object(calendar, "_published_sessions", return_value=published())):
        label = "Du 15 février 2030 au 28 février 2030 · SSIAP 1 sans SST"
        result = client.post("/demande-informations-formations", data={"formation":"SSIAP", "centre":"cote_azur",
            "dates":label, "ssiap_session_id":"sans", "ssiap_formule":"ssiap-1", "nom":"Exemple", "prenom":"Test",
            "mail":"test@example.invalid", "telephone":"0600000000", "souhaite_devis":"OUI", "ssiap_secourisme_valide":"OUI"})
        assert result.status_code == 302
        request_entry = next(e for e in store["demandes"] if e.get("source") == "demande_infos_formations")
        assert json.loads(request_entry["details"])["ssiap_session_id"] == "sans"
        assert "980 € TTC" in request_entry["mail_html"]
        quote = next(json.loads(e["details"]) for e in store["demandes"] if e.get("statut_devis"))
        assert quote["dates"] == label and quote["ssiap_formule"] == "ssiap-1"
        assert application.get_formation_tarif("SSIAP", quote) == 980
        contact = next(c for c in store["crm_contacts"] if c.get("source_demande_id") == request_entry["id"])
        assert contact["dates_formation"] == label and contact["formulaire"]["ssiap_session_id"] == "sans"
    assert application.get_formation_tarif("SSIAP", {"ssiap_formule":"ssiap-1-sst"}) == 1230
    assert application.get_formation_tarif("SSIAP", {}) == 1230


def test_fee_and_description_identify_sst_in_the_bundle():
    details = {"ssiap_formule": "ssiap-1-sst"}
    quote = application.build_devis_context("SSIAP", "SSIAP 1", "Du 11 février 2030 au 28 février 2030", formation_details=details)
    assert quote["devis_total"] == "1230 €"
    assert "formation SST incluse" in quote["devis_lignes"][0]["intitule"]
    assert "inclut la formation SST" in application._ssiap_first_aid_note("NON", "ssiap-1-sst")
