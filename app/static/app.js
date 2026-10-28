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
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

const fmt = (n) => new Intl.NumberFormat("de-CH", { style: "currency", currency: S.meta?.currency || "CHF" }).format(n || 0);
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

// ------------------------------------------------------------ Navigation
function setView(view) {
  S.view = view;
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== "view-" + view));
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
    if (S.view === "settings") renderSettings();
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
  $("#kpiDocs").textContent = `${sum.document_count} Beleg${sum.document_count === 1 ? "" : "e"}` + (sum.bank_count ? ` · ${sum.bank_count} Bankbuchung${sum.bank_count === 1 ? "" : "en"} ohne Beleg` : "");
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
  expDocs = await api(`/api/expenses?month=${S.month}`);
  const f = $("#expFilter"), prev = f.value;
  f.innerHTML = `<option value="all">Alle Kategorien</option><option value="none">Unkategorisiert</option>` +
    S.settings.category_tag_ids.map((id) => `<option value="${id}">${esc(tagName(id))}</option>`).join("");
  f.value = [...f.options].some((o) => o.value === prev) ? prev : "all";
  drawExpenses();
}
$("#expFilter").onchange = drawExpenses;

function drawExpenses() {
  const f = $("#expFilter").value;
  const list = expDocs.filter((d) => f === "all" || (f === "none" ? d.category == null : d.category === Number(f)));
  $("#expBody").innerHTML = list.length ? list.map((d) => `<tr data-id="${d.id}">
      <td>${fmtDate(d.date)}</td>
      <td><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.title)}</a></td>
      <td>${esc(corrName(d.correspondent))}</td>
      <td><select class="cat">${categoryOptions(d.category)}</select></td>
      <td class="num"><input class="amount" type="number" step="0.05" value="${d.amount}"></td>
    </tr>`).join("") : `<tr><td colspan="5" class="empty">Keine Ausgaben.</td></tr>`;
  $("#expTotal").textContent = fmt(list.reduce((a, d) => a + d.amount, 0));
}

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
  list.innerHTML = docs.length ? docs.map((d) => `<div class="rv" data-id="${d.id}">
      <div>
        <div class="rv-title"><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.title)}</a></div>
        <div class="rv-meta">${fmtDate(d.date)}${d.correspondent ? " · " + esc(corrName(d.correspondent)) : ""}${d.document_type ? " · " + esc(typeName(d.document_type)) : ""}</div>
      </div>
      <div class="rv-actions">
        <select class="cat">${categoryOptions(d.category)}</select>
        <input class="amount" type="number" step="0.05" placeholder="Betrag">
        <button class="primary save">Speichern</button>
      </div>
      ${d.content ? `<div class="rv-content">${esc(d.content)}</div>` : ""}
    </div>`).join("") : `<div class="empty">Alles erfasst – keine Dokumente ohne Betrag. 🎉</div>`;
}

$("#reviewList").addEventListener("click", async (e) => {
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
  $("#incMonth").innerHTML = sum ? (sum.incomes.length
    ? `<table class="table"><tbody>${sum.incomes.map((i) => `<tr><td>${esc(i.description)}</td><td class="num">${fmt(i.amount)}</td></tr>`).join("")}</tbody>
       <tfoot><tr><td>Total ${esc(monthLabel(S.month))}</td><td class="num">${fmt(sum.total_income)}</td></tr></tfoot></table>`
    : `<div class="empty">Keine Einnahmen in diesem Monat.</div>`) : `<div class="muted small">Monatsübersicht nicht verfügbar.</div>`;
}

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
  drawTx(); drawStatements(statements); drawRules(rules); drawOpenInvoices(openInv);
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
    act = `<span class="pill grey">Gutschrift</span><button class="linkbtn" data-act="ignore">ignorieren</button><button class="linkbtn" data-act="flip">± Vorzeichen</button>`;
  } else {
    if (t.candidates.length) {
      const opts = t.candidates.map((c) => `<option value="${c.id}">${esc(c.title)} · ${fmtDate(c.date)}${c.correspondent ? " · " + esc(corrName(c.correspondent)) : ""}</option>`).join("");
      act = `<select class="cand">${opts}</select><button class="primary" data-act="match">Zuordnen</button>`;
      if (t.unique) act = `<span class="pill warn" title="eindeutiger Treffer">Vorschlag</span>` + act;
    }
    act += `<select class="txcat" title="Kategorie (zählt als Ausgabe ohne Beleg)">${categoryOptions(t.category_tag_id).replace("– keine –", "Kategorie …")}</select>
      ${S.vehicles.length ? `<select class="txveh" title="Fahrzeug (zählt zu dessen Kosten)">${vehicleOptions(t.vehicle_id)}</select>` : ""}
      <button class="linkbtn" data-act="rule" title="Regel für diesen Händler anlegen">Regel</button>
      <button class="linkbtn" data-act="ignore" title="z.B. Übertrag aufs Sparkonto">ignorieren</button>
      <button class="linkbtn" data-act="flip">±</button>`;
  }
  return `<div class="tx ${k}" data-id="${t.id}">
    <div class="tx-date">${fmtDate(t.date)}</div>
    <div style="min-width:0"><div class="tx-merchant">${esc(t.merchant || t.text.split(" · ")[0])}</div><div class="tx-text" title="${esc(t.text)}">${esc(t.text)}</div></div>
    <div class="tx-amt ${t.amount > 0 ? "credit" : ""}">${t.amount < 0 ? "− " + fmt(-t.amount) : fmt(t.amount)}${unchecked}</div>
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
    f.reset(); toast(`Regel angelegt – ${r.applied} Buchung(en) angepasst`); await renderBank();
  } catch (err) { toast(err.message, true); }
};
$("#ruleList").addEventListener("click", async (e) => {
  const id = e.target.dataset.delrule; if (!id) return;
  await api(`/api/rules/${id}`, { method: "DELETE" }); renderBank();
});

function drawOpenInvoices(list) {
  $("#openInv").innerHTML = list.length ? list.map((d) => `<div class="invitem">
      <span><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.title)}</a> <span class="muted small">· ${fmtDate(d.date)}${d.correspondent ? " · " + esc(corrName(d.correspondent)) : ""}</span></span>
      <b class="num">${fmt(d.amount)}</b></div>`).join("") : `<div class="empty">Keine offenen Rechnungen.</div>`;
}

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
  const [rep, docs, entries] = await Promise.all([
    S.vehicles.length ? api(`/api/vehicles/report?until=${until}&months=${months}`) : Promise.resolve({ vehicles: [] }),
    api("/api/fuel-docs"),
    api(`/api/fuel-entries?month_from=${from}&month_to=${until}`),
  ]);
  const va = $("#vehAlert");
  va.hidden = !rep.traccar_error && rep.traccar_configured !== false;
  va.textContent = rep.traccar_error ? "Traccar: " + rep.traccar_error : (rep.traccar_configured === false ? "Traccar ist nicht konfiguriert (TRACCAR_URL in .env) – Kosten werden angezeigt, Kilometer und Kennzahlen pro km fehlen." : "");
  Object.keys(S.charts).filter((k) => k.startsWith("veh")).forEach((k) => { S.charts[k].destroy(); delete S.charts[k]; });
  $("#vehList").innerHTML = rep.vehicles.map((v) => vehCard(v, entries.filter((e) => e.resolved_vehicle_id === v.id))).join("");
  rep.vehicles.forEach((v) => drawVehChart(v));
  drawFuelDocs(docs);
  const ff = $("#fuelForm");
  ff.vehicle_id.innerHTML = vehicleOptions(null, "wählen …");
  if (!ff.date.value) ff.date.value = new Date().toISOString().slice(0, 10);
}

function vehCard(v, entries) {
  const unit = v.unit;
  const kpi = (label, value, sub = "") => `<div class="kpi"><div class="kpi-label">${label}</div><div class="kpi-value">${value}</div>${sub ? `<div class="kpi-sub">${sub}</div>` : ""}</div>`;
  const items = [
    ...entries.map((e) => ({ date: e.date, title: `${e.description || "Tanken"} · ${fmtN(e.quantity, 2)} ${e.unit}`, amount: e.amount, source: "Tanken", url: e.doc_url, id: e.doc_id ? null : e.id })),
    ...v.items,
  ].sort((a, b) => b.date.localeCompare(a.date)).slice(0, 40);
  return `<div class="card veh">
    <div class="veh-head"><h3>${esc(v.name)}</h3><span class="muted small">${esc(v.energy)}${v.traccar_device_id ? "" : " · kein Traccar-Gerät"}</span></div>
    <div class="veh-kpis">
      ${kpi("Kosten", fmt(v.cost), `davon Treibstoff ${fmt(v.fuel_total)}`)}
      ${kpi("Gefahren", fmtKm(v.km))}
      ${kpi("Kosten / km", v.cost_per_km == null ? "–" : fmt(v.cost_per_km).replace(/(\.\d\d)$/, "$1"), "alle Kosten")}
      ${kpi("Treibstoff / km", v.fuel_per_km == null ? "–" : fmt(v.fuel_per_km))}
      ${kpi("Verbrauch", v.consumption == null ? "–" : `${fmtN(v.consumption, 1)} ${unit}/100 km`,
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
        { type: "line", label: "km", data: v.months.map((m) => m.km), borderColor: css("--accent"), backgroundColor: css("--accent"), yAxisID: "km", tension: 0, pointRadius: 3, order: 1, spanGaps: true },
      ],
    },
    options: {
      maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      scales: {
        x: { stacked: true, grid: { display: false }, ticks: { color: muted } },
        y: { stacked: true, grid: { color: grid }, ticks: { color: muted, callback: (x) => fmt(x).replace(/\.00$/, "") } },
        km: { position: "right", grid: { display: false }, ticks: { color: muted, callback: (x) => fmtN(x) + " km" } },
      },
      plugins: {
        legend: { labels: { color: css("--text"), boxWidth: 10 } },
        tooltip: { callbacks: { label: (c) => ` ${c.dataset.label}: ${c.dataset.yAxisID === "km" ? fmtKm(c.raw) : fmt(c.raw)}` } },
      },
    },
  });
}

$("#vehMonths").onchange = () => renderVehicles().catch((e) => showAlert(e.message));
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
async function loadTraccarDevices() {
  if (S.traccarDevices !== null) return;
  try { S.traccarDevices = await api("/api/traccar/devices"); } catch { S.traccarDevices = []; }
  renderVehicleSettings();
}
function renderVehicleSettings() {
  loadTraccarDevices();
  const devs = S.traccarDevices || [];
  const tagOpts = (sel) => `<option value="">– kein Tag –</option>` + S.meta.tags.map((t) => `<option value="${t.id}" ${t.id === sel ? "selected" : ""}>${esc(t.name)}</option>`).join("");
  const devOpts = (sel) => `<option value="">– kein Gerät –</option>` + devs.map((d) => `<option value="${d.id}" ${d.id === sel ? "selected" : ""}>${esc(d.name)}</option>`).join("")
    + (sel && !devs.some((d) => d.id === sel) ? `<option value="${sel}" selected>Gerät ${sel}</option>` : "");
  const row = (v) => `<div class="vs-row" data-id="${v.id ?? ""}">
      <input name="name" value="${esc(v.name || "")}" placeholder="Name, z.B. Golf">
      <select name="energy">${ENERGIES.map((x) => `<option ${x === (v.energy || "Benzin") ? "selected" : ""}>${x}</option>`).join("")}</select>
      <select name="traccar">${devOpts(v.traccar_device_id)}</select>
      <select name="tag">${tagOpts(v.tag_id)}</select>
      <input name="cards" value="${esc(v.card_keys || "")}" placeholder="Tankkarten-Nr., z.B. 1318100">
      <button class="primary" data-vsave>Speichern</button>
      ${v.id ? `<button class="linkbtn danger" data-vdel>löschen</button>` : `<button class="linkbtn" data-vcancel>abbrechen</button>`}
    </div>`;
  const head = `<div class="vs-row vs-head"><span>Name</span><span>Antrieb</span><span>Traccar-Gerät${devs.length ? "" : " (nicht erreichbar)"}</span><span>Paperless-Tag</span><span>Tankkarte(n)</span><span></span><span></span></div>`;
  $("#vehSettings").innerHTML = head + (S.vehicles.map(row).join("") || "") + (S.newVehicle ? row({}) : "") +
    (!S.vehicles.length && !S.newVehicle ? `<div class="empty">Noch kein Fahrzeug.</div>` : "");
}
$("#vehAdd").onclick = () => { S.newVehicle = true; renderVehicleSettings(); };
$("#vehSettings").addEventListener("click", async (e) => {
  const r = e.target.closest(".vs-row"); if (!r) return;
  const id = r.dataset.id;
  try {
    if (e.target.hasAttribute("data-vcancel")) { S.newVehicle = false; renderVehicleSettings(); return; }
    if (e.target.hasAttribute("data-vdel")) {
      if (!confirm("Fahrzeug löschen? Zuordnungen von Buchungen und Regeln werden entfernt.")) return;
      await api(`/api/vehicles/${id}`, { method: "DELETE" });
    } else if (e.target.hasAttribute("data-vsave")) {
      const q = (n) => r.querySelector(`[name=${n}]`).value;
      if (!q("name").trim()) { toast("Name angeben", true); return; }
      const body = { name: q("name"), energy: q("energy"), traccar_device_id: q("traccar") ? Number(q("traccar")) : null,
        tag_id: q("tag") ? Number(q("tag")) : null, card_keys: q("cards") || null, active: true };
      await api(id ? `/api/vehicles/${id}` : "/api/vehicles", { method: id ? "PUT" : "POST", body: JSON.stringify(body) });
      S.newVehicle = false;
    } else return;
    S.vehicles = await api("/api/vehicles"); renderVehicleSettings(); toast("Fahrzeuge gespeichert");
  } catch (err) { toast(err.message, true); }
});

// ------------------------------------------------------------ Einstellungen
let draft = null;
function renderSettings() {
  if (!draft) draft = {
    cats: [...S.settings.category_tag_ids],
    budgets: { ...S.budgets },
    types: new Set(S.settings.review_document_type_ids),
    tags: new Set(S.settings.review_tag_ids),
    stTypes: new Set(S.settings.statement_document_type_ids || []),
    stTags: new Set(S.settings.statement_tag_ids || []),
    window: S.settings.match_window_days || 60,
    fuelCorr: new Set(S.settings.fuel_correspondent_ids || []),
    fuelTags: new Set(S.settings.fuel_tag_ids || []),
  };
  const q = $("#tagSearch").value.trim().toLowerCase();
  const match = (t) => !q || t.name.toLowerCase().includes(q);
  const on = draft.cats.map(tag).filter(Boolean).filter(match);
  const off = S.meta.tags.filter((t) => !draft.cats.includes(t.id) && match(t)).sort((a, b) => a.name.localeCompare(b.name, "de"));
  const row = (t, active, i) => `<div class="cs-row ${active ? "" : "off"}" data-id="${t.id}">
      <input type="checkbox" class="cs-on" ${active ? "checked" : ""}>
      <span class="cs-name"><span class="dot" style="background:${esc(t.color)}"></span>${esc(t.name)}</span>
      <span class="cs-budget"><input type="number" step="10" min="0" placeholder="Budget/Monat" value="${draft.budgets[t.id] ?? ""}"></span>
      <span class="cs-move"><button data-mv="-1" ${i === 0 ? "disabled" : ""}>↑</button><button data-mv="1" ${i === on.length - 1 ? "disabled" : ""}>↓</button></span>
    </div>`;
  $("#catSettings").innerHTML =
    (on.length ? on.map((t, i) => row(t, true, i)).join("") : `<div class="empty">Noch keine Kategorie gewählt.</div>`) +
    (off.length ? `<div class="cs-sep">Weitere Tags</div>` + off.map((t) => row(t, false, -1)).join("") : "");

  const chips = (items, set, attr) => items.map((x) => `<label class="chip ${set.has(x.id) ? "on" : ""}"><input type="checkbox" data-${attr}="${x.id}" ${set.has(x.id) ? "checked" : ""}>${esc(x.name)}</label>`).join("") || `<span class="muted small">keine vorhanden</span>`;
  $("#reviewTypes").innerHTML = chips(S.meta.document_types, draft.types, "type");
  $("#reviewTags").innerHTML = chips(S.meta.tags, draft.tags, "tag");
  $("#stTypes").innerHTML = chips(S.meta.document_types, draft.stTypes, "sttype");
  $("#stTags").innerHTML = chips(S.meta.tags, draft.stTags, "sttag");
  $("#matchWindow").value = draft.window;
  $("#paidTagName").textContent = S.meta.paid_tag_name;
  $("#paidFieldName").textContent = S.meta.paid_field_name;
  $("#fuelCorr").innerHTML = chips(S.meta.correspondents, draft.fuelCorr, "fcorr");
  $("#fuelTags").innerHTML = chips(S.meta.tags, draft.fuelTags, "ftag");
  renderVehicleSettings();
}
$("#matchWindow").onchange = (e) => (draft.window = Number(e.target.value) || 60);
$("#tagSearch").oninput = renderSettings;

$("#catSettings").addEventListener("change", (e) => {
  const r = e.target.closest(".cs-row"); if (!r) return; const id = Number(r.dataset.id);
  if (e.target.classList.contains("cs-on")) {
    draft.cats = e.target.checked ? [...draft.cats, id] : draft.cats.filter((x) => x !== id);
    renderSettings();
  } else if (e.target.type === "number") {
    draft.budgets[id] = e.target.value === "" ? null : Number(e.target.value);
  }
});
$("#catSettings").addEventListener("click", (e) => {
  const mv = e.target.dataset.mv; if (!mv) return;
  const id = Number(e.target.closest(".cs-row").dataset.id), i = draft.cats.indexOf(id), j = i + Number(mv);
  if (j < 0 || j >= draft.cats.length) return;
  [draft.cats[i], draft.cats[j]] = [draft.cats[j], draft.cats[i]]; renderSettings();
});
document.querySelector("#view-settings").addEventListener("change", (e) => {
  const d = e.target.dataset;
  const key = d.type ? "types" : d.tag ? "tags" : d.sttype ? "stTypes" : d.sttag ? "stTags" : d.fcorr ? "fuelCorr" : d.ftag ? "fuelTags" : null; if (!key) return;
  const set = draft[key], id = Number(d.type || d.tag || d.sttype || d.sttag || d.fcorr || d.ftag);
  e.target.checked ? set.add(id) : set.delete(id);
  e.target.closest(".chip").classList.toggle("on", e.target.checked);
});
$("#saveSettings").onclick = async () => {
  try {
    S.settings = await api("/api/settings", { method: "PUT", body: JSON.stringify({
      category_tag_ids: draft.cats, review_document_type_ids: [...draft.types], review_tag_ids: [...draft.tags],
      statement_document_type_ids: [...draft.stTypes], statement_tag_ids: [...draft.stTags], match_window_days: draft.window,
      fuel_correspondent_ids: [...draft.fuelCorr], fuel_tag_ids: [...draft.fuelTags],
    }) });
    const budgets = {};
    for (const [id, v] of Object.entries(draft.budgets)) budgets[id] = v;
    for (const id of Object.keys(S.budgets)) if (!(id in budgets)) budgets[id] = null;
    S.budgets = await api("/api/budgets", { method: "PUT", body: JSON.stringify({ budgets }) });
    draft = null; renderSettings(); toast("Einstellungen gespeichert");
  } catch (err) { toast(err.message, true); }
};

// ------------------------------------------------------------ Start
(async function init() {
  $("#monthLabel").textContent = monthLabel(S.month);
  try {
    [S.meta, S.settings, S.budgets, S.vehicles] = await Promise.all([api("/api/meta"), api("/api/settings"), api("/api/budgets"), api("/api/vehicles")]);
  } catch (e) { showAlert("Verbindung zu Paperless fehlgeschlagen: " + e.message); return; }
  render();
})();
