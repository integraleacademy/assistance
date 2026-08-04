"""Tests d'intégration du chat Socket.IO (session Flask réelle)."""
import uuid

import pytest

import app as application
from chat import Message, Participant, TEAM_ID, select


def web_client(email=None):
    client = application.app.test_client()
    if email:
        with client.session_transaction() as sess:
            sess["user_email"] = email
    return client


def socket_client(email=None):
    client = web_client(email)
    return application.socketio.test_client(application.app, flask_test_client=client, namespace="/chat"), client


@pytest.fixture(autouse=True)
def reset_chat():
    application.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False, CHAT_PRESENCE_GRACE=.02)
    application.chat.rate.clear()
    with application.chat.Session.begin() as db:
        db.query(Message).delete()
        db.query(Participant).filter(Participant.conversation_id != TEAM_ID).delete()
        from chat import Conversation
        db.query(Conversation).filter(Conversation.id != TEAM_ID).delete()
        for part in db.query(Participant):
            part.last_read_message_id = None
    yield


def test_socket_refuses_anonymous_and_accepts_authenticated_presence():
    anon, _ = socket_client()
    assert not anon.is_connected("/chat")
    user, _ = socket_client("clement@integraleacademy.com")
    assert user.is_connected("/chat")
    assert application.chat.presence.online("clement@integraleacademy.com")
    user.disconnect(namespace="/chat")


def test_multiple_tabs_stay_online_until_last_disconnect():
    one, _ = socket_client("cassandre@integraleacademy.com")
    two, _ = socket_client("cassandre@integraleacademy.com")
    one.disconnect(namespace="/chat")
    application.socketio.sleep(.03)
    assert application.chat.presence.online("cassandre@integraleacademy.com")
    two.disconnect(namespace="/chat")
    application.socketio.sleep(.03)
    assert not application.chat.presence.online("cassandre@integraleacademy.com")


def test_http_presence_fallback_marks_an_open_crm_online():
    client = web_client("elsa@integraleacademy.com")
    assert client.post("/api/chat/presence").get_json() == {"ok": True}
    assert application.chat.presence.online("elsa@integraleacademy.com")
    assert web_client().post("/api/chat/presence").status_code == 401


def test_team_send_persists_broadcasts_deduplicates_and_unread():
    sender, _ = socket_client("clement@integraleacademy.com")
    recipient, recipient_http = socket_client("aurelie@integraleacademy.com")
    token = str(uuid.uuid4())
    payload = {"conversation_id": TEAM_ID, "client_message_id": token, "body": "Bonjour <script>alert(1)</script>"}
    ack = sender.emit("chat:send", payload, namespace="/chat", callback=True)
    assert ack["ok"] and ack["message"]["body"] == payload["body"]  # texte brut, jamais HTML côté client
    assert any(x["name"] == "chat:new_message" for x in recipient.get_received("/chat"))
    duplicate = sender.emit("chat:send", payload, namespace="/chat", callback=True)
    assert duplicate["ok"] and duplicate["deduplicated"] and duplicate["message"]["id"] == ack["message"]["id"]
    with application.chat.Session() as db:
        assert db.scalar(select(Message).where(Message.client_message_id == token)).body == payload["body"]
    assert recipient_http.get("/api/chat/bootstrap").get_json()["unread"][str(TEAM_ID)] == 1


def test_direct_message_only_reaches_participants_and_access_is_enforced():
    sender, _ = socket_client("clement@integraleacademy.com")
    peer, peer_http = socket_client("elsa@integraleacademy.com")
    outsider, outsider_http = socket_client("aurelie@integraleacademy.com")
    opened = sender.emit("chat:open_direct", {"user_id": "elsa@integraleacademy.com"}, namespace="/chat", callback=True)
    cid = opened["conversation"]["id"]
    peer.get_received("/chat"); outsider.get_received("/chat")
    ack = sender.emit("chat:send", {"conversation_id": cid, "client_message_id": str(uuid.uuid4()), "body": "Privé"}, namespace="/chat", callback=True)
    assert ack["ok"]
    assert any(x["name"] == "chat:new_message" for x in peer.get_received("/chat"))
    assert not any(x["name"] == "chat:new_message" for x in outsider.get_received("/chat"))
    assert outsider_http.get(f"/api/chat/conversations/{cid}/messages").status_code == 403
    assert peer_http.get(f"/api/chat/conversations/{cid}/messages").status_code == 200


def test_validation_read_and_history_pagination():
    sender, _ = socket_client("clement@integraleacademy.com")
    recipient, http = socket_client("cassandre@integraleacademy.com")
    assert not sender.emit("chat:send", {"conversation_id": 1, "client_message_id": "empty", "body": "  "}, namespace="/chat", callback=True)["ok"]
    assert not sender.emit("chat:send", {"conversation_id": 1, "client_message_id": "long", "body": "x" * 2001}, namespace="/chat", callback=True)["ok"]
    ids = []
    for i in range(55):
        ack = sender.emit("chat:send", {"conversation_id": 1, "client_message_id": str(uuid.uuid4()), "body": f"m{i}"}, namespace="/chat", callback=True)
        if ack["ok"]: ids.append(ack["message"]["id"])
        else:  # Le rate limiter est testé séparément; le vider pour construire l'historique.
            application.chat.rate.clear()
    page = http.get("/api/chat/conversations/1/messages?limit=10").get_json()
    assert len(page["messages"]) == 10 and page["has_more"]
    last = page["messages"][-1]["id"]
    read = recipient.emit("chat:read", {"conversation_id": 1, "message_id": last}, namespace="/chat", callback=True)
    assert read["ok"] and http.get("/api/chat/bootstrap").get_json()["unread"]["1"] == 0


def test_rate_limit_ten_messages_per_ten_seconds():
    sender, _ = socket_client("clement@integraleacademy.com")
    replies = [sender.emit("chat:send", {"conversation_id": 1, "client_message_id": str(uuid.uuid4()), "body": "test"}, namespace="/chat", callback=True) for _ in range(11)]
    assert all(x["ok"] for x in replies[:10]) and not replies[10]["ok"]
