import copy

import app as application


def contact(**values):
    base = {"id": values.pop("id", "existing"), "prenom": "Clément", "nom": "Vaillant",
            "mail": "", "telephone": "06 12 34 56 78", "formation": "APS",
            "statut": "Converti", "commercial": "Alice", "origine": "Téléphone",
            "commentaires": "note protégée", "created_at": "2020-01-01", "activities": []}
    base.update(values)
    return base


def reconcile(data, **payload):
    return application.find_or_create_crm_contact(data, payload, "test", external_id=payload.pop("event_id", None))


def test_unknown_prospect_creates_contact_and_request():
    data = {"crm_contacts": []}
    matched, request, created = reconcile(data, prenom="Lina", nom="Martin", mail="lina@test.fr")
    assert created and matched["id"] == request["contact_id"]
    assert len(data["crm_contacts"]) == len(data["crm_inbound_requests"]) == 1


def test_french_phone_formats_and_accented_name_match_one_contact():
    for phone in ("06 12 34 56 78", "+336.12.34.56.78", "00336-12-34-56-78"):
        original = contact()
        data = {"crm_contacts": [original]}
        matched, _, created = reconcile(data, prenom="Clement", nom="VAILLANT", telephone=phone)
        assert matched is original and not created and len(data["crm_contacts"]) == 1


def test_email_is_case_insensitive_and_only_empty_fields_are_completed():
    original = contact(mail="LINA@Example.FR", telephone="", formation="", lieu="Non renseigné")
    protected = {key: copy.deepcopy(original[key]) for key in
                 ("id", "statut", "commercial", "origine", "commentaires", "created_at")}
    data = {"crm_contacts": [original]}
    matched, request, created = reconcile(data, prenom="Clément", nom="Vaillant",
                                          mail=" lina@example.fr ", telephone="0701020304",
                                          formation="A3P", lieu="Paris")
    assert matched is original and not created
    assert original["telephone"] == "0701020304" and original["formation"] == "A3P" and original["lieu"] == "Paris"
    assert {key: original[key] for key in protected} == protected
    assert request["differences"] == []


def test_different_values_are_preserved_and_reported():
    original = contact(mail="old@test.fr", telephone="0612345678", formation="APS")
    before = copy.deepcopy(original)
    data = {"crm_contacts": [original]}
    matched, request, _ = reconcile(data, prenom="Clément", nom="Vaillant",
                                    mail="old@test.fr", telephone="0798765432", formation="A3P")
    # L'e-mail certain rattache, les autres valeurs restent seulement dans la demande.
    assert matched is original
    assert original["telephone"] == before["telephone"] and original["formation"] == before["formation"]
    assert {"telephone", "formation"} <= set(request["differences"])
    assert request["formation"] == "A3P"


def test_same_name_with_different_coordinates_goes_to_review_without_contact():
    data = {"crm_contacts": [contact()]}
    matched, request, created = reconcile(data, prenom="Clement", nom="Vaillant",
                                          mail="other@test.fr", telephone="0711111111")
    assert matched is None and not created and request["status"] == "pending_review"
    assert len(data["crm_contacts"]) == 1


def test_duplicate_coordinate_and_split_email_phone_are_ambiguous():
    for contacts, payload in [
        ([contact(id="a"), contact(id="b")], {"prenom": "Clément", "nom": "Vaillant", "telephone": "0612345678"}),
        ([contact(id="a", mail="a@test.fr"), contact(id="b", telephone="0700000000")],
         {"prenom": "Clément", "nom": "Vaillant", "mail": "a@test.fr", "telephone": "0700000000"}),
    ]:
        data = {"crm_contacts": contacts}
        matched, request, _ = reconcile(data, **payload)
        assert matched is None and request["status"] == "pending_review"


def test_converted_status_history_and_all_protected_data_remain_unchanged():
    original = contact(mail="person@test.fr", activities=[{"id": "old", "title": "Historique"}])
    snapshot = copy.deepcopy(original)
    data = {"crm_contacts": [original]}
    reconcile(data, prenom="Clément", nom="Vaillant", mail="PERSON@test.fr", formation="A3P")
    for field in ("id", "statut", "commercial", "origine", "commentaires", "created_at", "formation"):
        assert original[field] == snapshot[field]
    assert any(row["id"] == "old" for row in original["activities"])
    assert len(data["crm_inbound_requests"]) == 1


def test_external_event_is_idempotent():
    data = {"crm_contacts": []}
    payload = {"prenom": "Lina", "nom": "Martin", "mail": "lina@test.fr"}
    application.find_or_create_crm_contact(data, payload, "zapier", external_id="evt-1")
    application.find_or_create_crm_contact(data, payload, "zapier", external_id="evt-1")
    assert len(data["crm_contacts"]) == len(data["crm_inbound_requests"]) == 1
