"""Plain language in the reception view originates from verified, scoped facts."""

from datetime import date

import pytest

import secretariat_calls as desk
from crm_app import legacy_app as app


def test_every_catalogue_course_has_a_speakable_name_and_purpose():
    catalogue = app.SECRETARIAT_FORMATIONS
    for code, config in catalogue.items():
        label, purpose = desk.desk_course(code, catalogue, code)
        assert len(label.split()) >= 3 and purpose
        assert desk.desk_course_code(config["short"].upper(), catalogue) == code
    assert desk.desk_course("BTS_MOS", catalogue, "BTS MOS")[0].startswith("Brevet de technicien supérieur")
    assert "validation des acquis de l’expérience" in desk.desk_course("DESP_VAE", catalogue, "DESP")[0]
    assert desk.desk_course_code("APR", catalogue) == "A3P"
    assert desk.desk_course("HIST_42", catalogue, "HIST_42") == ("Formation à préciser avec l’école", "")
    assert desk.desk_course("AUTRE FORMATION", catalogue, "Autre formation") == ("Autre formation", "")


def test_stage_and_financing_explain_the_actual_record():
    for status in app.CRM_STATUSES:
        label, wording = desk.desk_stage({"statut": status}, None)
        assert label != "Situation à préciser" and wording
    for secondary in (*app.CRM_SECONDARY_STATUSES, "Session FT"):
        facts = desk.desk_summary({"statut_secondaire": secondary, "cpf": "OUI"}, [], None)
        assert facts["secondary_label"] != secondary and "à préciser" not in facts["secondary_label"]
        assert "Compte personnel de formation" in facts["financement"]
    booked = {"statut": "RDV programmé"}
    assert desk.desk_stage(booked, None)[0] == "Rendez-vous à vérifier"
    assert desk.desk_stage(booked, {"start_time": "2030-01-01T10:00Z"})[0] == "Rendez-vous prévu"
    assert desk.desk_stage({"statut": "Converti", "archived_at": "2026-10-12"}, None)[0] == "Dossier archivé"
    facts = desk.desk_summary({"statut": "ANCIEN", "statut_secondaire": "CODE 42"}, [], None)
    assert facts["stage_label"] == "Situation à préciser" and facts["statut_secondaire"] == "CODE 42"


@pytest.mark.parametrize(("title", "meaning", "state", "expiry", "expected"), [
    ("AP SH", "Autorisation d’entrer en formation de surveillance", "ACTIF", "2027-01-08", "Fin de validité le 08/01/2027"),
    ("CP SH", "Carte professionnelle pour travailler dans la surveillance", "ACTIF", "2027-01-07", "dépassée"),
    ("AP A3P ACTIF", "Autorisation d’entrer en formation de protection rapprochée", "", "", "Date de fin à confirmer"),
    ("Carte professionnelle - Protection physique des personnes", "Carte professionnelle pour travailler dans la protection rapprochée", "INACTIF", "2030-04-03", "plus indiqué comme actif"),
    ("AP SH", "Autorisation d’entrer", "INCONNU", "2030-10-02", "reste à vérifier"),
    ("AP SH", "Autorisation d’entrer", "ACTIF", "illisible", "reste à vérifier"),
    ("TYPE 42", "Document à identifier", "ACTIF", "01/01/2030", "L’école doit préciser"),
])
def test_titles_explain_scope_and_validity_without_promising_admission(title, meaning, state, expiry, expected):
    row = desk._desk_document({"type": title, "state": state, "expires_at": expiry}, date(2027, 1, 8))
    assert meaning in row["name"] and expected in row["wording"]
    assert title in row["reference"]


def test_fresh_registry_results_replace_stale_titles_and_explain_absence_or_outages():
    old = {"type": "AP SH", "state": "ACTIF", "expires_at": "2030-10-03"}
    person = {"carte_pro": "OUI", "cnaps_card_validity": {"titles": [old]}}
    first = desk.cnaps_overview(person, "APS", {"raw_status": "INCONNU"})
    assert first["documents"] and first["card"] == "Oui" and "à confirmer" in first["request_wording"]
    for fields, message in [({}, "Aucun document retrouvé"), ({"cnaps_unavailable": True}, "indisponible"),
                            ({"nub_missing": True}, "numéro de dossier CNAPS manque")]:
        live = desk.cnaps_overview(person, "APS", {"raw_status": "TRANSMIS"}, {"cnaps": {"titles": [], **fields}})
        assert not live["documents"] and not live["titles"] and message in live["document_note"]
        assert "envoyée au CNAPS" in live["request_wording"] and live["card"] == "Oui"
    both = desk.cnaps_overview(person, "APS", {}, {"card_pro": {"titles": []}, "cnaps": {"titles": [old, old, {"type": "CP A3P", "state": "EXPIRE"}]}})
    assert len(both["documents"]) == 2 and "expiré" in both["documents"][1]["wording"]
    multiple = desk.cnaps_overview(person, "APS", {"raw_status": "ACCEPTE"}, {"match_count": 2, "cnaps": {"titles": [old]}})
    assert "Plusieurs dossiers" in multiple["request_wording"] and not multiple["titles"] and "Accepté" not in multiple["status"]
    assert desk.cnaps_overview({}, "BTS_CI") is None
