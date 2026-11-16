"""Les lectures périodiques utilisent le dernier instantané sans recopier le CRM."""
import copy

import pytest

from crm_app import legacy_app as crm


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(crm, "DATA_FILE", str(tmp_path / "crm.json"))
    return crm.app.test_client()


def forbid_mutable_read():
    pytest.fail("Une consultation a demandé une copie de toute la base")


def test_public_counts_follow_saves_without_copying(isolated_store, monkeypatch):
    client = isolated_store
    data = crm.load_data()
    data["demandes"] = [
        {"statut": " A TRAITER ", "motif": "Demande de devis détaillé", "statut_devis": "A envoyer"},
        {"statut": "Non traité", "motif": "Autre", "statut_devis": "A envoyer"},
        {"statut": "Traité", "motif": "Demande de devis détaillé", "statut_devis": "Envoyé"},
        {},
    ]
    data["hebergements"] = [{"paiement": "Payé"}, {"paiement": "En attente"}, {}]
    crm.save_data(data)
    monkeypatch.setattr(crm, "load_data", forbid_mutable_read)

    def assert_counts(demandes, devis, paid, unpaid):
        for path, expected in [
            ("/data.json", {"a_traiter": demandes}),
            ("/devis_data.json", {"a_envoyer": devis}),
            ("/hebergement_data.json", {"payes": paid, "non_payes": unpaid, "total": paid + unpaid}),
        ]:
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["Access-Control-Allow-Origin"] == "*"
            assert response.get_json() == expected

    assert_counts(2, 1, 1, 2)
    data["demandes"].clear()
    data["hebergements"].clear()
    crm.save_data(data)
    assert_counts(0, 0, 0, 0)


def test_notification_reads_share_snapshot_and_patch_stays_isolated(isolated_store, monkeypatch):
    client = isolated_store
    user = "reader@example.test"
    monkeypatch.setitem(crm.USERS, user, {"email": user, "role": "admin", "name": "Reader"})
    with client.session_transaction() as session:
        session["user_email"] = user
    data = crm.load_data()
    data["crm_notifications"] = [
        {"id": "mine", "recipient_email": user, "read": False},
        {"id": "other", "recipient_email": "other@example.test", "read": False},
    ]
    crm.save_data(data)
    previous = crm._load_data_snapshot()
    expected = copy.deepcopy(previous["crm_notifications"])
    with monkeypatch.context() as patch:
        patch.setattr(crm, "load_data", forbid_mutable_read)
        response = client.get("/api/crm/notifications")
        assert response.status_code == 200
        assert response.get_json() == expected[:1]
    assert client.patch("/api/crm/notifications", json={"id": "mine"}).status_code == 200
    assert previous["crm_notifications"] == expected
    assert [item["read"] for item in crm.load_data()["crm_notifications"]] == [True, False]
