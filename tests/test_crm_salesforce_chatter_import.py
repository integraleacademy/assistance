import copy
import io

from flask import Flask

from crm_salesforce_chatter_import import (
    _plain_text,
    import_salesforce_chatter_rows,
    parse_salesforce_comments_csv,
    parse_salesforce_publications_csv,
    parse_salesforce_users_csv,
    register_salesforce_chatter_import,
)


def _users_csv():
    return (
        b'"Id","Name","Email","IsActive"\n'
        b'"005author","CLEMENT VAILLANT","ecole@example.com","true"\n'
    )


def _publications_csv(*rows):
    header = (
        '"BestCommentId","Body","CommentCount","CreatedById","CreatedDate",'
        '"HasContent","HasFeedEntity","HasLink","HasVerifiedComment","Id",'
        '"InsertedById","IsClosed","IsDeleted","IsRichText","LastEditById",'
        '"LastEditDate","LastModifiedDate","LikeCount","LinkUrl","ParentId",'
        '"RelatedRecordId","Revision","Status","SystemModstamp","Title","Type"'
    )
    return (header + "\n" + "\n".join(rows) + "\n").encode("utf-8")


def _publication(
    *,
    item_id="0D5feed",
    parent_id="00Q123456789012ABC",
    body="<p>Bonjour <b>monsieur</b></p><p>À rappeler.</p>",
    comment_count="1",
    publication_type="TextPost",
    title="",
    link="",
    deleted="false",
):
    values = [
        "", body, comment_count, "005author", "2026-08-21T10:00:00.000Z",
        "false", "false", "false", "false", item_id, "005author", "false",
        deleted, "true", "", "", "2026-08-21T10:00:00.000Z", "0", link,
        parent_id, "", "1", "Published", "2026-08-21T10:00:00.000Z", title,
        publication_type,
    ]
    return ",".join(
        f'"{str(value).replace(chr(34), chr(34) * 2)}"'
        for value in values
    )


def _comments_csv(*rows):
    header = (
        '"CommentBody","CommentType","CreatedById","CreatedDate","FeedItemId",'
        '"HasEntityLinks","Id","InsertedById","IsDeleted","IsRichText",'
        '"IsVerified","LastEditById","LastEditDate","ParentId",'
        '"RelatedRecordId","Revision","Status","SystemModstamp",'
        '"ThreadChildrenCount","ThreadLastUpdatedDate","ThreadLevel","ThreadParentId"'
    )
    return (header + "\n" + "\n".join(rows) + "\n").encode("utf-8")


def _comment(
    *,
    comment_id="0D7comment",
    feed_item_id="0D5feed",
    parent_id="00Q123456789012ABC",
    body="<p><b>Commentaire</b> utile</p>",
):
    values = [
        body, "TextComment", "005author", "2026-08-21T11:00:00.000Z",
        feed_item_id, "", comment_id, "005author", "false", "true", "false",
        "", "", parent_id, "", "1", "Published",
        "2026-08-21T11:00:00.000Z", "0", "2026-08-21T11:00:00.000Z", "0", "",
    ]
    return ",".join(
        f'"{str(value).replace(chr(34), chr(34) * 2)}"'
        for value in values
    )


def _contact(**overrides):
    contact = {
        "id": "crm-1",
        "prenom": "Lina",
        "nom": "Martin",
        "formation": "A3P",
        "salesforce_id": "00Q123456789012",
        "salesforce_ids": ["00Q123456789012"],
        "salesforce_chatter": [],
        "updated_at": "2026-08-20T10:00:00+02:00",
    }
    contact.update(overrides)
    return contact


def _parsed():
    publications = parse_salesforce_publications_csv(
        _publications_csv(_publication())
    )
    comments = parse_salesforce_comments_csv(
        _comments_csv(_comment())
    )
    users = parse_salesforce_users_csv(_users_csv())
    return publications, comments, users


def test_exact_data_loader_files_are_parsed():
    publications, comments, users = _parsed()

    assert publications[0]["Id"] == "0D5feed"
    assert publications[0]["ParentId"] == "00Q123456789012ABC"
    assert comments[0]["FeedItemId"] == "0D5feed"
    assert users[0]["Name"] == "CLEMENT VAILLANT"


def test_rich_text_is_converted_to_readable_plain_text():
    assert _plain_text(
        "<p>Bonjour <b>Clément</b></p><ul><li>Un</li><li>Deux</li></ul>"
    ) == "Bonjour Clément\n• Un\n• Deux"


def test_eighteen_character_parent_matches_fifteen_character_crm_id():
    publications, comments, users = _parsed()
    contacts = [_contact()]

    result = import_salesforce_chatter_rows(
        contacts,
        publications,
        comments,
        users,
    )

    assert result["matched_contacts"] == 1
    assert result["publications_created"] == 1
    assert result["comments_created"] == 1
    item = contacts[0]["salesforce_chatter"][0]
    assert item["salesforce_feed_item_id"] == "0D5feed"
    assert item["author"] == "CLEMENT VAILLANT"
    assert item["texte"] == "Bonjour monsieur\n\nÀ rappeler."
    assert item["comments"][0]["texte"] == "Commentaire utile"


def test_non_lead_and_empty_automatic_rows_are_ignored():
    publication_rows = parse_salesforce_publications_csv(_publications_csv(
        _publication(item_id="case", parent_id="500123456789012ABC"),
        _publication(
            item_id="empty",
            body="",
            comment_count="0",
            publication_type="TrackedChange",
        ),
    ))
    comments = parse_salesforce_comments_csv(_comments_csv())
    users = parse_salesforce_users_csv(_users_csv())
    contacts = [_contact()]

    result = import_salesforce_chatter_rows(
        contacts,
        publication_rows,
        comments,
        users,
    )

    assert result["publications_ignored_non_lead"] == 1
    assert result["publications_ignored_empty"] == 1
    assert result["publications_created"] == 0
    assert contacts[0]["salesforce_chatter"] == []


def test_import_is_idempotent_and_updates_changed_text():
    publications, comments, users = _parsed()
    contacts = [_contact()]

    first = import_salesforce_chatter_rows(contacts, publications, comments, users)
    second = import_salesforce_chatter_rows(contacts, publications, comments, users)
    changed_publications = parse_salesforce_publications_csv(_publications_csv(
        _publication(body="<p>Texte corrigé</p>")
    ))
    changed = import_salesforce_chatter_rows(
        contacts,
        changed_publications,
        comments,
        users,
    )

    assert first["publications_created"] == 1
    assert second["publications_created"] == 0
    assert second["publications_unchanged"] == 1
    assert len(contacts[0]["salesforce_chatter"]) == 1
    assert len(contacts[0]["salesforce_chatter"][0]["comments"]) == 1
    assert changed["publications_updated"] == 1
    assert contacts[0]["salesforce_chatter"][0]["texte"] == "Texte corrigé"


def test_local_content_is_preserved_when_salesforce_item_is_updated():
    publications, comments, users = _parsed()
    contacts = [_contact()]
    import_salesforce_chatter_rows(contacts, publications, comments, users)
    item = contacts[0]["salesforce_chatter"][0]
    item["local_flag"] = "keep"
    item["comments"].append({
        "id": "local-comment",
        "texte": "Note CRM",
        "author": "Aurélie",
        "date": "2026-08-22T09:00:00+02:00",
    })

    changed_comments = parse_salesforce_comments_csv(_comments_csv(
        _comment(body="<p>Commentaire Salesforce corrigé</p>")
    ))
    result = import_salesforce_chatter_rows(
        contacts,
        publications,
        changed_comments,
        users,
    )

    item = contacts[0]["salesforce_chatter"][0]
    assert result["comments_updated"] == 1
    assert item["local_flag"] == "keep"
    assert {comment["id"] for comment in item["comments"]} == {
        "sf-comment-0D7comment",
        "local-comment",
    }


def test_unmatched_and_ambiguous_parent_ids_are_not_written():
    publications, comments, users = _parsed()
    unmatched_contacts = []
    unmatched = import_salesforce_chatter_rows(
        unmatched_contacts,
        publications,
        comments,
        users,
    )
    ambiguous_contacts = [
        _contact(id="one"),
        _contact(id="two"),
    ]
    ambiguous = import_salesforce_chatter_rows(
        ambiguous_contacts,
        publications,
        comments,
        users,
    )

    assert unmatched["unmatched_parent_count"] == 1
    assert unmatched["publications_created"] == 0
    assert ambiguous["ambiguous_parent_count"] == 1
    assert all(not contact["salesforce_chatter"] for contact in ambiguous_contacts)


def test_dry_run_never_mutates_contacts():
    publications, comments, users = _parsed()
    contacts = [_contact()]
    before = copy.deepcopy(contacts)

    result = import_salesforce_chatter_rows(
        contacts,
        publications,
        comments,
        users,
        dry_run=True,
    )

    assert result["publications_created"] == 1
    assert contacts == before


def test_route_requires_preview_token_and_saves_after_confirmation():
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test")
    store = {"crm_contacts": [_contact()]}
    saves = []

    register_salesforce_chatter_import(
        app,
        current_user_fn=lambda: {"role": "admin"},
        load_data_fn=lambda: store,
        login_required_fn=lambda function: function,
        save_data_fn=lambda data: saves.append(copy.deepcopy(data)),
    )
    client = app.test_client()

    def data(token="", dry_run="1"):
        payload = {
            "dry_run": dry_run,
            "publications_file": (
                io.BytesIO(_publications_csv(_publication())),
                "salesforce-publications.csv",
            ),
            "comments_file": (
                io.BytesIO(_comments_csv(_comment())),
                "salesforce-commentaires.csv",
            ),
            "users_file": (
                io.BytesIO(_users_csv()),
                "salesforce-utilisateurs.csv",
            ),
        }
        if token:
            payload["preview_token"] = token
        return payload

    preview = client.post(
        "/api/crm/import-salesforce-chatter",
        data=data(),
        content_type="multipart/form-data",
    )
    assert preview.status_code == 200
    preview_payload = preview.get_json()
    assert preview_payload["publications_created"] == 1
    assert store["crm_contacts"][0]["salesforce_chatter"] == []

    rejected = client.post(
        "/api/crm/import-salesforce-chatter",
        data=data(dry_run="0"),
        content_type="multipart/form-data",
    )
    assert rejected.status_code == 409

    imported = client.post(
        "/api/crm/import-salesforce-chatter",
        data=data(preview_payload["preview_token"], "0"),
        content_type="multipart/form-data",
    )
    assert imported.status_code == 200
    assert imported.get_json()["publications_created"] == 1
    assert len(store["crm_contacts"][0]["salesforce_chatter"]) == 1
    assert len(saves) == 1
