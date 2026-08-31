const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const state = {
  monthDate: new Date(), // used for the "This Month" nav
  clients: [],
  editingCid: null,
  sessionsCid: null,
  sessionsMonthDate: new Date(),
  rescheduleOrigDate: null,
};

// ── fetch helpers ────────────────────────────────────────────────────────

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (res.status === 401) {
    showLogin();
    throw new Error("Not authenticated");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${res.status})`);
  }
  return res.json();
}

const get = (path) => api(path);
const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data) });
const put = (path, data) => api(path, { method: "PUT", body: JSON.stringify(data) });
const del = (path) => api(path, { method: "DELETE" });

// ── screens ──────────────────────────────────────────────────────────────

function showLogin() {
  document.getElementById("app-screen").classList.add("hidden");
  document.getElementById("login-screen").classList.remove("hidden");
}

function showApp() {
  document.getElementById("login-screen").classList.add("hidden");
  document.getElementById("app-screen").classList.remove("hidden");
  loadDashboard();
}

async function checkAuth() {
  const { authed } = await get("/api/me");
  if (authed) showApp();
  else showLogin();
}

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const password = document.getElementById("login-password").value;
  const errEl = document.getElementById("login-error");
  errEl.textContent = "";
  try {
    await post("/api/login", { password });
    document.getElementById("login-password").value = "";
    showApp();
  } catch (err) {
    errEl.textContent = "Incorrect password.";
  }
});

document.getElementById("logout-btn").addEventListener("click", async () => {
  await post("/api/logout", {});
  showLogin();
});

// ── tabs ─────────────────────────────────────────────────────────────────

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

function switchView(name) {
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
  document.getElementById(`view-${name}`).classList.remove("hidden");
  document.querySelector(`.tab-btn[data-view="${name}"]`).classList.add("active");
  if (name === "dashboard") loadDashboard();
  if (name === "clients") loadClients();
  if (name === "receipt") loadReceiptTab();
  if (name === "settings") loadSettings();
}

// ── Dashboard ────────────────────────────────────────────────────────────

function fmtDate(iso) {
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString(undefined, { day: "2-digit", month: "short" });
}

function fmtMoney(n) {
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`;
  if (abs >= 10_000) return `$${(n / 1000).toFixed(1)}K`;
  return `$${n.toFixed(2)}`;
}

function fmtHours(n) {
  const abs = Math.abs(n);
  if (abs >= 10_000) return `${(n / 1000).toFixed(1)}K`;
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

async function loadAlltime() {
  const s = await get("/api/summary/alltime");
  document.getElementById("stat-earnings").textContent = fmtMoney(s.total_amount);
  document.getElementById("stat-hours").textContent = fmtHours(s.total_hours);
  document.getElementById("stat-students").textContent = String(s.active_students);
  const sinceLabel = new Date(s.since + "T00:00:00").toLocaleDateString(undefined, { month: "long", year: "numeric" });
  document.getElementById("stat-since-hint").textContent = `Since ${sinceLabel} · edit in Settings`;
}

async function loadDashboard() {
  await loadAlltime();
  const week = await get("/api/summary/week");
  const weekCard = document.getElementById("week-card");
  if (week.entries.length === 0) {
    weekCard.innerHTML = `<p class="empty-text">No lessons scheduled this week.</p>`;
  } else {
    weekCard.innerHTML = week.entries.map((e) => `
      <div class="session-row">
        <span class="session-date">${fmtDate(e.date)}</span>
        <span class="session-mid">${e.client_name}${e.time ? " · " + e.time : ""}</span>
        <span class="session-hours">${e.hours ?? ""}${e.hours ? " hr" + (e.hours !== 1 ? "s" : "") : ""}</span>
      </div>
    `).join("");
  }
  renderMonthLabel();
  await loadMonth();
}

function renderMonthLabel() {
  document.getElementById("month-label").textContent =
    state.monthDate.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

async function loadMonth() {
  const year = state.monthDate.getFullYear();
  const month = state.monthDate.getMonth() + 1;
  const summary = await get(`/api/summary/month?year=${year}&month=${month}`);
  const card = document.getElementById("month-card");
  if (summary.clients.length === 0) {
    card.innerHTML = `<p class="empty-text">No tuition lessons conducted this month.</p>`;
    return;
  }
  const rows = summary.clients.map((c) => `
    <div class="session-row">
      <span class="session-mid">${c.name}</span>
      <span class="session-hours">${c.sessions} session${c.sessions !== 1 ? "s" : ""}, ${c.hours} hr${c.hours !== 1 ? "s" : ""}</span>
      <span class="session-hours">$${c.amount.toFixed(2)}</span>
    </div>
  `).join("");
  card.innerHTML = rows + `
    <div class="total-row">
      <span>Total (${summary.total_hours} hrs)</span>
      <span>$${summary.total_amount.toFixed(2)}</span>
    </div>
  `;
}

document.getElementById("month-prev").addEventListener("click", () => {
  state.monthDate = new Date(state.monthDate.getFullYear(), state.monthDate.getMonth() - 1, 1);
  renderMonthLabel();
  loadMonth();
});
document.getElementById("month-next").addEventListener("click", () => {
  state.monthDate = new Date(state.monthDate.getFullYear(), state.monthDate.getMonth() + 1, 1);
  renderMonthLabel();
  loadMonth();
});

// ── Clients ──────────────────────────────────────────────────────────────

async function loadClients() {
  state.clients = await get("/api/clients");
  const listEl = document.getElementById("clients-list");
  if (state.clients.length === 0) {
    listEl.innerHTML = `<p class="empty-text">No clients yet.</p>`;
    return;
  }
  listEl.innerHTML = state.clients.map((c) => `
    <div class="client-card">
      <div class="client-card-top">
        <div>
          <div class="client-name">${c.name}${c.subject ? ` · ${c.subject}` : ""}</div>
          <div class="client-sub">${c.schedule_label}</div>
        </div>
        <div class="client-rate">$${Number(c.rate).toFixed(2)}/hr</div>
      </div>
      <div class="client-actions">
        <button class="secondary-btn" data-sessions="${c.cid}">Sessions</button>
        <button class="secondary-btn" data-edit="${c.cid}">Edit</button>
        <button class="danger-btn" data-delete="${c.cid}">Delete</button>
      </div>
    </div>
  `).join("");

  listEl.querySelectorAll("[data-edit]").forEach((btn) =>
    btn.addEventListener("click", () => openClientModal(btn.dataset.edit))
  );
  listEl.querySelectorAll("[data-delete]").forEach((btn) =>
    btn.addEventListener("click", () => deleteClient(btn.dataset.delete))
  );
  listEl.querySelectorAll("[data-sessions]").forEach((btn) =>
    btn.addEventListener("click", () => openSessionsModal(btn.dataset.sessions))
  );
}

async function deleteClient(cid) {
  const client = state.clients.find((c) => c.cid === cid);
  if (!confirm(`Delete ${client ? client.name : "this client"}? This can't be undone.`)) return;
  await del(`/api/clients/${cid}`);
  loadClients();
}

function openClientModal(cid) {
  state.editingCid = cid || null;
  const modal = document.getElementById("client-modal");
  const title = document.getElementById("client-modal-title");
  const form = document.getElementById("client-form");
  form.reset();

  if (cid) {
    const c = state.clients.find((x) => x.cid === cid);
    title.textContent = "Edit Client";
    document.getElementById("c-name").value = c.name || "";
    document.getElementById("c-subject").value = c.subject || "";
    document.getElementById("c-parent").value = c.parent_name || "";
    document.getElementById("c-contact").value = c.contact || "";
    document.getElementById("c-location").value = c.location || "";
    document.getElementById("c-rate").value = c.rate ?? "";
    document.getElementById("c-day").value = c.schedule_day ?? "";
    document.getElementById("c-hours").value = c.schedule_hours ?? "";
    document.getElementById("c-time").value = c.schedule_time || "";
  } else {
    title.textContent = "Add Client";
  }
  modal.classList.remove("hidden");
}

function closeClientModal() {
  document.getElementById("client-modal").classList.add("hidden");
  state.editingCid = null;
}

document.getElementById("add-client-btn").addEventListener("click", () => openClientModal(null));
document.getElementById("client-cancel-btn").addEventListener("click", closeClientModal);

document.getElementById("client-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const dayVal = document.getElementById("c-day").value;
  const hoursVal = document.getElementById("c-hours").value;
  const timeVal = document.getElementById("c-time").value;
  const payload = {
    name: document.getElementById("c-name").value.trim(),
    subject: document.getElementById("c-subject").value.trim(),
    parent_name: document.getElementById("c-parent").value.trim(),
    contact: document.getElementById("c-contact").value.trim(),
    location: document.getElementById("c-location").value.trim(),
    rate: parseFloat(document.getElementById("c-rate").value),
    schedule_day: dayVal === "" ? null : parseInt(dayVal, 10),
    schedule_hours: hoursVal === "" ? null : parseFloat(hoursVal),
    schedule_time: timeVal === "" ? null : timeVal,
  };
  if (state.editingCid) {
    await put(`/api/clients/${state.editingCid}`, payload);
  } else {
    await post("/api/clients", payload);
  }
  closeClientModal();
  loadClients();
});

// ── Sessions editor ──────────────────────────────────────────────────────

function openSessionsModal(cid) {
  state.sessionsCid = cid;
  state.sessionsMonthDate = new Date();
  const client = state.clients.find((c) => c.cid === cid);
  document.getElementById("sessions-modal-title").textContent = `Sessions — ${client ? client.name : ""}`;
  document.getElementById("sessions-modal").classList.remove("hidden");
  renderSessionsMonthLabel();
  loadSessionsList();
}

function closeSessionsModal() {
  document.getElementById("sessions-modal").classList.add("hidden");
  state.sessionsCid = null;
}

document.getElementById("sessions-close-btn").addEventListener("click", closeSessionsModal);

function renderSessionsMonthLabel() {
  document.getElementById("sessions-month-label").textContent =
    state.sessionsMonthDate.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

document.getElementById("sessions-month-prev").addEventListener("click", () => {
  state.sessionsMonthDate = new Date(state.sessionsMonthDate.getFullYear(), state.sessionsMonthDate.getMonth() - 1, 1);
  renderSessionsMonthLabel();
  loadSessionsList();
});
document.getElementById("sessions-month-next").addEventListener("click", () => {
  state.sessionsMonthDate = new Date(state.sessionsMonthDate.getFullYear(), state.sessionsMonthDate.getMonth() + 1, 1);
  renderSessionsMonthLabel();
  loadSessionsList();
});

function sessionRowHtml(row) {
  const dateLabel = fmtDate(row.date);
  const timeLabel = row.time ? ` · ${row.time}` : "";
  const hoursLabel = row.hours != null ? ` · ${row.hours} hr${row.hours !== 1 ? "s" : ""}` : "";

  let statusNote = "";
  let actions = "";
  let dim = false;

  if (row.kind === "extra") {
    statusNote = `<span class="session-status">One-off</span>`;
    actions = `<button class="danger-btn" data-remove-extra="${row.date}">Remove</button>`;
  } else if (row.status === "active") {
    statusNote = `<span class="session-status">Scheduled</span>`;
    actions = `
      <button class="secondary-btn" data-reschedule="${row.date}">Reschedule</button>
      <button class="danger-btn" data-cancel="${row.date}">Cancel</button>
    `;
  } else if (row.status === "cancelled") {
    statusNote = `<span class="session-status session-status-muted">Cancelled</span>`;
    actions = `<button class="secondary-btn" data-restore="${row.date}">Restore</button>`;
    dim = true;
  } else if (row.status === "rescheduled_out") {
    statusNote = `<span class="session-status session-status-muted">Moved to ${fmtDate(row.new_date)}${row.new_time ? " · " + row.new_time : ""}</span>`;
    actions = `<button class="secondary-btn" data-undo="${row.date}">Undo</button>`;
    dim = true;
  } else if (row.status === "rescheduled_in") {
    statusNote = `<span class="session-status">Makeup for ${fmtDate(row.orig_date)}</span>`;
    actions = `<button class="secondary-btn" data-undo="${row.orig_date}">Undo</button>`;
  }

  return `
    <div class="session-row${dim ? " session-row-dim" : ""}">
      <span class="session-date">${dateLabel}</span>
      <span class="session-mid">${statusNote}${timeLabel}${hoursLabel}</span>
      <span class="session-row-actions">${actions}</span>
    </div>
  `;
}

async function loadSessionsList() {
  const year = state.sessionsMonthDate.getFullYear();
  const month = state.sessionsMonthDate.getMonth() + 1;
  const rows = await get(`/api/clients/${state.sessionsCid}/sessions?year=${year}&month=${month}`);
  const listEl = document.getElementById("sessions-list");
  listEl.innerHTML = rows.length === 0
    ? `<p class="empty-text">No sessions this month.</p>`
    : rows.map(sessionRowHtml).join("");

  listEl.querySelectorAll("[data-cancel]").forEach((btn) =>
    btn.addEventListener("click", () => cancelSession(btn.dataset.cancel))
  );
  listEl.querySelectorAll("[data-restore]").forEach((btn) =>
    btn.addEventListener("click", () => restoreSession(btn.dataset.restore))
  );
  listEl.querySelectorAll("[data-undo]").forEach((btn) =>
    btn.addEventListener("click", () => undoReschedule(btn.dataset.undo))
  );
  listEl.querySelectorAll("[data-remove-extra]").forEach((btn) =>
    btn.addEventListener("click", () => removeExtraSession(btn.dataset.removeExtra))
  );
  listEl.querySelectorAll("[data-reschedule]").forEach((btn) =>
    btn.addEventListener("click", () => openRescheduleModal(btn.dataset.reschedule))
  );
}

async function cancelSession(dateStr) {
  await post(`/api/clients/${state.sessionsCid}/cancel`, { date: dateStr });
  loadSessionsList();
}
async function restoreSession(dateStr) {
  await post(`/api/clients/${state.sessionsCid}/restore`, { date: dateStr });
  loadSessionsList();
}
async function undoReschedule(origDate) {
  await post(`/api/clients/${state.sessionsCid}/undo-reschedule`, { date: origDate });
  loadSessionsList();
}
async function removeExtraSession(dateStr) {
  await del(`/api/clients/${state.sessionsCid}/extra-sessions/${dateStr}`);
  loadSessionsList();
}

function openRescheduleModal(origDate) {
  state.rescheduleOrigDate = origDate;
  document.getElementById("reschedule-orig-label").textContent = `Moving session from ${fmtDate(origDate)}`;
  document.getElementById("rsc-new-date").value = origDate;
  document.getElementById("rsc-new-time").value = "";
  document.getElementById("reschedule-modal").classList.remove("hidden");
}
function closeRescheduleModal() {
  document.getElementById("reschedule-modal").classList.add("hidden");
  state.rescheduleOrigDate = null;
}
document.getElementById("reschedule-cancel-btn").addEventListener("click", closeRescheduleModal);

document.getElementById("reschedule-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const newDate = document.getElementById("rsc-new-date").value;
  const newTime = document.getElementById("rsc-new-time").value;
  await post(`/api/clients/${state.sessionsCid}/reschedule`, {
    orig_date: state.rescheduleOrigDate,
    new_date: newDate,
    time: newTime || null,
  });
  closeRescheduleModal();
  loadSessionsList();
});

document.getElementById("add-session-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const dateVal = document.getElementById("as-date").value;
  const timeVal = document.getElementById("as-time").value;
  const hoursVal = parseFloat(document.getElementById("as-hours").value);
  await post(`/api/clients/${state.sessionsCid}/extra-sessions`, {
    date: dateVal, time: timeVal || null, hours: hoursVal,
  });
  e.target.reset();
  loadSessionsList();
});

// ── Receipt ──────────────────────────────────────────────────────────────

async function loadReceiptTab() {
  document.getElementById("receipt-output-wrap").classList.add("hidden");
  if (state.clients.length === 0) {
    state.clients = await get("/api/clients");
  }
  const select = document.getElementById("receipt-client");
  select.innerHTML = state.clients.map((c) => `<option value="${c.cid}">${c.name}</option>`).join("");
  const now = new Date();
  document.getElementById("receipt-month").value =
    `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

document.getElementById("receipt-generate-btn").addEventListener("click", async () => {
  const cid = document.getElementById("receipt-client").value;
  const monthVal = document.getElementById("receipt-month").value; // "YYYY-MM"
  if (!cid || !monthVal) return;
  const [year, month] = monthVal.split("-").map(Number);
  const { text } = await get(`/api/receipt?cid=${cid}&year=${year}&month=${month}`);
  document.getElementById("receipt-output").textContent = text;
  document.getElementById("receipt-output-wrap").classList.remove("hidden");
});

document.getElementById("receipt-copy-btn").addEventListener("click", async () => {
  const text = document.getElementById("receipt-output").textContent;
  try {
    await navigator.clipboard.writeText(text);
    const btn = document.getElementById("receipt-copy-btn");
    const original = btn.textContent;
    btn.textContent = "Copied!";
    setTimeout(() => (btn.textContent = original), 1500);
  } catch {
    alert("Copy failed — select and copy the text manually.");
  }
});

// ── Settings ─────────────────────────────────────────────────────────────

async function loadSettings() {
  const cfg = await get("/api/settings");
  document.getElementById("settings-tutor").value = cfg.tutor_name || "";
  document.getElementById("settings-payment").value = cfg.payment_info || "";
  document.getElementById("settings-since").value = (cfg.tracking_since || "").slice(0, 7); // "YYYY-MM"
  document.getElementById("settings-saved").classList.add("hidden");

  const { path } = await get("/api/settings/calendar-url");
  document.getElementById("calendar-url").value = `${location.origin}${path}`;
}

document.getElementById("calendar-copy-btn").addEventListener("click", async () => {
  const input = document.getElementById("calendar-url");
  try {
    await navigator.clipboard.writeText(input.value);
  } catch {
    input.select();
    document.execCommand("copy");
  }
  const btn = document.getElementById("calendar-copy-btn");
  const original = btn.textContent;
  btn.textContent = "Copied!";
  setTimeout(() => (btn.textContent = original), 1500);
});

document.getElementById("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const sinceVal = document.getElementById("settings-since").value; // "YYYY-MM"
  await post("/api/settings", {
    tutor_name: document.getElementById("settings-tutor").value.trim(),
    payment_info: document.getElementById("settings-payment").value.trim(),
    tracking_since: sinceVal ? `${sinceVal}-01` : "",
  });
  await loadAlltime();
  const saved = document.getElementById("settings-saved");
  saved.classList.remove("hidden");
  setTimeout(() => saved.classList.add("hidden"), 1500);
});

// ── PWA service worker ───────────────────────────────────────────────────

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}

// ── init ─────────────────────────────────────────────────────────────────

checkAuth();
