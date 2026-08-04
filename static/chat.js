(() => {
  "use strict";
  if (window.__integraleChat) return;

  const root = document.querySelector("#integrale-chat");
  if (!root) return;
  const $ = (selector) => root.querySelector(selector);
  const state = { me: null, conversations: [], colleagues: [], unread: {}, current: null,
    seen: new Set(), oldest: null, socket: null, lastAction: null };
  const home = $(".ic-home");
  const conversation = $(".ic-conversation");
  const messages = $(".ic-messages");
  const draft = $("textarea");
  const originalTitle = document.title;
  const tabId = sessionStorage.getItem("chat_tab_id") || crypto.randomUUID();
  sessionStorage.setItem("chat_tab_id", tabId);

  function setOpen(open) {
    root.dataset.state = open ? "open" : "closed";
    $(".ic-panel").setAttribute("aria-hidden", String(!open));
    $(".ic-launcher").setAttribute("aria-expanded", String(open));
    $(".ic-launcher").setAttribute("aria-label", open ? "Fermer le chat interne" : "Ouvrir le chat interne");
    localStorage.setItem("ic-chat-open", open ? "1" : "0");
    if (open) markRead();
  }

  function connection(text, connected = false) {
    const element = $(".ic-connection");
    element.textContent = text;
    element.classList.add("show");
    element.classList.toggle("connected", connected);
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

  async function bootstrap() {
    try {
      const data = await request("/api/chat/bootstrap");
      Object.assign(state, { me: data.current_user_id, conversations: data.conversations,
        colleagues: data.colleagues, unread: data.unread, onlineCount: data.online_users_count });
      render(); clearNotice();
      const saved = Number(sessionStorage.getItem("ic-chat-current"));
      if (saved && state.conversations.some((item) => item.id === saved)) openConversation(saved, false);
    } catch (error) { notice(`Connexion au chat impossible : ${error.message}`, bootstrap); }
  }

  function appendMessage(message) {
    if (state.seen.has(message.id)) return;
    state.seen.add(message.id);
    const element = document.createElement("article");
    element.className = `ic-msg ${message.sender_user_id === state.me ? "mine" : ""}`; element.dataset.id = message.id;
    const body = document.createElement("span"); body.textContent = message.body;
    const time = document.createElement("time"); time.textContent = new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit" }).format(new Date(message.created_at));
    if (message.sender_user_id !== state.me) { const sender = document.createElement("small"); sender.textContent = message.sender_name; element.append(sender); }
    element.append(body, time); messages.append(element); messages.scrollTop = messages.scrollHeight;
  }

  async function loadMessages(before) {
    const id = state.current;
    try {
      const data = await request(`/api/chat/conversations/${id}/messages?limit=50${before ? `&before_id=${before}` : ""}`);
      if (id !== state.current) return;
      if (!before) { messages.querySelectorAll(".ic-msg").forEach((item) => item.remove()); state.seen.clear(); }
      data.messages.forEach(appendMessage); state.oldest = data.messages[0]?.id; $(".ic-older").hidden = !data.has_more;
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

  function markRead() {
    if (!state.socket?.connected || root.dataset.state !== "open" || document.hidden || !state.current) return;
    const last = messages.querySelector(".ic-msg:last-of-type"); if (!last) return;
    state.socket.timeout(8000).emit("chat:read", { conversation_id: state.current, message_id: Number(last.dataset.id) }, (error, ack) => {
      if (!error && ack?.ok) { state.unread[String(state.current)] = 0; updateBadges(); }
    });
  }

  async function heartbeat() {
    try { const data = await request("/api/chat/presence", { method: "POST", body: JSON.stringify({ tab_id: tabId }) }); state.onlineCount = data.online_users_count; $(".ic-online").textContent = `${state.onlineCount} en ligne`; await bootstrap(); }
    catch (error) { notice(`Présence indisponible : ${error.message}`, heartbeat); }
  }

  function initSocket() {
    if (state.socket) return;
    if (typeof window.io !== "function") {
      connection("Temps réel indisponible — reconnexion en cours"); setTimeout(initSocket, 1500); return;
    }
    try {
      const socket = window.io("/chat", { auth: { tab_id: tabId }, transports: ["websocket", "polling"], reconnection: true }); state.socket = socket;
      socket.on("connect", () => { connection("Temps réel connecté", true); heartbeat(); bootstrap(); });
      socket.on("disconnect", () => connection("Déconnecté — reconnexion en cours…"));
      socket.on("connect_error", (error) => connection(`Déconnecté — reconnexion en cours… (${error.message})`));
      socket.on("error", (error) => notice(`Erreur temps réel : ${error?.message || error}`));
      socket.io.on("reconnect_attempt", () => connection("Déconnecté — reconnexion en cours…"));
      socket.io.on("reconnect", () => connection("Temps réel connecté", true));
      socket.on("presence:changed", (payload) => { const user = state.colleagues.find((item) => item.id === payload.user_id); if (user) { user.online = payload.online; user.last_seen_at = payload.last_seen_at; bootstrap(); } });
      socket.on("chat:conversation_created", bootstrap);
      socket.on("chat:unread_changed", (data) => { state.unread = data.unread; updateBadges(); });
      socket.on("chat:new_message", (message) => {
        if (message.sender_user_id !== state.me && state.current !== message.conversation_id) state.unread[String(message.conversation_id)] = (state.unread[String(message.conversation_id)] || 0) + 1;
        if (state.current === message.conversation_id) appendMessage(message); updateBadges(); markRead();
      });
      socket.on("chat:typing_changed", (payload) => { if (payload.conversation_id === state.current && payload.user_id !== state.me) $(".ic-typing").textContent = payload.typing ? `${payload.name} écrit…` : ""; });
    } catch (error) { connection(`Temps réel indisponible — reconnexion en cours (${error.message})`); state.socket = null; setTimeout(initSocket, 1500); }
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
  root.addEventListener("click", (event) => { const user = event.target.closest("[data-user]"); const item = event.target.closest("[data-cid]"); if (user) openDirect(user.dataset.user); else if (item) openConversation(item.dataset.cid); });
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
  window.addEventListener("pagehide", () => navigator.sendBeacon("/api/chat/presence/close", new Blob([JSON.stringify({ tab_id: tabId })], { type: "application/json" })));

  if (localStorage.getItem("ic-chat-open") === "1") setOpen(true);
  heartbeat(); setInterval(heartbeat, 20000);
  bootstrap().finally(initSocket);
  window.__integraleChat = true;
})();
