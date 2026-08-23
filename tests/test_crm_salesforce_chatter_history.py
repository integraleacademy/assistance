from crm_salesforce_chatter_history import register_salesforce_chatter_history


def test_history_endpoint_returns_only_the_requested_contact():
    import pytest
    Flask = pytest.importorskip("flask").Flask

    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test")
    store = {
        "crm_contacts": [
            {
                "id": "crm-one",
                "salesforce_chatter": [
                    {
                        "id": "sf-feed-one",
                        "date": "2026-08-21T10:00:00Z",
                        "texte": "Publication récente",
                        "comments": [{"id": "comment-one"}],
                    },
                    {
                        "id": "sf-feed-two",
                        "date": "2026-08-20T10:00:00Z",
                        "texte": "Publication ancienne",
                        "comments": [],
                    },
                ],
                "salesforce_chatter_imported_at": "2026-08-23T09:00:00+02:00",
            },
            {
                "id": "crm-two",
                "salesforce_chatter": [{"id": "other"}],
            },
        ]
    }

    def login_required(function):
        return function

    register_salesforce_chatter_history(
        app,
        load_data_fn=lambda: store,
        login_required_fn=login_required,
    )
    client = app.test_client()

    response = client.get(
        "/api/crm/contacts/crm-one/salesforce-chatter"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contact_id"] == "crm-one"
    assert payload["publication_count"] == 2
    assert payload["comment_count"] == 1
    assert [item["id"] for item in payload["items"]] == [
        "sf-feed-one",
        "sf-feed-two",
    ]
    assert payload["last_imported_at"] == "2026-08-23T09:00:00+02:00"


def test_history_endpoint_returns_404_for_an_unknown_contact():
    import pytest
    Flask = pytest.importorskip("flask").Flask

    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test")

    def login_required(function):
        return function

    register_salesforce_chatter_history(
        app,
        load_data_fn=lambda: {"crm_contacts": []},
        login_required_fn=login_required,
    )

    response = app.test_client().get(
        "/api/crm/contacts/unknown/salesforce-chatter"
    )
    assert response.status_code == 404
