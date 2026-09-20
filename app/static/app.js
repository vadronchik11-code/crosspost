/* Crosspost – фронт без сборки. Всё состояние – в объекте state. */
const $ = (s) => document.querySelector(s);
const VK_LIMIT = 16000, TG_TEXT_LIMIT = 4096, TG_CAPTION_LIMIT = 1024;
const TZ_OFFSET = 5; // Челябинск, UTC+5 – все даты на сайте в этом поясе
const ROLE_LABEL = { head: "Руковод", teamlead: "Тимлид медиа", media: "Медиа" };

const state = {
  posts: [],
  filter: "all",
  backgrounds: [],
  targets: null,
  user: null,      // {id, username, role}
  current: null,   // пост, открытый в редакторе (объект как в БД)
  dirty: false,
  history: [],
};

// ------------------------------------------------------------------ api
async function api(path, opts = {}) {
  const init = { credentials: "same-origin", ...opts };
  if (init.body && !(init.body instanceof FormData)) {
    init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
    init.body = JSON.stringify(init.body);
  }
  const r = await fetch("/api" + path, init);
  if (r.status === 401) { showLogin(); throw new Error("Нужен вход"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

// ------------------------------------------------------------------ auth
function showLogin() { $("#login").classList.remove("hidden"); $("#app").classList.add("hidden"); }
function showApp() { $("#login").classList.add("hidden"); $("#app").classList.remove("hidden"); }
const isManager = () => state.user && (state.user.role === "head" || state.user.role === "teamlead");
const isHead = () => state.user?.role === "head";
const canEdit = (p) => isManager() || !p.id || p.created_by === state.user?.username;

$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  $("#loginError").classList.add("hidden");
  try {
    await api("/login", { method: "POST", body: { login: fd.get("login"), password: fd.get("password") } });
    e.target.reset();
    await boot();
  } catch (err) {
    $("#loginError").textContent = err.message;
    $("#loginError").classList.remove("hidden");
  }
});
$("#logoutBtn").addEventListener("click", async () => { await api("/logout", { method: "POST" }); closeModal("#profileModal"); state.user = null; showLogin(); });

// ------------------------------------------------------------------ boot
async function boot() {
  try { state.user = await api("/me"); } catch { return; }
  showApp();
  renderUser();
  await Promise.all([loadPosts(), loadBackgrounds()]);
  if (!state.current) openPost(newPost());
  loadTargets();
}

function newPost() {
  return {
    id: null, title: "", common_text: "", vk_enabled: true, tg_enabled: true,
    vk_text: "", tg_text: "", tg_html: true, tg_as_file: false, images: [], vk_background: null, tg_background: null,
    scheduled_at: null, status: "draft", vk_status: "pending", tg_status: "pending", remote_dirty: false,
  };
}

function renderUser() {
  const u = state.user;
  $("#userBtn").textContent = `${u.username} · ${ROLE_LABEL[u.role] || u.role}`;
  $("#teamBtn").classList.toggle("hidden", !isHead());
  $("#tgLoginBtn").classList.toggle("hidden", !isHead());
  $("#vkWebBtn").classList.toggle("hidden", !isHead());
}

// ------------------------------------------------------------------ VK-браузер (отложка VK с фото)
function renderVkWebStatus() {
  const b = state.targets?.vk?.browser, box = $("#vkWebStatus");
  if (!b) { box.innerHTML = ""; return; }
  box.innerHTML = (b.session ? `<div class="ok">✓ Сессия есть – посты с фото уходят в отложку VK.</div>` : `<div class="err">Сессии нет – в VK уходит только текст.</div>`)
    + (b.last_error ? `<div class="err">Последняя ошибка: ${esc(b.last_error)}</div>` : "");
}
$("#vkWebBtn").addEventListener("click", async () => { openModal("#vkWebModal"); $("#vkWebShotList").innerHTML = ""; await loadTargets(true); renderVkWebStatus(); });
$("#vkWebClose").addEventListener("click", () => closeModal("#vkWebModal"));
$("#vkWebLogin").addEventListener("click", async () => {
  try {
    await api("/vk/web/login", { method: "POST" });
    $("#vkWebStatus").innerHTML = `<div class="muted">Открыл окно Chromium на компьютере с сервером – войди в VK. Жду…</div>`;
    const t = setInterval(async () => {
      const st = await api("/vk/web/login");
      if (st.running) return;
      clearInterval(t);
      showMsg(st.message, !st.ok); await loadTargets(true); renderVkWebStatus();
    }, 2000);
  } catch (e) { showMsg(e.message, true); }
});
$("#vkWebCheck").addEventListener("click", async () => {
  $("#vkWebStatus").innerHTML = `<div class="muted">Проверяю (открываю vk.com в фоне)…</div>`;
  try {
    const r = await api("/vk/web/check", { method: "POST" });
    $("#vkWebStatus").innerHTML = r.ok ? `<div class="ok">✓ Сессия живая${r.name ? ` (${esc(r.name)})` : ""}</div>` : `<div class="err">${esc(r.message)}</div>`;
  } catch (e) { $("#vkWebStatus").innerHTML = `<div class="err">${esc(e.message)}</div>`; }
});
$("#vkWebShots").addEventListener("click", async () => {
  const list = await api("/vk/web/shots");
  $("#vkWebShotList").innerHTML = list.length ? list.map(n => `<a href="/api/vk/web/shot/${n}" target="_blank"><img src="/api/vk/web/shot/${n}"><small>${esc(n)}</small></a>`).join("") : `<div class="hint">Скриншотов пока нет</div>`;
});

// ------------------------------------------------------------------ Telegram-аккаунт (отложка на серверах Telegram)
function renderTgUserStatus() {
  const u = state.targets?.tg?.user, box = $("#tgUserStatus");
  if (!u) { box.innerHTML = ""; return; }
  if (!u.configured) box.innerHTML = `<div class="err">Не заданы TG_API_ID / TG_API_HASH в .env</div>`;
  else if (u.authorized) box.innerHTML = `<div class="ok">✓ Вошли как ${esc(u.name || "")}. Отложка уходит на сервера Telegram.</div>`;
  else box.innerHTML = `<div class="err">${esc(u.error || "Не авторизован – введи телефон и код")}</div>`;
  $("#tgPhoneForm").classList.toggle("hidden", !u.configured || !!u.authorized);
  $("#tgCodeForm").classList.add("hidden");
  $("#tgLogout").classList.toggle("hidden", !u.authorized);
}
$("#tgLoginBtn").addEventListener("click", async () => { openModal("#tgModal"); await loadTargets(true); renderTgUserStatus(); });
$("#tgClose").addEventListener("click", () => closeModal("#tgModal"));
$("#tgPhoneForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/tg/user/code", { method: "POST", body: { phone: new FormData(e.target).get("phone") } });
    $("#tgCodeForm").classList.remove("hidden"); showMsg("Код отправлен в Telegram");
  } catch (err) { showMsg(err.message, true); }
});
$("#tgCodeForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    const r = await api("/tg/user/signin", { method: "POST", body: { code: fd.get("code"), password: fd.get("password") || null } });
    if (r.need_password) { $("#tgPwRow").classList.remove("hidden"); showMsg("Включена двухэтапная защита – введи пароль", true); return; }
    showMsg(`Вошли как ${r.name}`); await loadTargets(true); renderTgUserStatus();
  } catch (err) { showMsg(err.message, true); }
});
$("#tgLogout").addEventListener("click", async () => {
  if (!confirm("Выйти из Telegram-аккаунта? Отложка снова будет на сервере сайта.")) return;
  await api("/tg/user/logout", { method: "POST" }); await loadTargets(true); renderTgUserStatus();
});

// ------------------------------------------------------------------ targets (шапка)
async function loadTargets(refresh = false) {
  try { state.targets = await api("/targets" + (refresh ? "?refresh=1" : "")); }
  catch (e) { state.targets = null; }
  renderTargets();
  renderPreviews();
}

function renderTargets() {
  const t = state.targets, box = $("#targets");
  if (!t) { box.innerHTML = ""; return; }
  const items = [];
  const vk = t.vk;
  if (!vk.configured) items.push(`<span class="target bad"><span class="dot"></span>VK не настроен</span>`);
  else if (vk.error) items.push(`<span class="target bad" title="${esc(vk.error)}"><span class="dot"></span>VK: ошибка токена</span>`);
  else {
    const need = vk.info?.token_type === "group" ? ["wall", "photos", "messages"].filter(x => !(vk.info.perms || []).includes(x)) : [];
    const lack = { wall: "Стена", photos: "Фотографии", messages: "Сообщения" };
    if (need.length) items.push(`<span class="target bad" title="Пересоздай ключ сообщества с этими правами"><span class="dot"></span>${esc(vk.info?.name || vk.group_id)} · у ключа нет: ${need.map(x => lack[x]).join(", ")}</span>`);
    else if (vk.info?.token_type === "group") items.push(`<span class="target ${vk.browser?.session ? "ok" : "warn"} ${vk.browser?.session ? "" : "link"}" id="vkWebChip" title="${vk.browser?.session ? "Фото уходят в отложку VK через браузер" : "Сессии VK-браузера нет – в VK уйдёт только текст. Нажми, чтобы войти"}"><span class="dot"></span>${vk.info?.photo ? `<img src="${vk.info.photo}">` : ""}${esc(vk.info?.name || vk.group_id)} · ${vk.browser?.session ? "фото через браузер" : "только текст"}</span>`);
    else items.push(`<span class="target ok" title="${vk.info?.token_type === "group" ? "ключ сообщества" : "токен пользователя"}"><span class="dot"></span>${vk.info?.photo ? `<img src="${vk.info.photo}">` : ""}${esc(vk.info?.name || vk.group_id)}</span>`);
  }

  const tg = t.tg;
  if (!tg.configured) items.push(`<span class="target bad"><span class="dot"></span>TG не настроен</span>`);
  else if (!tg.chat_id) items.push(`<span class="target bad link" id="findChat"><span class="dot"></span>TG_CHAT_ID не задан – найти</span>`);
  else if (tg.error) items.push(`<span class="target bad" title="${esc(tg.error)}"><span class="dot"></span>TG: ${esc(tg.error.slice(0, 60))}</span>`);
  else items.push(`<span class="target ok"><span class="dot"></span>${esc(tg.info?.name || tg.chat_id)}</span>`);
  const tu = tg.user;
  if (tu?.authorized) items.push(`<span class="target ok" title="Отложка на серверах Telegram через аккаунт ${esc(tu.name || "")}"><span class="dot"></span>отложка в Telegram</span>`);
  else if (tu?.configured) items.push(`<span class="target warn link" id="tgLoginChip" title="${esc(tu.error || "")}"><span class="dot"></span>отложка на сервере – войди в Telegram</span>`);
  else items.push(`<span class="target warn" title="Задай TG_API_ID и TG_API_HASH в .env, чтобы отложка лежала на серверах Telegram"><span class="dot"></span>отложка на сервере сайта</span>`);

  if (tg.vless || tg.proxy) {
    if (tg.proxy) items.push(`<span class="target ok" title="${esc(tg.proxy)}"><span class="dot"></span>VPN</span>`);
    else items.push(`<span class="target bad" title="${esc(tg.proxy_error || "")}"><span class="dot"></span>VPN не запущен</span>`);
  }
  box.innerHTML = items.join("");
  $("#findChat")?.addEventListener("click", openChatsModal);
  $("#tgLoginChip")?.addEventListener("click", () => $("#tgLoginBtn").click());
  $("#vkWebChip.link")?.addEventListener("click", () => $("#vkWebBtn").click());
}

// ------------------------------------------------------------------ модалки
function openModal(sel) { $(sel).classList.remove("hidden"); }
function closeModal(sel) { $(sel).classList.add("hidden"); }
document.querySelectorAll(".modal").forEach(m => m.addEventListener("mousedown", (e) => { if (e.target === m) m.classList.add("hidden"); }));

// поиск chat id
async function openChatsModal() { openModal("#chatsModal"); await refreshChats(); }
async function refreshChats() {
  const list = $("#chatsList");
  list.innerHTML = `<div class="muted">Загрузка…</div>`;
  try {
    const chats = await api("/tg/chats");
    if (!chats.length) { list.innerHTML = `<div class="muted">Пока ничего. Напиши сообщение в канал, где бот админ, и обнови.</div>`; return; }
    list.innerHTML = chats.map(c => `<div class="chat-row"><span>${esc(c.title || "")} <span class="muted">(${esc(c.type)})</span></span><code>${c.username ? "@" + esc(c.username) : c.id}</code></div>`).join("");
  } catch (e) { list.innerHTML = `<div class="error">${esc(e.message)}</div>`; }
}
$("#chatsRefresh").addEventListener("click", refreshChats);
$("#chatsClose").addEventListener("click", () => closeModal("#chatsModal"));

// профиль
$("#userBtn").addEventListener("click", () => {
  $("#profileName").textContent = state.user.username;
  $("#profileRole").textContent = ROLE_LABEL[state.user.role] || state.user.role;
  openModal("#profileModal");
});
$("#profileClose").addEventListener("click", () => closeModal("#profileModal"));
$("#pwForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/me/password", { method: "PUT", body: { password: new FormData(e.target).get("password") } });
    e.target.reset(); closeModal("#profileModal"); showMsg("Пароль изменён");
  } catch (err) { showMsg(err.message, true); }
});

// команда
$("#teamBtn").addEventListener("click", async () => { openModal("#teamModal"); await renderUsers(); });
$("#teamClose").addEventListener("click", () => closeModal("#teamModal"));
async function renderUsers() {
  const box = $("#usersList");
  try {
    const users = await api("/users");
    box.innerHTML = users.map(u => `
      <div class="list-row user-row" data-id="${u.id}">
        <span class="list-label"><span class="avatar">${esc(u.username[0].toUpperCase())}</span>
          <span><b>${esc(u.username)}</b><span class="hint"> · ${ROLE_LABEL[u.role]}${u.created_by ? ` · добавил ${esc(u.created_by)}` : ""}</span></span></span>
        <span class="row-actions">
          ${u.id !== state.user.id ? `<button class="btn plain small" data-act="pw">Пароль</button><button class="btn plain small danger" data-act="del">Удалить</button>` : `<span class="hint">это ты</span>`}
        </span>
      </div>`).join("");
  } catch (e) { box.innerHTML = `<div class="error">${esc(e.message)}</div>`; }
}
$("#usersList").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-act]"); if (!b) return;
  const id = +b.closest(".user-row").dataset.id;
  try {
    if (b.dataset.act === "del") {
      if (!confirm("Удалить профиль?")) return;
      await api(`/users/${id}`, { method: "DELETE" });
    } else {
      const pw = prompt("Новый пароль (не короче 4 символов):"); if (!pw) return;
      await api(`/users/${id}`, { method: "PUT", body: { password: pw } });
      showMsg("Пароль обновлён – скинь его человеку");
    }
    await renderUsers();
  } catch (err) { showMsg(err.message, true); }
});
$("#userForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    const u = await api("/users", { method: "POST", body: { username: fd.get("username"), password: fd.get("password"), role: fd.get("role") } });
    showMsg(`Профиль ${u.username} создан – скинь ник и пароль`);
    e.target.reset(); await renderUsers();
  } catch (err) { showMsg(err.message, true); }
});
// тимлид не может назначать руковода
document.addEventListener("DOMContentLoaded", () => {});

// ------------------------------------------------------------------ список постов
async function loadPosts() {
  state.posts = await api("/posts");
  renderList();
}

$("#filterTabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  state.filter = b.dataset.f;
  $("#filterTabs").querySelectorAll("button").forEach(x => x.classList.toggle("active", x === b));
  renderList();
});

function renderCounts() {
  $("#filterTabs").querySelectorAll("button").forEach(b => {
    const f = b.dataset.f;
    const n = f === "all" ? state.posts.length : state.posts.filter(p => p.status === f).length;
    b.querySelector(".sl-count").textContent = n || "";
  });
}

function renderList() {
  renderCounts();
  const posts = state.posts.filter(p => state.filter === "all" || p.status === state.filter);
  const box = $("#postList");
  if (!posts.length) { box.innerHTML = `<div class="empty">Пусто</div>`; return; }
  box.innerHTML = posts.map(p => `
    <div class="post-item ${state.current?.id === p.id ? "active" : ""}" data-id="${p.id}">
      <div class="t">${VKEmoji.html(esc(p.title || firstLine(RTE.plainLength(p.vk_text) ? stripTags(p.vk_text) : stripTags(p.tg_text) || p.common_text) || "Без названия"))}</div>
      <div class="m">
        <span class="badge ${p.status}">${statusLabel(p.status)}</span>
        ${p.remote_dirty ? `<span class="badge warn">правки</span>` : ""}
        ${p.vk_enabled ? `<span class="pill vk">VK</span>` : ""}${p.tg_enabled ? `<span class="pill tg">TG</span>` : ""}
        ${p.scheduled_at ? `<span>${fmtDate(p.scheduled_at)}</span>` : ""}
      </div>
      <div class="m"><span>${esc(p.created_by || "–")}</span></div>
    </div>`).join("");
}
$("#postList").addEventListener("click", (e) => {
  const item = e.target.closest(".post-item"); if (!item) return;
  const p = state.posts.find(x => x.id === +item.dataset.id);
  if (p && confirmDiscard()) { openPost(structuredClone(p)); setTab("editor"); }
});
$("#newPostBtn").addEventListener("click", () => { if (confirmDiscard()) { openPost(newPost()); setTab("editor"); } });

function confirmDiscard() {
  return !state.dirty || confirm("Есть несохранённые изменения. Продолжить без сохранения?");
}

// ------------------------------------------------------------------ мобильные вкладки
function setTab(tab) {
  $("#layout").dataset.tab = tab;
  document.querySelectorAll("#tabbar button").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  window.scrollTo(0, 0);
}
$("#tabbar").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) setTab(b.dataset.tab); });

// ------------------------------------------------------------------ редактор
const F = {
  title: $("#f_title"), common_text: $("#f_common_text"),
  vk_text: $("#f_vk_text"), tg_text: $("#f_tg_text"), tg_as_file: $("#f_tg_as_file"), scheduled_at: $("#f_scheduled_at"),
};
RTE.init(F.vk_text, "vk");
RTE.init(F.tg_text, "tg");
RTE.init(F.common_text, "common");

async function openPost(p) {
  state.current = p;
  state.dirty = false;
  F.title.value = p.title || "";
  RTE.set(F.common_text, p.common_text || "");
  F.tg_as_file.checked = !!p.tg_as_file;
  RTE.set(F.vk_text, p.vk_text || "");
  RTE.set(F.tg_text, p.tg_text || "");
  F.scheduled_at.value = p.scheduled_at ? toLocalInput(p.scheduled_at) : "";
  renderStatus();
  renderImages();
  renderBgPickers();
  renderTargetButtons();
  renderPreviews();
  renderList();
  hideMsg();
  await loadHistory();
}

function readForm() {
  const p = state.current;
  p.title = F.title.value.trim();
  p.common_text = RTE.get(F.common_text);
  p.tg_as_file = F.tg_as_file.checked;
  p.vk_text = RTE.get(F.vk_text);
  p.tg_text = RTE.get(F.tg_text);
  p.vk_enabled = RTE.plainLength(p.vk_text) > 0;   // площадка включена, если для неё есть текст
  p.tg_enabled = RTE.plainLength(p.tg_text) > 0;
  renderTargetButtons();
  p.tg_html = true;
  p.scheduled_at = F.scheduled_at.value ? fromLocalInput(F.scheduled_at.value) : null;
  return p;
}

Object.values(F).forEach(el => el.addEventListener("input", () => {
  state.dirty = true; readForm();
  if (el === F.vk_text || el === F.tg_text) { placeTgEditor(); updateCounts(); }
  else if (el !== F.common_text && el !== F.title) renderPreviews();
}));

// кнопки площадок: зелёная – текст есть; клик по серой копирует общий текст, клик по зелёной очищает
function renderTargetButtons() {
  const p = state.current; if (!p) return;
  for (const [pl, el] of [["vk", $("#btnVk")], ["tg", $("#btnTg")]]) {
    const on = RTE.plainLength(p[`${pl}_text`]) > 0;
    el.classList.toggle("on", on);
    el.querySelector(".tb-state").textContent = on ? "· пост уйдёт" : "· выключено";
  }
}
document.querySelectorAll("[data-copy]").forEach(b => b.addEventListener("click", () => {
  const where = b.dataset.copy, field = where === "vk" ? F.vk_text : F.tg_text;
  const has = RTE.plainLength(RTE.get(field)) > 0;
  if (has) {
    if (!confirm(`Очистить текст для ${where === "vk" ? "VK" : "Telegram"} и не постить туда?`)) return;
    RTE.set(field, "");
  } else {
    const t = RTE.get(F.common_text);
    if (!RTE.plainLength(t)) { showMsg("Сначала вставь общий текст", true); return; }
    RTE.set(field, t);
  }
  state.dirty = true; readForm(); renderPreviews();
}));

// быстрые кнопки времени (UTC+5)
document.querySelectorAll("[data-quick]").forEach(b => b.addEventListener("click", () => {
  const d = tzNow();
  if (b.dataset.quick === "tomorrow") { d.setUTCDate(d.getUTCDate() + 1); d.setUTCHours(13, 0, 0, 0); }
  else { d.setUTCMinutes(d.getUTCMinutes() + +b.dataset.quick); d.setUTCSeconds(0, 0); }
  F.scheduled_at.value = tzToInput(d);
  state.dirty = true; readForm(); renderPreviews();
}));

function renderStatus() {
  const p = state.current;
  const b = $("#statusBadge");
  b.className = "badge " + p.status;
  b.textContent = p.id ? `#${p.id} · ${statusLabel(p.status)}` : "новый";

  const editable = canEdit(p);
  $("#authorLine").innerHTML = p.id
    ? `Создал <b>${esc(p.created_by || "–")}</b> ${fmtDateTime(p.created_at)}${p.updated_by && p.updated_at !== p.created_at ? ` · правил <b>${esc(p.updated_by)}</b> ${fmtDateTime(p.updated_at)}` : ""}${editable ? "" : ` · <span class="error">только просмотр</span>`}`
    : `Новый пост · автор <b>${esc(state.user.username)}</b>`;
  $("#dirtyBanner").classList.toggle("hidden", !p.remote_dirty);

  const res = $("#resultBox");
  const lines = [];
  for (const pl of ["vk", "tg"]) {
    if (!p[`${pl}_enabled`]) continue;
    const name = pl.toUpperCase();
    if (p[`${pl}_status`] === "published") {
      const r = parseResult(p[`${pl}_result`]);
      const vkQ = r?.postponed && r?.publish_at && r.publish_at * 1000 > Date.now();
      const queued = (r?.scheduled && r?.when) || vkQ;
      const note = r?.scheduled && r?.when ? ` – в отложке Telegram, выйдет ${fmtDateTime(new Date(r.when * 1000).toISOString())}`
        : r?.postponed ? ` – ${vkQ ? "в отложке VK, выйдет" : "вышло из отложки VK"} ${fmtDateTime(new Date(r.publish_at * 1000).toISOString())}${r.post_id ? "" : " (id записи не найден – править только в VK)"}`
        : (r?.via === "user" ? " (через аккаунт)" : "");
      lines.push(`<div class="ok">✓ ${name}: ${queued ? "в очереди" : "опубликовано"}${note} ${r?.url ? `– <a href="${esc(r.url)}" target="_blank">открыть</a>` : ""}${p[`${pl}_error`] ? `<div class="err">${esc(p[`${pl}_error`])}</div>` : ""}</div>`);
    } else if (p[`${pl}_status`] === "error") {
      lines.push(`<div class="err">✗ ${name}: ${esc(p[`${pl}_error`] || "ошибка")}</div>`);
    }
  }
  res.innerHTML = lines.join("");
  const tgQueued = p.tg_status === "published" && parseResult(p.tg_result)?.scheduled;
  const vr = parseResult(p.vk_result);
  const vkQueued = p.vk_status === "published" && vr?.postponed && vr.publish_at * 1000 > Date.now();
  const published = (p.vk_status === "published" && !vkQueued) || (p.tg_status === "published" && !tgQueued);
  $("#deleteBtn").disabled = !p.id || !editable;
  $("#saveBtn").disabled = !editable;
  $("#scheduleBtn").disabled = !editable;
  $("#publishBtn").disabled = !editable;
  $("#scheduleBtn").classList.toggle("hidden", published);
  $("#scheduleBtn").textContent = (tgQueued || vkQueued) && !published ? "Перепланировать" : "В отложку";
  $("#publishBtn").classList.toggle("hidden", published && p.status === "published");
  $("#saveBtn").classList.toggle("primary", published);
  F.scheduled_at.disabled = published;
  document.querySelectorAll("[data-quick]").forEach(x => x.disabled = published);
  [F.vk_text, F.tg_text, F.common_text].forEach(el => el.contentEditable = editable ? "true" : "false");
}

function parseResult(r) { if (!r) return null; try { return typeof r === "string" ? JSON.parse(r) : r; } catch { return null; } }

// ------------------------------------------------------------------ история
const FIELD_LABEL = {
  title: "название", common_text: "общий текст", vk_text: "текст VK", tg_text: "текст Telegram", images: "картинки",
  vk_background: "подложку VK", tg_background: "подложку Telegram", scheduled_at: "время публикации", status: "статус",
  vk_enabled: "VK вкл/выкл", tg_enabled: "Telegram вкл/выкл", tg_as_file: "режим «как файл»", reset_vk: "повтор VK", reset_tg: "повтор Telegram",
};
async function loadHistory() {
  const p = state.current;
  const box = $("#historyList");
  if (!p.id) { $("#historyGroup").classList.add("hidden"); return; }
  $("#historyGroup").classList.remove("hidden");
  try { state.history = await api(`/posts/${p.id}/history`); } catch { state.history = []; }
  if (!state.history.length) { box.innerHTML = `<div class="empty">Пока пусто</div>`; return; }
  box.innerHTML = state.history.map(h => {
    let what = "";
    const c = h.changes || {};
    if (h.action === "create") what = `создал пост`;
    else if (h.action === "update") {
      const keys = Object.keys(c).filter(k => FIELD_LABEL[k]);
      what = `изменил: ${keys.map(k => FIELD_LABEL[k]).join(", ") || "поля"}`;
      if (c.status) what += ` (${statusLabel(c.status[0])} → ${statusLabel(c.status[1])})`;
      if (c.scheduled_at) what += ` (${c.scheduled_at[0] ? fmtDate(c.scheduled_at[0]) : "–"} → ${c.scheduled_at[1] ? fmtDate(c.scheduled_at[1]) : "–"})`;
    }
    else if (h.action === "publish") what = `опубликовал в ${(c.platform || "").toUpperCase()}`;
    else if (h.action === "error") what = `ошибка публикации в ${(c.platform || "").toUpperCase()}: ${c.error || ""}`;
    else if (h.action === "sync") what = `применил правки в ${(c.platform || "").toUpperCase()}`;
    else if (h.action === "sync_error") what = `не смог применить правки в ${(c.platform || "").toUpperCase()}: ${c.error || ""}`;
    else what = h.action;
    return `<div class="list-row hist-row"><span class="list-label"><span class="avatar">${esc((h.user || "?")[0].toUpperCase())}</span><span><b>${esc(h.user || "система")}</b> ${esc(what)}</span></span><span class="hint nowrap">${fmtDateTime(h.created_at)}</span></div>`;
  }).join("");
}

// ------------------------------------------------------------------ картинки
$("#imageInput").addEventListener("change", async (e) => {
  for (const file of e.target.files) {
    const fd = new FormData(); fd.append("file", file);
    try {
      const { name } = await api("/upload", { method: "POST", body: fd });
      state.current.images.push(name);
    } catch (err) { showMsg(err.message, true); }
  }
  e.target.value = "";
  state.dirty = true; renderImages(); renderPreviews();
});

function renderImages() {
  const imgs = state.current.images || [];
  $("#imageList").innerHTML = imgs.map((n, i) => `
    <div class="thumb" draggable="true" data-i="${i}">
      <img src="/api/files/uploads/${n}">
      <span class="order">${i + 1}</span>
      <button class="x" data-rm="${i}" title="Убрать">✕</button>
    </div>`).join("");
}
$("#imageList").addEventListener("click", (e) => {
  const b = e.target.closest("[data-rm]"); if (!b) return;
  state.current.images.splice(+b.dataset.rm, 1);
  state.dirty = true; renderImages(); renderPreviews();
});
let dragFrom = null;
$("#imageList").addEventListener("dragstart", (e) => { dragFrom = +e.target.closest(".thumb")?.dataset.i; });
$("#imageList").addEventListener("dragover", (e) => e.preventDefault());
$("#imageList").addEventListener("drop", (e) => {
  const to = +e.target.closest(".thumb")?.dataset.i;
  if (dragFrom == null || isNaN(to) || dragFrom === to) return;
  const arr = state.current.images;
  arr.splice(to, 0, arr.splice(dragFrom, 1)[0]);
  dragFrom = null; state.dirty = true; renderImages(); renderPreviews();
});

// ------------------------------------------------------------------ подложка (своя у каждого поста)
async function loadBackgrounds() { state.backgrounds = []; renderBgPickers(); }
for (const pl of ["vk", "tg"]) {
  $(pl === "vk" ? "#bgInputVk" : "#bgInputTg").addEventListener("change", async (e) => {
    const file = e.target.files[0]; if (!file) return;
    const fd = new FormData(); fd.append("file", file);
    try {
      const { name } = await api("/backgrounds", { method: "POST", body: fd });
      state.current[`${pl}_background`] = name;
      state.dirty = true; renderBgPickers(); renderPreviews();
    } catch (err) { showMsg(err.message, true); }
    e.target.value = "";
  });
}
$("#bgSameBtn").addEventListener("click", () => {
  const p = state.current;
  const src = p.vk_background || p.tg_background;
  if (!src) { showMsg("Сначала загрузи подложку в VK или Telegram", true); return; }
  p.vk_background = p.tg_background = src;
  state.dirty = true; renderBgPickers(); renderPreviews();
});
function renderBgPickers() {
  if (!state.current) return;
  for (const pl of ["vk", "tg"]) {
    const key = `${pl}_background`, sel = state.current[key];
    const box = $(pl === "vk" ? "#bgVk" : "#bgTg");
    box.innerHTML = sel
      ? `<div class="bg-opt selected" title="Подложка ${pl.toUpperCase()}"><img src="/api/files/backgrounds/${sel}"><button class="del" data-rm="1" title="Убрать">✕</button></div><div class="bg-opt add" data-add="1" title="Заменить">↺</div>`
      : `<div class="bg-opt add" data-add="1" title="Загрузить подложку">+</div>`;
    box.onclick = (e) => {
      if (e.target.closest("[data-rm]")) { state.current[key] = null; state.dirty = true; renderBgPickers(); renderPreviews(); return; }
      if (e.target.closest("[data-add]")) $(pl === "vk" ? "#bgInputVk" : "#bgInputTg").click();
    };
  }
}

// ------------------------------------------------------------------ сохранение / публикация / синхронизация
let saving = false;   // защита от двойного клика: пока запрос идёт, кнопки выключены, повторный вызов игнорируется
async function save(status) {
  if (saving) { showMsg("Подожди – пост ещё отправляется"); return null; }
  const p = readForm();
  if (!canEdit(p)) { showMsg("Это чужой пост – править может автор, тимлид или руковод", true); return null; }
  if (status) p.status = status;
  if (p.status === "scheduled" && !p.scheduled_at) { showMsg("Укажи время публикации", true); return null; }
  if (!p.vk_enabled && !p.tg_enabled) { showMsg("Нет текста ни для VK, ни для Telegram – нажми VK или Telegram под общим текстом", true); return null; }
  const wasPublished = p.id && (p.vk_status === "published" || p.tg_status === "published");
  const before = wasPublished && state.posts.find(x => x.id === p.id);
  saving = true; setBusy(true);
  const btn = status === "scheduled" ? $("#scheduleBtn") : $("#saveBtn"), label = btn.textContent;
  if (p.status === "scheduled") { btn.textContent = "Отправляю…"; showMsg("Отправляю в отложку VK/Telegram – это может занять до 2 минут, не нажимай повторно"); }
  try {
    const saved = p.id
      ? await api(`/posts/${p.id}`, { method: "PUT", body: p })
      : await api("/posts", { method: "POST", body: p });
    await loadPosts();
    await openPost(saved);
    if (wasPublished && before && contentChanged(before, saved)) askSync(saved);
    return saved;
  } catch (e) { showMsg(e.message, true); return null; }
  finally { saving = false; btn.textContent = label; setBusy(false); }
}
function contentChanged(a, b) {
  return ["vk_text", "tg_text", "vk_background", "tg_background", "tg_as_file", "scheduled_at"].some(k => (a[k] ?? null) !== (b[k] ?? null))
    || JSON.stringify(a.images) !== JSON.stringify(b.images);
}
function askSync(post) {
  const where = ["vk", "tg"].filter(p => post[`${p}_status`] === "published").map(p => p.toUpperCase()).join(" и ");
  const queued = parseResult(post.tg_result)?.scheduled;
  $("#syncInfo").textContent = queued
    ? `Будет обновлён пост в ${where}. Telegram-отложка ещё не вышла – пост будет пересоздан с новым содержимым и временем.`
    : `Будет обновлён пост в ${where}. Telegram не даёт менять число фото и переносить текст между подписью и отдельным сообщением.`;
  openModal("#syncModal");
}
$("#syncLater").addEventListener("click", () => { closeModal("#syncModal"); showMsg("Сохранено. Правки на площадках не применены – кнопка «Применить» наверху", true); });
$("#syncNow").addEventListener("click", async () => { closeModal("#syncModal"); await doSync(); });
$("#syncBtn").addEventListener("click", async () => { if (state.dirty) { if (!(await save())) return; } await doSync(); });
async function doSync() {
  const p = state.current; if (!p.id) return;
  setBusy(true); showMsg("Применяю правки…");
  try {
    const res = await api(`/posts/${p.id}/sync`, { method: "POST" });
    await loadPosts(); await openPost(res);
    const errs = Object.entries(res.sync_errors || {});
    if (errs.length) showMsg(errs.map(([k, v]) => `${k.toUpperCase()}: ${v}`).join("\n"), true);
    else showMsg("Правки применены на площадках");
  } catch (e) { showMsg(e.message, true); }
  finally { setBusy(false); }
}

$("#saveBtn").addEventListener("click", async () => {
  const p = state.current, published = p.vk_status === "published" || p.tg_status === "published";
  if (await save(published ? undefined : "draft") && !$("#syncModal").matches(":not(.hidden)")) showMsg("Сохранено в черновик");
});
$("#scheduleBtn").addEventListener("click", async () => {
  const s = await save("scheduled");
  if (s) showMsg(`В отложке: выйдет ${fmtDate(s.scheduled_at)}`);
});
$("#publishBtn").addEventListener("click", async () => {
  if (!confirm("Опубликовать сейчас? Пост уйдёт в отложку и выйдет через 2 минуты, время в поле «Когда публиковать» не учитывается.")) return;
  const d = new Date(Date.now() + 2 * 60 * 1000); d.setSeconds(0, 0);
  F.scheduled_at.value = toLocalInput(d.toISOString());
  const b = $("#publishBtn"), label = b.textContent; b.textContent = "Публикую…";
  const s = await save("scheduled");
  b.textContent = label;
  if (s) showMsg(`Уйдёт через 2 минуты, в ${fmtDate(s.scheduled_at)}`);
});
$("#deleteBtn").addEventListener("click", async () => {
  const p = state.current; if (!p.id || !confirm("Удалить пост? На площадках он останется.")) return;
  try { await api(`/posts/${p.id}`, { method: "DELETE" }); } catch (e) { showMsg(e.message, true); return; }
  state.dirty = false;
  await loadPosts(); openPost(newPost()); setTab("posts");
});

function setBusy(b) { ["#saveBtn", "#scheduleBtn", "#publishBtn", "#syncBtn"].forEach(s => $(s).disabled = b); if (!b) renderStatus(); }

// ------------------------------------------------------------------ превью
const ICON = {
  like: `<svg viewBox="0 0 24 24"><path d="M12 20.5s-7.5-4.6-9.3-9.2C1.4 8 3.6 4.5 7.1 4.5c2 0 3.5 1.1 4.9 2.8 1.4-1.7 2.9-2.8 4.9-2.8 3.5 0 5.7 3.5 4.4 6.8-1.8 4.6-9.3 9.2-9.3 9.2z"/></svg>`,
  comment: `<svg viewBox="0 0 24 24"><path d="M4 12c0-4.4 3.6-7.5 8-7.5s8 3.1 8 7.5-3.6 7.5-8 7.5c-1 0-2-.2-2.9-.5L5 20.5l.9-3.7C4.7 15.5 4 13.8 4 12z"/></svg>`,
  share: `<svg viewBox="0 0 24 24"><path d="M13 5l8 7-8 7v-4.3c-5 0-8 1.5-10 5 .6-6 4-10 10-10.4z"/></svg>`,
  eye: `<svg viewBox="0 0 24 24"><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/></svg>`,
  tgEye: `<svg viewBox="0 0 24 24"><path d="M12 5C6.5 5 2.5 12 2.5 12S6.5 19 12 19s9.5-7 9.5-7-4-7-9.5-7zm0 11a4 4 0 110-8 4 4 0 010 8zm0-6.2a2.2 2.2 0 100 4.4 2.2 2.2 0 000-4.4z"/></svg>`,
  file: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>`,
};

function fmtMembers(n, forms) {
  if (n == null) return "";
  const m = n % 100, d = n % 10;
  const f = (m >= 11 && m <= 19) ? forms[2] : d === 1 ? forms[0] : (d >= 2 && d <= 4) ? forms[1] : forms[2];
  return `${n.toLocaleString("ru")} ${f}`;
}

function vkWhen(iso) {
  if (!iso) return "только что";
  const d = tzDate(iso), now = tzNow();
  const hm = tzTime(iso);
  const same = (a, b) => a.getUTCDate() === b.getUTCDate() && a.getUTCMonth() === b.getUTCMonth();
  const tomorrow = new Date(now); tomorrow.setUTCDate(now.getUTCDate() + 1);
  if (same(d, now)) return `сегодня в ${hm}`;
  if (same(d, tomorrow)) return `завтра в ${hm}`;
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} в ${hm}`;
}
const MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];

function imgUrls(p, pl) {
  return [p[`${pl}_background`] && `/api/files/backgrounds/${p[`${pl}_background`]}`, ...p.images.map(n => `/api/files/uploads/${n}`)].filter(Boolean);
}

/* Пересобирает «оболочку» превью (шапка, картинки, подвал) и вставляет в неё живые редакторы текста.
   Вызывается при смене картинок/подложек/времени/площадок – не при наборе текста. */
function renderPreviews() {
  const p = state.current; if (!p) return;
  const t = state.targets;
  const vkImgs = imgUrls(p, "vk"), tgImgs = imgUrls(p, "tg");

  // ---- VK: карточка поста как в мобильном приложении
  const vkName = t?.vk?.info?.name || "Сообщество";
  const vkAva = t?.vk?.info?.photo ? `<img src="${t.vk.info.photo}">` : esc(vkName[0] || "V");
  $("#previewVk").innerHTML = `
    <div class="vk-head">
      <div class="vk-ava">${vkAva}</div>
      <div class="vk-who"><div class="vk-name">${esc(vkName)}</div><div class="vk-time">${vkWhen(p.scheduled_at)}</div></div>
      <div class="vk-dots">⋮</div>
    </div>
    ${mediaGrid(vkImgs)}
    <div class="slot"></div>
    <div class="vk-foot">
      <span>${ICON.like}</span><span>${ICON.comment}</span><span>${ICON.share}</span>
      <span class="views">${ICON.eye} 0</span>
    </div>`;
  $("#previewVk .slot").appendChild(F.vk_text);
  $(".vk-phone").classList.toggle("off", !p.vk_enabled);

  // ---- Telegram: экран канала
  const tgName = t?.tg?.info?.name || "Канал";
  const tgSubs = fmtMembers(t?.tg?.info?.members, ["подписчик", "подписчика", "подписчиков"]) || "канал";
  const time = p.scheduled_at ? tzTime(p.scheduled_at) : tzTime(new Date().toISOString());
  const media = p.tg_as_file ? fileList(tgImgs) : mediaGrid(tgImgs);
  const meta = `<div class="tg-meta"><span>${ICON.tgEye} 1 &nbsp;${time}</span></div>`;
  $("#previewTg").innerHTML = `
    <div class="tg-header">
      <span class="back">←</span>
      <div class="tg-ava">${esc(tgName[0] || "T")}</div>
      <div class="tg-who"><div class="tg-title">${esc(tgName)}</div><div class="tg-subs">${esc(tgSubs)}</div></div>
      <span class="vk-dots" style="color:#fff">⋮</span>
    </div>
    <div class="tg-body">
      <div class="tg-msg" id="tgMsg1">${media}<div class="slot"></div>${meta}</div>
      <div class="tg-msg hidden" id="tgMsg2"><div class="slot"></div>${meta}</div>
      <div class="tg-note hidden" id="tgNote"></div>
    </div>`;
  $(".tg-phone").classList.toggle("off", !p.tg_enabled);
  placeTgEditor(true);
  updateCounts();
}

/* Telegram: подпись до 1024 символов живёт под фото, длиннее – отдельным сообщением.
   Переставляем редактор между пузырями, сохраняя выделение. */
function placeTgEditor(force = false) {
  const p = state.current; if (!p || !$("#tgMsg1")) return;
  const tgImgs = imgUrls(p, "tg");
  const len = RTE.plainLength(p.tg_text);
  const separate = tgImgs.length > 0 && len > TG_CAPTION_LIMIT;
  const target = $(separate ? "#tgMsg2 .slot" : "#tgMsg1 .slot");
  if (force || F.tg_text.parentNode !== target) {
    const sel = getSelection();
    const keep = sel.rangeCount && F.tg_text.contains(sel.anchorNode) ? sel.getRangeAt(0).cloneRange() : null;
    target.appendChild(F.tg_text);
    if (keep) { sel.removeAllRanges(); sel.addRange(keep); }
  }
  $("#tgMsg2").classList.toggle("hidden", !separate);
  $("#tgMsg1").classList.toggle("media-only", separate);
  const note = $("#tgNote");
  const notes = [];
  if (separate) notes.push("Подпись длиннее 1024 символов – текст уйдёт вторым сообщением");
  if (tgImgs.length > 10) notes.push("⚠ Больше 10 картинок – Telegram не примет");
  note.textContent = notes.join(" · ");
  note.classList.toggle("hidden", !notes.length);
}

function updateCounts() {
  const p = state.current; if (!p) return;
  const vkLen = RTE.plainLength(p.vk_text), tgLen = RTE.plainLength(p.tg_text);
  setCount($("#vkCount"), vkLen, VK_LIMIT);
  const hasMedia = imgUrls(p, "tg").length > 0;
  setCount($("#tgCount"), tgLen, hasMedia ? TG_CAPTION_LIMIT : TG_TEXT_LIMIT, hasMedia && tgLen > TG_CAPTION_LIMIT && tgLen <= TG_TEXT_LIMIT ? " (вторым сообщением)" : "");
}

function fileList(urls) {
  if (!urls.length) return "";
  return `<div class="tg-files">${urls.map((u, i) => `<div class="tg-file"><img src="${u}"><span>${ICON.file} файл ${i + 1}<small>без сжатия</small></span></div>`).join("")}</div>`;
}

$("#themeBtn").addEventListener("click", () => {
  const light = $("#previews").classList.toggle("light");
  $("#themeBtn").textContent = light ? "🌙 Тёмная тема" : "☀ Светлая тема";
  try { localStorage.setItem("previewTheme", light ? "light" : "dark"); } catch {}
});
try { if (localStorage.getItem("previewTheme") === "light") { $("#previews").classList.add("light"); $("#themeBtn").textContent = "🌙 Тёмная тема"; } } catch {}

function mediaGrid(urls) {
  if (!urls.length) return "";
  const cls = urls.length === 1 ? "n1" : urls.length === 2 ? "n2" : urls.length === 3 ? "n3" : urls.length === 4 ? "n4" : "nmany";
  return `<div class="media ${cls}">${urls.map(u => `<img src="${u}">`).join("")}</div>`;
}

function setCount(el, n, limit, extra = "") {
  el.textContent = `${n} / ${limit}${extra}`;
  el.classList.toggle("over", n > limit);
}

// ------------------------------------------------------------------ время: всё в UTC+5
const pad = (n) => String(n).padStart(2, "0");
function tzDate(iso) { return new Date(new Date(iso).getTime() + TZ_OFFSET * 3600e3); }   // сдвинутая дата, читать через getUTC*
function tzNow() { return tzDate(new Date().toISOString()); }
function tzTime(iso) { const d = tzDate(iso); return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`; }
function tzToInput(d) { return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`; }
function toLocalInput(iso) { return tzToInput(tzDate(iso)); }
function fromLocalInput(value) { return new Date(`${value}:00+05:00`).toISOString(); }
function fmtDate(iso) { if (!iso) return ""; const d = tzDate(iso); return `${pad(d.getUTCDate())}.${pad(d.getUTCMonth() + 1)}, ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`; }
function fmtDateTime(iso) { if (!iso) return ""; const d = tzDate(iso); return `${pad(d.getUTCDate())}.${pad(d.getUTCMonth() + 1)}.${d.getUTCFullYear()} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`; }

// ------------------------------------------------------------------ утилиты
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function linkify(s) {
  return s.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank">$1</a>')
          .replace(/(^|[^\w&\/])(#[\wа-яА-ЯёЁ_]+)/g, '$1<a href="#">$2</a>');
}
function stripTags(html) { const d = document.createElement("div"); d.innerHTML = html || ""; return d.textContent || ""; }
function firstLine(s) { return (s || "").split("\n")[0].slice(0, 60); }
function statusLabel(s) { return { draft: "черновик", scheduled: "в очереди", published: "опубликовано", error: "ошибка" }[s] || s; }
function showMsg(text, err = false) {
  const m = $("#msg"); m.textContent = text; m.className = "msg" + (err ? " err" : "");
  clearTimeout(showMsg.t); showMsg.t = setTimeout(hideMsg, err ? 8000 : 3500);
}
function hideMsg() { $("#msg").classList.add("hidden"); }

window.addEventListener("beforeunload", (e) => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });

boot();
