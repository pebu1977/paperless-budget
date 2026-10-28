"use strict";
/* Immobilien (Häuser, Wohnungen, Nebenkosten, Mieten) und Projekte – nutzt Helfer aus app.js */

const RE = { props: [], projects: [], pid: null, year: null, sub: "overview", costs: null, prId: null };
const MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"];
const reProp = () => RE.props.find((p) => p.id === RE.pid);
const num = (v) => (v === "" || v == null ? null : Number(v));
const fmt2 = (n) => (n == null ? "" : Number(n).toFixed(2));

async function loadRE() {
  [RE.props, RE.projects] = await Promise.all([api("/api/properties-full"), api("/api/re/projects")]);
  if (!RE.props.some((p) => p.id === RE.pid)) RE.pid = RE.props[0]?.id ?? null;
}

// ------------------------------------------------------------ generische Tabelle mit Inline-Bearbeitung
function etable(ent, rows, cols, { add = true, del = true, foot = "" } = {}) {
  const cell = (r, c) => {
    const v = r[c.f];
    const a = `data-ent="${ent}" data-id="${r.id}" data-f="${c.f}"`;
    if (c.type === "check") return `<input type="checkbox" ${a} ${v ? "checked" : ""}>`;
    if (c.type === "select") return `<select ${a}>${c.options(r).map(([k, l]) => `<option value="${k ?? ""}" ${String(k ?? "") === String(v ?? "") ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
    if (c.type === "static") return c.render(r);
    return `<input ${a} type="${c.type || "text"}" ${c.type === "number" ? 'step="any"' : ""} value="${esc(v ?? "")}" placeholder="${esc(c.ph || "")}">`;
  };
  return `<table class="etable"><thead><tr>${cols.map((c) => `<th style="${c.w ? `width:${c.w}` : ""}" class="${c.type === "number" ? "num" : ""}">${esc(c.label)}</th>`).join("")}${del ? "<th></th>" : ""}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${cols.map((c) => `<td>${cell(r, c)}</td>`).join("")}${del ? `<td><button class="linkbtn danger" data-del="${ent}" data-id="${r.id}">löschen</button></td>` : ""}</tr>`).join("")
      || `<tr><td colspan="${cols.length + 1}" class="muted small">– noch keine Einträge –</td></tr>`}</tbody>
    ${foot}</table>${add ? `<button class="linkbtn" data-add="${ent}">+ hinzufügen</button>` : ""}`;
}

async function reSaveField(el) {
  const { ent, id, f } = el.dataset;
  let v = el.type === "checkbox" ? el.checked : el.value;
  if (el.type === "number") v = num(v);
  if (el.tagName === "SELECT" && /(_id|key_id)$/.test(f)) v = v === "" ? null : Number(v);
  try { await api(`/api/re/${ent}/${id}`, { method: "PUT", body: JSON.stringify({ [f]: v }) }); toast("Gespeichert"); }
  catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------ Immobilien: Rahmen
function periodLabel(p, y) {
  const sm = p.start_month || 1;
  const s = new Date(y, sm - 1, 1), e = new Date(y + (sm > 1 ? 1 : 1), sm - 1, 0);
  return sm === 1 ? `${y}` : `${y}/${String(y + 1).slice(2)} (${s.toLocaleDateString("de-CH")} – ${e.toLocaleDateString("de-CH")})`;
}
function defaultYear(p) {
  const now = new Date(), sm = p.start_month || 1;
  let y = now.getFullYear() - (now.getMonth() + 1 < sm ? 1 : 0);
  return y - 1; // zuletzt abgeschlossene Periode
}

async function renderRE() {
  await loadRE();
  const p = reProp();
  $("#reProp").innerHTML = RE.props.map((x) => `<option value="${x.id}" ${x.id === RE.pid ? "selected" : ""}>${esc(x.name)}</option>`).join("");
  $("#reProp").hidden = !p; $("#reYear").hidden = !p; $("#reTabs").hidden = !p;
  $("#reEmpty").hidden = !!p;
  if (!p) { $("#reBody").innerHTML = ""; return; }
  if (RE.year == null) RE.year = defaultYear(p);
  const ys = []; for (let y = new Date().getFullYear(); y >= new Date().getFullYear() - 6; y--) ys.push(y);
  $("#reYear").innerHTML = ys.map((y) => `<option value="${y}" ${y === RE.year ? "selected" : ""}>${esc(periodLabel(p, y))}</option>`).join("");
  document.querySelectorAll("#reTabs button").forEach((b) => b.classList.toggle("active", b.dataset.sub === RE.sub));
  const f = { overview: reOverview, costs: reCosts, rent: reRent, statement: reStatement, setup: reSetup }[RE.sub];
  $("#reBody").innerHTML = `<div class="empty">Lade …</div>`;
  await f(p);
}
$("#reProp").onchange = (e) => { RE.pid = Number(e.target.value); RE.year = null; renderRE(); };
$("#reYear").onchange = (e) => { RE.year = Number(e.target.value); renderRE(); };
$("#reTabs").addEventListener("click", (e) => { const s = e.target.dataset.sub; if (s) { RE.sub = s; renderRE(); } });
$("#reAddProp").onclick = async () => {
  const r = await openDialog({ title: "Neues Haus", label: "Name / Adresse", value: "", ok: "Anlegen" });
  if (!r || !r.value) return;
  const p = await api("/api/re/properties", { method: "POST", body: JSON.stringify({ name: r.value, address: r.value, start_month: 1, room_pct: 70 }) });
  RE.pid = p.id; RE.sub = "setup"; RE.year = null; renderRE();
};

// ------------------------------------------------------------ Übersicht
async function reOverview(p) {
  if (!p.units.length) { $("#reBody").innerHTML = `<div class="card hint">Zuerst unter <a href="#" data-resub="setup">Einrichtung</a> Wohnungen erfassen.</div>`; return; }
  const [sum, rent] = await Promise.all([api(`/api/properties/${p.id}/summary?year=${RE.year}`), api(`/api/properties/${p.id}/rent?year=${RE.year}`)]);
  const paid = rent.tenancies.reduce((a, t) => a + t.paid_total, 0), due = rent.tenancies.reduce((a, t) => a + t.due_total, 0);
  const arrears = rent.tenancies.reduce((a, t) => a + Math.max(0, t.arrears), 0);
  $("#reBody").innerHTML = `
    <div class="kpis">
      <div class="kpi"><div class="kpi-label">Umlegbare Kosten</div><div class="kpi-value">${fmt(sum.umlegbar)}</div><div class="kpi-sub">${fmtDate(sum.period[0])} – ${fmtDate(sum.period[1])}</div></div>
      <div class="kpi"><div class="kpi-label">Nicht umlegbar</div><div class="kpi-value">${fmt(sum.nicht_umlegbar)}</div><div class="kpi-sub">Unterhalt, Eigentümerkosten</div></div>
      <div class="kpi"><div class="kpi-label">Mieteingänge</div><div class="kpi-value">${fmt(paid)}</div><div class="kpi-sub">von ${fmt(due)} fällig</div></div>
      <div class="kpi"><div class="kpi-label">Ausstände</div><div class="kpi-value ${arrears > 0.5 ? "neg" : "pos"}">${fmt(arrears)}</div><div class="kpi-sub">bis heute</div></div>
    </div>
    <div class="card"><h3>Verteilung der Nebenkosten</h3>
      <table class="etable"><thead><tr><th>Wohnung</th><th>Art</th><th>Name</th><th class="num">Tage</th><th class="num">Kosten</th><th class="num">Akonto</th><th class="num">Saldo</th><th></th></tr></thead>
      <tbody>${sum.rows.map((r) => `<tr><td>${esc(r.unit)}</td><td>${esc(r.type)}</td><td>${esc(r.name)}</td><td class="num">${r.days}</td>
        <td class="num">${fmt(r.total)}</td><td class="num">${r.type === "Mieter" ? fmt(r.akonto) : ""}</td>
        <td class="num ${r.type === "Mieter" ? (r.saldo > 0 ? "neg" : "pos") : ""}">${r.type === "Mieter" ? fmt(r.saldo) : ""}</td>
        <td>${r.type !== "Leerstand" ? `<button class="linkbtn" data-goto-st="${r.tenancy_id ?? ""}" data-unit="${r.unit_id}">Abrechnung</button>` : ""}</td></tr>`).join("")}</tbody>
      <tfoot><tr><td colspan="4">Total verteilt</td><td class="num">${fmt(sum.verteilt)}</td><td colspan="3"></td></tr></tfoot></table>
      <p class="muted small">Saldo positiv = Nachzahlung der Mieterin/des Mieters. Leerstand trägt der Eigentümer.</p>
    </div>`;
}
$("#reBody").addEventListener("click", (e) => {
  const g = e.target.closest("[data-goto-st]");
  if (g) { RE.stSel = g.dataset.gotoSt ? `t${g.dataset.gotoSt}` : `u${g.dataset.unit}`; RE.sub = "statement"; renderRE(); return; }
  const s = e.target.closest("[data-resub]"); if (s) { e.preventDefault(); RE.sub = s.dataset.resub; renderRE(); }
});

// ------------------------------------------------------------ Kosten
const unitOpts = (p, empty = "verteilt (Schlüssel)") => [["", empty], ...p.units.map((u) => [u.id, `nur ${u.name}`])];
const catOpts = (p, empty = "– nicht umlegbar –") => [["", empty], ...p.categories.map((c) => [c.id, `${c.grp} · ${c.name}`])];

async function reCosts(p) {
  const c = await api(`/api/properties/${p.id}/costs?year=${RE.year}`);
  RE.costs = c;
  const umlegbar = c.lines.filter((l) => l.category_id).reduce((a, l) => a + l.amount, 0);
  const row = (l) => `<tr data-key="${l.key}">
      <td>${fmtDate(l.date)}</td>
      <td><span class="srcb ${l.source}">${l.source}</span></td>
      <td><div class="it-purpose" title="${esc(l.purpose || "")}">${l.url ? `<a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.title)}</a>` : esc(l.title)}</div>
          <div class="it-sub">${esc(l.purpose || "")}${l.implicit ? " · über Tag" : ""}${l.percent != null && l.percent !== 100 && l.alloc_id ? ` · ${l.percent} % von ${fmt(l.full_amount)}` : ""}${l.fixed_amount != null && l.alloc_id ? ` · Teil von ${fmt(l.full_amount)}` : ""}</div></td>
      <td><select class="lc-cat">${catOpts(p).map(([k, t]) => `<option value="${k}" ${String(k) === String(l.category_id ?? "") ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>${l.category_guessed ? `<div class="guess">automatisch erkannt</div>` : ""}</td>
      <td><select class="lc-unit">${unitOpts(p).map(([k, t]) => `<option value="${k}" ${String(k) === String(l.unit_id ?? "") ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></td>
      <td><input class="lc-qty" type="number" step="any" value="${l.quantity ?? ""}" ${l.source === "Fix" ? "disabled" : ""}>${l.quantity_guessed ? `<div class="guess">aus Beleg</div>` : ""}</td>
      <td class="num"><b>${fmt(l.amount)}</b></td>
      <td>${l.alloc_id ? `<button class="linkbtn danger lc-del" title="Zuordnung entfernen">entfernen</button>` : l.source === "Manuell" || l.source === "Fix" ? `<button class="linkbtn danger lc-del">löschen</button>` : ""}</td>
    </tr>`;
  $("#reBody").innerHTML = `<div class="card">
    <div class="cardhead"><h3>Kosten ${esc(periodLabel(p, RE.year))}</h3>
      <div class="row"><button id="lcSearch">+ Beleg / Bankbuchung</button><button id="lcManual">+ Manuelle Kosten</button><button id="lcFixed">+ Fixkosten</button></div></div>
    <p class="muted small">Belege mit dem Haus-Tag${p.tag_id ? ` „${esc(tagName(p.tag_id))}“` : ""} erscheinen automatisch. Kostenart wählen (oder „nicht umlegbar“), Wohnung „verteilt“ = per Schlüssel, „nur …“ = individuell belastet.</p>
    <div class="tablewrap"><table class="etable"><thead><tr><th style="width:84px">Datum</th><th style="width:70px">Quelle</th><th>Beleg / Zweck</th><th style="width:210px">Kostenart</th><th style="width:170px">Wohnung</th><th style="width:90px">Menge</th><th class="num" style="width:100px">Betrag</th><th style="width:70px"></th></tr></thead>
      <tbody>${c.lines.map(row).join("") || `<tr><td colspan="8" class="empty">Keine Kosten in dieser Periode.</td></tr>`}</tbody>
      <tfoot><tr><td colspan="6">Umlegbar ${c.unassigned ? `<span class="muted small">(${c.unassigned} Position(en) nicht umlegbar)</span>` : ""}</td><td class="num">${fmt(umlegbar)}</td><td></td></tr>
             <tr><td colspan="6" class="muted">Total inkl. nicht umlegbar</td><td class="num muted">${fmt(c.total)}</td><td></td></tr></tfoot></table></div>
  </div>`;
  $("#lcSearch").onclick = () => openSearch({ type: "property", id: p.id, name: p.name });
  $("#lcManual").onclick = async () => {
    await api("/api/re/manual-costs", { method: "POST", body: JSON.stringify({ property_id: p.id, category_id: p.categories[0]?.id, date: c.period[1], description: "Manuelle Kosten", amount: 0 }) });
    RE.sub = "setup"; RE.setupFocus = "manual"; renderRE();
  };
  $("#lcFixed").onclick = async () => {
    await api("/api/re/fixed-costs", { method: "POST", body: JSON.stringify({ property_id: p.id, category_id: p.categories[0]?.id, name: "Fixkosten", amount: 0, interval: "jahr" }) });
    RE.sub = "setup"; RE.setupFocus = "fixed"; renderRE();
  };
}

$("#reBody").addEventListener("change", async (e) => {
  const tr = e.target.closest("tr[data-key]");
  if (tr && RE.sub === "costs") {
    const l = RE.costs.lines.find((x) => x.key === tr.dataset.key);
    const field = e.target.classList.contains("lc-cat") ? "category_id" : e.target.classList.contains("lc-unit") ? "unit_id" : e.target.classList.contains("lc-qty") ? "quantity" : null;
    if (!field) return;
    const v = num(e.target.value);
    try {
      if (l.alloc_id) await api(`/api/allocations/${l.alloc_id}`, { method: "PUT", body: JSON.stringify({ [field]: v }) });
      else if (l.source === "Manuell") await api(`/api/re/manual-costs/${l.source_id}`, { method: "PUT", body: JSON.stringify({ [field]: v }) });
      else if (l.source === "Fix") await api(`/api/re/fixed-costs/${l.source_id}`, { method: "PUT", body: JSON.stringify({ [field]: v }) });
      else {  // über Tag (implizit) → explizite Zuordnung anlegen
        const body = { source_type: l.source_type, source_id: l.source_id, target_type: "property", target_id: RE.pid, percent: 100,
          category_id: l.category_id, unit_id: l.unit_id, quantity: l.quantity, [field]: v };
        await api("/api/allocations", { method: "POST", body: JSON.stringify(body) });
      }
      toast("Gespeichert"); renderRE();
    } catch (err) { toast(err.message, true); }
    return;
  }
  if (e.target.dataset.ent) { await reSaveField(e.target); if (["units", "keys"].includes(e.target.dataset.ent) || e.target.dataset.f === "type") renderRE(); return; }
  if (e.target.classList.contains("share")) {
    const k = e.target.dataset.key, inputs = [...document.querySelectorAll(`input.share[data-key="${k}"]`)];
    const shares = Object.fromEntries(inputs.map((i) => [i.dataset.unit, num(i.value)]));
    await api(`/api/re/keys/${k}/shares`, { method: "PUT", body: JSON.stringify({ shares }) });
    const sum = inputs.reduce((a, i) => a + (Number(i.value) || 0), 0);
    const cell = document.querySelector(`[data-sum="${k}"]`); if (cell) { cell.textContent = sum.toFixed(2) + " %"; cell.classList.toggle("warn", Math.abs(sum - 100) > 0.01); }
    toast("Gespeichert");
  }
  if (e.target.id === "pfForm" || e.target.closest("#pfForm")) {
    const el = e.target; let v = el.type === "checkbox" ? el.checked : el.value;
    if (el.type === "number" || /_id$|start_month/.test(el.name)) v = num(v);
    try { await api(`/api/re/properties/${RE.pid}`, { method: "PUT", body: JSON.stringify({ [el.name]: v }) }); toast("Gespeichert"); if (el.name === "name") renderRE(); }
    catch (err) { toast(err.message, true); }
  }
  if (e.target.id === "stPick") { RE.stSel = e.target.value; renderRE(); }
});
$("#reBody").addEventListener("click", async (e) => {
  const del = e.target.closest(".lc-del");
  if (del) {
    const l = RE.costs.lines.find((x) => x.key === del.closest("tr").dataset.key);
    if (!confirm(l.alloc_id ? "Zuordnung entfernen?" : "Position löschen?")) return;
    if (l.alloc_id) await api(`/api/allocations/${l.alloc_id}`, { method: "DELETE" });
    else if (l.source === "Manuell") await api(`/api/re/manual-costs/${l.source_id}`, { method: "DELETE" });
    else if (l.source === "Fix") await api(`/api/re/fixed-costs/${l.source_id}`, { method: "DELETE" });
    renderRE(); return;
  }
  const add = e.target.dataset.add, d = e.target.dataset.del;
  if (add) {
    const p = reProp(), today = new Date().toISOString().slice(0, 10);
    const defaults = {
      units: { property_id: p.id, name: `Wohnung ${p.units.length + 1}`, area: 0 },
      keys: { property_id: p.id, name: "Neuer Schlüssel", type: "percent" },
      categories: { property_id: p.id, grp: "Nebenkosten", name: "Neue Kostenart", key_id: p.keys[0]?.id ?? null, heating: false, sort: 50 },
      tenancies: { unit_id: p.units[0]?.id, tenant_name: "Neue Mieterin / neuer Mieter", start: today, rent: 0, akonto: 0 },
      "fixed-costs": { property_id: p.id, category_id: p.categories[0]?.id, name: "Fixkosten", amount: 0, interval: "jahr" },
      "manual-costs": { property_id: p.id, category_id: p.categories[0]?.id, date: today, description: "Manuelle Kosten", amount: 0 },
      "heating-stock": { property_id: p.id, date: today, liters: 0 },
    }[add];
    if (!defaults) return;
    if ((add === "tenancies" && !p.units.length) || (["fixed-costs", "manual-costs"].includes(add) && !p.categories.length)) {
      toast(add === "tenancies" ? "Zuerst eine Wohnung erfassen" : "Zuerst Kostenarten anlegen", true); return;
    }
    await api(`/api/re/${add}`, { method: "POST", body: JSON.stringify(defaults) }); renderRE(); return;
  }
  if (d) {
    if (!confirm("Wirklich löschen?")) return;
    await api(`/api/re/${d}/${e.target.dataset.id}`, { method: "DELETE" });
    if (d === "properties") { RE.pid = null; RE.year = null; }
    renderRE(); return;
  }
  if (e.target.id === "pfDefaults") {
    try { await api(`/api/properties/${RE.pid}/defaults`, { method: "POST" }); toast("Schlüssel und Kostenarten angelegt"); renderRE(); }
    catch (err) { toast(err.message, true); }
  }
});

// ------------------------------------------------------------ Mieten
async function reRent(p) {
  const r = await api(`/api/properties/${p.id}/rent?year=${RE.year}`);
  const mlabel = (m) => MONTHS[Number(m.slice(5)) - 1].slice(0, 3) + " " + m.slice(2, 4);
  const tenOpts = r.tenancies.map((t) => `<option value="${t.tenancy.id}">${esc(t.tenancy.tenant_name)} (${esc(t.unit_name)})</option>`).join("");
  $("#reBody").innerHTML = `<div class="card"><h3>Mietzinseingänge ${esc(periodLabel(p, RE.year))}</h3>
    <p class="muted small">Gutschriften aus den Kontoauszügen werden über Stichwort und Betrag erkannt (Einrichtung → Mietverhältnis). Zahlungen ab dem 20. gelten für den Folgemonat. Unter dem Betrag steht das Eingangsdatum.</p>
    ${r.tenancies.length ? `<div class="matrix"><table><thead><tr><th style="text-align:left">Mieter</th>${r.months.map((m) => `<th>${mlabel(m)}</th>`).join("")}<th>Total</th><th>Ausstand</th></tr></thead>
    <tbody>${r.tenancies.map((t) => `<tr><td style="text-align:left"><b>${esc(t.tenancy.tenant_name)}</b><br><span class="muted">${esc(t.unit_name)} · ${fmt(t.tenancy.rent + t.tenancy.akonto)}/Mt.</span></td>
      ${t.months.map((m) => { const c = !m.due && !m.paid ? "na" : m.paid >= m.due - 0.01 ? "ok" : m.paid > 0 ? "part" : m.month > new Date().toISOString().slice(0, 7) ? "na" : "miss";
        return `<td class="${c}" title="${m.payments.map((x) => `${fmtDate(x.date)}: ${fmt(x.amount)}`).join("\n")}">${m.paid ? fmt2(m.paid) : m.due ? "–" : ""}${m.payments.length ? `<br><span class="muted small">${m.payments.map((x) => fmtDate(x.date).replace(/\d{4}$/, "")).join(" ")}</span>` : ""}</td>`; }).join("")}
      <td><b>${fmt(t.paid_total)}</b><br><span class="muted">von ${fmt(t.due_total)}</span></td><td class="${t.arrears > 0.5 ? "miss" : "ok"}">${fmt(t.arrears)}</td></tr>`).join("")}</tbody></table></div>`
    : `<div class="empty">Keine Mietverhältnisse in dieser Periode – unter Einrichtung erfassen.</div>`}
  </div>
  ${r.tenancies.map((t) => `<div class="card"><h3>Erkannte Mietzahlungen · ${esc(t.tenancy.tenant_name)}</h3>
    <p class="muted small">Automatisch nur, wenn das Stichwort (${t.tenancy.match_text ? `„${esc(t.tenancy.match_text)}“` : "<b>keins erfasst</b> – unter Einrichtung → Mietverhältnis"}) im Buchungstext steht
      <b>und</b> der Betrag zur Monatsmiete von ${fmt(t.tenancy.rent + t.tenancy.akonto)} passt (oder einem Vielfachen).
      ${(t.tenancy.match_text || "").split(",").length > 2 ? `<br><b>Hinweis:</b> Komma heisst „oder“ – jedes Wort zwischen den Kommas genügt allein. Am besten nur den Nachnamen eintragen (z.B. „${esc(t.tenancy.match_text.split(",")[0].trim())}“).` : ""}</p>
    ${t.bookings.length ? `<table class="tbl small"><thead><tr><th>Eingang</th><th>für Monat (ab)</th><th class="num">Betrag</th><th>Buchungstext</th><th></th><th></th></tr></thead><tbody>
      ${t.bookings.map((b) => `<tr data-tx="${b.tx_id}" data-ten="${t.tenancy.id}"><td>${fmtDate(b.date)}</td>
        <td><select class="rp-month">${r.months.map((m) => `<option value="${m}" ${m === b.month ? "selected" : ""}>${mlabel(m)}</option>`).join("")}</select></td>
        <td class="num">${fmt(b.amount)}${b.n_months > 1 ? `<br><span class="muted small">${b.n_months} Monatsmieten</span>` : ""}</td><td class="muted">${esc(b.text)}</td><td><span class="pill ${b.auto ? "" : "grey"}">${b.auto ? "automatisch" : "von Hand"}</span></td>
        <td><button class="linkbtn danger rp-del" title="Keine Mietzahlung – wird nicht mehr automatisch zugeordnet">entfernen</button></td></tr>`).join("")}
    </tbody></table>` : `<div class="empty">Noch keine Zahlungen erkannt.</div>`}</div>`).join("")}
  <div class="card"><h3>Nicht zugeordnete Gutschriften</h3>
    ${r.unassigned_credits.length ? r.unassigned_credits.map((c) => { const sg = r.tenancies.find((t) => t.tenancy.id === c.suggest);
      return `<div class="invitem ${sg ? "rp-sug" : ""}" data-tx="${c.id}">
      <span>${fmtDate(c.date)} · <b>${esc(c.merchant || c.text.slice(0, 50))}</b> <span class="muted small">${esc(c.text.slice(0, 80))}</span>
        ${c.hint ? `<div class="small" style="color:var(--warn,#b26a00)">${esc(c.hint)}</div>` : ""}</span>
      <span class="row"><b class="num">${fmt(c.amount)}</b>${sg ? `<button class="rp-take" data-ten="${sg.tenancy.id}">Miete ${esc(sg.tenancy.tenant_name)}?</button>` : ""}
        ${tenOpts ? `<select class="rp-ten"><option value="">als Miete von …</option>${tenOpts}</select>` : ""}</span></div>`; }).join("")
    : `<div class="empty">Keine offenen Gutschriften.</div>`}
  </div>`;
  const post = async (tx, ten, month) => { await api("/api/rent-payments", { method: "POST", body: JSON.stringify({ tx_id: Number(tx), tenancy_id: Number(ten), month }) }); };
  document.querySelectorAll(".rp-take").forEach((b) => b.onclick = async () => {
    await post(b.closest("[data-tx]").dataset.tx, b.dataset.ten); toast("Als Mietzahlung erfasst"); renderRE();
  });
  document.querySelectorAll(".rp-month").forEach((s) => s.onchange = async () => {
    const tr = s.closest("tr"); await post(tr.dataset.tx, tr.dataset.ten, s.value); toast("Monat geändert"); renderRE();
  });
  document.querySelectorAll(".rp-del").forEach((b) => b.onclick = async () => {
    await api(`/api/rent-payments/${b.closest("tr").dataset.tx}`, { method: "DELETE" }); toast("Zuordnung entfernt"); renderRE();
  });
  document.querySelectorAll(".rp-ten").forEach((s) => s.onchange = async () => {
    if (!s.value) return;
    await api("/api/rent-payments", { method: "POST", body: JSON.stringify({ tx_id: Number(s.closest("[data-tx]").dataset.tx), tenancy_id: Number(s.value) }) });
    toast("Als Mietzahlung erfasst"); renderRE();
  });
}

// ------------------------------------------------------------ Abrechnung
async function reStatement(p) {
  const opts = [
    ...p.tenancies.map((t) => [`t${t.id}`, `${t.tenant_name} – ${p.units.find((u) => u.id === t.unit_id)?.name || ""} (ab ${fmtDate(t.start)}${t.end ? ` bis ${fmtDate(t.end)}` : ""})`]),
    ...p.units.filter((u) => u.owner_occupied).map((u) => [`u${u.id}`, `Eigenanteil – ${u.name}`]),
  ];
  if (!opts.length) { $("#reBody").innerHTML = `<div class="card hint">Zuerst unter Einrichtung Wohnungen und Mietverhältnisse erfassen.</div>`; return; }
  if (!opts.some(([k]) => k === RE.stSel)) RE.stSel = opts[0][0];
  const q = RE.stSel[0] === "t" ? `tenancy_id=${RE.stSel.slice(1)}` : `unit_id=${RE.stSel.slice(1)}`;
  const ak = RE.akontoOverride != null ? `&akonto=${RE.akontoOverride}` : "";
  const sm = p.start_month || 1, pStart = `${RE.year}-${String(sm).padStart(2, "0")}-01`;
  const pEnd = new Date(Date.UTC(RE.year + 1, sm - 1, 0)).toISOString().slice(0, 10);
  if (RE.stMode === "interim") {
    const ten = RE.stSel[0] === "t" ? p.tenancies.find((t) => t.id === Number(RE.stSel.slice(1))) : null;
    const today = new Date().toISOString().slice(0, 10);
    if (!RE.stFrom || RE.stFrom < pStart || RE.stFrom > pEnd) RE.stFrom = pStart;
    if (!RE.stTo || RE.stTo < RE.stFrom || RE.stTo > pEnd) RE.stTo = ten?.end && ten.end >= pStart && ten.end <= pEnd ? ten.end : (today < pEnd ? today : pEnd);
  }
  const per = RE.stMode === "interim" ? `&period_from=${RE.stFrom}&period_to=${RE.stTo}` : "";
  const [st, hist] = await Promise.all([api(`/api/properties/${p.id}/statement?year=${RE.year}&${q}${ak}${per}`), api(`/api/nk-statements?property_id=${p.id}`)]);
  const h = st.heating, tenant = RE.stSel[0] === "t";
  const tr = (a, b, cls = "") => `<tr class="${cls}"><td>${a}</td><td class="num">${b}</td></tr>`;
  $("#reBody").innerHTML = `<div class="grid2">
    <div class="card pv">
      <div class="cardhead"><h3>Vorschau</h3><select id="stPick">${opts.map(([k, t]) => `<option value="${k}" ${k === RE.stSel ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></div>
      <div class="muted">${st.interim ? "<b>Zwischenabrechnung</b> · " : ""}${esc(st.unit.name)} · ${fmtDate(st.stay[0])} – ${fmtDate(st.stay[1])} · ${st.days} von ${st.days_total} Tagen</div>
      ${st.oil ? `<div class="muted small" style="margin-top:4px">🛢 Heizöl ${st.oil.complete ? `nach Verbrauch: ${fmtN(st.oil.liters)} Liter = ${fmt(st.oil.amount)} (Bestand ${fmtDate(st.oil.start.date)} → ${fmtDate(st.oil.end.date)})`
        : `<span class="neg">nach Einkauf – Tankstand-Ablesung ${!st.oil.start ? "zum Beginn" : "zum Ende"} der Periode fehlt (Einrichtung → Heizöl-Lagerbestand)</span>`}</div>` : ""}
      ${(st.meters || []).map((m) => `<div class="muted small" style="margin-top:4px">⚡ Zähler ${esc(m.name)} (allgemein): ${m.quantity != null
        ? `${fmtN(m.quantity)} ${esc(m.unit)} in der Periode${m.amount != null ? `, erwartet ca. <b>${fmt(m.amount)}</b>` : ""}
           (Stand ${fmtDate(m.start[0].slice(0, 10))} → ${fmtDate(m.end[0].slice(0, 10))}${m.estimated ? ", Ende hochgerechnet" : ""})${m.category ? ` – steht in Klammern bei „${esc(m.category)}“` : " – keine NK-Kategorie zugeordnet (Zähler → Einstellungen)"}, abgerechnet wird nach Rechnungen`
        : esc(m.missing)}</div>`).join("")}
      ${st.interim ? `<div class="muted small" style="margin-top:4px">Es zählen nur Kosten, die im Zeitraum verbucht sind (Jahresrechnungen, z.B. Wasser, fehlen evtl. noch).</div>` : ""}
      <table>
        ${h.total ? tr("Raumheizung", `${fmt(h.room_unit)} <span class="muted small">(${h.room_share_pct} % von ${fmt(h.room_total)})</span>`) + tr("Warmwasser", `${fmt(h.water_unit)} <span class="muted small">(${h.water_share_pct} % von ${fmt(h.water_total)})</span>`) : ""}
        ${st.groups.map((g) => tr(esc(g.name), fmt(g.unit_total))).join("")}
        ${st.rounding ? tr("Rundungsdifferenz", fmt(st.rounding)) : ""}
        ${tr("Total Kosten", fmt(st.total), "tot")}
        ${tenant ? tr("Akonto", "− " + fmtPos(st.akonto)) + tr(st.saldo >= 0 ? "Nachzahlung Mieter" : "Guthaben Mieter", fmt(Math.abs(st.saldo)), "tot") : ""}
      </table>
      ${st.groups.map((g) => `<h4>${esc(g.name)}</h4><table>${g.rows.map((r) => `<tr><td>${esc(r.name)} <span class="muted small">· ${esc(r.key)} ${r.share_pct} %</span></td><td class="num muted">${fmt(r.total)}</td><td class="num">${fmt(r.unit_amount)}</td></tr>`).join("")}</table>`).join("")}
      ${h.total ? `<h4>Heizpool</h4><table>${h.rows.map((r) => `<tr><td>${esc(r.name)} <span class="muted small">${r.quantity ? `· ${fmtN(r.quantity, 0)} ${esc(r.unit_label)}` : ""}</span></td><td class="num">${fmt(r.amount)}</td></tr>`).join("")}<tr class="tot"><td>Total (${h.room_pct} % Raumheizung / ${100 - h.room_pct} % Warmwasser)</td><td class="num">${fmt(h.total)}</td></tr></table>` : ""}
      ${st.unassigned.length ? `<p class="muted small">Nicht umgelegt: ${st.unassigned.map((l) => `${esc(l.title)} (${fmt(l.amount)})`).join(", ")}</p>` : ""}
    </div>
    <div class="card">
      <h3>Erzeugen</h3>
      <div class="formgrid">
        <label>Art<select id="stMode"><option value="year" ${RE.stMode !== "interim" ? "selected" : ""}>Jahresabrechnung</option><option value="interim" ${RE.stMode === "interim" ? "selected" : ""}>Zwischenabrechnung</option></select>
          <span class="muted small">Zwischenabrechnung z.B. bei Auszug während der Periode</span></label>
        ${RE.stMode === "interim" ? `<label>von<input id="stFrom" type="date" min="${pStart}" max="${pEnd}" value="${RE.stFrom}"></label>
          <label>bis (Stichtag)<input id="stTo" type="date" min="${pStart}" max="${pEnd}" value="${RE.stTo}"></label>` : ""}
        ${tenant ? `<label>Akonto CHF<input id="stAkonto" type="number" step="0.05" value="${st.akonto}"><span class="muted small">erhalten ${fmt(st.akonto_received)} · laut Vertrag ${fmt(st.akonto_expected)}</span></label>` : ""}
        <label>Rechnungsdatum<input id="stDate" type="date" value="${new Date().toISOString().slice(0, 10)}"></label>
        <label class="check"><input id="stUpload" type="checkbox" checked> PDF in Paperless ablegen</label>
      </div>
      <div class="row" style="margin-top:12px"><button id="stGen" class="primary">Word & PDF erzeugen</button></div>
      <div id="stResult" style="margin-top:10px"></div>
      <h4 style="margin-top:18px">Bisherige Abrechnungen</h4>
      ${hist.length ? hist.map((x) => `<div class="invitem"><span>${fmtDate(x.created_at.slice(0, 10))} · ${x.interim ? "Zwischenabr. " : ""}${fmtDate(x.period_start)}–${fmtDate(x.period_end)}
        ${x.docx ? `<a href="/api/files/${encodeURIComponent(x.docx)}">Word</a>` : ""} ${x.pdf ? `· <a href="/api/files/${encodeURIComponent(x.pdf)}" target="_blank">PDF</a>` : ""}</span>
        <span class="row"><b class="num">${fmt(x.saldo)}</b>${x.deletable ? `<button class="linkbtn danger" data-nkdel="${x.id}" data-pl="${x.paperless_task ? 1 : 0}" title="Nur die letzte Abrechnung kann gelöscht werden">löschen</button>` : ""}</span></div>`).join("") : `<div class="muted small">Noch keine.</div>`}
    </div></div>`;
  const ai = $("#stAkonto");
  if (ai) ai.onchange = () => { RE.akontoOverride = num(ai.value); renderRE(); };
  $("#stMode").onchange = (e) => { RE.stMode = e.target.value; RE.akontoOverride = null; renderRE(); };
  if ($("#stFrom")) { $("#stFrom").onchange = (e) => { RE.stFrom = e.target.value; RE.akontoOverride = null; renderRE(); };
    $("#stTo").onchange = (e) => { RE.stTo = e.target.value; RE.akontoOverride = null; renderRE(); }; }
  $("#reBody").querySelectorAll("[data-nkdel]").forEach((b) => (b.onclick = async () => {
    if (!confirm("Die letzte Abrechnung löschen? Word- und PDF-Datei werden entfernt. Danach kann die vorherige gelöscht werden.")) return;
    const pl = b.dataset.pl === "1" && confirm("Auch das in Paperless abgelegte PDF löschen?\n\nOK = ja, auch in Paperless löschen\nAbbrechen = nur in der App löschen");
    try {
      const r = await api(`/api/nk-statements/${b.dataset.nkdel}?paperless=${pl}`, { method: "DELETE" });
      toast("Abrechnung gelöscht" + (r.paperless ? ` · Paperless: ${r.paperless}` : ""), r.paperless && r.paperless !== "gelöscht");
      renderRE();
    } catch (err) { toast(err.message, true); }
  }));
  $("#stGen").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "Erzeuge …";
    try {
      const body = { year: RE.year, invoice_date: $("#stDate").value, upload: $("#stUpload").checked, akonto: ai ? num(ai.value) : null };
      if (RE.stMode === "interim") { body.period_from = RE.stFrom; body.period_to = RE.stTo; }
      if (tenant) body.tenancy_id = Number(RE.stSel.slice(1)); else body.unit_id = Number(RE.stSel.slice(1));
      const r = await api(`/api/properties/${p.id}/statement/generate`, { method: "POST", body: JSON.stringify(body) });
      $("#stResult").innerHTML = `<div class="card hint" style="margin:0">✓ Erzeugt: <a href="${r.docx}">Word herunterladen</a>${r.pdf ? ` · <a href="${r.pdf}" target="_blank">PDF öffnen</a>` : ""}
        ${r.paperless_task ? "<br>In Paperless abgelegt (wird gerade verarbeitet)." : ""}${r.warnings.length ? `<br><span class="neg">${r.warnings.map(esc).join("<br>")}</span>` : ""}</div>`;
      toast("Abrechnung erzeugt");
    } catch (err) { toast(err.message, true); }
    e.target.disabled = false; e.target.textContent = "Word & PDF erzeugen";
  };
}

// ------------------------------------------------------------ Einrichtung
function reSetup(p) {
  const tagSel = (v) => `<option value="">– kein Tag –</option>` + S.meta.tags.map((t) => `<option value="${t.id}" ${t.id === v ? "selected" : ""}>${esc(t.name)}</option>`).join("");
  const keyOpts = (empty) => [["", empty], ...p.keys.map((k) => [k.id, k.name])];
  const pctKeys = p.keys;
  const keyTable = `<table class="etable"><thead><tr><th>Schlüssel</th><th style="width:150px">Art</th>${p.units.map((u) => `<th class="num">${esc(u.name)}</th>`).join("")}<th class="num">Summe</th><th></th></tr></thead><tbody>
    ${pctKeys.map((k) => { const sum = p.units.reduce((a, u) => a + (Number(k.shares[u.id]) || 0), 0);
      return `<tr><td><input data-ent="keys" data-id="${k.id}" data-f="name" value="${esc(k.name)}"></td>
        <td><select data-ent="keys" data-id="${k.id}" data-f="type">${[["percent", "feste Prozente"], ["area", "nach Fläche"], ["equal", "gleichmässig"]].map(([v, l]) => `<option value="${v}" ${v === k.type ? "selected" : ""}>${l}</option>`).join("")}</select></td>
        ${p.units.map((u) => `<td>${k.type === "percent" ? `<input class="share" type="number" step="0.01" data-key="${k.id}" data-unit="${u.id}" value="${k.shares[u.id] ?? ""}">`
          : `<span class="muted">${k.type === "area" ? (u.area ? ((u.area / p.units.reduce((a, x) => a + (x.area || 0), 0)) * 100).toFixed(2) : "0.00") : (100 / p.units.length).toFixed(2)} %</span>`}</td>`).join("")}
        <td class="num ${k.type === "percent" && Math.abs(sum - 100) > 0.01 ? "warn" : ""}" data-sum="${k.id}">${k.type === "percent" ? sum.toFixed(2) + " %" : "100.00 %"}</td>
        <td><button class="linkbtn danger" data-del="keys" data-id="${k.id}">löschen</button></td></tr>`; }).join("") || `<tr><td colspan="${p.units.length + 4}" class="muted small">– noch keine –</td></tr>`}
    </tbody></table><button class="linkbtn" data-add="keys">+ Schlüssel</button>`;
  const months = MONTHS.map((m, i) => `<option value="${i + 1}" ${i + 1 === p.start_month ? "selected" : ""}>${m}</option>`).join("");
  $("#reBody").innerHTML = `
  <div class="card"><div class="cardhead"><h3>Haus</h3><button class="linkbtn danger" data-del="properties" data-id="${p.id}">Haus löschen</button></div>
    <form id="pfForm" class="formgrid" onsubmit="return false">
      <label>Name<input name="name" value="${esc(p.name)}"></label>
      <label>Adresse (für die Abrechnung)<textarea name="address">${esc(p.address || "")}</textarea></label>
      <label>Vermieter / Absender<input name="owner_name" value="${esc(p.owner_name || "")}"></label>
      <label>Adresse Vermieter<textarea name="owner_address">${esc(p.owner_address || "")}</textarea></label>
      <label>Mietzinskonto (IBAN)<input name="owner_iban" value="${esc(p.owner_iban || "")}"></label>
      <label>Abrechnungsperiode beginnt im<select name="start_month">${months}</select></label>
      <label>Heizpool: Anteil Raumheizung %<input name="room_pct" type="number" step="0.1" value="${p.room_pct}"><span class="muted small">Rest = Warmwasser</span></label>
      <label>Schlüssel Raumheizung<select name="room_key_id">${keyOpts("– wählen –").map(([k, l]) => `<option value="${k}" ${String(k) === String(p.room_key_id ?? "") ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
      <label>Schlüssel Warmwasser<select name="water_key_id">${keyOpts("– wählen –").map(([k, l]) => `<option value="${k}" ${String(k) === String(p.water_key_id ?? "") ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
      <label>Paperless-Tag für Belege des Hauses<select name="tag_id">${tagSel(p.tag_id)}</select></label>
      <label>Zahlbar innert Tagen<input name="due_days" type="number" value="${p.due_days ?? 30}"></label>
      <label class="check"><input name="exclude_from_budget" type="checkbox" ${p.exclude_from_budget ? "checked" : ""}> Kosten nicht im Haushaltsbudget</label>
      <label>Einleitungstext<textarea name="intro" placeholder="Mit diesem Brief erhalten Sie die Nebenkostenabrechnung …">${esc(p.intro || "")}</textarea></label>
      <label>Schlusstext<textarea name="closing" placeholder="Die Belege … können Sie … einsehen.">${esc(p.closing || "")}</textarea></label>
    </form></div>

  <div class="card"><h3>Wohnungen</h3>
    ${etable("units", p.units, [{ f: "name", label: "Bezeichnung" }, { f: "area", label: "Fläche m²", type: "number", w: "120px" }, { f: "owner_occupied", label: "selbst bewohnt", type: "check", w: "110px" }])}</div>

  <div class="card"><div class="cardhead"><h3>Verteilschlüssel</h3>${!p.categories.length ? `<button id="pfDefaults" class="primary">Standard-Schlüssel & Kostenarten anlegen</button>` : ""}</div>
    <p class="muted small">Feste Prozente pro Wohnung (Summe 100 %), nach Fläche oder gleichmässig. Raumheizung/Warmwasser oben beim Haus zuweisen.</p>
    ${p.units.length ? keyTable : `<div class="muted small">Zuerst Wohnungen erfassen.</div>`}</div>

  <div class="card"><h3>Kostenarten</h3>
    <p class="muted small">„Heizpool“ = wird zuerst auf Raumheizung/Warmwasser aufgeteilt. Stichworte ordnen Belege automatisch zu (Korrespondent, Titel, Zweck; kommagetrennt).</p>
    ${etable("categories", p.categories, [
      { f: "grp", label: "Gruppe", w: "170px" }, { f: "name", label: "Kostenart" },
      { f: "key_id", label: "Schlüssel", type: "select", options: () => keyOpts("–"), w: "150px" },
      { f: "heating", label: "Heizpool", type: "check", w: "70px" }, { f: "unit_label", label: "Einheit", w: "80px" },
      { f: "keywords", label: "Stichworte", ph: "heizöl, brennstoff" }])}</div>

  <div class="card"><h3>Heizöl-Lagerbestand</h3>
    <p class="muted small">Optional: Heizöl nach <b>Verbrauch</b> abrechnen statt nach Einkauf – Anfangsbestand + Einkäufe − Endbestand.
      Dafür den Tankstand zu Beginn und Ende der Periode (bzw. am Stichtag einer Zwischenabrechnung) erfassen.</p>
    <div class="field"><div class="lbl"><b>Kostenart Heizöl</b><span>leer = nach Einkauf abrechnen</span></div>
      <div><select data-ent="properties" data-id="${p.id}" data-f="oil_category_id">${[["", "– nach Einkauf –"], ...p.categories.filter((c) => c.heating).map((c) => [c.id, `${c.grp} · ${c.name}`])]
        .map(([k, l]) => `<option value="${k}" ${String(k) === String(p.oil_category_id ?? "") ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></div></div>
    <div id="stockTable">…</div></div>

  <div class="card"><h3>Mietverhältnisse</h3>
    <p class="muted small">Stichworte = Text in der Mietzahlung (z.B. Name), mehrere mit Komma; „+“ verbindet Begriffe. Kein Ende = laufend.</p>
    <div class="tablewrap wide">${etable("tenancies", p.tenancies, [
      { f: "unit_id", label: "Wohnung", type: "select", options: () => p.units.map((u) => [u.id, u.name]), w: "150px" },
      { f: "tenant_name", label: "Mieter" }, { f: "tenant_address", label: "Adresse", ph: "Strasse, PLZ Ort" },
      { f: "salutation", label: "Anrede", ph: "Sehr geehrte Frau …" },
      { f: "start", label: "Beginn", type: "date", w: "130px" }, { f: "end", label: "Ende", type: "date", w: "130px" },
      { f: "rent", label: "Miete/Mt.", type: "number", w: "90px" }, { f: "akonto", label: "Akonto/Mt.", type: "number", w: "90px" },
      { f: "match_text", label: "Stichworte Zahlung", ph: "Nachname (Komma = oder, + = und)" }])}</div></div>

  <div class="card" id="fixedCard"><h3>Fixkosten</h3>
    <p class="muted small">Wiederkehrende Kosten ohne Beleg, z.B. Hauswart-Pauschale. „nur …“ = einer Wohnung individuell belastet.</p>
    ${etable("fixed-costs", p.fixed_costs, [
      { f: "name", label: "Bezeichnung" }, { f: "category_id", label: "Kostenart", type: "select", options: () => catOpts(p, "–"), w: "200px" },
      { f: "unit_id", label: "Wohnung", type: "select", options: () => unitOpts(p), w: "170px" },
      { f: "amount", label: "Betrag", type: "number", w: "100px" },
      { f: "interval", label: "pro", type: "select", options: () => [["jahr", "Jahr"], ["monat", "Monat"]], w: "90px" },
      { f: "valid_from", label: "gültig ab", type: "date", w: "130px" }, { f: "valid_to", label: "bis", type: "date", w: "130px" }])}</div>

  <div class="card" id="manualCard"><h3>Manuelle Kosten</h3>
    <p class="muted small">Einzelne Kosten ohne Paperless-Beleg (z.B. Eigenleistung, abgelesener Betrag). Datum bestimmt die Periode.</p>
    <div id="manualTable">…</div></div>`;
  api(`/api/re/heating-stock?property_id=${p.id}`).then((rows) => {
    $("#stockTable").innerHTML = etable("heating-stock", rows.sort((a, b) => b.date.localeCompare(a.date)), [
      { f: "date", label: "Ablesung am", type: "date", w: "150px" }, { f: "liters", label: "Tankstand Liter", type: "number", w: "140px" },
      { f: "price", label: "Preis CHF / 100 l", type: "number", w: "140px", ph: "z.B. 114.90" }, { f: "note", label: "Notiz", ph: "z.B. Ablesung Periodenende" }]);
  });
  api(`/api/re/manual-costs?property_id=${p.id}`).then((rows) => {
    $("#manualTable").innerHTML = etable("manual-costs", rows.sort((a, b) => b.date.localeCompare(a.date)), [
      { f: "date", label: "Datum", type: "date", w: "130px" }, { f: "description", label: "Bezeichnung" },
      { f: "category_id", label: "Kostenart", type: "select", options: () => catOpts(p, "–"), w: "200px" },
      { f: "unit_id", label: "Wohnung", type: "select", options: () => unitOpts(p), w: "170px" },
      { f: "quantity", label: "Menge", type: "number", w: "90px" }, { f: "amount", label: "Betrag", type: "number", w: "100px" }]);
    if (RE.setupFocus) { $(RE.setupFocus === "manual" ? "#manualCard" : "#fixedCard").scrollIntoView({ behavior: "smooth" }); RE.setupFocus = null; }
  });
}

// ------------------------------------------------------------ Projekte
async function renderProjects() {
  RE.projects = await api("/api/projects-overview");
  if (!RE.props.length) await loadRE().catch(() => {});
  const tagSel = (v) => [["", "– kein Tag –"], ...S.meta.tags.map((t) => [t.id, t.name])];
  $("#prList").innerHTML = RE.projects.length ? RE.projects.map((pr) => {
    const pct = pr.budget ? Math.min(100, (pr.total / pr.budget) * 100) : 0;
    return `<div class="pr ${pr.id === RE.prId ? "active" : ""}" data-pr="${pr.id}">
      <div class="cat-top"><b>${esc(pr.name)}</b><span class="cat-amt"><b>${fmt(pr.total)}</b>${pr.budget ? " / " + fmt(pr.budget) : ""}</span></div>
      ${pr.budget ? `<div class="bar ${pr.total > pr.budget ? "over" : pct > 80 ? "warn" : ""}"><div style="width:${pct}%"></div></div>` : ""}
      <div class="muted small">${pr.count} Position(en) · ${esc(pr.status)}${pr.tag_id ? ` · Tag „${esc(tagName(pr.tag_id))}“` : ""}${pr.exclude_from_budget ? " · nicht im Haushaltsbudget" : ""}</div></div>`;
  }).join("") : `<div class="empty">Noch keine Projekte.</div>`;
  if (RE.prId) await drawProject(); else { $("#prTitle").textContent = "Projekt wählen"; $("#prSearch").hidden = true; }
}
async function drawProject() {
  const r = await api(`/api/projects/${RE.prId}/items`), pr = r.project;
  $("#prTitle").textContent = pr.name; $("#prSearch").hidden = false;
  const tagOpts = [["", "– kein Tag –"], ...S.meta.tags.map((t) => [t.id, t.name])];
  $("#prDetail").innerHTML = `
    <div class="formgrid" id="prForm">
      <label>Name<input name="name" value="${esc(pr.name)}"></label>
      <label>Budget CHF<input name="budget" type="number" step="any" value="${pr.budget ?? ""}"></label>
      <label>Paperless-Tag<select name="tag_id">${tagOpts.map(([k, l]) => `<option value="${k}" ${String(k) === String(pr.tag_id ?? "") ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
      <label>Status<select name="status">${["aktiv", "geplant", "abgeschlossen"].map((s) => `<option ${s === pr.status ? "selected" : ""}>${s}</option>`).join("")}</select></label>
      <label class="check"><input name="exclude_from_budget" type="checkbox" ${pr.exclude_from_budget ? "checked" : ""}> nicht im Haushaltsbudget</label>
    </div>
    <table class="etable" style="margin-top:12px"><thead><tr><th style="width:84px">Datum</th><th>Beleg / Zweck</th><th class="num" style="width:110px">Anteil</th><th class="num" style="width:100px">Betrag</th><th style="width:70px"></th></tr></thead>
    <tbody>${r.lines.map((l) => `<tr data-alloc="${l.alloc_id ?? ""}"><td>${fmtDate(l.date)}</td>
      <td><div class="it-purpose">${l.url ? `<a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.title)}</a>` : esc(l.title)}</div><div class="it-sub">${esc(l.purpose || "")}${l.implicit ? " · über Tag" : ""}</div></td>
      <td class="num">${l.implicit ? "100 %" : l.fixed_amount != null ? `<input class="pr-amt" type="number" step="0.05" value="${l.fixed_amount}" title="Betrag CHF" style="width:90px">` : `<input class="pr-pct" type="number" step="1" value="${l.percent ?? 100}" title="Prozent" style="width:70px"> %`}</td>
      <td class="num"><b>${fmt(l.amount)}</b>${l.amount !== l.full_amount ? `<div class="muted small">von ${fmt(l.full_amount)}</div>` : ""}</td>
      <td>${l.alloc_id ? `<button class="linkbtn danger pr-del">entfernen</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="5" class="empty">Noch nichts zugeordnet.</td></tr>`}</tbody>
    <tfoot><tr><td colspan="3">Total</td><td class="num">${fmt(r.total)}</td><td></td></tr></tfoot></table>
    <div class="row" style="justify-content:flex-end;margin-top:8px"><button class="linkbtn danger" id="prDel">Projekt löschen</button></div>`;
}
$("#prList").addEventListener("click", (e) => { const c = e.target.closest("[data-pr]"); if (c) { RE.prId = Number(c.dataset.pr); renderProjects(); } });
$("#prAdd").onclick = async () => {
  const r = await openDialog({ title: "Neues Projekt", label: "Name", ok: "Anlegen" });
  if (!r || !r.value) return;
  const pr = await api("/api/re/projects", { method: "POST", body: JSON.stringify({ name: r.value, status: "aktiv" }) });
  RE.prId = pr.id; renderProjects();
};
$("#prSearch").onclick = () => { const pr = RE.projects.find((x) => x.id === RE.prId); openSearch({ type: "project", id: RE.prId, name: pr?.name }); };
$("#prDetail").addEventListener("change", async (e) => {
  const el = e.target;
  if (el.closest("#prForm")) {
    let v = el.type === "checkbox" ? el.checked : el.value;
    if (el.type === "number" || el.name === "tag_id") v = num(v);
    await api(`/api/re/projects/${RE.prId}`, { method: "PUT", body: JSON.stringify({ [el.name]: v }) }); toast("Gespeichert"); renderProjects(); return;
  }
  const aid = el.closest("tr")?.dataset.alloc; if (!aid) return;
  const body = el.classList.contains("pr-pct") ? { percent: num(el.value) } : { amount: num(el.value) };
  await api(`/api/allocations/${aid}`, { method: "PUT", body: JSON.stringify(body) }); toast("Gespeichert"); renderProjects();
});
$("#prDetail").addEventListener("click", async (e) => {
  if (e.target.classList.contains("pr-del")) { await api(`/api/allocations/${e.target.closest("tr").dataset.alloc}`, { method: "DELETE" }); renderProjects(); }
  if (e.target.id === "prDel" && confirm("Projekt löschen? Zuordnungen werden entfernt, Belege bleiben.")) {
    await api(`/api/re/projects/${RE.prId}`, { method: "DELETE" }); RE.prId = null; renderProjects();
  }
});

// ------------------------------------------------------------ Suche: Belege einem Ziel zuordnen
let seTarget = null, seTimer = null;
function openSearch(target) {
  seTarget = target;
  $("#seTitle").textContent = `Belege zuordnen: ${target.name || ""}`;
  $("#seInput").value = ""; $("#seResults").innerHTML = `<div class="muted small">Suchbegriff eingeben (z.B. Händler, Rechnungstitel, Inhalt).</div>`;
  $("#searchDlg").showModal(); $("#seInput").focus();
}
$("#seClose").onclick = () => { $("#searchDlg").close(); if (S.view === "realestate") renderRE(); if (S.view === "projects") renderProjects(); };
$("#seInput").oninput = () => {
  clearTimeout(seTimer);
  seTimer = setTimeout(async () => {
    const q = $("#seInput").value.trim(); if (q.length < 2) return;
    const res = await api(`/api/doc-search?q=${encodeURIComponent(q)}`).catch((e) => { toast(e.message, true); return []; });
    $("#seResults").innerHTML = res.length ? res.map((d, i) => {
      const already = (d.allocations || []).some((a) => a.target_type === seTarget.type && a.target_id === seTarget.id);
      return `<div class="invitem" data-i="${i}"><span><span class="srcb ${d.kind === "doc" ? "Beleg" : "Bank"}">${d.kind === "doc" ? "Beleg" : "Bank"}</span>
        ${fmtDate(d.date)} · <b>${esc(d.purpose && d.kind === "doc" ? d.purpose : d.title)}</b> <span class="muted small">${esc(d.kind === "doc" ? d.title : "")}</span></span>
        <span class="row"><b class="num">${fmt(d.amount || 0)}</b>${already ? `<span class="muted small">✓ zugeordnet</span>` : `<button class="se-add">ganz</button><button class="se-part">Teil …</button>`}</span></div>`;
    }).join("") : `<div class="empty">Nichts gefunden.</div>`;
    $("#seResults").onclick = async (e) => {
      const row = e.target.closest("[data-i]"); if (!row) return;
      const d = res[Number(row.dataset.i)];
      if (e.target.classList.contains("se-add")) {
        await api("/api/allocations", { method: "POST", body: JSON.stringify({ source_type: d.kind, source_id: d.id, target_type: seTarget.type, target_id: seTarget.id, percent: 100 }) });
        e.target.parentElement.innerHTML = `<b class="num">${fmt(d.amount || 0)}</b><span class="muted small">✓ zugeordnet</span>`; toast("Zugeordnet");
      } else if (e.target.classList.contains("se-part")) {
        openAlloc({ kind: d.kind, id: d.id, title: d.purpose || d.title, amount: d.amount, allocations: d.allocations || [] }, `${seTarget.type}:${seTarget.id}`);
      }
    };
  }, 300);
};

// ------------------------------------------------------------ Zuordnen-Dialog (aus Ausgaben, Übersicht, Abgleich)
let alItem = null;
async function openAlloc(item, preselect = null) {
  if (!RE.props.length && !RE.projects.length) await loadRE().catch(() => {});
  alItem = item;
  $("#alSub").textContent = `${item.title || ""} · ${fmt(item.amount || 0)}`;
  const ex = (item.allocations || []);
  $("#alExisting").innerHTML = ex.length ? `<h4>Bestehend</h4>` + ex.map((a) => `<div class="invitem"><span>${a.target_type === "property" ? "🏠" : "📁"} ${esc(a.target_name)}
      <span class="muted small">${a.implicit ? "über Paperless-Tag" : a.amount != null ? "Betrag" : `${a.percent} %`}${a.excludes_budget ? " · nicht im Haushaltsbudget" : ""}</span></span>
      <span class="row"><b class="num">${fmt(a.value)}</b>${a.id ? `<button class="linkbtn danger" data-aldel="${a.id}">entfernen</button>` : ""}</span></div>`).join("") : "";
  const opts = [...RE.props.map((p) => [`property:${p.id}`, `🏠 ${p.name}`]), ...RE.projects.map((p) => [`project:${p.id}`, `📁 ${p.name}`])];
  $("#alTarget").innerHTML = opts.length ? opts.map(([k, l]) => `<option value="${k}" ${k === preselect ? "selected" : ""}>${esc(l)}</option>`).join("") : `<option value="">– zuerst Haus oder Projekt anlegen –</option>`;
  const rest = Math.max(0, 100 - ex.filter((a) => !a.implicit && a.percent != null && a.amount == null).reduce((s, a) => s + a.percent, 0));
  $("#alMode").value = "percent"; $("#alValue").value = rest || 100; $("#alQty").value = ""; $("#alNote").value = "";
  alTargetChanged();
  $("#allocDlg").showModal();
}
function alTargetChanged() {
  const [tt, tid] = ($("#alTarget").value || ":").split(":");
  const p = tt === "property" ? RE.props.find((x) => x.id === Number(tid)) : null;
  $("#alPropFields").hidden = !p; $("#alQtyWrap").hidden = !p;
  if (p) {
    $("#alCat").innerHTML = catOpts(p, "– automatisch / nicht umlegbar –").map(([k, l]) => `<option value="${k}">${esc(l)}</option>`).join("");
    $("#alUnit").innerHTML = unitOpts(p).map(([k, l]) => `<option value="${k}">${esc(l)}</option>`).join("");
  }
}
$("#alTarget").onchange = alTargetChanged;
$("#alClose").onclick = () => { $("#allocDlg").close(); render(); };
$("#alExisting").addEventListener("click", async (e) => {
  const id = e.target.dataset.aldel; if (!id) return;
  await api(`/api/allocations/${id}`, { method: "DELETE" });
  alItem.allocations = alItem.allocations.filter((a) => String(a.id) !== id); openAlloc(alItem);
});
$("#alSave").onclick = async () => {
  const [tt, tid] = ($("#alTarget").value || ":").split(":"); if (!tid) return;
  const body = { source_type: alItem.kind === "bank" || alItem.kind === "tx" ? "tx" : "doc", source_id: alItem.id, target_type: tt, target_id: Number(tid),
    note: $("#alNote").value || null, [$("#alMode").value]: num($("#alValue").value) };
  if (tt === "property") { body.category_id = num($("#alCat").value); body.unit_id = num($("#alUnit").value); body.quantity = num($("#alQty").value); }
  try {
    await api("/api/allocations", { method: "POST", body: JSON.stringify(body) });
    toast("Zugeordnet"); $("#allocDlg").close(); render();
  } catch (err) { toast(err.message, true); }
};
const allocBadges = (d) => (d.allocations || []).length
  ? `<span class="allocs">${d.allocations.map((a) => `<span class="allocb" title="${a.implicit ? "über Paperless-Tag" : ""}">${a.target_type === "property" ? "🏠" : "📁"} ${esc(a.target_name)}${a.value !== (d.amount_full ?? d.amount) ? " " + fmtPos(a.value) : ""}</span>`).join("")}</span>` : "";
