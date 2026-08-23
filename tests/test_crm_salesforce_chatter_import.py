import copy
import io

from crm_salesforce_chatter_import import (
    _plain_text,
    import_salesforce_chatter_rows,
    parse_salesforce_comments_csv,
    parse_salesforce_publications_csv,
    parse_salesforce_users_csv,
    register_salesforce_chatter_import,
)


def _csv(headers, rows):
    import csv
    import io
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _users():
    return [{
        "Id": "005AUTHOR000001",
        "Name": "Clément Test",
        "Email": "clement@example.com",
        "IsActive": "true",
    }]


def _publication(**overrides):
    row = {
        "Id": "0D5POST00000001",
        "ParentId": "00QLEAD00000001AAA",
        "CreatedById": "005AUTHOR000001",
        "CreatedDate": "2026-08-20T10:00:00.000Z",
        "LastModifiedDate": "2026-08-20T10:00:00.000Z",
        "Type": "TextPost",
        "Body": "<p>Premier appel</p><p><b>À rappeler.</b></p>",
        "Title": "",
        "LinkUrl": "",
        "CommentCount": "1",
        "LikeCount": "0",
        "IsDeleted": "false",
        "Status": "Published",
        "HasContent": "false",
        "RelatedRecordId": "",
    }
    row.update(overrides)
    return row


def _comment(**overrides):
    row = {
        "Id": "0D7COMMENT00001",
        "FeedItemId": "0D5POST00000001",
        "ParentId": "00QLEAD00000001AAA",
        "CreatedById": "005AUTHOR000001",
        "CreatedDate": "2026-08-20T11:00:00.000Z",
        "CommentBody": "<p><b>CPF validé.</b></p>",
        "CommentType": "TextComment",
        "IsDeleted": "false",
        "Status": "Published",
        "LastEditDate": "",
    }
    row.update(overrides)
    return row


def _contact(**overrides):
    contact = {
        "id": "crm-1",
        "prenom": "Lina",
        "nom": "Martin",
        "formation": "A3P",
        "salesforce_id": "00QLEAD00000001",
        "salesforce_ids": ["00QLEAD00000001"],
        "salesforce_chatter": [],
        "updated_at": "2026-08-20T09:00:00+02:00",
    }
    contact.update(overrides)
    return contact


def test_rich_text_is_converted_to_readable_plain_text():
    text = _plain_text("<p>Bonjour&nbsp;Monsieur</p><ul><li>Point 1</li><li>Point 2</li></ul>")
    assert text == "Bonjour Monsieur\n• Point 1\n• Point 2"


def test_exact_data_loader_headers_are_parsed():
    publication_headers = list(_publication().keys())
    comment_headers = list(_comment().keys())
    user_headers = list(_users()[0].keys())

    publications = parse_salesforce_publications_csv(
        _csv(publication_headers, [_publication()])
    )
    comments = parse_salesforce_comments_csv(
        _csv(comment_headers, [_comment()])
    )
    users = parse_salesforce_users_csv(
        _csv(user_headers, _users())
    )

    assert publications[0]["Id"] == "0D5POST00000001"
    assert comments[0]["FeedItemId"] == "0D5POST00000001"
    assert users[0]["Name"] == "Clément Test"


def test_import_matches_15_and_18_character_salesforce_ids():
    contacts = [_contact()]

    result = import_salesforce_chatter_rows(
        contacts,
        [_publication()],
        [_comment()],
        _users(),
    )

    assert result["matched_contacts"] == 1
    assert result["publications_created"] == 1
    assert result["comments_created"] == 1
    assert result["unmatched_parent_count"] == 0
    item = contacts[0]["salesforce_chatter"][0]
    assert item["texte"] == "Premier appel\nÀ rappeler."
    assert item["author"] == "Clément Test"
    assert item["author_email"].startswith("salesforce://")
    assert item["comments"][0]["texte"] == "CPF validé."


def test_import_is_idempotent_and_updates_changed_content():
    contacts = [_contact()]
    first = import_salesforce_chatter_rows(
        contacts,
        [_publication()],
        [_comment()],
        _users(),
    )
    identical = import_salesforce_chatter_rows(
        contacts,
        [_publication()],
        [_comment()],
        _users(),
    )
    changed = import_salesforce_chatter_rows(
        contacts,
        [_publication(Body="<p>Texte corrigé</p>")],
        [_comment(CommentBody="<p>Commentaire corrigé</p>")],
        _users(),
    )

    assert first["publications_created"] == 1
    assert identical["publications_created"] == 0
    assert identical["publications_unchanged"] == 1
    assert identical["contacts_updated"] == 0
    assert changed["publications_updated"] == 1
    assert changed["comments_updated"] == 1
    assert len(contacts[0]["salesforce_chatter"]) == 1
    assert contacts[0]["salesforce_chatter"][0]["texte"] == "Texte corrigé"
    assert contacts[0]["salesforce_chatter"][0]["comments"][0]["texte"] == "Commentaire corrigé"


def test_local_comments_are_preserved_when_salesforce_content_is_updated():
    contacts = [_contact()]
    import_salesforce_chatter_rows(
        contacts,
        [_publication()],
        [_comment()],
        _users(),
    )
    contacts[0]["salesforce_chatter"][0]["comments"].append({
        "id": "local-comment",
        "date": "2026-08-21T09:00:00Z",
        "texte": "Note ajoutée dans le CRM",
        "author": "Cassandre",
        "author_email": "cassandre@example.com",
    })

    import_salesforce_chatter_rows(
        contacts,
        [_publication(Body="<p>Texte corrigé</p>")],
        [_comment()],
        _users(),
    )

    comments = contacts[0]["salesforce_chatter"][0]["comments"]
    assert len(comments) == 2
    assert any(comment.get("id") == "local-comment" for comment in comments)


def test_empty_tracked_change_is_ignored_unless_it_has_a_comment():
    contacts = [_contact()]
    empty = _publication(
        Id="0D5TRACKED00001",
        Type="TrackedChange",
        Body="",
        Title="",
        LinkUrl="",
        CommentCount="0",
    )

    result = import_salesforce_chatter_rows(
        contacts,
        [empty],
        [],
        _users(),
    )
    assert result["publications_ignored_empty"] == 1
    assert result["publications_created"] == 0

    comment = _comment(
        FeedItemId="0D5TRACKED00001",
        CommentBody="<p>Information utile</p>",
    )
    result = import_salesforce_chatter_rows(
        contacts,
        [{**empty, "CommentCount": "1"}],
        [comment],
        _users(),
    )
    assert result["publications_created"] == 1
    assert contacts[0]["salesforce_chatter"][0]["texte"] == "Modification Salesforce"


def test_non_lead_feed_items_and_unmatched_leads_are_not_imported():
    contacts = [_contact()]
    rows = [
        _publication(Id="contact-post", ParentId="003CONTACT00001AAA"),
        _publication(Id="other-lead", ParentId="00QOTHER0000001AAA"),
    ]

    result = import_salesforce_chatter_rows(
        contacts,
        rows,
        [],
        _users(),
    )

    assert result["publications_ignored_non_lead"] == 1
    assert result["unmatched_parent_count"] == 1
    assert result["publications_created"] == 0
    assert contacts[0]["salesforce_chatter"] == []


def test_ambiguous_salesforce_id_is_blocked():
    contacts = [
        _contact(id="crm-1"),
        _contact(id="crm-2", prenom="Autre"),
    ]

    result = import_salesforce_chatter_rows(
        contacts,
        [_publication()],
        [_comment()],
        _users(),
    )

    assert result["ambiguous_parent_count"] == 1
    assert result["publications_created"] == 0
    assert all(not contact["salesforce_chatter"] for contact in contacts)


def test_dry_run_never_mutates_contacts():
    contacts = [_contact()]
    before = copy.deepcopy(contacts)

    result = import_salesforce_chatter_rows(
        contacts,
        [_publication()],
        [_comment()],
        _users(),
        dry_run=True,
    )

    assert result["publications_created"] == 1
    assert contacts == before


def test_route_requires_preview_token_before_write():
    import pytest
    Flask = pytest.importorskip("flask").Flask

    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test")
    store = {"crm_contacts": [_contact()]}
    saves = []

    def load_data():
        return store

    def save_data(data):
        saves.append(copy.deepcopy(data))

    def login_required(function):
        return function

    register_salesforce_chatter_import(
        app,
        current_user_fn=lambda: {"role": "admin"},
        load_data_fn=load_data,
        login_required_fn=login_required,
        save_data_fn=save_data,
    )
    client = app.test_client()
    publication_raw = _csv(list(_publication().keys()), [_publication()])
    comment_raw = _csv(list(_comment().keys()), [_comment()])
    user_raw = _csv(list(_users()[0].keys()), _users())

    preview = client.post(
        "/api/crm/import-salesforce-chatter",
        data={
            "dry_run": "1",
            "publications_file": (io.BytesIO(publication_raw), "publications.csv"),
            "comments_file": (io.BytesIO(comment_raw), "comments.csv"),
            "users_file": (io.BytesIO(user_raw), "users.csv"),
        },
        content_type="multipart/form-data",
    )
    assert preview.status_code == 200
    payload = preview.get_json()
    assert payload["publications_created"] == 1
    assert payload["preview_token"]
    assert store["crm_contacts"][0]["salesforce_chatter"] == []

    rejected = client.post(
        "/api/crm/import-salesforce-chatter",
        data={
            "publications_file": (io.BytesIO(publication_raw), "publications.csv"),
            "comments_file": (io.BytesIO(comment_raw), "comments.csv"),
            "users_file": (io.BytesIO(user_raw), "users.csv"),
        },
        content_type="multipart/form-data",
    )
    assert rejected.status_code == 409

    imported = client.post(
        "/api/crm/import-salesforce-chatter",
        data={
            "preview_token": payload["preview_token"],
            "publications_file": (io.BytesIO(publication_raw), "publications.csv"),
            "comments_file": (io.BytesIO(comment_raw), "comments.csv"),
            "users_file": (io.BytesIO(user_raw), "users.csv"),
        },
        content_type="multipart/form-data",
    )
    assert imported.status_code == 200
    assert imported.get_json()["publications_created"] == 1
    assert len(store["crm_contacts"][0]["salesforce_chatter"]) == 1
    assert len(saves) == 1
