import app as application


def crm_client(tmp_path, monkeypatch, email="cassandre@integraleacademy.com"):
    monkeypatch.setattr(application, "DATA_FILE", str(tmp_path / "data.json"))
    application._DATA_CACHE_PAYLOAD = None
    application._DATA_CACHE_SIGNATURE = None
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    client = application.app.test_client()
    with client.session_transaction() as session:
        session["user_email"] = email
    return client


def test_publication_author_can_edit_text_without_losing_related_data(tmp_path, monkeypatch):
    client = crm_client(tmp_path, monkeypatch)
    contact = client.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    created = client.post(
        f"/api/crm/contacts/{contact['id']}/publications",
        json={"texte": "Avant"},
    ).get_json()["publication"]

    response = client.patch(
        f"/api/crm/contacts/{contact['id']}/publications/{created['id']}",
        json={"texte": "  Après  "},
    )

    assert response.status_code == 200
    edited = response.get_json()["publication"]
    assert edited["id"] == created["id"]
    assert edited["texte"] == "Après"
    assert edited["comments"] == []
    assert edited["likes"] == []


def test_publication_edit_rejects_empty_text_and_another_user(tmp_path, monkeypatch):
    client = crm_client(tmp_path, monkeypatch)
    contact = client.post("/api/crm/contacts", json={"prenom": "Lina"}).get_json()
    publication = client.post(
        f"/api/crm/contacts/{contact['id']}/publications",
        json={"texte": "Texte protégé"},
    ).get_json()["publication"]
    endpoint = f"/api/crm/contacts/{contact['id']}/publications/{publication['id']}"

    empty = client.patch(endpoint, json={"texte": "   "})
    assert empty.status_code == 400
    assert empty.get_json()["error"] == "Le texte de la publication est requis"

    with client.session_transaction() as session:
        session["user_email"] = "aurelie@integraleacademy.com"
    forbidden = client.patch(endpoint, json={"texte": "Modification interdite"})
    assert forbidden.status_code == 403
    assert forbidden.get_json()["error"] == "Vous ne pouvez modifier que vos publications"

    stored = application.load_data()["crm_contacts"][0]["publications"][0]
    assert stored["texte"] == "Texte protégé"
