"use strict";
/* Rest vom Lohn: Einnahmen − Ausgaben − noch fällige Vertragszahlungen − vorgemerkte Bestellungen */

const PL = { data: null, editId: null };

async function renderPlan() {
  if (typeof canSee === "function" && !canSee("income")) { $("#planCard").hidden = true; return; }   // ohne Einnahmen-Recht sinnlos
  $("#planCard").hidden = false;
  let d;
  try { d = await api(`/api/plan?month=${S.month}`); } catch (e) { $("#planBody").textContent = e.message; return; }
  PL.data = d;
  const inMonth = new Set(d.orders_in_month);
  const line = (label, val, cls = "", sub = "") => `<div class="pl-line ${cls}"><span>${label}${sub ? ` <span class="muted small">${sub}</span>` : ""}</span><b class="num">${val}</b></div>`;
  const rec = d.recurring;
  const recRow = (r) => `<div class="invitem ${r.status === "bezahlt" ? "pl-paid" : ""}"><span>${fmtDate(r.due)} · <a href="#" data-goct="${r.contract_id}">${esc(r.name)}</a>
      <span class="muted small">${r.kind === "kredit" ? "Kredit" : esc(r.category || "Vertrag")}${r.interval && r.interval !== "monat" ? " · " + esc(IV_LABEL[r.interval] || r.interval) : ""}</span></span>
      <span class="row">${r.status === "bezahlt" ? `<span class="pill">bezahlt ${fmtDate(r.paid_date)}</span>` : r.overdue ? `<span class="pill warn">überfällig?</span>` : `<span class="pill grey">offen</span>`}
      <b class="num">${r.amount != null ? fmt(r.amount) : "–"}</b></span></div>`;
  const ordRow = (o) => `<div class="invitem ${inMonth.has(o.id) ? "" : "pl-later"}" data-oid="${o.id}"><span>${o.source === "paperless" ? `<span class="srcb Beleg">Mail</span>` : `<span class="srcb Manuell">Hand</span>`}
      <b>${esc(o.merchant)}</b> <span class="muted small">${esc(o.description && o.description !== o.merchant ? o.description : "")} · bestellt ${fmtDate(o.date)} · erwartet ${fmtDate(o.expected)}${inMonth.has(o.id) ? "" : " (später)"}</span>
      ${o.url ? `<a href="${esc(o.url)}" target="_blank" rel="noopener" class="small">↗</a>` : ""}</span>
      <span class="row">${o.amount != null ? `<b class="num">${fmt(o.amount)}</b>` : `<span class="pill warn">Betrag?</span>`}
      <button class="linkbtn" data-odone="${o.id}" title="Rechnung/Abbuchung ist da">erledigt</button><button class="linkbtn" data-oedit="${o.id}">bearbeiten</button></span></div>`;
  const pastNote = d.past ? `<p class="muted small">Vergangener Monat – offene Vormerkungen werden nicht mehr eingerechnet.</p>` : "";
  $("#planBody").classList.remove("muted", "small");
  $("#planBody").innerHTML = `
    <div class="pl-grid">
      <div class="pl-calc">
        ${line("Einnahmen", fmt(d.income), "", !d.income ? `– unter <a href="#" data-goto="income">Einnahmen</a> den Lohn erfassen` : d.income_expected ? `davon ${fmt(d.income_expected)} erwartet` : "")}
        ${line("− bereits ausgegeben", fmt(d.spent))}
        ${line("− noch fällig", fmt(d.due_open), "", `${rec.filter((r) => r.status === "offen").length} Vertragszahlung(en)`)}
        ${line("− vorgemerkt", fmt(d.orders), "", `${d.orders_in_month.length} Bestellung(en)`)}
        <div class="pl-line pl-total"><span>frei verfügbar</span><b class="num ${d.free >= 0 ? "pos" : "neg"}">${fmt(d.free)}</b></div>
        ${pastNote}
      </div>
      <div class="pl-lists">
        <h4>Verträge & Kredite in diesem Monat</h4>
        ${rec.length ? rec.map(recRow).join("") : `<div class="muted small">Keine fälligen Zahlungen – Verträge unter <a href="#" data-goto="contracts">Verträge</a> erfassen.</div>`}
        <h4 style="margin-top:14px">Vorgemerkt</h4>
        ${d.open_orders.length ? d.open_orders.map(ordRow).join("") : `<div class="muted small">Nichts vorgemerkt. ${d.order_tag_found ? "" : `Für automatische Vormerkungen in Paperless den Tag „${esc(d.order_tag)}“ anlegen und per Mail-Regel an Bestellbestätigungen hängen.`}</div>`}
        ${d.recent_done.length ? `<details class="small" style="margin-top:6px"><summary class="muted">Kürzlich erledigt (${d.recent_done.length})</summary>${d.recent_done.map((o) => `<div class="invitem pl-paid"><span>${esc(o.merchant)} <span class="muted small">${esc(o.done_title || "")}</span></span>
          <span class="row"><b class="num">${o.amount != null ? fmt(o.amount) : ""}</b><button class="linkbtn" data-oreopen="${o.id}">wieder öffnen</button></span></div>`).join("")}</details>` : ""}
      </div>
    </div>`;
}

function openOrder(o = null) {
  PL.editId = o?.id ?? null;
  const f = $("#orderForm"), today = new Date().toISOString().slice(0, 10);
  $("#odTitle").textContent = o ? "Vormerkung bearbeiten" : "Ausgabe vormerken";
  f.merchant.value = o?.merchant || ""; f.description.value = o?.description || ""; f.amount.value = o?.amount ?? "";
  f.date.value = o?.date || today; f.expected.value = o?.expected || ""; f.note.value = o?.note || "";
  $("#odDelete").hidden = !o;
  const names = new Set([...(PL.data?.open_orders || []).map((x) => x.merchant), ...S.meta.correspondents.map((c) => c.name)]);
  $("#odMerchants").innerHTML = [...names].map((n) => `<option value="${esc(n)}">`).join("");
  $("#orderDlg").showModal(); f.merchant.focus();
}
$("#planAdd").onclick = () => openOrder();
$("#orderDlg").addEventListener("close", async () => {
  if ($("#orderDlg").returnValue !== "ok") return;
  const f = $("#orderForm");
  const body = { merchant: f.merchant.value.trim(), description: f.description.value || null, amount: f.amount.value === "" ? null : Number(f.amount.value),
    date: f.date.value || null, expected: f.expected.value || null, note: f.note.value || null };
  try {
    if (PL.editId) await api(`/api/orders/${PL.editId}`, { method: "PUT", body: JSON.stringify(body) });
    else await api("/api/orders", { method: "POST", body: JSON.stringify(body) });
    toast("Vorgemerkt"); renderPlan();
  } catch (e) { toast(e.message, true); }
});
$("#odDelete").onclick = async () => {
  if (!PL.editId || !confirm("Vormerkung löschen?")) return;
  await api(`/api/orders/${PL.editId}`, { method: "DELETE" }); $("#orderDlg").close(); renderPlan();
};
$("#planBody").addEventListener("click", async (e) => {
  const t = e.target;
  try {
    if (t.dataset.odone) { await api(`/api/orders/${t.dataset.odone}`, { method: "PUT", body: JSON.stringify({ status: "done" }) }); toast("Erledigt"); renderPlan(); }
    else if (t.dataset.oreopen) { await api(`/api/orders/${t.dataset.oreopen}`, { method: "PUT", body: JSON.stringify({ status: "open" }) }); renderPlan(); }
    else if (t.dataset.oedit) openOrder(PL.data.open_orders.find((o) => o.id === Number(t.dataset.oedit)));
    else if (t.dataset.goct) { e.preventDefault(); CT.id = Number(t.dataset.goct); setView("contracts"); }
  } catch (err) { toast(err.message, true); }
});
