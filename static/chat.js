(() => {
  "use strict";
  if (window.__integraleChat) return;

  const root = document.querySelector("#integrale-chat");
  if (!root) return;
  const $ = (selector) => root.querySelector(selector);
  const state = { me: null, conversations: [], colleagues: [], unread: {}, current: null,
    seen: new Set(), oldest: null, socket: null, lastAction: null,
    readConfirmedByConversation: {}, readInFlightByConversation: {}, readPendingByConversation: {} };
  const home = $(".ic-home");
  const conversation = $(".ic-conversation");
  const messages = $(".ic-messages");
  const draft = $("textarea");
  const originalTitle = document.title;
  const tabId = sessionStorage.getItem("chat_tab_id") || crypto.randomUUID();
  let bootstrapSequence = 0;
  let bootstrapInFlight = null;
  let unreadRevision = 0;
  let connectionHideTimer;
  let connectionDegradedTimer;
  let initialConnectionExpired = false;
  sessionStorage.setItem("chat_tab_id", tabId);

  function setOpen(open) {
    root.dataset.state = open ? "open" : "closed";
    $(".ic-panel").setAttribute("aria-hidden", String(!open));
    $(".ic-launcher").setAttribute("aria-expanded", String(open));
    $(".ic-launcher").setAttribute("aria-label", open ? "Fermer le chat interne" : "Ouvrir le chat interne");
    localStorage.setItem("ic-chat-open", open ? "1" : "0");
    if (open) markRead();
  }

  function connection(status, text) {
    const element = $(".ic-connection");
    element.textContent = text;
    element.classList.add("show");
    element.classList.toggle("connected", status === "connected");
    element.dataset.status = status;
    clearTimeout(connectionHideTimer);
    if (status === "connected") {
      clearTimeout(connectionDegradedTimer);
      connectionHideTimer = setTimeout(() => element.classList.remove("show"), 800);
    }
  }

  function notice(text, retry) {
    const element = $(".ic-notice");
    element.querySelector("span").textContent = text;
    element.hidden = false;
    $(".ic-retry").hidden = !retry;
    state.lastAction = retry || null;
  }

  function clearNotice() { $(".ic-notice").hidden = true; state.lastAction = null; }
  function initials(name) { return name.trim().split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase(); }
  function totalUnread() { return Object.values(state.unread).reduce((sum, value) => sum + Number(value || 0), 0); }

  function updateBadges() {
    const count = totalUnread();
    $(".ic-total").textContent = count; $(".ic-total").hidden = !count;
    document.title = count ? `(${count}) ${originalTitle}` : originalTitle;
    root.querySelectorAll("[data-cid]").forEach((row) => {
      const badge = row.querySelector(".ic-badge");
      if (!badge) return;
      const value = state.unread[row.dataset.cid] || 0;
      badge.textContent = value; badge.hidden = !value;
    });
  }

  function userRow(user, picker = false) {
    const button = document.createElement("button");
    button.type = "button"; button.className = "ic-row"; button.dataset.user = user.id;
    button.innerHTML = `<span class="ic-avatar"></span><span><strong></strong><small><i class="ic-dot"></i></small></span>`;
    button.querySelector(".ic-avatar").textContent = initials(user.name);
    button.querySelector("strong").textContent = user.name;
    button.querySelector("small").append(user.online ? "En ligne" : "Hors ligne");
    button.querySelector(".ic-dot").classList.toggle("online", user.online);
    if (picker) button.classList.add("ic-picker-row");
    return button;
  }

  function renderPicker() {
    const query = $(".ic-search").value.trim().toLocaleLowerCase("fr");
    const users = state.colleagues.filter((user) => `${user.name} ${user.id}`.toLocaleLowerCase("fr").includes(query));
    $(".ic-picker").replaceChildren(...users.map((user) => userRow(user, true)));
    $(".ic-empty").hidden = users.length !== 0;
  }

  function render() {
    state.colleagues.sort((a, b) => Number(b.online) - Number(a.online) || a.name.localeCompare(b.name, "fr"));
    $(".ic-colleagues").replaceChildren(...state.colleagues.map((user) => userRow(user)));
    const directs = state.conversations.filter((item) => item.type === "direct");
    const directRows = directs.map((item) => {
      const row = document.createElement("button"); row.type = "button"; row.className = "ic-row"; row.dataset.cid = item.id;
      row.innerHTML = '<span class="ic-avatar"></span><span><strong></strong><small>Discussion privée</small></span><b class="ic-badge" hidden>0</b>';
      row.querySelector(".ic-avatar").textContent = initials(item.title); row.querySelector("strong").textContent = item.title; return row;
    });
    if (!directRows.length) { const empty = document.createElement("small"); empty.textContent = "Aucune discussion privée"; directRows.push(empty); }
    $(".ic-directs").replaceChildren(...directRows);
    $(".ic-online").textContent = `${state.onlineCount || 0} en ligne`;
    renderPicker(); updateBadges();
  }

  async function request(url, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(url, { credentials: "same-origin", ...options, signal: controller.signal,
        headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
      let data = {}; try { data = await response.json(); } catch (_) { /* réponse sans JSON */ }
      if (!response.ok || data.ok === false) throw new Error(data.error || `Erreur HTTP ${response.status}`);
      return data;
    } finally { clearTimeout(timer); }
  }

  function bootstrap() {
    if (bootstrapInFlight) return bootstrapInFlight;
    const sequence = ++bootstrapSequence;
    const revision = unreadRevision;
    bootstrapInFlight = (async () => {
      try {
        const data = await request("/api/chat/bootstrap");
        if (sequence !== bootstrapSequence) return;
        Object.assign(state, { me: data.current_user_id, conversations: data.conversations,
          colleagues: data.colleagues, onlineCount: data.online_users_count });
        if (revision === unreadRevision) state.unread = data.unread;
        render(); clearNotice();
        const saved = Number(sessionStorage.getItem("ic-chat-current"));
        if (!state.current && saved && state.conversations.some((item) => item.id === saved)) openConversation(saved, false);
        else markRead();
      } catch (error) { notice(`Connexion au chat impossible : ${error.message}`, bootstrap); }
    })().finally(() => { bootstrapInFlight = null; });
    return bootstrapInFlight;
  }

  function appendMessage(message, beforeElement = null) {
    if (state.seen.has(message.id)) return;
    state.seen.add(message.id);
    const element = document.createElement("article");
    element.className = `ic-msg ${message.sender_user_id === state.me ? "mine" : ""}`; element.dataset.id = message.id;
    const body = document.createElement("span"); body.textContent = message.body;
    const del = document.createElement("button"); del.type = "button"; del.className = "ic-delete"; del.textContent = "×"; del.title = "Supprimer ce message"; del.setAttribute("aria-label", "Supprimer ce message");
    const time = document.createElement("time"); time.textContent = new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit" }).format(new Date(message.created_at));
    if (message.sender_user_id !== state.me) { const sender = document.createElement("small"); sender.textContent = message.sender_name; element.append(sender); }
    element.append(body, del, time);
    if (beforeElement) messages.insertBefore(element, beforeElement); else messages.append(element);
    if (!beforeElement) messages.scrollTop = messages.scrollHeight;
  }

  async function loadMessages(before) {
    const id = state.current;
    try {
      const data = await request(`/api/chat/conversations/${id}/messages?limit=50${before ? `&before_id=${before}` : ""}`);
      if (id !== state.current) return;
      if (!before) {
        messages.querySelectorAll(".ic-msg").forEach((item) => item.remove()); state.seen.clear();
        data.messages.forEach((message) => appendMessage(message));
      } else {
        const previousHeight = messages.scrollHeight;
        const firstMessage = messages.querySelector(".ic-msg");
        data.messages.forEach((message) => appendMessage(message, firstMessage));
        messages.scrollTop += messages.scrollHeight - previousHeight;
      }
      state.oldest = data.messages[0]?.id || state.oldest; $(".ic-older").hidden = !data.has_more;
      markRead(); clearNotice();
    } catch (error) { notice(`Impossible de charger les messages : ${error.message}`, () => loadMessages(before)); }
  }

  function openConversation(id, save = true) {
    const item = state.conversations.find((candidate) => candidate.id === Number(id));
    if (!item) return notice("Impossible d’ouvrir cette discussion", bootstrap);
    state.current = item.id; home.hidden = true; conversation.hidden = false;
    $(".ic-conv-title").textContent = item.title;
    const peer = state.colleagues.find((user) => user.id === item.peer_user_id);
    $(".ic-conv-status").textContent = item.type === "team" ? "Salon général" : (peer?.online ? "En ligne" : "Hors ligne");
    draft.value = localStorage.getItem(`ic-draft-${item.id}`) || "";
    if (save) sessionStorage.setItem("ic-chat-current", item.id);
    loadMessages(); setTimeout(() => draft.focus(), 0);
  }

  async function openDirect(userId) {
    notice("Ouverture de la discussion…");
    try {
      const data = await request("/api/chat/direct", { method: "POST", body: JSON.stringify({ peer_user_id: userId }) });
      const existing = state.conversations.findIndex((item) => item.id === data.conversation.id);
      if (existing < 0) state.conversations.push(data.conversation); else state.conversations[existing] = data.conversation;
      $(".ic-modal").hidden = true; render(); clearNotice(); openConversation(data.conversation.id);
    } catch (error) { notice(`Impossible d’ouvrir cette discussion : ${error.message}`, () => openDirect(userId)); }
  }

  function isConversationActuallyVisible(conversationId) {
    return root.dataset.state === "open" && state.current === Number(conversationId) &&
      document.visibilityState === "visible" && !conversation.hidden;
  }

  async function sendPendingRead(conversationId) {
    if (state.readInFlightByConversation[conversationId]) return;
    const messageId = Number(state.readPendingByConversation[conversationId] || 0);
    if (!messageId || messageId <= Number(state.readConfirmedByConversation[conversationId] || 0)) return;
    state.readPendingByConversation[conversationId] = 0;
    state.readInFlightByConversation[conversationId] = messageId;
    console.info("[CRM CHAT] marquage comme lu demandé", { conversationId, messageId });
    try {
      const data = await request(`/api/chat/conversations/${conversationId}/read`, {
        method: "POST", body: JSON.stringify({ message_id: messageId })
      });
      state.readConfirmedByConversation[conversationId] = Math.max(
        Number(state.readConfirmedByConversation[conversationId] || 0), Number(data.message_id));
      unreadRevision += 1;
      state.unread = data.unread;
      updateBadges();
      console.info("[CRM CHAT] marquage comme lu confirmé", { conversationId, messageId: data.message_id });
    } catch (error) {
      state.readPendingByConversation[conversationId] = Math.max(
        Number(state.readPendingByConversation[conversationId] || 0), messageId);
      console.warn("[CRM CHAT] échec du marquage comme lu", { conversationId, message: error.message });
    } finally {
      delete state.readInFlightByConversation[conversationId];
      if (Number(state.readPendingByConversation[conversationId] || 0) >
          Number(state.readConfirmedByConversation[conversationId] || 0)) sendPendingRead(conversationId);
    }
  }

  function markRead() {
    if (!state.current || !isConversationActuallyVisible(state.current)) return;
    const last = messages.querySelector(".ic-msg:last-of-type");
    if (!last || !messages.contains(last)) return;
    const conversationId = state.current;
    const messageId = Number(last.dataset.id);
    if (!messageId || messageId <= Number(state.readConfirmedByConversation[conversationId] || 0)) return;
    state.readPendingByConversation[conversationId] = Math.max(
      Number(state.readPendingByConversation[conversationId] || 0), messageId);
    sendPendingRead(conversationId);
  }

  async function heartbeat() {
    try { const data = await request("/api/chat/presence", { method: "POST", body: JSON.stringify({ tab_id: tabId }) }); state.onlineCount = data.online_users_count; $(".ic-online").textContent = `${state.onlineCount} en ligne`; }
    catch (error) { notice(`Présence indisponible : ${error.message}`, heartbeat); }
  }

  function initSocket() {
    if (state.socket) return;
    if (typeof window.io !== "function") {
      connection("reconnecting", "Temps réel indisponible — nouvelle tentative…"); setTimeout(initSocket, 1500); return;
    }
    try {
      console.info("[CRM CHAT] démarrage du client", { version: "4.8.1", protocol: window.io.protocol, namespace: "/chat" });
      connection("connecting", "Connexion au chat…");
      connectionDegradedTimer = setTimeout(() => {
        if (!state.socket?.connected) {
          initialConnectionExpired = true;
          connection("degraded", "Temps réel indisponible — le chat reste accessible");
        }
      }, 10000);
      const socket = window.io("/chat", {
        path: "/socket.io", auth: { tab_id: tabId }, withCredentials: true,
        transports: ["polling", "websocket"], upgrade: true, reconnection: true,
        reconnectionAttempts: Infinity, reconnectionDelay: 1000, reconnectionDelayMax: 10000,
        randomizationFactor: 0.5, timeout: 10000
      });
      state.socket = socket;
      console.info("[CRM CHAT] tentative de connexion", { transport: socket.io.engine?.transport?.name || "polling" });
      socket.io.engine?.on("upgrade", (transport) => console.info("[CRM CHAT] transport upgraded", transport.name));
      socket.on("connect", () => {
        initialConnectionExpired = false;
        console.info("[CRM CHAT] connecté", { socketId: socket.id, transport: socket.io.engine.transport.name });
        connection("connected", "Chat connecté"); heartbeat(); if (state.me) markRead(); else bootstrap().then(markRead);
      });
      socket.on("disconnect", (reason) => {
        console.info("[CRM CHAT] déconnexion", { reason });
        connection("disconnected", "Déconnecté — reconnexion en cours…");
      });
      socket.on("connect_error", (error) => {
        console.warn("[CRM CHAT] erreur de connexion", { message: error?.message, type: error?.type,
          description: error?.description, context: error?.context });
        connection(initialConnectionExpired ? "degraded" : "reconnecting", initialConnectionExpired ?
          "Temps réel indisponible — le chat reste accessible" : "Temps réel indisponible — nouvelle tentative…");
      });
      socket.on("error", (error) => notice(`Erreur temps réel : ${error?.message || error}`));
      socket.io.on("reconnect_attempt", () => {
        if (!initialConnectionExpired) connection("reconnecting", "Déconnecté — reconnexion en cours…");
      });
      socket.io.on("reconnect", () => connection("connected", "Chat connecté"));
      socket.on("presence:changed", (payload) => { const user = state.colleagues.find((item) => item.id === payload.user_id); if (user) { user.online = payload.online; user.last_seen_at = payload.last_seen_at; render(); } });
      socket.on("chat:conversation_created", bootstrap);
      socket.on("chat:unread_changed", (data) => {
        unreadRevision += 1;
        state.unread = data.unread;
        if (state.current && isConversationActuallyVisible(state.current) &&
            state.readConfirmedByConversation[state.current]) state.unread[String(state.current)] = 0;
        updateBadges();
      });
      socket.on("chat:new_message", (message) => {
        const alreadySeen = state.seen.has(message.id);
        const visible = isConversationActuallyVisible(message.conversation_id);
        if (state.current === message.conversation_id) appendMessage(message);
        if (message.sender_user_id !== state.me && !visible && !alreadySeen) {
          unreadRevision += 1;
          state.unread[String(message.conversation_id)] = Number(state.unread[String(message.conversation_id)] || 0) + 1;
        }
        updateBadges();
        if (message.sender_user_id !== state.me && visible) markRead();
      });
      socket.on("chat:message_deleted", (payload) => { messages.querySelector(`[data-id="${payload.message_id}"]`)?.remove(); state.seen.delete(payload.message_id); });
      socket.on("chat:history_cleared", (payload) => { if (payload.conversation_id === state.current) { messages.querySelectorAll(".ic-msg").forEach((item) => item.remove()); state.seen.clear(); } bootstrap(); });
      socket.on("chat:typing_changed", (payload) => { if (payload.conversation_id === state.current && payload.user_id !== state.me) $(".ic-typing").textContent = payload.typing ? `${payload.name} écrit…` : ""; });
    } catch (error) {
      console.warn("[CRM CHAT] erreur de connexion", { message: error.message, type: error.name });
      connection("reconnecting", "Temps réel indisponible — nouvelle tentative…");
      state.socket = null; setTimeout(initSocket, 1500);
    }
  }

  // Les interactions sont installées avant tout accès HTTP ou Socket.IO.
  $(".ic-launcher").addEventListener("click", () => setOpen(root.dataset.state !== "open"));
  $(".ic-close").addEventListener("click", () => setOpen(false));
  $(".ic-back").addEventListener("click", () => { conversation.hidden = true; home.hidden = false; state.current = null; sessionStorage.removeItem("ic-chat-current"); });
  $(".ic-team").addEventListener("click", () => openConversation(1));
  $(".ic-new").addEventListener("click", () => { $(".ic-modal").hidden = false; $(".ic-search").value = ""; renderPicker(); $(".ic-search").focus(); });
  $(".ic-modal-close").addEventListener("click", () => { $(".ic-modal").hidden = true; });
  $(".ic-search").addEventListener("input", renderPicker);
  $(".ic-sound").addEventListener("click", () => { const on = localStorage.getItem("ic-chat-sound") !== "off"; localStorage.setItem("ic-chat-sound", on ? "off" : "on"); $(".ic-sound").textContent = on ? "🔕" : "🔔"; });
  $(".ic-retry").addEventListener("click", () => state.lastAction?.());
  $(".ic-older").addEventListener("click", () => loadMessages(state.oldest));
  $(".ic-clear").addEventListener("click", async () => { if (!state.current || !confirm("Supprimer tout l’historique de cette conversation ?")) return; try { await request(`/api/chat/conversations/${state.current}/messages`, { method: "DELETE" }); messages.querySelectorAll(".ic-msg").forEach((item) => item.remove()); state.seen.clear(); clearNotice(); } catch (error) { notice(`Impossible de supprimer l’historique : ${error.message}`); } });
  root.addEventListener("click", async (event) => { const del = event.target.closest(".ic-delete"); if (del) { event.stopPropagation(); const item = del.closest(".ic-msg"); if (!state.current || !item || !confirm("Supprimer ce message ?")) return; try { await request(`/api/chat/conversations/${state.current}/messages/${item.dataset.id}`, { method: "DELETE" }); item.remove(); state.seen.delete(Number(item.dataset.id)); clearNotice(); } catch (error) { notice(`Impossible de supprimer le message : ${error.message}`); } return; } const user = event.target.closest("[data-user]"); const item = event.target.closest("[data-cid]"); if (user) openDirect(user.dataset.user); else if (item) openConversation(item.dataset.cid); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") { if (!$(".ic-modal").hidden) $(".ic-modal").hidden = true; else setOpen(false); } });
  draft.addEventListener("input", () => { if (!state.current) return; localStorage.setItem(`ic-draft-${state.current}`, draft.value); if (state.socket?.connected) state.socket.emit("chat:typing", { conversation_id: state.current, typing: true }); });
  draft.addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $(".ic-compose").requestSubmit(); } });
  $(".ic-compose").addEventListener("submit", async (event) => {
    event.preventDefault(); const body = draft.value.trim(); if (!body || !state.current) return;
    const payload = { client_message_id: crypto.randomUUID(), body }; notice("Envoi du message…");
    try { const data = await request(`/api/chat/conversations/${state.current}/messages`, { method: "POST", body: JSON.stringify(payload) }); appendMessage(data.message); draft.value = ""; localStorage.removeItem(`ic-draft-${state.current}`); clearNotice(); }
    catch (error) { notice(`Impossible d’envoyer le message : ${error.message}`, () => $(".ic-compose").requestSubmit()); }
  });
  document.addEventListener("visibilitychange", markRead);
  messages.addEventListener("scroll", () => {
    if (messages.scrollHeight - messages.scrollTop - messages.clientHeight < 24) markRead();
  });
  window.addEventListener("pagehide", () => navigator.sendBeacon("/api/chat/presence/close", new Blob([JSON.stringify({ tab_id: tabId })], { type: "application/json" })));

  if (localStorage.getItem("ic-chat-open") === "1") setOpen(true);
  connection("connecting", "Connexion au chat…");
  setInterval(heartbeat, 20000);
  bootstrap().finally(initSocket);
  window.__integraleChat = true;
})();
