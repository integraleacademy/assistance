import copy

from crm_salesforce_chatter_import import (
    _plain,
    import_salesforce_chatter_rows,
    parse_salesforce_comments_csv,
    parse_salesforce_publications_csv,
    parse_salesforce_users_csv,
)


def _publication(**overrides):
    row = {
        "Id": "0D5feed",
        "ParentId": "00Q123456789012ABC",
        "CreatedById": "005author",
        "CreatedDate": "2026-08-21T10:00:00.000Z",
        "Type": "TextPost",
        "Body": "<p>Bonjour <b>Madame</b></p><ul><li>Un</li><li>Deux</li></ul>",
        "Title": "",
        "LinkUrl": "",
        "CommentCount": "1",
        "LikeCount": "0",
        "Status": "Published",
        "IsDeleted": "false",
        "HasContent": "false",
    }
    row.update(overrides)
    return row


def _comment(**overrides):
    row = {
        "Id": "0D7comment",
        "FeedItemId": "0D5feed",
        "ParentId": "00Q123456789012ABC",
        "CreatedById": "005author",
        "CreatedDate": "2026-08-21T11:00:00.000Z",
        "CommentBody": "<p><b>Réponse validée.</b></p>",
        "CommentType": "TextComment",
        "Status": "Published",
        "IsDeleted": "false",
    }
    row.update(overrides)
    return row


def _users():
    return [{
        "Id": "005author",
        "Name": "CLEMENT VAILLANT",
        "Email": "ecole@integraleacademy.com",
        "IsActive": "true",
    }]


def _contact(**overrides):
    contact = {
        "id": "crm-contact",
        "prenom": "Lina",
        "nom": "Martin",
        "formation": "A3P",
        "salesforce_id": "00Q123456789012",
        "salesforce_ids": ["00Q123456789012"],
        "salesforce_chatter": [],
        "relances": [],
        "updated_at": "2026-08-20T10:00:00+02:00",
    }
    contact.update(overrides)
    return contact


def test_exact_data_loader_headers_are_parsed():
    publications = (
        '"Id","ParentId","CreatedById","CreatedDate","Type","Body"\n'
        '"0D5feed","00Q123456789012ABC","005author","2026-08-21T10:00:00Z","TextPost","Bonjour"\n'
    ).encode()
    comments = (
        '"Id","FeedItemId","ParentId","CreatedById","CreatedDate","CommentBody"\n'
        '"0D7comment","0D5feed","00Q123456789012ABC","005author","2026-08-21T11:00:00Z","Réponse"\n'
    ).encode()
    users = (
        '"Id","Name","Email","IsActive"\n'
        '"005author","CLEMENT VAILLANT","ecole@example.com","true"\n'
    ).encode()

    assert parse_salesforce_publications_csv(publications)[0]["ParentId"].startswith("00Q")
    assert parse_salesforce_comments_csv(comments)[0]["FeedItemId"] == "0D5feed"
    assert parse_salesforce_users_csv(users)[0]["Name"] == "CLEMENT VAILLANT"


def test_rich_text_is_safely_converted_to_plain_text():
    assert _plain("<p>Bonjour <b>Madame</b></p><ul><li>Un</li><li>Deux</li></ul>") == (
        "Bonjour Madame\n\n• Un\n• Deux"
    )


def test_lead_id_18_characters_matches_crm_id_15_characters():
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
    publication = contacts[0]["salesforce_chatter"][0]
    assert publication["author"] == "CLEMENT VAILLANT"
    assert publication["texte"] == "Bonjour Madame\n\n• Un\n• Deux"
    assert publication["comments"][0]["texte"] == "Réponse validée."


def test_task_parent_is_attached_through_the_existing_salesforce_relance():
    contact = _contact(
        salesforce_id="",
        salesforce_ids=[],
        relances=[{"salesforce_task_id": "00T123456789012"}],
    )
    publication = _publication(
        Id="0D5task",
        ParentId="00T123456789012ABC",
        CommentCount="0",
    )

    result = import_salesforce_chatter_rows(
        [contact],
        [publication],
        [],
        _users(),
    )

    assert result["matched_publications"] == 1
    assert contact["salesforce_chatter"][0]["salesforce_parent_id"].startswith("00T")


def test_empty_system_changes_and_unsupported_contacts_are_ignored():
    rows = [
        _publication(
            Id="empty",
            Body="",
            Title="",
            LinkUrl="",
            CommentCount="0",
            Type="TrackedChange",
        ),
        _publication(Id="contact", ParentId="003123456789012ABC"),
    ]

    result = import_salesforce_chatter_rows(
        [_contact()],
        rows,
        [],
        _users(),
        dry_run=True,
    )

    assert result["prepared_publications"] == 0
    assert result["publications_ignored_empty"] == 1
    assert result["publications_ignored_unsupported_parent"] == 1


def test_reimport_is_idempotent_and_source_changes_are_updated():
    contacts = [_contact()]
    import_salesforce_chatter_rows(
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
        [_publication(Body="<p>Texte modifié</p>")],
        [_comment(CommentBody="<p>Commentaire modifié</p>")],
        _users(),
    )

    assert identical["publications_created"] == 0
    assert identical["publications_unchanged"] == 1
    assert len(contacts[0]["salesforce_chatter"]) == 1
    assert changed["publications_updated"] == 1
    assert changed["comments_updated"] == 1
    assert contacts[0]["salesforce_chatter"][0]["texte"] == "Texte modifié"
    assert contacts[0]["salesforce_chatter"][0]["comments"][0]["texte"] == (
        "Commentaire modifié"
    )


def test_ambiguous_salesforce_id_is_never_imported():
    contacts = [
        _contact(id="one"),
        _contact(id="two", prenom="Autre"),
    ]

    result = import_salesforce_chatter_rows(
        contacts,
        [_publication()],
        [],
        _users(),
    )

    assert result["ambiguous_parent_count"] == 1
    assert result["matched_publications"] == 0
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
