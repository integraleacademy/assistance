"""Chat interne persistant et temps reel, adosse a l'authentification Flask."""
from __future__ import annotations

import os
import logging
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from flask import abort, jsonify, request, session
from flask_socketio import SocketIO, join_room
from sqlalchemy import (Column, DateTime, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, create_engine, event, func, select)
from sqlalchemy.orm import declarative_base, sessionmaker

NAMESPACE = "/chat"
TEAM_ID = 1
Base = declarative_base()


class Conversation(Base):
    __tablename__ = "chat_conversations"
    id = Column(Integer, primary_key=True)
    type = Column(String(10), nullable=False)
    title = Column(String(120), nullable=False)
    direct_key = Column(String(600), unique=True)
    created_by_user_id = Column(String(320))
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class Participant(Base):
    __tablename__ = "chat_participants"
    conversation_id = Column(Integer, ForeignKey("chat_conversations.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(String(320), primary_key=True)
    joined_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    last_read_message_id = Column(Integer)
    last_read_at = Column(DateTime(timezone=True))
    __table_args__ = (Index("ix_chat_participant_user", "user_id"), Index("ix_chat_unread", "user_id", "last_read_message_id"))


class Message(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False)
    sender_user_id = Column(String(320), nullable=False)
    client_message_id = Column(String(64), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (UniqueConstraint("sender_user_id", "client_message_id", name="uq_chat_sender_client"),
                      Index("ix_chat_message_conversation_created", "conversation_id", "created_at", "id"))


class PresenceConnection(Base):
    """Une ligne partagée par onglet; aucun état de présence ne vit en mémoire."""
    __tablename__ = "chat_presence_connections"
    user_id = Column(String(320), primary_key=True)
    tab_id = Column(String(64), primary_key=True)
    last_heartbeat_at = Column(DateTime(timezone=True), nullable=False)
    connected_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (Index("ix_chat_presence_heartbeat", "last_heartbeat_at"),)


class SharedPresence:
    """Présence Redis (TTL natif) ou SQL, utilisable par plusieurs processus."""
    ttl = 60

    def __init__(self, service, redis_url=None):
        self.service = service
        self.redis = None
        if redis_url:
            try:
                import redis
                self.redis = redis.Redis.from_url(redis_url, decode_responses=True)
                self.redis.ping()
            except Exception:
                logging.exception("Redis présence indisponible, bascule SQL")
                self.redis = None
        self.backend = "redis" if self.redis else "sql"

    @staticmethod
    def _tab(tab_id):
        value = str(tab_id or "").strip()[:64]
        return value if value and all(c.isalnum() or c in "-_" for c in value) else None

    def heartbeat(self, user_id, tab_id):
        tab_id = self._tab(tab_id)
        if not tab_id: return False
        was_online = self.online(user_id)
        now = datetime.now(timezone.utc)
        if self.redis:
            self.redis.setex(f"chat:presence:{user_id}:{tab_id}", self.ttl, now.isoformat())
            self.redis.set(f"chat:last_seen:{user_id}", now.isoformat())
        else:
            with self.service.Session.begin() as db:
                row = db.get(PresenceConnection, (user_id, tab_id))
                if row:
                    row.last_heartbeat_at = row.last_seen_at = now
                else:
                    db.add(PresenceConnection(user_id=user_id, tab_id=tab_id,
                        connected_at=now, last_heartbeat_at=now, last_seen_at=now))
                self._cleanup(db, now)
        if not was_online: self.broadcast(user_id, True)
        return True

    def close(self, user_id, tab_id):
        tab_id = self._tab(tab_id)
        if not tab_id: return
        # Grace de navigation: la ligne/clé expire naturellement; un nouveau document
        # renouvelle le même tab_id immédiatement.
        if self.redis:
            key = f"chat:presence:{user_id}:{tab_id}"
            if self.redis.exists(key): self.redis.expire(key, int(self.service.presence_grace))
        else:
            cutoff = datetime.now(timezone.utc).timestamp() - self.ttl + self.service.presence_grace
            with self.service.Session.begin() as db:
                row = db.get(PresenceConnection, (user_id, tab_id))
                if row: row.last_heartbeat_at = datetime.fromtimestamp(cutoff, timezone.utc)

    def _cleanup(self, db, now=None):
        cutoff = datetime.fromtimestamp((now or datetime.now(timezone.utc)).timestamp() - self.ttl, timezone.utc)
        db.query(PresenceConnection).filter(PresenceConnection.last_heartbeat_at < cutoff).delete(synchronize_session=False)

    def online(self, user_id):
        if self.redis:
            return bool(next(self.redis.scan_iter(match=f"chat:presence:{user_id}:*", count=1), None))
        cutoff = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - self.ttl, timezone.utc)
        with self.service.Session() as db:
            return bool(db.scalar(select(func.count()).select_from(PresenceConnection).where(
                PresenceConnection.user_id == user_id, PresenceConnection.last_heartbeat_at >= cutoff)))

    def last_seen(self, user_id):
        if self.redis:
            return self.redis.get(f"chat:last_seen:{user_id}")
        with self.service.Session() as db:
            value = db.scalar(select(func.max(PresenceConnection.last_seen_at)).where(PresenceConnection.user_id == user_id))
            return value.isoformat() if value else None

    def broadcast(self, user_id, online=None):
        online = self.online(user_id) if online is None else online
        self.service.socketio.emit("presence:changed", {"user_id": user_id, "online": online,
            "last_seen_at": None if online else self.last_seen(user_id)}, namespace=NAMESPACE)


class ChatService:
    def __init__(self, app, users, current_user):
        self.app, self.users, self.current_user = app, users, current_user
        url = os.getenv("CHAT_DATABASE_URL") or os.getenv("DATABASE_URL")
        if not url:
            disk = (os.getenv("DATA_DIR") or os.getenv("RENDER_DISK_PATH")
                    or os.getenv("RENDER_DISK_MOUNT_PATH") or os.path.join(app.root_path, "data"))
            path = os.getenv("CHAT_DB_PATH") or os.path.join(disk, "chat.sqlite3")
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            url = "sqlite:///" + os.path.abspath(path)
        if url.startswith("postgres://"): url = "postgresql://" + url[11:]
        args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, future=True, pool_pre_ping=True, connect_args=args)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def sqlite_options(conn, _):
                cur = conn.cursor(); cur.execute("PRAGMA journal_mode=WAL"); cur.execute("PRAGMA busy_timeout=30000"); cur.execute("PRAGMA foreign_keys=ON"); cur.close()
        self.Session = sessionmaker(self.engine, expire_on_commit=False, future=True)
        Base.metadata.create_all(self.engine)
        self._ensure_team()
        redis_url = os.getenv("REDIS_URL")
        self.socketio = SocketIO(app, async_mode="threading", message_queue=redis_url or None,
                                 ping_interval=20, ping_timeout=45, cors_allowed_origins=[])
        self.presence_grace = float(app.config.get("CHAT_PRESENCE_GRACE", 15))
        self.presence = SharedPresence(self, redis_url)
        self.rate = defaultdict(deque); self.rate_lock = threading.Lock()
        self._routes(); self._events()

    def _ensure_team(self):
        with self.Session.begin() as db:
            team = db.get(Conversation, TEAM_ID)
            if not team:
                db.add(Conversation(id=TEAM_ID, type="team", title="Équipe"))
            for uid in self.users:
                if not db.get(Participant, (TEAM_ID, uid)):
                    db.add(Participant(conversation_id=TEAM_ID, user_id=uid))

    def _uid(self):
        user = self.current_user()
        return user["email"] if user and user["email"] in self.users else None

    def _member(self, db, conversation_id, uid):
        return db.get(Participant, (conversation_id, uid)) is not None

    def _message_json(self, message):
        sender = self.users.get(message.sender_user_id, {})
        created = message.created_at
        if created.tzinfo is None: created = created.replace(tzinfo=timezone.utc)
        return {"id": message.id, "conversation_id": message.conversation_id,
                "sender_user_id": message.sender_user_id, "sender_name": sender.get("name", message.sender_user_id),
                "client_message_id": message.client_message_id, "body": message.body,
                "created_at": created.astimezone(timezone.utc).isoformat()}

    def _unreads(self, db, uid):
        result = {}
        parts = db.scalars(select(Participant).where(Participant.user_id == uid)).all()
        for part in parts:
            q = select(func.count(Message.id)).where(Message.conversation_id == part.conversation_id,
                Message.sender_user_id != uid, Message.id > (part.last_read_message_id or 0))
            result[str(part.conversation_id)] = db.scalar(q) or 0
        return result

    def _conversation_json(self, db, conv, uid):
        peers = db.scalars(select(Participant.user_id).where(Participant.conversation_id == conv.id, Participant.user_id != uid)).all()
        peer = peers[0] if conv.type == "direct" and peers else None
        return {"id": conv.id, "type": conv.type, "title": self.users.get(peer, {}).get("name", conv.title), "peer_user_id": peer}

    def _routes(self):
        @self.app.get("/api/chat/bootstrap")
        def bootstrap():
            uid = self._uid()
            if not uid: abort(401)
            self._ensure_team()
            with self.Session() as db:
                ids = db.scalars(select(Participant.conversation_id).where(Participant.user_id == uid)).all()
                conversations = db.scalars(select(Conversation).where(Conversation.id.in_(ids)).order_by(Conversation.type.desc(), Conversation.id)).all()
                unread = self._unreads(db, uid)
                conversation_data = [self._conversation_json(db, c, uid) for c in conversations]
            colleagues = [{"id": u["email"], "name": u["name"], "online": self.presence.online(u["email"]),
                           "last_seen_at": self.presence.last_seen(u["email"])}
                          for u in self.users.values() if u["email"] != uid]
            return jsonify({"current_user_id": uid, "conversations": conversation_data,
                            "colleagues": colleagues, "unread": unread,
                            "online_users_count": sum(self.presence.online(x) for x in self.users)})

        @self.app.post("/api/chat/presence")
        def presence_heartbeat():
            uid = self._uid()
            if not uid: abort(401)
            payload = request.get_json(silent=True) or {}
            tab_id = payload.get("tab_id") or request.form.get("tab_id")
            if not self.presence.heartbeat(uid, tab_id):
                return jsonify({"ok": False, "error": "Identifiant d’onglet invalide"}), 400
            return jsonify({"ok": True, "online_users_count": sum(self.presence.online(x) for x in self.users)})

        @self.app.post("/api/chat/presence/close")
        def presence_close():
            uid = self._uid()
            if not uid: abort(401)
            payload = request.get_json(silent=True) or request.form
            self.presence.close(uid, payload.get("tab_id"))
            return jsonify({"ok": True})

        @self.app.post("/api/chat/direct")
        def direct_http():
            uid = self._uid()
            if not uid: abort(401)
            peer = str((request.get_json(silent=True) or {}).get("peer_user_id", "")).lower()
            data, error = self._open_direct(uid, peer)
            if error: return jsonify({"ok": False, "error": error}), 400
            self.socketio.emit("chat:conversation_created", data, room=f"user:{peer}", namespace=NAMESPACE)
            return jsonify({"ok": True, "conversation": data})

        @self.app.get("/api/chat/conversations/<int:conversation_id>/messages")
        def history(conversation_id):
            uid = self._uid()
            if not uid: abort(401)
            limit = min(max(request.args.get("limit", 50, type=int), 1), 50)
            before = request.args.get("before_id", type=int)
            after = request.args.get("after_id", type=int)
            with self.Session() as db:
                if not self._member(db, conversation_id, uid): abort(403)
                q = select(Message).where(Message.conversation_id == conversation_id)
                if before: q = q.where(Message.id < before)
                if after: q = q.where(Message.id > after).order_by(Message.id).limit(limit)
                else: q = q.order_by(Message.id.desc()).limit(limit)
                rows = db.scalars(q).all()
                if not after: rows.reverse()
                return jsonify({"messages": [self._message_json(m) for m in rows], "has_more": len(rows) == limit})

        @self.app.post("/api/chat/conversations/<int:conversation_id>/messages")
        def send_http(conversation_id):
            uid = self._uid()
            if not uid: abort(401)
            data, status = self._save_message(uid, conversation_id, request.get_json(silent=True) or {})
            return jsonify(data), status

        @self.app.post("/api/chat/conversations/<int:conversation_id>/read")
        def read_http(conversation_id):
            uid = self._uid()
            if not uid: abort(401)
            payload = request.get_json(silent=True) or {}
            try:
                message_id = int(payload.get("message_id"))
                if message_id <= 0: raise ValueError
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "Identifiant de message invalide"}), 400
            data, status = self._mark_conversation_read(uid, conversation_id, message_id)
            return jsonify(data), status

        @self.app.get("/api/chat/diagnostics")
        def diagnostics():
            user = self.current_user()
            if not user: abort(401)
            if user.get("role") != "admin": abort(403)
            git_commit = os.getenv("RENDER_GIT_COMMIT", "local")
            asset_version = self.app.config.get("CHAT_ASSET_VERSION", git_commit)
            backend = "postgresql" if self.engine.url.drivername.startswith("postgresql") else "sqlite"
            return jsonify({"ok": True, "git_commit": git_commit, "asset_version": asset_version,
                "socketio_async_mode": self.socketio.async_mode, "presence_backend": self.presence.backend,
                "redis_configured": bool(os.getenv("REDIS_URL")), "database_backend": backend,
                "process_id": os.getpid(), "render_instance_id": os.getenv("RENDER_INSTANCE_ID", ""),
                "current_user_id": user["email"], "current_tab_online": self.presence.online(user["email"]),
                "online_users_count": sum(self.presence.online(x) for x in self.users)})

    def _allowed_rate(self, uid):
        now = time.monotonic()
        with self.rate_lock:
            q = self.rate[uid]
            while q and now - q[0] > 60: q.popleft()
            if len(q) >= 60 or sum(now - t <= 10 for t in q) >= 10: return False
            q.append(now); return True

    def _open_direct(self, uid, peer):
        if peer not in self.users or peer == uid: return None, "Collègue invalide"
        key = "|".join(sorted((uid, peer)))
        with self.Session.begin() as db:
            conv = db.scalar(select(Conversation).where(Conversation.direct_key == key))
            if not conv:
                conv = Conversation(type="direct", title="Discussion privée", direct_key=key, created_by_user_id=uid)
                db.add(conv); db.flush()
                db.add_all([Participant(conversation_id=conv.id, user_id=uid),
                            Participant(conversation_id=conv.id, user_id=peer)])
            return self._conversation_json(db, conv, uid), None

    def _save_message(self, uid, cid, payload):
        body = str(payload.get("body", "")).strip()
        client_id = str(payload.get("client_message_id", ""))[:64]
        if not body: return {"ok": False, "error": "Le message est vide"}, 400
        if len(body) > 2000: return {"ok": False, "error": "Le message dépasse 2 000 caractères"}, 400
        if not client_id: return {"ok": False, "error": "Identifiant de message manquant"}, 400
        with self.Session.begin() as db:
            if not self._member(db, cid, uid): return {"ok": False, "error": "Accès refusé"}, 403
            existing = db.scalar(select(Message).where(Message.sender_user_id == uid, Message.client_message_id == client_id))
            if existing: return {"ok": True, "message": self._message_json(existing), "deduplicated": True}, 200
            if not self._allowed_rate(uid): return {"ok": False, "error": "Trop de messages, patientez quelques secondes"}, 429
            message = Message(conversation_id=cid, sender_user_id=uid, client_message_id=client_id, body=body)
            db.add(message); db.flush(); data = self._message_json(message)
        with self.Session() as db:
            recipients = db.scalars(select(Participant.user_id).where(Participant.conversation_id == cid)).all()
        for recipient in recipients:
            self.socketio.emit("chat:new_message", data, room=f"user:{recipient}", namespace=NAMESPACE)
        return {"ok": True, "message": data}, 201

    def _mark_conversation_read(self, uid, cid, mid):
        """Marque une conversation lue, indépendamment du transport utilisé."""
        with self.Session.begin() as db:
            part = db.get(Participant, (cid, uid)) if uid else None
            if not part:
                return {"ok": False, "error": "Accès refusé"}, 403
            msg = db.get(Message, mid)
            if not msg:
                return {"ok": False, "error": "Message introuvable"}, 404
            if msg.conversation_id != cid:
                return {"ok": False, "error": "Le message n’appartient pas à cette conversation"}, 400
            part.last_read_message_id = max(part.last_read_message_id or 0, mid)
            part.last_read_at = datetime.now(timezone.utc)
            unread = self._unreads(db, uid)

        data = {"conversation_id": cid, "user_id": uid, "message_id": mid}
        with self.Session() as db:
            recipients = db.scalars(select(Participant.user_id).where(
                Participant.conversation_id == cid)).all()
        for recipient in recipients:
            self.socketio.emit("chat:read_changed", data, room=f"user:{recipient}", namespace=NAMESPACE)
        self.socketio.emit("chat:unread_changed", {"unread": unread},
                           room=f"user:{uid}", namespace=NAMESPACE)
        return {"ok": True, **data, "unread": unread,
                "total_unread": sum(unread.values())}, 200

    def _events(self):
        @self.socketio.on("connect", namespace=NAMESPACE)
        def connect(auth=None):
            uid = self._uid()
            if not uid: return False
            self._ensure_team(); join_room(f"user:{uid}"); join_room(f"conversation:{TEAM_ID}")
            with self.Session() as db:
                for cid in db.scalars(select(Participant.conversation_id).where(Participant.user_id == uid)):
                    join_room(f"conversation:{cid}")
            self.presence.heartbeat(uid, str((auth or {}).get("tab_id") or f"socket-{request.sid}"))
            return True

        @self.socketio.on("disconnect", namespace=NAMESPACE)
        def disconnect(): pass  # présence conservée pendant le délai TTL

        @self.socketio.on("presence:heartbeat", namespace=NAMESPACE)
        def heartbeat(payload=None): return {"ok": self.presence.heartbeat(self._uid(), (payload or {}).get("tab_id"))}

        @self.socketio.on("chat:open_direct", namespace=NAMESPACE)
        def open_direct(payload):
            uid, peer = self._uid(), str((payload or {}).get("user_id", "")).lower()
            if not uid: return {"ok": False, "error": "Authentification requise"}
            data, error = self._open_direct(uid, peer)
            if error: return {"ok": False, "error": error}
            join_room(f"conversation:{data['id']}")
            self.socketio.emit("chat:conversation_created", data, room=f"user:{peer}", namespace=NAMESPACE)
            return {"ok": True, "conversation": data}

        @self.socketio.on("chat:send", namespace=NAMESPACE)
        def send(payload):
            uid, payload = self._uid(), payload or {}
            if not uid: return {"ok": False, "error": "Authentification requise"}
            try: cid = int(payload.get("conversation_id"))
            except (TypeError, ValueError): return {"ok": False, "error": "Conversation invalide"}
            data, _status = self._save_message(uid, cid, payload)
            return data

        @self.socketio.on("chat:read", namespace=NAMESPACE)
        def read(payload):
            uid = self._uid(); payload = payload or {}
            try: cid, mid = int(payload.get("conversation_id")), int(payload.get("message_id"))
            except (TypeError, ValueError): return {"ok": False, "error": "Lecture invalide"}
            data, _status = self._mark_conversation_read(uid, cid, mid)
            return data

        @self.socketio.on("chat:typing", namespace=NAMESPACE)
        def typing(payload):
            uid = self._uid(); payload = payload or {}
            try: cid = int(payload.get("conversation_id"))
            except (TypeError, ValueError): return {"ok": False}
            with self.Session() as db:
                if not uid or not self._member(db, cid, uid): return {"ok": False}
            data = {"conversation_id": cid, "user_id": uid, "name": self.users[uid]["first_name"], "typing": bool(payload.get("typing"))}
            with self.Session() as db:
                recipients = db.scalars(select(Participant.user_id).where(Participant.conversation_id == cid, Participant.user_id != uid)).all()
            for recipient in recipients:
                self.socketio.emit("chat:typing_changed", data, room=f"user:{recipient}", namespace=NAMESPACE)
            return {"ok": True}


def init_chat(app, users, current_user):
    return ChatService(app, users, current_user)
