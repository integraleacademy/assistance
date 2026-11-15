"""Keep the public and secretariat form contracts intact during visual changes."""

from html.parser import HTMLParser
import json
import re

import pytest

import app as application


class FormContract(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.fields = {}
        self.forms = []
        self.steps = []
        self.progress = []
        self.options = {}
        self.current_select = None
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.forms.append(attrs)
        if tag in {"input", "select", "textarea"} and attrs.get("name"):
            self.fields.setdefault(attrs["name"], []).append(attrs)
        if tag == "select":
            self.current_select = attrs.get("name")
            self.options[self.current_select] = []
        if tag == "option" and self.current_select:
            self.options[self.current_select].append(attrs.get("value"))
        if "data-step" in attrs:
            self.steps.append(attrs["data-step"])
        if "data-pill" in attrs:
            self.progress.append(attrs["data-pill"])

    def handle_endtag(self, tag):
        if tag == "select":
            self.current_select = None


@pytest.mark.parametrize("secretariat", [False, True])
def test_visual_layout_preserves_submission_fields_and_conditional_modes(secretariat, monkeypatch):
    sessions = {
        "cote_azur": {"APS": [{"label": "Session témoin", "date_examen": "2027-01-20"}]},
        "auvergne": {"A3P": [{"label": "Ancienne session à Aurillac"}]},
    }
    monkeypatch.setattr(application, "load_data", lambda: {})
    monkeypatch.setattr(application, "get_upcoming_formation_sessions", lambda store: sessions)
    response = application.app.test_client().get("/demande-informations-formations", query_string={
        "secretariat": int(secretariat), "gclid": "tracking-test", "utm_source": "source-test",
    })
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    form = FormContract(page)

    assert len(form.forms) == 1
    assert form.forms[0]["method"] == "POST"
    assert form.forms[0]["id"] == "infosForm"
    assert form.steps == form.progress == [str(i) for i in range(1, 7 if secretariat else 6)]

    fields = {
        "nom", "prenom", "mail", "mail_confirm", "telephone", "formation", "centre",
        "dates", "date_examen", "cpf_consulte", "cpf_montant", "france_travail",
        "ft_refus_ok", "financement_perso", "identite_numerique", "cnaps_ok",
        "garde_vue", "titre_sejour", "ssiap_secourisme_valide", "souhaite_devis",
        "gclid", "wbraid", "gbraid", "gad_source", "gad_campaignid", "utm_source",
        "utm_medium", "utm_campaign", "source_secretariat", "draft_form_id",
    }
    if secretariat:
        fields |= {"rdv_telephonique", "commentaires_secretariat"}
    assert set(form.fields) == fields
    for name in ("nom", "prenom", "mail", "mail_confirm", "telephone", "formation", "dates"):
        assert "required" in form.fields[name][0]
    for name in ("mail", "mail_confirm"):
        assert form.fields[name][0]["type"] == "email"
    assert form.options["formation"] == ["", "A3P", "APS", "SSIAP", "DESP_INIT", "DESP_VAE", "VTC"]
    assert [field["value"] for field in form.fields["centre"]] == ["paris", "cote_azur"]
    for image in ("destination-paris.webp", "destination-cote-azur.webp"):
        assert f'{application.PUBLIC_CATALOGUE}/images/illustrations-2026/{image}' in page
    assert not re.search(r"aurillac|auvergne|arpajon|cantal", page, re.I)
    for name in ("cpf_consulte", "france_travail", "ft_refus_ok", "financement_perso",
                 "identite_numerique", "cnaps_ok", "garde_vue", "titre_sejour",
                 "ssiap_secourisme_valide", "souhaite_devis"):
        assert form.options[name] == ["", "OUI", "NON"]
    assert form.fields["source_secretariat"][0]["value"] == ("1" if secretariat else "")
    assert form.fields["gclid"][0]["value"] == "tracking-test"
    assert form.fields["utm_source"][0]["value"] == "source-test"
    session_payload = json.loads(re.search(r"const sessions = (.*?);", page).group(1))
    assert session_payload["cote_azur"]["APS"] == [
        {"label": "Session témoin", "date_examen": "2027-01-20"}
    ]
    assert 'title="Choisir mon centre de formation"' in page


def test_centre_simulator_only_offers_current_centres():
    page = application.app.test_client().get("/admin/choisir-centre-formation").get_data(as_text=True)
    assert "34 rue Poissonnière" in page and "83480 Puget-sur-Argens" in page
    assert not re.search(r"aurillac|auvergne|arpajon|cantal", page, re.I)


def test_old_centre_cannot_create_a_request_or_supply_dates_for_a_new_draft(monkeypatch):
    store = {}
    monkeypatch.setattr(application, "load_data", lambda: store)
    monkeypatch.setattr(application, "save_data", lambda *_: None)
    monkeypatch.setattr(application, "creer_piste_salesforce", lambda *_: pytest.fail("Unexpected lead"))
    client = application.app.test_client()
    fields = {"formation": "A3P", "centre": "auvergne", "dates": "Ancienne session", "date_examen": "2027-01-20"}
    assert client.post("/demande-informations-formations", data=fields).status_code == 400
    assert store == {}
    response = client.post("/api/demande-informations-formations/autosave", json={
        "form_id": "old-tab", "fields": fields,
    })
    assert response.status_code == 204
    assert store["formulaires_abandonnes"][0]["fields"] == {"formation": "A3P"}
