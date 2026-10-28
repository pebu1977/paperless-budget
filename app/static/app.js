"use strict";

// ------------------------------------------------------------ Zustand & Helfer
const S = {
  month: new Date().toISOString().slice(0, 7),
  view: "overview",
  meta: null,
  settings: null,
  budgets: {},
  charts: {},
  vehicles: [],
  traccarDevices: null,
};
const vehName = (id) => S.vehicles.find((v) => v.id === id)?.name ?? "";
const vehicleOptions = (sel, empty = "Fahrzeug …") => `<option value="">${empty}</option>` +
  S.vehicles.filter((v) => v.active || v.id === sel).map((v) => `<option value="${v.id}" ${v.id === sel ? "selected" : ""}>${esc(v.name)}</option>`).join("");
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (r.status === 401 && !path.startsWith("/api/login")) { location.href = "/login.html"; throw new Error("Nicht angemeldet"); }
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

const fmtPos = (n) => new Intl.NumberFormat("de-CH", { style: "currency", currency: S.meta?.currency || "CHF" }).format(n || 0);
const fmt = (n) => (n < 0 ? "− " + fmtPos(-n) : fmtPos(n));
const fmtDate = (d) => (d ? new Date(d + "T00:00:00").toLocaleDateString("de-CH") : "");
const monthLabel = (m) => new Date(m + "-01T00:00:00").toLocaleDateString("de-CH", { month: "long", year: "numeric" });
const monthShort = (m) => new Date(m + "-01T00:00:00").toLocaleDateString("de-CH", { month: "short", year: "2-digit" });
function shiftMonth(m, n) { const [y, mo] = m.split("-").map(Number); const d = new Date(y, mo - 1 + n, 1); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`; }

const tag = (id) => S.meta?.tags.find((t) => t.id === id);
const tagName = (id) => tag(id)?.name ?? `Tag ${id}`;
const tagColor = (id) => tag(id)?.color ?? "#888";
const corrName = (id) => S.meta?.correspondents.find((c) => c.id === id)?.name ?? "";
const typeName = (id) => S.meta?.document_types.find((t) => t.id === id)?.name ?? "";
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

let toastTimer;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), err ? 6000 : 2500);
}
function showAlert(msg) { const a = $("#alert"); a.textContent = msg || ""; a.hidden = !msg; }

function categoryOptions(selected) {
  const cats = S.settings?.category_tag_ids || [];
  return `<option value="">– keine –</option>` + cats.map((id) => `<option value="${id}" ${id === selected ? "selected" : ""}>${esc(tagName(id))}</option>`).join("");
}

// ------------------------------------------------------------ Dialog (Grund / Zweck)
let reasonCache = null;
function openDialog({ title, sub = "", label, value = "", suggestions = [], ok = "OK", extra = null }) {
  const d = $("#dlg");
  $("#dlgTitle").textContent = title; $("#dlgSub").textContent = sub; $("#dlgLabel").textContent = label;
  const inp = $("#dlgInput"); inp.value = value;
  $("#dlgList").innerHTML = suggestions.map((x) => `<option value="${esc(x)}">`).join("");
  $("#dlgChips").innerHTML = suggestions.slice(0, 6).map((x) => `<span class="chip" data-v="${esc(x)}">${esc(x)}</span>`).join("");
  $("#dlgOk").textContent = ok;
  const ex = $("#dlgExtra"); ex.hidden = !extra; if (extra) ex.textContent = extra.label;
  return new Promise((resolve) => {
    const chipClick = (e) => { const v = e.target.dataset.v; if (v) { inp.value = v; inp.focus(); } };
    const extraClick = () => { d.close("extra"); };
    $("#dlgChips").addEventListener("click", chipClick);
    ex.addEventListener("click", extraClick);
    d.addEventListener("close", () => {
      $("#dlgChips").removeEventListener("click", chipClick); ex.removeEventListener("click", extraClick);
      resolve(d.returnValue === "ok" ? { value: inp.value.trim() } : d.returnValue === "extra" ? { extra: true } : null);
    }, { once: true });
    d.returnValue = ""; d.showModal(); inp.focus(); inp.select();
  });
}

async function setBudgetFlag(item, include) {
  if (item.kind === "bank") {
    await api(`/api/transactions/${item.id}`, { method: "PATCH", body: JSON.stringify({ status: include ? "open" : "ignored" }) });
    return true;
  }
  if (include && item.exclude_source === "tag") {
    toast(`Wird über den Tag „${item.exclude_reason}“ ausgeschlossen – unter Einstellungen ändern`, true); return false;
  }
  let reason = null;
  if (!include) {
    reasonCache = reasonCache || await api("/api/exclude-reasons").catch(() => []);
    const r = await openDialog({ title: "Nicht im Budget berücksichtigen", sub: `${item.purpose || item.title} · ${fmt(item.amount || 0)}`,
      label: "Grund (optional)", suggestions: reasonCache, ok: "Ausschliessen" });
    if (!r) return false;
    reason = r.value || null;
    if (reason && !reasonCache.includes(reason)) reasonCache.unshift(reason);
  }
  await api(`/api/documents/${item.id}/flag`, { method: "PUT", body: JSON.stringify({ excluded: !include, reason }) });
  toast(include ? "Wieder im Budget" : "Nicht mehr im Budget" + (reason ? ` (${reason})` : ""));
  return true;
}

async function editPurpose(item) {
  const r = await openDialog({ title: "Wofür ist dieser Beleg?", sub: `${item.title} · ${fmtDate(item.date)}`, label: "Zweck",
    value: item.purpose || "", ok: "Speichern", extra: item.purpose_source === "manuell" ? { label: "neu aus Rechnung lesen" } : null });
  if (!r) return false;
  const res = r.extra ? await api(`/api/documents/${item.id}/purpose`, { method: "DELETE" })
    : await api(`/api/documents/${item.id}/purpose`, { method: "PUT", body: JSON.stringify({ purpose: r.value }) });
  item.purpose = res.purpose; item.purpose_source = res.purpose_source;
  return true;
}
const purposeHtml = (d) => d.purpose
  ? `<span class="it-purpose" title="${esc(d.purpose)}">${esc(d.purpose)}</span>`
  : `<span class="it-purpose none">Zweck nicht erkannt</span>`;
const whyHtml = (d) => d.excluded ? `<span class="why">${esc(d.exclude_reason || "nicht berücksichtigt")}</span>` : "";

// ------------------------------------------------------------ Navigation
const VIEW_AREA = { overview: "budget", expenses: "budget", review: "budget", bank: "bank", vehicles: "vehicles", realestate: "realestate",
  projects: "projects", contracts: "contracts", abos: "contracts", income: "income", meters: "meters" };
const canSee = (area) => !S.me || S.me.is_admin || ["view", "edit"].includes((S.me.perms || {})[area]);
const canEdit = (area) => !S.me || S.me.is_admin || (S.me.perms || {})[area] === "edit";
function applyPermissions() {
  document.querySelectorAll(".tabs button").forEach((b) => {
    const a = VIEW_AREA[b.dataset.view];
    b.hidden = a ? !canSee(a) : false;
  });
  const me = S.me;
  $("#userBox").hidden = !me?.users_enabled;
  if (me?.users_enabled && me.user) $("#userName").textContent = me.user.name || me.user.username;
  if (VIEW_AREA[S.view] && !canSee(VIEW_AREA[S.view])) {
    const first = [...document.querySelectorAll(".tabs button")].find((b) => !b.hidden);
    S.view = first ? first.dataset.view : "settings";
  }
}
function setView(view) {
  if (VIEW_AREA[view] && !canSee(VIEW_AREA[view])) return;
  S.view = view;
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== "view-" + view));
  const ro = VIEW_AREA[view] && !canEdit(VIEW_AREA[view]);
  $("#roBanner").hidden = !ro;
  render();
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
document.addEventListener("click", (e) => { const g = e.target.closest("[data-goto]"); if (g) { e.preventDefault(); setView(g.dataset.goto); } });
$("#prevMonth").onclick = () => { S.month = shiftMonth(S.month, -1); render(); };
$("#nextMonth").onclick = () => { S.month = shiftMonth(S.month, 1); render(); };

async function render() {
  $("#monthLabel").textContent = monthLabel(S.month);
  showAlert("");
  try {
    if (S.view === "overview") await renderOverview();
    if (S.view === "expenses") await renderExpenses();
    if (S.view === "review") await renderReview();
    if (S.view === "income") await renderIncome();
    if (S.view === "bank") await renderBank();
    if (S.view === "vehicles") await renderVehicles();
    if (S.view === "realestate") await renderRE();
    if (S.view === "projects") await renderProjects();
    if (S.view === "contracts") await renderContracts();
    if (S.view === "abos") await renderAbos();
    if (S.view === "meters") await renderMeters();
    if (S.view === "settings") await renderSettings();
  } catch (e) { showAlert(e.message); }
  updateReviewBadge();
}

async function updateReviewBadge() {
  if (!S.meta?.amount_field) return;
  try {
    const docs = await api(`/api/review?month=${S.month}`);
    const b = $("#reviewBadge"); b.textContent = docs.length; b.hidden = docs.length === 0;
  } catch {}
}

// ------------------------------------------------------------ Übersicht
async function renderOverview() {
  const missing = !S.meta.amount_field;
  $("#setupCard").hidden = !missing;
  $("#setupFieldName").textContent = S.meta.amount_field_name;
  $("#noCatHint").hidden = missing || S.settings.category_tag_ids.length > 0;
  if (missing) return;

  const [sum, trend] = await Promise.all([api(`/api/summary?month=${S.month}`), api(`/api/trend?until=${S.month}&months=12`)]);

  $("#kpiIncome").textContent = fmt(sum.total_income);
  $("#kpiSpent").textContent = fmt(sum.total_spent);
  $("#kpiDocs").innerHTML = esc(`${sum.document_count} Beleg${sum.document_count === 1 ? "" : "e"}` + (sum.bank_count ? ` · ${sum.bank_count} Bankbuchung${sum.bank_count === 1 ? "" : "en"} ohne Beleg` : ""))
    + (sum.excluded && sum.excluded.count ? ` · <a href="#" id="kpiExcl">${sum.excluded.count} nicht berücksichtigt (${esc(fmt(sum.excluded.amount))})</a>` : "");
  const ke = $("#kpiExcl"); if (ke) ke.onclick = (e) => { e.preventDefault(); $("#ovFilter").value = "out"; drawOvItems(); $("#ovItems").scrollIntoView({ behavior: "smooth", block: "center" }); };
  ovItems = await api(`/api/month-items?month=${S.month}`).catch(() => []); ovShowAll = false; drawOvItems();
  renderPlan();
  const bal = $("#kpiBalance"); bal.textContent = fmt(sum.balance); bal.className = "kpi-value " + (sum.balance >= 0 ? "pos" : "neg");
  const budgetedSpent = sum.categories.filter((c) => c.budget != null).reduce((a, c) => a + c.spent, 0);
  if (sum.total_budget > 0) {
    const pct = Math.round((budgetedSpent / sum.total_budget) * 100);
    $("#kpiBudget").textContent = pct + " %";
    $("#kpiBudget").className = "kpi-value " + (pct > 100 ? "neg" : "");
    $("#kpiBudgetSub").textContent = `${fmt(budgetedSpent)} von ${fmt(sum.total_budget)}`;
  } else { $("#kpiBudget").textContent = "–"; $("#kpiBudgetSub").textContent = "keine Budgets gesetzt"; }

  // Kategorien-Liste
  const rows = [...sum.categories].sort((a, b) => b.spent - a.spent);
  if (sum.uncategorized.spent) rows.push({ name: "Unkategorisiert", color: "#999", spent: sum.uncategorized.spent, count: sum.uncategorized.count, budget: null });
  $("#catList").innerHTML = rows.length ? rows.map((c) => {
    const pct = c.budget ? (c.spent / c.budget) * 100 : 0;
    const cls = c.budget ? (pct > 100 ? "over" : pct > 80 ? "warn" : "") : "";
    const rest = c.budget != null ? c.budget - c.spent : null;
    return `<div>
      <div class="cat-top"><span class="cat-name"><span class="dot" style="background:${esc(c.color)}"></span>${esc(c.name)}</span>
      <span class="cat-amt"><b>${fmt(c.spent)}</b>${c.budget != null ? " / " + fmt(c.budget) : ""}</span></div>
      ${c.budget != null ? `<div class="bar ${cls}"><div style="width:${Math.min(pct, 100)}%"></div></div>
      <div class="cat-rest ${rest < 0 ? "over" : ""}">${rest < 0 ? fmt(-rest) + " über Budget" : fmt(rest) + " übrig"}</div>` : `<div class="cat-rest">${c.count} Dokument(e), kein Budget</div>`}
    </div>`;
  }).join("") : `<div class="empty">Keine Ausgaben in diesem Monat.</div>`;

  // Kreisdiagramm
  const pieRows = rows.filter((r) => r.spent > 0);
  drawChart("pieChart", {
    type: "doughnut",
    data: { labels: pieRows.map((r) => r.name), datasets: [{ data: pieRows.map((r) => r.spent), backgroundColor: pieRows.map((r) => r.color), borderColor: css("--surface"), borderWidth: 2 }] },
    options: { maintainAspectRatio: false, cutout: "62%", plugins: { legend: { position: "right", labels: { color: css("--text"), boxWidth: 10 } }, tooltip: { callbacks: { label: (c) => ` ${c.label}: ${fmt(c.raw)}` } } } },
  });

  // Verlauf
  const grid = css("--line"), muted = css("--muted");
  drawChart("trendChart", {
    data: {
      labels: trend.map((t) => monthShort(t.month)),
      datasets: [
        { type: "bar", label: "Ausgaben", data: trend.map((t) => t.spent), backgroundColor: css("--bad") + "cc", borderRadius: 4, order: 2 },
        { type: "line", label: "Einnahmen", data: trend.map((t) => t.income), borderColor: css("--accent"), backgroundColor: css("--accent"), tension: 0, pointRadius: 3, order: 1 },
      ],
    },
    options: {
      maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      scales: { x: { grid: { display: false }, ticks: { color: muted } }, y: { grid: { color: grid }, ticks: { color: muted, callback: (v) => fmt(v).replace(/\.00$/, "") } } },
      plugins: { legend: { labels: { color: css("--text"), boxWidth: 10 } }, tooltip: { callbacks: { label: (c) => ` ${c.dataset.label}: ${fmt(c.raw)}` } } },
      onClick: (_, els) => { if (els.length) { S.month = trend[els[0].index].month; render(); } },
    },
  });
}

let ovItems = [], ovShowAll = false;
function drawOvItems() {
  const f = $("#ovFilter").value, q = $("#ovSearch").value.trim().toLowerCase();
  let list = ovItems.filter((d) => (f === "all" || (f === "in" ? !d.excluded : d.excluded)));
  if (q) list = list.filter((d) => `${d.purpose || ""} ${d.title} ${corrName(d.correspondent)} ${d.exclude_reason || ""}`.toLowerCase().includes(q));
  list = [...list].sort((a, b) => b.amount - a.amount);
  const shown = ovShowAll || q ? list : list.slice(0, 12);
  $("#ovItems").innerHTML = shown.length ? shown.map((d) => `<div class="it ${d.excluded ? "out" : ""}" data-kind="${d.kind}" data-id="${d.id}">
      <input type="checkbox" class="budget" ${d.excluded ? "" : "checked"} ${d.exclude_source === "tag" ? "disabled" : ""} title="Im Budget berücksichtigen">
      <span class="it-date muted">${fmtDate(d.date)}</span>
      <div class="it-main">
        <div>${purposeHtml(d)}${d.kind === "doc" ? ` <button class="edit" data-edit title="Zweck bearbeiten">✎</button>` : ""}<button class="edit" data-alloc title="Haus / Projekt zuordnen">↗</button>${whyHtml(d)}${allocBadges(d)}</div>
        <div class="it-sub">${d.url ? `<a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.title)}</a>` : esc(d.title)}${d.correspondent ? " · " + esc(corrName(d.correspondent)) : ""}</div>
      </div>
      <span class="it-cat">${d.category ? `<span class="dot" style="background:${esc(tagColor(d.category))}"></span>${esc(tagName(d.category))}` : "–"}</span>
      <span class="it-amt">${fmt(d.amount)}</span>
    </div>`).join("") : `<div class="empty">Keine Einträge.</div>`;
  const more = $("#ovMore"); more.hidden = ovShowAll || !!q || list.length <= 12; more.textContent = `alle ${list.length} anzeigen`;
}
$("#ovFilter").onchange = drawOvItems;
$("#ovSearch").oninput = drawOvItems;
$("#ovMore").onclick = () => { ovShowAll = true; drawOvItems(); };
$("#ovItems").addEventListener("click", async (e) => {
  const row = e.target.closest(".it"); if (!row) return;
  const item = ovItems.find((x) => x.kind === row.dataset.kind && x.id === Number(row.dataset.id));
  try {
    if (e.target.classList.contains("budget")) {
      e.preventDefault();
      if (await setBudgetFlag(item, !!e.target.checked === false ? false : true)) render();
    } else if (e.target.hasAttribute("data-edit")) {
      if (await editPurpose(item)) drawOvItems();
    } else if (e.target.hasAttribute("data-alloc")) {
      openAlloc({ ...item, amount: item.amount_full ?? item.amount });
    }
  } catch (err) { toast(err.message, true); }
});

function drawChart(id, cfg) {
  if (!window.Chart) return;
  S.charts[id]?.destroy();
  S.charts[id] = new Chart(document.getElementById(id), cfg);
}

$("#createField").onclick = async () => {
  try { await api("/api/setup/amount-field", { method: "POST" }); S.meta = await api("/api/meta?refresh=true"); toast("Feld angelegt"); render(); }
  catch (e) { toast(e.message, true); }
};

// ------------------------------------------------------------ Ausgaben
let expDocs = [];
async function renderExpenses() {
  if (!S.meta.amount_field) { $("#expBody").innerHTML = `<tr><td colspan="5" class="empty">Zuerst das Betrags-Feld anlegen (Übersicht).</td></tr>`; return; }
  const [inc, excl] = await Promise.all([api(`/api/expenses?month=${S.month}&all=true`), api(`/api/excluded?month=${S.month}`)]);
  const seen = new Set(inc.map((d) => d.id));
  expDocs = [...inc, ...excl.filter((d) => !seen.has(d.id))].map((d) => ({ ...d, kind: "doc" }));
  const f = $("#expFilter"), prev = f.value;
  f.innerHTML = `<option value="all">Alle Kategorien</option><option value="none">Unkategorisiert</option>` +
    S.settings.category_tag_ids.map((id) => `<option value="${id}">${esc(tagName(id))}</option>`).join("") +
    `<option value="out">Nicht berücksichtigt</option>`;
  f.value = [...f.options].some((o) => o.value === prev) ? prev : "all";
  drawExpenses();
}
$("#expFilter").onchange = drawExpenses;
try { $("#expShowOut").checked = localStorage.getItem("expShowOut") === "1"; } catch {}
$("#expShowOut").onchange = (e) => { try { localStorage.setItem("expShowOut", e.target.checked ? "1" : "0"); } catch {} drawExpenses(); };

$("#expSearch").oninput = () => drawExpenses();
function drawExpenses() {
  const f = $("#expFilter").value, q = $("#expSearch").value.trim().toLowerCase();
  const showOut = $("#expShowOut").checked;
  let list = expDocs.filter((d) => f === "out" ? d.excluded
    : (f === "all" || (f === "none" ? d.category == null : d.category === Number(f))) && (showOut || !d.excluded));
  const hidden = f === "out" || showOut ? 0 : expDocs.filter((d) => d.excluded).length;
  $("#expOutCount").textContent = hidden ? `(${hidden})` : "";
  $("#expShowOut").closest("label").hidden = f === "out";
  if (q) list = list.filter((d) => `${d.purpose || ""} ${d.title} ${corrName(d.correspondent)} ${d.exclude_reason || ""}`.toLowerCase().includes(q));
  $("#expBody").innerHTML = list.length ? list.map((d) => `<tr data-id="${d.id}" class="${d.excluded ? "out" : ""}">
      <td><input type="checkbox" class="budget" ${d.excluded ? "" : "checked"} ${d.exclude_source === "tag" ? "disabled" : ""} title="Im Budget berücksichtigen"></td>
      <td>${fmtDate(d.date)}</td>
      <td style="max-width:380px"><div class="it-main"><div>${purposeHtml(d)} <button class="edit" data-edit title="Zweck bearbeiten">✎</button><button class="edit" data-alloc title="Haus / Projekt zuordnen">↗</button>${whyHtml(d)}${allocBadges(d)}</div>
        <div class="it-sub"><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.title)}</a></div></div></td>
      <td>${esc(corrName(d.correspondent))}</td>
      <td><select class="cat">${categoryOptions(d.category)}</select></td>
      <td class="num">${d.amount == null ? `<span class="muted small">ohne Betrag</span>` : `<input class="amount" type="number" step="0.05" value="${(d.amount_full ?? d.amount).toFixed(2)}">${d.alloc_note ? `<div class="muted small">${esc(d.alloc_note)}</div>` : ""}`}</td>
    </tr>`).join("") : `<tr><td colspan="6" class="empty">Keine Ausgaben.</td></tr>`;
  const counted = list.filter((d) => !d.excluded && d.amount != null);
  $("#expTotal").textContent = fmt(counted.reduce((a, d) => a + d.amount, 0));
  const nOut = list.filter((d) => d.excluded).length;
  $("#expTotalNote").textContent = nOut && f !== "out" ? `(ohne ${nOut} nicht berücksichtigte)` : f === "out" ? "(zählt nicht zum Budget)"
    : hidden ? `(${hidden} nicht berücksichtigte ausgeblendet)` : "";
}
$("#expBody").addEventListener("click", async (e) => {
  const tr = e.target.closest("tr[data-id]"); if (!tr) return;
  const doc = expDocs.find((d) => d.id === Number(tr.dataset.id));
  try {
    if (e.target.classList.contains("budget")) {
      e.preventDefault();
      if (await setBudgetFlag(doc, !doc.excluded ? false : true)) await renderExpenses();
    } else if (e.target.hasAttribute("data-edit")) {
      if (await editPurpose(doc)) drawExpenses();
    } else if (e.target.hasAttribute("data-alloc")) {
      openAlloc({ ...doc, amount: doc.amount_full ?? doc.amount });
    }
  } catch (err) { toast(err.message, true); }
});

$("#expBody").addEventListener("change", async (e) => {
  const tr = e.target.closest("tr[data-id]"); if (!tr) return;
  const id = Number(tr.dataset.id), doc = expDocs.find((d) => d.id === id);
  const body = {};
  if (e.target.classList.contains("cat")) body.category_tag_id = e.target.value ? Number(e.target.value) : null;
  if (e.target.classList.contains("amount")) body.amount = e.target.value === "" ? null : Number(e.target.value);
  try {
    const upd = await api(`/api/documents/${id}`, { method: "PATCH", body: JSON.stringify(body) });
    Object.assign(doc, upd);
    if (upd.amount == null) expDocs = expDocs.filter((d) => d.id !== id);
    drawExpenses(); toast("In Paperless gespeichert");
  } catch (err) { toast(err.message, true); }
});

// ------------------------------------------------------------ Erfassen
async function renderReview() {
  const list = $("#reviewList");
  if (!S.meta.amount_field) { list.innerHTML = `<div class="empty">Zuerst das Betrags-Feld anlegen (Übersicht).</div>`; return; }
  const st = S.settings;
  const parts = [];
  if (st.review_document_type_ids.length) parts.push("Typ: " + st.review_document_type_ids.map(typeName).join(", "));
  if (st.review_tag_ids.length) parts.push("Tag: " + st.review_tag_ids.map(tagName).join(", "));
  $("#reviewInfo").innerHTML = `Dokumente aus ${esc(monthLabel(S.month))} ohne „${esc(S.meta.amount_field.name)}“` + (parts.length ? ` · ${esc(parts.join(" · "))}` : ` · alle Dokumenttypen (eingrenzen unter <a href="#" data-goto="settings">Einstellungen</a>)`);
  const docs = await api(`/api/review?month=${S.month}`);
  reviewDocs = docs;
  list.innerHTML = docs.length ? docs.map((d) => `<div class="rv" data-id="${d.id}">
      <div>
        <div class="rv-title"><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.title)}</a></div>
        <div class="rv-meta">${fmtDate(d.date)}${d.correspondent ? " · " + esc(corrName(d.correspondent)) : ""}${d.document_type ? " · " + esc(typeName(d.document_type)) : ""}</div>
        <div class="rv-purpose">${purposeHtml(d)} <button class="edit" data-edit title="Zweck bearbeiten">✎</button></div>
      </div>
      <div class="rv-actions">
        <select class="cat">${categoryOptions(d.category)}</select>
        <input class="amount" type="number" step="0.05" placeholder="Betrag">
        <button class="primary save">Speichern</button>
        <button class="linkbtn" data-skip title="Zählt nicht zum Budget und verschwindet aus dieser Liste">nicht berücksichtigen</button>
      </div>
      ${d.content ? `<div class="rv-content">${esc(d.content)}</div>` : ""}
    </div>`).join("") : `<div class="empty">Alles erfasst – keine Dokumente ohne Betrag. 🎉</div>`;
}

let reviewDocs = [];
$("#reviewList").addEventListener("click", async (e) => {
  const card0 = e.target.closest(".rv"); if (!card0) return;
  const item = reviewDocs.find((d) => d.id === Number(card0.dataset.id));
  if (e.target.hasAttribute("data-edit")) {
    try { if (await editPurpose(item)) card0.querySelector(".rv-purpose").innerHTML = `${purposeHtml(item)} <button class="edit" data-edit title="Zweck bearbeiten">✎</button>`; }
    catch (err) { toast(err.message, true); }
    return;
  }
  if (e.target.hasAttribute("data-skip")) {
    try {
      if (await setBudgetFlag({ ...item, kind: "doc" }, false)) { card0.remove(); updateReviewBadge(); if (!$("#reviewList").children.length) renderReview(); }
    } catch (err) { toast(err.message, true); }
    return;
  }
  if (!e.target.classList.contains("save")) return;
  const card = e.target.closest(".rv"), id = Number(card.dataset.id);
  const amount = card.querySelector(".amount").value, cat = card.querySelector(".cat").value;
  if (amount === "") { toast("Bitte Betrag eingeben", true); card.querySelector(".amount").focus(); return; }
  e.target.disabled = true;
  try {
    await api(`/api/documents/${id}`, { method: "PATCH", body: JSON.stringify({ amount: Number(amount), category_tag_id: cat ? Number(cat) : null }) });
    card.remove(); toast("In Paperless gespeichert"); updateReviewBadge();
    if (!$("#reviewList").children.length) renderReview();
  } catch (err) { e.target.disabled = false; toast(err.message, true); }
});
$("#reviewList").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.classList.contains("amount")) e.target.closest(".rv").querySelector(".save").click();
});

// ------------------------------------------------------------ Einnahmen
let incomes = [];
async function renderIncome() {
  incomes = await api("/api/incomes");
  const form = $("#incForm");
  if (!form.date.value) form.date.value = S.month + "-25";
  $("#incBody").innerHTML = incomes.length ? incomes.map((i) => `<tr>
      <td>${esc(i.description)}</td>
      <td>${fmtDate(i.date)}${i.recurring && i.end_date ? " – " + fmtDate(i.end_date) : ""}</td>
      <td>${i.recurring ? "monatlich" : "einmalig"}</td>
      <td class="num">${fmt(i.amount)}</td>
      <td class="num"><button data-edit="${i.id}">Bearbeiten</button> <button class="danger" data-del="${i.id}">Löschen</button></td>
    </tr>`).join("") : `<tr><td colspan="5" class="empty">Noch keine Einnahmen erfasst.</td></tr>`;
  const sum = await api(`/api/summary?month=${S.month}`).catch(() => null);
  drawIncomeRules();
  drawPayslips();
  $("#incMonth").innerHTML = sum ? (sum.incomes.length
    ? `<table class="table"><tbody>${sum.incomes.map((i) => `<tr><td>${esc(i.description)}
        ${i.source === "bank" ? ` <span class="srcb Bank" title="${esc(i.detail || "")}">Bank ${fmtDate(i.date)}</span>` : i.source === "erwartet" ? ` <span class="srcb Manuell" title="${esc(i.detail || "")}">erwartet ${fmtDate(i.date)}</span>`
          : i.source === "Lohnabrechnung" ? ` <span class="srcb Beleg" title="${esc(i.detail || "")}">Lohnabrechnung ${fmtDate(i.date)}</span>` : ""}</td>
        <td class="num">${fmt(i.amount)}</td></tr>`).join("")}</tbody>
       <tfoot><tr><td>Total ${esc(monthLabel(S.month))}</td><td class="num">${fmt(sum.total_income)}</td></tr></tfoot></table>`
    : `<div class="empty">Keine Einnahmen in diesem Monat.</div>`) : `<div class="muted small">Monatsübersicht nicht verfügbar.</div>`;
}

let psAuto = false;
async function drawPayslips() {
  const yNow = Number(S.month.slice(0, 4));
  if (!$("#psYear").options.length) $("#psYear").innerHTML = [0, 1, 2, 3].map((i) => `<option ${i === 0 ? "selected" : ""}>${new Date().getFullYear() - i}</option>`).join("");
  const year = Number($("#psYear").value) || yNow;
  if (!psAuto) {   // einmal pro Seitenaufruf neue Abrechnungen einlesen
    psAuto = true;
    try { const r = await api("/api/payslips/parse-new", { method: "POST" }); if (r.parsed) toast(`${r.parsed} Lohnabrechnung(en) eingelesen${r.parsed - r.ok ? `, ${r.parsed - r.ok} nicht erkannt` : ""}`); } catch {}
  }
  const d = await api(`/api/payslips?year=${year}`);
  const k = (label, v) => v ? `<div><span class="muted small">${label}</span><b class="num">${fmt(v)}</b></div>` : "";
  $("#psTotals").innerHTML = d.totals.map((p) => `<div class="ps-card"><div class="ps-head"><b>${esc(p.person)}</b> <span class="muted small">${esc(p.employers.join(", "))} · ${p.count} Abrechnung(en) ${year}</span></div>
      <div class="ps-grid">${k("Brutto", p.gross)}${k("davon Kinderzulagen", p.child_allowance)}${k("AHV/IV/EO", p.ahv)}${k("ALV", p.alv)}${k("Pensionskasse", p.bvg)}
      ${k("Versicherungen", p.insurance)}${k("Quellensteuer", p.tax)}${k("übrige Abzüge", p.other)}${k("Netto", p.net)}${k("ausbezahlt", p.payout)}</div></div>`).join("");
  $("#psBody").innerHTML = d.payslips.length ? d.payslips.map((r) => r.ok ? `<tr>
      <td>${esc(r.month || "")}</td>
      <td><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.person || "–")}</a> <span class="muted small">· ${esc(r.employer || "")}</span>
        ${r.deductions.length ? `<div class="it-sub" title="${esc(r.deductions.map((x) => `${x.label}: ${x.amount.toFixed(2)}`).join("\n"))}">${esc(r.deductions.map((x) => x.label).slice(0, 4).join(", "))}${r.deductions.length > 4 ? " …" : ""}</div>` : ""}</td>
      <td class="num">${fmt(r.gross)}</td><td class="num">${r.deductions_total ? "− " + fmtPos(r.deductions_total) : "–"}</td><td class="num">${fmt(r.net || 0)}</td>
      <td class="num"><b>${fmt(r.payout)}</b></td>
      <td>${r.bank ? `<span class="pill">✓ ${fmtDate(r.bank.date)}</span>` : `<span class="pill grey" title="Noch keine passende Gutschrift – zählt aus der Abrechnung">offen</span>`}</td></tr>`
    : `<tr data-ps="${r.doc_id}"><td><input name="month" type="month" value="${esc(r.month || "")}"></td>
      <td><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title || "Lohnabrechnung")}</a> <span class="neg small">nicht erkannt – Werte eintragen</span>
        <input name="person" value="${esc(r.person || "")}" placeholder="Person"></td>
      <td><input name="gross" type="number" step="0.05" value="${r.gross ?? ""}" placeholder="Brutto"></td><td></td>
      <td><input name="net" type="number" step="0.05" value="${r.net ?? ""}" placeholder="Netto"></td>
      <td><input name="payout" type="number" step="0.05" value="${r.payout ?? ""}" placeholder="ausbezahlt"></td><td></td></tr>`).join("")
    : `<tr><td colspan="7" class="muted small">Keine Lohnabrechnungen ${year} gefunden.</td></tr>`;
}
$("#psYear").onchange = drawPayslips;
$("#psParseNew").onclick = async (e) => {
  e.target.disabled = true;
  try { const r = await api("/api/payslips/parse-new", { method: "POST" }); toast(r.parsed ? `${r.parsed} Lohnabrechnung(en) eingelesen` : "Keine neuen Lohnabrechnungen"); renderIncome(); }
  catch (err) { toast(err.message, true); }
  e.target.disabled = false;
};
$("#psBody").addEventListener("change", async (e) => {
  const tr = e.target.closest("[data-ps]"); if (!tr) return;
  const v = e.target.type === "number" ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value || null;
  try { await api(`/api/payslips/${tr.dataset.ps}`, { method: "PUT", body: JSON.stringify({ [e.target.name]: v }) }); toast("Gespeichert"); }
  catch (err) { toast(err.message, true); }
});
async function drawIncomeRules() {
  const [rules, credits] = await Promise.all([api("/api/income-rules"), api(`/api/income-credits?month=${S.month}`)]);
  $("#irBody").innerHTML = rules.length ? rules.map((r) => `<tr data-ir="${r.id}">
      <td><input name="keyword" value="${esc(r.keyword)}" placeholder="z.B. Muster AG + Lohn"></td>
      <td><input name="label" value="${esc(r.label)}"></td>
      <td><input name="expected" type="number" step="0.05" value="${r.expected ?? ""}" placeholder="optional"></td>
      <td><input name="day" type="number" min="1" max="31" value="${r.day ?? ""}" placeholder="25"></td>
      <td><input name="active" type="checkbox" ${r.active ? "checked" : ""}></td>
      <td><button class="linkbtn danger" data-irdel="${r.id}">löschen</button></td></tr>`).join("")
    : `<tr><td colspan="6" class="muted small">Noch keine Regel – unten bei einer Gutschrift „als Einnahme erkennen“ wählen oder „+ Regel“.</td></tr>`;
  $("#irCredits").innerHTML = credits.length ? credits.map((t) => `<div class="invitem"><span>${fmtDate(t.date)} · <b>${esc(t.merchant || t.text.slice(0, 50))}</b>
      <span class="muted small">${esc(t.text.slice(0, 70))}</span></span>
      <span class="row"><b class="num">${fmt(t.amount)}</b>${t.rule ? `<span class="pill">💰 ${esc(t.rule)}</span>` : t.rent ? `<span class="pill grey">Miete</span>`
        : t.status === "ignored" ? `<span class="pill grey">ignoriert</span>` : `<button class="linkbtn" data-irnew="${esc(t.merchant || t.text.split(" · ")[0] || "")}" data-amt="${t.amount}" data-day="${Number(t.date.slice(8))}">als Einnahme erkennen</button>`}</span></div>`).join("")
    : `<div class="muted small">Keine Gutschriften im ${esc(monthLabel(S.month))} – Kontoauszüge unter <a href="#" data-goto="bank">Abgleich</a> einlesen.</div>`;
}
async function saveIncomeRule(tr) {
  const q = (n) => tr.querySelector(`[name=${n}]`);
  const body = { keyword: q("keyword").value, label: q("label").value, expected: q("expected").value === "" ? null : Number(q("expected").value),
    day: q("day").value === "" ? null : Number(q("day").value), active: q("active").checked };
  try { await api(`/api/income-rules/${tr.dataset.ir}`, { method: "PUT", body: JSON.stringify(body) }); toast("Gespeichert"); renderIncome(); }
  catch (err) { toast(err.message, true); }
}
$("#irBody").addEventListener("change", (e) => { const tr = e.target.closest("[data-ir]"); if (tr) saveIncomeRule(tr); });
$("#irBody").addEventListener("click", async (e) => {
  const id = e.target.dataset.irdel; if (!id || !confirm("Regel löschen?")) return;
  await api(`/api/income-rules/${id}`, { method: "DELETE" }); renderIncome();
});
$("#irAdd").onclick = async () => {
  const r = await openDialog({ title: "Einnahme automatisch erkennen", label: "Stichwort in der Gutschrift (z.B. Name des Arbeitgebers)", ok: "Anlegen" });
  if (!r || !r.value) return;
  await api("/api/income-rules", { method: "POST", body: JSON.stringify({ keyword: r.value, label: "Lohn" }) }); renderIncome();
};
$("#irCredits").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-irnew]"); if (!b) return;
  const r = await openDialog({ title: "Als Einnahme erkennen", sub: "Alle Gutschriften mit diesem Stichwort zählen künftig als Einnahme.",
    label: "Stichwort", value: b.dataset.irnew, ok: "Weiter" });
  if (!r || !r.value) return;
  const l = await openDialog({ title: "Bezeichnung", label: "z.B. Lohn Peter, Kindergeld, AHV", value: "Lohn", ok: "Anlegen" });
  if (!l) return;
  await api("/api/income-rules", { method: "POST", body: JSON.stringify({ keyword: r.value, label: l.value || "Lohn",
    expected: Number(b.dataset.amt), day: Number(b.dataset.day) }) });
  toast("Regel angelegt – erwarteter Betrag und Zahltag aus dieser Gutschrift übernommen"); renderIncome();
});
const incForm = $("#incForm");
incForm.recurring.onchange = () => ($(".endDate").hidden = !incForm.recurring.checked);
function resetIncForm() {
  incForm.reset(); incForm.income_id.value = ""; $(".endDate").hidden = true;
  incForm.date.value = S.month + "-25"; $("#incFormTitle").textContent = "Einnahme erfassen"; $("#incCancel").hidden = true;
}
$("#incCancel").onclick = resetIncForm;
incForm.onsubmit = async (e) => {
  e.preventDefault();
  const body = {
    description: incForm.description.value, amount: Number(incForm.amount.value), date: incForm.date.value,
    recurring: incForm.recurring.checked, end_date: incForm.recurring.checked && incForm.end_date.value ? incForm.end_date.value : null,
  };
  try {
    const id = incForm.income_id.value;
    await api(id ? `/api/incomes/${id}` : "/api/incomes", { method: id ? "PUT" : "POST", body: JSON.stringify(body) });
    resetIncForm(); toast("Einnahme gespeichert"); renderIncome();
  } catch (err) { toast(err.message, true); }
};
$("#incBody").addEventListener("click", async (e) => {
  const del = e.target.dataset.del, edit = e.target.dataset.edit;
  if (del && confirm("Einnahme löschen?")) { await api(`/api/incomes/${del}`, { method: "DELETE" }); renderIncome(); }
  if (edit) {
    const i = incomes.find((x) => x.id === Number(edit));
    incForm.income_id.value = i.id; incForm.description.value = i.description; incForm.amount.value = i.amount; incForm.date.value = i.date;
    incForm.recurring.checked = !!i.recurring; incForm.end_date.value = i.end_date || ""; $(".endDate").hidden = !i.recurring;
    $("#incFormTitle").textContent = "Einnahme bearbeiten"; $("#incCancel").hidden = false; incForm.description.focus();
  }
});

// ------------------------------------------------------------ Abgleich (Kontoauszüge)
let txs = [];
const hasStatementFilter = () => (S.settings.statement_document_type_ids || []).length || (S.settings.statement_tag_ids || []).length;

async function renderBank() {
  const ready = hasStatementFilter() && S.meta.amount_field;
  $("#bankSetup").hidden = !!ready;
  if (!ready) { $("#bkList").innerHTML = ""; return; }
  const [list, statements, rules, openInv] = await Promise.all([
    api(`/api/transactions?month=${S.month}`), api("/api/statements"), api("/api/rules"), api("/api/open-invoices").catch(() => []),
  ]);
  txs = list;
  drawTx(); drawStatements(statements); drawOpenInvoices(openInv);
  $("#bankRuleSummary").textContent = rules.length ? `${rules.length} Regel(n) aktiv: ${rules.slice(0, 6).map((r) => r.keyword).join(", ")}${rules.length > 6 ? " …" : ""}` : "Noch keine Regeln.";
}

const txKind = (t) => t.status === "ignored" ? "ignored" : t.status === "matched" ? "matched" : t.amount > 0 ? "credit" : t.candidates.length ? "suggest" : "nodoc";

function drawTx() {
  const f = $("#bkFilter").value;
  const kinds = txs.map(txKind);
  const cnt = (k) => kinds.filter((x) => x === k).length;
  const nodoc = txs.filter((t, i) => kinds[i] === "nodoc" || kinds[i] === "suggest");
  $("#bkCount").textContent = txs.length;
  $("#bkCountSub").textContent = `${cnt("credit")} Gutschrift(en) · ${cnt("ignored")} ignoriert`;
  $("#bkMatched").textContent = cnt("matched");
  $("#bkSuggest").textContent = cnt("suggest");
  $("#bkNoDoc").textContent = fmt(nodoc.reduce((a, t) => a - t.amount, 0));
  const uncategorized = nodoc.filter((t) => t.category_tag_id == null).length;
  $("#bkNoDocSub").textContent = `${nodoc.length} Buchungen` + (uncategorized ? ` · ${uncategorized} ohne Kategorie` : "");
  const uniq = txs.filter((t) => t.unique).length;
  $("#bkMatchUnique").disabled = !uniq; $("#bkMatchUnique").textContent = `Eindeutige zuordnen${uniq ? ` (${uniq})` : ""}`;
  const badge = $("#bankBadge"); badge.textContent = uniq; badge.hidden = !uniq;

  const show = txs.filter((t, i) => {
    const k = kinds[i];
    if (f === "all") return true;
    if (f === "todo") return k === "suggest" || (k === "nodoc" && t.category_tag_id == null);
    if (f === "nodoc") return k === "nodoc" || k === "suggest";
    return k === f;
  });
  $("#bkList").innerHTML = show.length ? show.map(txRow).join("") :
    `<div class="empty">${f === "todo" ? (txs.length ? "Alles erledigt für diesen Monat. 🎉" : "Keine Buchungen – Kontoauszug einlesen.") : "Keine Buchungen."}</div>`;
}

function txRow(t) {
  const k = txKind(t);
  const unchecked = t.sign_source === "stichwort" ? `<small title="Vorzeichen nicht über den Saldo geprüft">ungeprüft</small>` : "";
  let act = "";
  if (k === "matched") {
    act = `<span class="pill">✓ <a href="${esc(t.matched_url)}" target="_blank" rel="noopener">${esc(t.matched_title || "Rechnung")}</a></span>
      <button class="linkbtn" data-act="unmatch">lösen</button>`;
  } else if (k === "ignored") {
    act = `<span class="pill grey">ignoriert</span><button class="linkbtn" data-act="unignore">zurückholen</button>`;
  } else if (k === "credit") {
    act = `<span class="pill grey">${t.rent ? "Mietzahlung" : t.income ? "💰 " + esc(t.income) : "Gutschrift"}</span><button class="linkbtn" data-act="ignore">ignorieren</button><button class="linkbtn" data-act="flip">± Vorzeichen</button>`;
  } else {
    if (t.candidates.length) {
      const opts = t.candidates.map((c) => `<option value="${c.id}">${esc(c.title)} · ${fmtDate(c.date)}${c.correspondent ? " · " + esc(corrName(c.correspondent)) : ""}${c.excluded ? " (nicht im Budget)" : ""}</option>`).join("");
      act = `<select class="cand">${opts}</select><button class="primary" data-act="match">Zuordnen</button>`;
      if (t.unique) act = `<span class="pill warn" title="eindeutiger Treffer">Vorschlag</span>` + act;
    }
    act += `<select class="txcat" title="Kategorie (zählt als Ausgabe ohne Beleg)">${categoryOptions(t.category_tag_id).replace("– keine –", "Kategorie …")}</select>
      ${S.vehicles.length ? `<select class="txveh" title="Fahrzeug (zählt zu dessen Kosten)">${vehicleOptions(t.vehicle_id)}</select>` : ""}
      <button class="linkbtn" data-act="alloc" title="Haus / Projekt zuordnen">↗ zuordnen</button>
      <button class="linkbtn" data-act="rule" title="Regel für diesen Händler anlegen">Regel</button>
      <button class="linkbtn" data-act="ignore" title="z.B. Übertrag aufs Sparkonto">ignorieren</button>
      <button class="linkbtn" data-act="flip">±</button>`;
  }
  return `<div class="tx ${k}" data-id="${t.id}">
    <div class="tx-date">${fmtDate(t.date)}</div>
    <div style="min-width:0"><div class="tx-merchant">${esc(t.merchant || t.text.split(" · ")[0])}${t.rent ? ` <span class="allocb">🏠 Miete ${esc(t.rent.tenant_name)} ${esc(t.rent.month)}</span>` : ""}${t.contract ? ` <span class="allocb">${t.contract.kind === "kredit" ? "🏦" : "📄"} ${esc(t.contract.name)}</span>` : ""}${allocBadges({ ...t, amount: t.abs_amount })}</div><div class="tx-text" title="${esc(t.text)}">${esc(t.text)}</div></div>
    <div class="tx-amt ${t.amount > 0 ? "credit" : ""}">${fmt(t.amount)}${unchecked}</div>
    <div class="tx-act">${act}</div>
  </div>`;
}

$("#bkFilter").onchange = drawTx;
$("#bkList").addEventListener("click", async (e) => {
  const act = e.target.dataset.act; if (!act) return;
  const row = e.target.closest(".tx"), id = Number(row.dataset.id), t = txs.find((x) => x.id === id);
  try {
    if (act === "match") {
      e.target.disabled = true;
      await api(`/api/transactions/${id}/match`, { method: "POST", body: JSON.stringify({ doc_id: Number(row.querySelector(".cand").value) }) });
      toast("Zugeordnet – Rechnung in Paperless als bezahlt markiert");
    } else if (act === "unmatch") {
      await api(`/api/transactions/${id}/unmatch`, { method: "POST" }); toast("Zuordnung gelöst");
    } else if (act === "ignore" || act === "unignore") {
      await api(`/api/transactions/${id}`, { method: "PATCH", body: JSON.stringify({ status: act === "ignore" ? "ignored" : "open" }) });
    } else if (act === "flip") {
      await api(`/api/transactions/${id}`, { method: "PATCH", body: JSON.stringify({ flip_sign: true }) });
    } else if (act === "alloc") {
      openAlloc({ kind: "tx", id: t.id, title: t.merchant || t.text, amount: t.abs_amount, allocations: t.allocations || [] }); return;
    } else if (act === "rule") {
      const cat = row.querySelector(".txcat").value, vsel = row.querySelector(".txveh"), vid = vsel ? vsel.value : "";
      if (!cat && !vid) { toast("Erst Kategorie und/oder Fahrzeug wählen, dann „Regel“", true); return; }
      const kw = prompt("Stichwort für die Regel (Händler/Text/Karten-Nr., mehrere mit + verbinden):", (t.merchant || "") + (vid && t.card ? ` + ${t.card}` : ""));
      if (!kw) return;
      const r = await api("/api/rules", { method: "POST", body: JSON.stringify({ keyword: kw, category_tag_id: cat ? Number(cat) : null, vehicle_id: vid ? Number(vid) : null }) });
      await api(`/api/transactions/${id}`, { method: "PATCH", body: JSON.stringify({ category_tag_id: cat ? Number(cat) : null, vehicle_id: vid ? Number(vid) : null }) });
      toast(`Regel angelegt – ${r.applied} weitere Buchung(en) kategorisiert`);
    }
    await renderBank();
  } catch (err) { e.target.disabled = false; toast(err.message, true); }
});
$("#bkList").addEventListener("change", async (e) => {
  if (e.target.classList.contains("txveh")) {
    const id = Number(e.target.closest(".tx").dataset.id);
    try {
      const upd = await api(`/api/transactions/${id}`, { method: "PATCH", body: JSON.stringify({ vehicle_id: e.target.value ? Number(e.target.value) : null }) });
      Object.assign(txs.find((x) => x.id === id), { vehicle_id: upd.vehicle_id }); toast("Fahrzeug gespeichert");
    } catch (err) { toast(err.message, true); }
    return;
  }
  if (!e.target.classList.contains("txcat")) return;
  const id = Number(e.target.closest(".tx").dataset.id);
  try {
    const upd = await api(`/api/transactions/${id}`, { method: "PATCH", body: JSON.stringify({ category_tag_id: e.target.value ? Number(e.target.value) : null }) });
    Object.assign(txs.find((x) => x.id === id), { category_tag_id: upd.category_tag_id });
    drawTx(); toast("Kategorie gespeichert");
  } catch (err) { toast(err.message, true); }
});
$("#bkMatchUnique").onclick = async (e) => {
  e.target.disabled = true;
  try { const r = await api(`/api/transactions/match-unique?month=${S.month}`, { method: "POST" }); toast(`${r.matched} Buchung(en) zugeordnet`); await renderBank(); }
  catch (err) { toast(err.message, true); e.target.disabled = false; }
};

function drawStatements(list) {
  $("#stList").innerHTML = list.length ? list.slice(0, 24).map((s) => `<div class="stitem" data-id="${s.id}">
      <div><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a><div class="muted small">${fmtDate(s.date)} · ${s.parsed_at ? `${s.tx_count} Buchungen eingelesen` : "noch nicht eingelesen"}</div></div>
      <button data-parse="${s.id}" ${s.parsed_at ? "" : 'class="primary"'}>${s.parsed_at ? "Neu einlesen" : "Einlesen"}</button>
    </div>`).join("") : `<div class="empty">Keine Kontoauszüge gefunden (Dokumenttyp/Tag in den Einstellungen prüfen).</div>`;
}
$("#stList").addEventListener("click", async (e) => {
  const id = e.target.dataset.parse; if (!id) return;
  e.target.disabled = true;
  try {
    const r = await api(`/api/statements/${id}/parse`, { method: "POST" });
    toast(`${r.found} Buchungen gelesen, ${r.added} neu` + (r.unchecked ? ` · ${r.unchecked} ungeprüft` : ""), r.found === 0);
    await renderBank();
  } catch (err) { e.target.disabled = false; toast(err.message, true); }
});
$("#bkParseNew").onclick = async (e) => {
  e.target.disabled = true;
  try { const r = await api("/api/statements/parse-new", { method: "POST" }); toast(`${r.parsed} Auszug/Auszüge eingelesen, ${r.added} neue Buchungen`); await renderBank(); }
  catch (err) { toast(err.message, true); }
  e.target.disabled = false;
};

function drawRules(rules) {
  const sel = $("#ruleForm").category;
  sel.innerHTML = `<option value="">Kategorie …</option>` + S.settings.category_tag_ids.map((id) => `<option value="${id}">${esc(tagName(id))}</option>`).join("") + `<option value="ignore">→ ignorieren</option>`;
  const vs = $("#ruleForm").vehicle;
  vs.hidden = !S.vehicles.length; vs.innerHTML = vehicleOptions(null, "Fahrzeug (optional)");
  $("#ruleList").innerHTML = rules.length ? rules.map((r) => `<div class="ruleitem"><span><b>${esc(r.keyword)}</b> → ${r.ignore ? "ignorieren" : [r.category_tag_id ? esc(tagName(r.category_tag_id)) : "", r.vehicle_id ? "🚗 " + esc(vehName(r.vehicle_id)) : ""].filter(Boolean).join(" · ")}</span><button class="linkbtn danger" data-delrule="${r.id}">löschen</button></div>`).join("")
    : `<div class="muted small">Noch keine Regeln.</div>`;
}
$("#ruleForm").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target, cat = f.category.value, vid = f.vehicle.value;
  if (!cat && !vid) { toast("Kategorie oder Fahrzeug wählen", true); return; }
  try {
    const r = await api("/api/rules", { method: "POST", body: JSON.stringify({
      keyword: f.keyword.value, category_tag_id: cat && cat !== "ignore" ? Number(cat) : null, ignore: cat === "ignore", vehicle_id: vid ? Number(vid) : null }) });
    f.reset(); toast(`Regel angelegt – ${r.applied} Buchung(en) angepasst`); drawRules(await api("/api/rules"));
  } catch (err) { toast(err.message, true); }
};
$("#ruleList").addEventListener("click", async (e) => {
  const id = e.target.dataset.delrule; if (!id) return;
  await api(`/api/rules/${id}`, { method: "DELETE" }); drawRules(await api("/api/rules"));
});

let openInvList = [];
function drawOpenInvoices(list) {
  openInvList = list;
  $("#openInv").innerHTML = list.length ? list.map((d) => `<div class="invitem" data-id="${d.id}">
      <span><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.title)}</a> <span class="muted small">· ${fmtDate(d.date)}${d.correspondent ? " · " + esc(corrName(d.correspondent)) : ""}</span>
        <button class="linkbtn" data-skip title="Zählt nicht zum Budget und erscheint nicht mehr als offen">nicht berücksichtigen</button></span>
      <b class="num">${fmt(d.amount)}</b></div>`).join("") : `<div class="empty">Keine offenen Rechnungen.</div>`;
}
$("#openInv").addEventListener("click", async (e) => {
  if (!e.target.hasAttribute("data-skip")) return;
  const d = openInvList.find((x) => x.id === Number(e.target.closest(".invitem").dataset.id));
  try { if (await setBudgetFlag({ ...d, kind: "doc" }, false)) renderBank(); } catch (err) { toast(err.message, true); }
});

// ------------------------------------------------------------ Fahrzeuge
const ENERGIES = ["Benzin", "Diesel", "Elektro", "Hybrid"];
const fmtN = (n, d = 0) => (n == null ? "–" : new Intl.NumberFormat("de-CH", { minimumFractionDigits: d, maximumFractionDigits: d }).format(n));
const fmtKm = (n) => (n == null ? "–" : fmtN(n) + " km");

function vehPeriod() {
  const sel = $("#vehMonths").value;
  if (sel === "ytd") return { until: S.month, months: Number(S.month.slice(5, 7)) };
  return { until: S.month, months: Number(sel) };
}

async function renderVehicles() {
  $("#vehSetup").hidden = S.vehicles.length > 0;
  const { until, months } = vehPeriod();
  const from = shiftMonth(until, -(months - 1));
  $("#vehPeriod").textContent = `${monthLabel(from)} – ${monthLabel(until)}`;
  const [rep, docs, entries, pdocs] = await Promise.all([
    S.vehicles.length ? api(`/api/vehicles/report?until=${until}&months=${months}`) : Promise.resolve({ vehicles: [] }),
    api("/api/fuel-docs"),
    api(`/api/fuel-entries?month_from=${from}&month_to=${until}`),
    api("/api/parking-docs").catch(() => []),
  ]);
  const va = $("#vehAlert");
  va.hidden = !rep.traccar_error && rep.traccar_configured !== false;
  va.textContent = rep.traccar_error ? "Traccar: " + rep.traccar_error : (rep.traccar_configured === false ? "Traccar ist nicht konfiguriert (TRACCAR_URL in .env) – Kosten werden angezeigt, Kilometer und Kennzahlen pro km fehlen." : "");
  Object.keys(S.charts).filter((k) => k.startsWith("veh")).forEach((k) => { S.charts[k].destroy(); delete S.charts[k]; });
  $("#vehList").innerHTML = rep.vehicles.map((v) => vehCard(v, entries.filter((e) => e.resolved_vehicle_id === v.id))).join("");
  rep.vehicles.forEach((v) => drawVehChart(v));
  clearTimeout(vehPoll);
  if (rep.vehicles.some((v) => v.gps_progress && v.gps_progress.running)) {
    vehPoll = setTimeout(() => { if (S.view === "vehicles") renderVehicles().catch(() => {}); }, 15000);
  }
  drawFuelDocs(docs);
  drawParking(pdocs, rep.parking_unassigned || []);
  parkingAuto(pdocs);
  const ff = $("#fuelForm");
  ff.vehicle_id.innerHTML = vehicleOptions(null, "wählen …");
  if (!ff.date.value) ff.date.value = new Date().toISOString().slice(0, 10);
}

let vehPoll = null;
function tcInfo(v) {
  const gp = v.gps_progress;
  const gapH = (v.months || []).reduce((a, m) => a + (m.gps_gap_hours || 0), 0);
  const gaps = v.km_source === "gps" && gapH ? ` · <span class="warn" title="Für diese Zeit liefert Traccar einen Fehler – „km neu laden“ versucht es erneut">${gapH >= 24 ? `${Math.round(gapH / 24 * 10) / 10} Tag(e)` : `${gapH} Std.`} ohne GPS-Daten</span>` : "";
  const gps = v.km_source === "gps" ? (gp ? (gp.running
      ? ` · <span class="warn">km aus GPS werden berechnet (${gp.done} von ${gp.total} Tagen${gp.current ? `, gerade ${fmtDate(gp.current)}` : ""})${gp.error ? " – letzter Fehler: " + esc(gp.error) : ""}</span>`
      : ` · <span class="neg">km aus GPS: Berechnung gestoppt (${gp.done} von ${gp.total} Tagen)${gp.error ? " – " + esc(gp.error) : ""} – neuer Versuch in 10 Minuten</span>`)
    : " · km aus GPS") + gaps : "";
  return tcInfoBase(v) + gps;
}
function tcInfoBase(v) {
  if (!v.traccar_device_id) return " · kein Traccar-Gerät zugewiesen";
  if (v.traccar_device_missing) return ` · <span class="neg">Traccar-Gerät ${v.traccar_device_id} existiert nicht (mehr) – unter Einstellungen neu wählen</span>`;
  if (!v.traccar_last_update) return v.traccar_device_name ? ` · Traccar: ${esc(v.traccar_device_name)} · noch nie gemeldet` : "";
  const last = new Date(v.traccar_last_update + "Z"), days = Math.floor((Date.now() - last) / 86400000);
  const txt = ` · Traccar: ${esc(v.traccar_device_name || "")} · letzte Meldung ${last.toLocaleDateString("de-CH")}`;
  return days > 7 ? ` · <span class="neg">${txt.slice(3)} (vor ${days} Tagen)</span>` : txt;
}
function vehCard(v, entries) {
  const unit = v.unit;
  const kpi = (label, value, sub = "") => `<div class="kpi"><div class="kpi-label">${label}</div><div class="kpi-value">${value}</div>${sub ? `<div class="kpi-sub">${sub}</div>` : ""}</div>`;
  const items = [
    ...entries.map((e) => ({ date: e.date, title: `${e.description || "Tanken"} · ${fmtN(e.quantity, 2)} ${e.unit}`, amount: e.amount, source: "Tanken", url: e.doc_url, id: e.doc_id ? null : e.id })),
    ...v.items,
  ].sort((a, b) => b.date.localeCompare(a.date)).slice(0, 40);
  return `<div class="card veh">
    <div class="veh-head"><h3>${esc(v.name)}</h3><span class="muted small">${esc(v.energy)}${tcInfo(v)}</span></div>
    <div class="veh-kpis">
      ${kpi("Kosten", fmt(v.cost), `davon Treibstoff ${fmt(v.fuel_total)}${v.parking_cost ? ` · Parkieren ${fmt(v.parking_cost)}` : ""}`)}
      ${kpi("Gefahren", fmtKm(v.km))}
      ${kpi("Kosten / km", v.cost_per_km == null ? "–" : fmt(v.cost_per_km).replace(/(\.\d\d)$/, "$1"), "alle Kosten")}
      ${kpi("Treibstoff / km", v.fuel_per_km == null ? "–" : fmt(v.fuel_per_km))}
      ${kpi("Verbrauch", v.consumption == null ? "–" : `${fmtN(v.consumption, 1)} <small>${unit}/100 km</small>`,
        v.consumption == null ? `${fmtN(v.quantity, 1)} ${unit} getankt` : `aus ${v.consumption_months} Monat(en)${v.consumption_complete ? "" : " · unvollständig"}`)}
      ${kpi(`Preis / ${unit}`, v.price_per_unit == null ? "–" : fmt(v.price_per_unit), "nach Rabatt")}
    </div>
    <div class="veh-body">
      <div class="chartbox"><canvas id="veh${v.id}"></canvas></div>
      <div class="veh-items">${items.length ? items.map((i) => `<div class="invitem">
        <span><span class="src">${i.source}</span>${i.url ? `<a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a>` : esc(i.title)} <span class="muted">· ${fmtDate(i.date)}</span>
        ${i.id ? `<button class="linkbtn danger" data-delfuel="${i.id}">löschen</button>` : ""}</span>
        <b class="num">${fmt(i.amount)}</b></div>`).join("") : `<div class="empty">Noch keine Kosten im Zeitraum.</div>`}</div>
    </div>
  </div>`;
}

function drawVehChart(v) {
  if (!window.Chart) return;
  const muted = css("--muted"), grid = css("--line");
  S.charts["veh" + v.id] = new Chart(document.getElementById("veh" + v.id), {
    data: {
      labels: v.months.map((m) => monthShort(m.month)),
      datasets: [
        { type: "bar", label: "Treibstoff", data: v.months.map((m) => m.fuel_total), backgroundColor: "#e0a82ecc", stack: "c", yAxisID: "y", order: 2 },
        { type: "bar", label: "Belege", data: v.months.map((m) => m.doc_cost), backgroundColor: "#1565c0cc", stack: "c", yAxisID: "y", order: 2 },
        { type: "bar", label: "Bank", data: v.months.map((m) => m.bank_cost), backgroundColor: "#8e8e8ecc", stack: "c", yAxisID: "y", order: 2 },
        ...(v.parking_cost ? [{ type: "bar", label: "Parkieren", data: v.months.map((m) => m.parking_cost), backgroundColor: "#7b5ea7cc", stack: "c", yAxisID: "y", order: 2 }] : []),
        { type: "line", label: "km", data: v.months.map((m) => m.km), borderColor: css("--accent"), backgroundColor: css("--accent"), yAxisID: "km", tension: 0, pointRadius: 3, order: 1, spanGaps: true },
      ],
    },
    options: {
      maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      scales: {
        x: { stacked: true, grid: { display: false }, ticks: { color: muted } },
        y: { stacked: true, grid: { color: grid }, ticks: { color: muted, callback: (x) => fmt(x).replace(/\.00$/, "") } },
        km: { position: "right", beginAtZero: true, grid: { display: false }, ticks: { color: muted, callback: (x) => fmtN(x) + " km" } },
      },
      plugins: {
        legend: { labels: { color: css("--text"), boxWidth: 10 } },
        tooltip: { callbacks: { label: (c) => ` ${c.dataset.label}: ${c.dataset.yAxisID === "km" ? fmtKm(c.raw) : fmt(c.raw)}` } },
      },
    },
  });
}

$("#vehMonths").onchange = () => renderVehicles().catch((e) => showAlert(e.message));
$("#vehRefresh").onclick = async () => {
  await api("/api/traccar/refresh", { method: "POST" }); toast("Kilometer werden neu geladen …");
  renderVehicles().catch((e) => showAlert(e.message));
};
$("#vehList").addEventListener("click", async (e) => {
  const id = e.target.dataset.delfuel; if (!id || !confirm("Tankvorgang löschen?")) return;
  await api(`/api/fuel-entries/${id}`, { method: "DELETE" }); renderVehicles();
});

function drawFuelDocs(list) {
  const configured = (S.settings.fuel_correspondent_ids || []).length || (S.settings.fuel_tag_ids || []).length;
  $("#fuelDocs").innerHTML = !configured
    ? `<div class="muted small">Unter <a href="#" data-goto="settings">Einstellungen → Fahrzeuge</a> den Korrespondenten der Tankkarten-Rechnungen wählen (z.B. Vögtlin-Meyer).</div>`
    : list.length ? list.slice(0, 24).map((d) => `<div class="stitem">
        <div><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.title)}</a><div class="muted small">${fmtDate(d.date)} · ${d.parsed_at ? `${d.entries} Tankvorgänge` : "noch nicht eingelesen"}</div></div>
        <button data-fparse="${d.id}" ${d.parsed_at ? "" : 'class="primary"'}>${d.parsed_at ? "Neu einlesen" : "Einlesen"}</button></div>`).join("")
    : `<div class="empty">Keine Tankkarten-Rechnungen gefunden.</div>`;
}
function drawParking(list, unassigned) {
  const configured = (S.settings.parking_correspondent_ids || []).length || (S.settings.parking_tag_ids || []).length;
  $("#parkDocs").innerHTML = !configured
    ? `<div class="muted small">Unter <a href="#" data-gosettings="vehicles">Einstellungen → Fahrzeuge</a> den Korrespondenten der Parkingpay-Auszüge wählen und bei den Fahrzeugen die Kennzeichen eintragen.</div>`
    : list.length ? list.slice(0, 24).map((d) => `<div class="stitem">
        <div><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.title)}</a><div class="muted small">${fmtDate(d.date)} · ${d.parsed_at ? `${d.entries} Parkvorgänge` : "noch nicht eingelesen"}</div></div>
        <button data-pparse="${d.id}" ${d.parsed_at ? "" : 'class="primary"'}>${d.parsed_at ? "Neu einlesen" : "Einlesen"}</button></div>`).join("")
    : `<div class="empty">Keine Parkingpay-Auszüge gefunden.</div>`;
  $("#parkUnassigned").innerHTML = unassigned.length ? unassigned.map((e) => `<div class="invitem" data-pe="${e.id}">
      <span>${fmtDate(e.date)}${e.time ? " " + esc(e.time) : ""} · <b>${esc(e.plate || "ohne Kennzeichen")}</b>${e.driver ? ` (${esc(e.driver)})` : ""} <span class="muted small">${esc(e.location || "")}</span></span>
      <span class="row"><b class="num">${fmt(e.amount)}</b><select class="pe-veh">${vehicleOptions(null, "Fahrzeug …")}</select></span></div>`).join("")
    : `<div class="muted small">Alle Parkvorgänge sind einem Fahrzeug zugeordnet.</div>`;
}
let parkAutoDone = false;
async function parkingAuto(pdocs) {
  // einmal pro Seitenaufruf: neue Parkingpay-Auszüge aus Paperless einlesen
  if (parkAutoDone) return; parkAutoDone = true;
  try {
    if (pdocs.some((d) => !d.parsed_at)) {
      const r = await api("/api/parking-docs/parse-new", { method: "POST" });
      if (r.parsed) { parkResultToast(r.results); if (S.view === "vehicles") renderVehicles(); }
    }
  } catch {}
}
function parkResultToast(results) {
  const plates = results.flatMap((r) => r.plates), unknown = plates.filter((p) => !p.vehicle_id);
  const n = results.reduce((a, r) => a + r.found, 0);
  toast(`${n} Parkvorgänge gelesen` + (unknown.length ? ` · ohne Fahrzeug: ${unknown.map((p) => (p.plate || "?") + (p.driver ? ` (${p.driver})` : "")).join(", ")}` : "")
    + (n === 0 ? " – Format nicht erkannt? Bitte einen Auszug als Beispiel schicken." : ""), unknown.length > 0 || n === 0);
}
$("#parkDocs").addEventListener("click", async (e) => {
  const id = e.target.dataset.pparse; if (!id) return;
  e.target.disabled = true;
  try { parkResultToast([await api(`/api/parking-docs/${id}/parse`, { method: "POST" })]); await renderVehicles(); }
  catch (err) { e.target.disabled = false; toast(err.message, true); }
});
$("#parkParseNew").onclick = async (e) => {
  e.target.disabled = true;
  try { const r = await api("/api/parking-docs/parse-new", { method: "POST" }); r.parsed ? parkResultToast(r.results) : toast("Keine neuen Auszüge"); await renderVehicles(); }
  catch (err) { toast(err.message, true); }
  e.target.disabled = false;
};
$("#parkUnassigned").addEventListener("change", async (e) => {
  if (!e.target.classList.contains("pe-veh") || !e.target.value) return;
  const id = e.target.closest("[data-pe]").dataset.pe;
  try { await api(`/api/parking-entries/${id}`, { method: "PUT", body: JSON.stringify({ vehicle_id: Number(e.target.value) }) }); toast("Zugeordnet"); renderVehicles(); }
  catch (err) { toast(err.message, true); }
});
function fuelResultToast(results) {
  const cards = results.flatMap((r) => r.cards);
  const unknown = cards.filter((c) => !c.vehicle_id);
  const n = results.reduce((a, r) => a + r.found, 0);
  toast(`${n} Tankvorgänge gelesen` + (unknown.length ? ` · Karte(n) ohne Fahrzeug: ${unknown.map((c) => "…" + c.card.slice(-7)).join(", ")}` : ""), unknown.length > 0);
}
$("#fuelDocs").addEventListener("click", async (e) => {
  const id = e.target.dataset.fparse; if (!id) return;
  e.target.disabled = true;
  try { fuelResultToast([await api(`/api/fuel-docs/${id}/parse`, { method: "POST" })]); await renderVehicles(); }
  catch (err) { e.target.disabled = false; toast(err.message, true); }
});
$("#fuelParseNew").onclick = async (e) => {
  e.target.disabled = true;
  try { const r = await api("/api/fuel-docs/parse-new", { method: "POST" }); r.parsed ? fuelResultToast(r.results) : toast("Keine neuen Rechnungen"); await renderVehicles(); }
  catch (err) { toast(err.message, true); }
  e.target.disabled = false;
};
$("#fuelForm").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target, v = S.vehicles.find((x) => x.id === Number(f.vehicle_id.value));
  try {
    await api("/api/fuel-entries", { method: "POST", body: JSON.stringify({
      vehicle_id: Number(f.vehicle_id.value), date: f.date.value, quantity: Number(f.quantity.value), amount: Number(f.amount.value),
      unit: v?.energy === "Elektro" ? "kWh" : "L", description: f.description.value || null }) });
    f.quantity.value = ""; f.amount.value = ""; f.description.value = ""; toast("Tankvorgang gespeichert"); renderVehicles();
  } catch (err) { toast(err.message, true); }
};

// --- Verwaltung (Einstellungen)
// ------------------------------------------------------------ Start
(async function init() {
  $("#monthLabel").textContent = monthLabel(S.month);
  try { S.me = await api("/api/me"); } catch { S.me = null; }
  applyPermissions();
  document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== "view-" + S.view));
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === S.view));
  $("#roBanner").hidden = !(VIEW_AREA[S.view] && !canEdit(VIEW_AREA[S.view]));
  try {
    [S.meta, S.settings, S.budgets, S.vehicles] = await Promise.all([api("/api/meta"), api("/api/settings"), api("/api/budgets"), api("/api/vehicles")]);
  } catch (e) { showAlert("Verbindung zu Paperless fehlgeschlagen: " + e.message); return; }
  render();
})();
