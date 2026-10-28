"use strict";
/* Zähler: Stand, Verbrauch seit Periodenbeginn, Hochrechnung und erwartete Abrechnung abzüglich Akonto */

const MT = { data: null, open: new Set() };
const MT_SRC = { ams: "AMS-Leser", http: "HTTP-JSON (z.B. Shelly)", homeassistant: "Home Assistant", manuell: "von Hand" };
const MT_KIND = { strom: "Strom", wasser: "Wasser", gas: "Gas", waerme: "Wärme" };
const mtNum = (v, d = 1) => (v == null ? "–" : new Intl.NumberFormat("de-CH", { maximumFractionDigits: d }).format(v));

async function renderMeters() {
  MT.data = await api("/api/meters");
  const d = MT.data;
  Object.keys(S.charts).filter((k) => k.startsWith("mt")).forEach((k) => { S.charts[k].destroy(); delete S.charts[k]; });
  const bal = d.meters.filter((m) => m.projection?.balance != null);
  const sum = bal.reduce((a, m) => a + m.projection.balance, 0);
  $("#mtKpis").innerHTML = bal.length ? `
    <div class="kpi"><div class="kpi-label">Erwartete Abrechnungen</div><div class="kpi-value ${sum > 0 ? "neg" : "pos"}">${sum > 0 ? "Nachzahlung " : "Guthaben "}${fmt(Math.abs(sum))}</div>
      <div class="kpi-sub">${bal.length} Zähler mit Tarif und Akonto</div></div>` : "";
  let lastProp;
  $("#mtList").innerHTML = d.meters.length ? d.meters.map((m) => {
    const head = m.property_id !== lastProp ? `<h3 class="mt-grp">${esc(d.properties.find((x) => x.id === m.property_id)?.name || "Ohne Zuordnung")}</h3>` : "";
    lastProp = m.property_id; return head + meterCard(m);
  }).join("")
    : `<div class="card hint">Noch kein Zähler. Oben einen hinzufügen – z.B. <b>Strom (AMS-Leser)</b>.</div>`;
  d.meters.forEach((m) => drawMeterChart(m));
}

function meterCard(m) {
  const p = m.projection || {};
  const ctOpts = [["", "– kein Akonto-Vertrag –"], ...MT.data.contracts.map((c) => [c.id, c.name])];
  const opt = (list, v) => list.map(([k, l]) => `<option value="${esc(k ?? "")}" ${String(k ?? "") === String(v ?? "") ? "selected" : ""}>${esc(l)}</option>`).join("");
  const months = MONTHS.map((n, i) => [i + 1, n]);
  const u = esc(m.unit || "");
  const head = `<div class="mt-head"><h3>${m.icon} ${esc(m.name)}</h3>
      <span class="muted small">${esc(MT_KIND[m.kind] || m.kind)} · ${esc(MT_SRC[m.source] || m.source)}${mtProp(m)}${p.latest_ts ? ` · Stand <b>${mtNum(p.latest_value, 2)} ${u}</b> (${new Date(p.latest_ts).toLocaleString("de-CH", { dateStyle: "short", timeStyle: "short" })})` : ""}</span>
      <span class="row">${m.source !== "manuell" && m.active ? `<button class="linkbtn" data-mtread>jetzt ablesen</button>` : ""}<button class="linkbtn" data-mtadd>Stand erfassen</button></span></div>
    ${m.last_error && m.active ? `<div class="alert" style="margin:6px 0">Zähler nicht lesbar: ${esc(m.last_error)}</div>` : ""}`;
  let body;
  if (!m.active) {
    body = `<div class="card hint" style="margin:8px 0">Vorgemerkt – noch nicht aktiv${m.note ? ` (${esc(m.note)})` : ""}. Unter „Einstellungen“ Adresse eintragen, „Quelle testen“ und <b>aktiv</b> setzen.</div>`;
  } else if (p.status !== "ok") {
    body = `<p class="muted small">${esc(p.status || "keine Daten")}${p.status === "zu wenig Daten" ? " – die Hochrechnung startet nach ein paar Tagen Ablesungen. Schneller geht's mit dem Zählerstand der letzten Rechnung (Einstellungen)." : ""}</p>`;
  } else {
    const a = p.akonto;
    const balance = p.balance == null ? "" : `<div class="mt-bal ${p.balance > 0 ? "neg" : "pos"}">${p.balance > 0 ? "Nachzahlung" : "Guthaben"} ca. <b>${fmt(Math.abs(p.balance))}</b></div>`;
    body = `<div class="mt-grid">
      <div><div class="l">Periode</div><div class="v">${fmtDate(p.period[0])} – ${fmtDate(p.period[1])}</div><div class="bar"><div style="width:${Math.round(p.progress * 100)}%"></div></div></div>
      <div><div class="l">Verbrauch bisher</div><div class="v">${mtNum(p.so_far)} ${u}</div><div class="muted small">seit ${fmtDate(p.start_ts.slice(0, 10))} · ${mtNum(p.daily, 2)} ${u}/Tag</div></div>
      <div><div class="l">Hochrechnung Periode</div><div class="v">${mtNum(p.total, 0)} ${u}</div><div class="muted small">${esc(p.method)}</div></div>
      <div><div class="l">Rechnung ca.</div><div class="v">${p.cost == null ? "–" : fmt(p.cost)}</div><div class="muted small">${m.tariff != null ? `${mtNum(m.tariff, 4)} CHF/${u}${m.base_fee ? ` + ${fmt(m.base_fee)}/Mt.` : ""}` : "Tarif fehlt (Einstellungen)"}</div></div>
      <div><div class="l">Akonto</div><div class="v">${a ? fmt(a.total) : "–"}</div><div class="muted small">${a ? `bezahlt ${fmt(a.paid)} (${a.payments}) + noch ${fmt(a.remaining)} (${a.remaining_count})` : "Akonto-Vertrag wählen (Einstellungen)"}</div></div>
      <div>${balance || `<div class="l">Saldo</div><div class="v">–</div><div class="muted small">${a ? "" : "ohne Akonto – nur Verbrauch"}</div>`}</div>
    </div><div class="chartbox small"><canvas id="mt${m.id}"></canvas></div>`;
  }
  const f = (label, hint, html) => `<div class="field"><div class="lbl"><b>${label}</b>${hint ? `<span>${hint}</span>` : ""}</div><div>${html}</div></div>`;
  const settings = `<details class="mt-set" ${MT.open.has(m.id) ? "open" : ""} data-mtset><summary>Einstellungen</summary>
    ${f("Name / Art", "", `<div class="row"><input name="name" value="${esc(m.name)}"><select name="kind">${opt(Object.entries(MT_KIND), m.kind)}</select><input name="unit" value="${u}" style="width:80px" placeholder="Einheit"></div>`)}
    ${f("Quelle", "", `<select name="source">${opt(Object.entries(MT_SRC), m.source)}</select>`)}
    ${m.source === "homeassistant" ? f("Sensor in Home Assistant", MT.data.ha_configured ? "Entitäts-ID, Zählerstand (kein Momentanwert)" : "HOMEASSISTANT_URL und _TOKEN in der .env fehlen", `<input name="ha_entity" value="${esc(m.ha_entity || "")}" placeholder="sensor.ams_energie_bezug">`) : ""}
    ${["ams", "http"].includes(m.source) ? f("Adresse", m.source === "ams" ? "IP oder Name des AMS-Lesers – gelesen wird /data.json" : "URL, die JSON liefert", `<input name="url" value="${esc(m.url || "")}" placeholder="${m.source === "ams" ? "192.168.1.50" : "http://192.168.1.60/rpc/Input.GetStatus?id=2"}" style="width:100%">`)
      + f("JSON-Feld / Faktor", m.source === "ams" ? "„ic“ = Bezug kumuliert in kWh" : "z.B. counts.total; Faktor 0.001 = Liter-Impulse → m³", `<div class="row"><input name="json_path" value="${esc(m.json_path || (m.source === "ams" ? "ic" : ""))}" placeholder="ic"><input name="factor" type="number" step="any" value="${m.factor ?? 1}" style="width:110px" title="Faktor"></div>`) : ""}
    ${f("Tarif", "CHF pro Einheit inkl. MWST (alle Komponenten) und Grundgebühr pro Monat", `<div class="row"><input name="tariff" type="number" step="0.0001" value="${m.tariff ?? ""}" placeholder="z.B. 0.2950" style="width:130px"><input name="base_fee" type="number" step="0.05" value="${m.base_fee ?? ""}" placeholder="Grundgeb./Mt." style="width:130px"></div>`)}
    ${f("Abrechnungsperiode", "beginnt im", `<select name="period_start_month">${opt(months, m.period_start_month)}</select>`)}
    ${f("Stand der letzten Rechnung", "Startwert der Periode – sonst zählt die erste Ablesung", `<div class="row"><input name="start_reading" type="number" step="any" value="${m.start_reading ?? ""}" placeholder="Zählerstand" style="width:140px"><input name="start_date" type="date" value="${esc(m.start_date || "")}"></div>`)}
    ${f("Zuordnung", "Haus = allgemein (z.B. Allgemeinstrom), sonst die Wohnung", `<select name="assign">${opt([["", "– keine –"],
      ...MT.data.properties.flatMap((x) => [[`${x.id}:`, `${x.name} – Haus (allgemein)`],
        ...MT.data.units.filter((u) => u.property_id === x.id).map((u) => [`${x.id}:${u.id}`, `${x.name} – Wohnung ${u.name}`])])],
      m.property_id ? `${m.property_id}:${m.unit_id ?? ""}` : "")}</select>`)}
    ${m.property_id && !m.unit_id ? f("In der NK anzeigen bei", "erwartete Kosten in Klammern hinter dieser Kategorie (leer: Kategorie mit „Strom“ im Namen)",
      `<select name="nk_category_id">${opt([["", "– automatisch –"], ...MT.data.nk_categories.filter((c) => c.property_id === m.property_id).map((c) => [c.id, `${c.grp} · ${c.name}`])], m.nk_category_id)}</select>`) : ""}
    ${f("Akonto-Vertrag", "Vertrag mit den Akonto-Zahlungen (unter Verträge erfassen, Stichwort Bankbuchung setzen)", `<select name="contract_id">${opt(ctOpts, m.contract_id)}</select>`)}
    <div class="row" style="justify-content:space-between;margin-top:8px"><label class="check small"><input type="checkbox" name="active" ${m.active ? "checked" : ""}> aktiv</label>
      <span class="row">${m.source !== "manuell" ? `<button class="linkbtn" data-mttest>Quelle testen</button>` : ""}<button class="linkbtn danger" data-mtdel>Zähler löschen</button></span></div>
  </details>`;
  return `<div class="card mt ${m.active ? "" : "off"}" data-mid="${m.id}">${head}${body}${settings}</div>`;
}

function mtProp(m) {
  const pr = MT.data.properties.find((x) => x.id === m.property_id);
  const u = MT.data.units.find((x) => x.id === m.unit_id);
  return pr ? ` · <span class="pill">${esc(pr.name)} · ${u ? "Wohnung " + esc(u.name) : "allgemein"}</span>` : "";
}

function drawMeterChart(m) {
  const p = m.projection, el = document.getElementById("mt" + m.id);
  if (!el || !window.Chart || !p?.monthly?.length) return;
  S.charts["mt" + m.id] = new Chart(el, {
    type: "bar",
    data: { labels: p.monthly.map((x) => monthShort(x.month)), datasets: [{ label: `Verbrauch ${m.unit}`, data: p.monthly.map((x) => x.value),
      backgroundColor: p.monthly.map((x) => (x.partial ? "#9aa5a0aa" : (m.kind === "wasser" ? "#2b7bb9cc" : "#e0a82ecc"))) }] },
    options: { maintainAspectRatio: false, plugins: { legend: { display: false } },
      scales: { x: { grid: { display: false }, ticks: { color: css("--muted") } }, y: { grid: { color: css("--line") }, ticks: { color: css("--muted") } } } },
  });
}

$("#mtList").addEventListener("toggle", (e) => {
  const c = e.target.closest?.("[data-mid]"); if (!c || !e.target.matches("[data-mtset]")) return;
  const id = Number(c.dataset.mid); e.target.open ? MT.open.add(id) : MT.open.delete(id);
}, true);
$("#mtList").addEventListener("change", async (e) => {
  const c = e.target.closest("[data-mid]"), el = e.target; if (!c || !el.name || !el.closest("[data-mtset]")) return;
  let v = el.type === "checkbox" ? el.checked : el.value;
  if (el.type === "number" || ["contract_id", "period_start_month", "nk_category_id"].includes(el.name)) v = v === "" ? null : Number(v);
  let body = { [el.name]: v };
  if (el.name === "assign") { const [pid, uid] = v.split(":"); body = { property_id: pid ? Number(pid) : null, unit_id: uid ? Number(uid) : null }; }
  try { await api(`/api/meters/${c.dataset.mid}`, { method: "PUT", body: JSON.stringify(body) }); toast("Gespeichert"); renderMeters(); }
  catch (err) { toast(err.message, true); }
});
$("#mtList").addEventListener("click", async (e) => {
  const c = e.target.closest("[data-mid]"); if (!c) return;
  const id = c.dataset.mid, m = MT.data.meters.find((x) => String(x.id) === id);
  try {
    if (e.target.hasAttribute("data-mtread")) {
      const r = await api(`/api/meters/${id}/read`, { method: "POST" }); toast(`Stand ${mtNum(r.value, 2)} ${m.unit}`); renderMeters();
    } else if (e.target.hasAttribute("data-mtadd")) {
      const r = await openDialog({ title: "Zählerstand erfassen", sub: `${m.name} – z.B. abgelesen oder von der Rechnung`, label: `Stand in ${m.unit}`, ok: "Speichern" });
      if (!r || r.value === "") return;
      const d = await openDialog({ title: "Datum", label: "TT.MM.JJJJ (leer = jetzt)", value: new Date().toLocaleDateString("de-CH"), ok: "Speichern" });
      if (!d) return;
      const [dd, mm, yy] = (d.value || "").split(".");
      const iso = yy ? `${yy.length === 2 ? "20" + yy : yy}-${mm.padStart(2, "0")}-${dd.padStart(2, "0")}T12:00:00` : null;
      await api(`/api/meters/${id}/readings`, { method: "POST", body: JSON.stringify({ value: Number(String(r.value).replace("'", "").replace(",", ".")), date: iso }) });
      toast("Zählerstand gespeichert"); renderMeters();
    } else if (e.target.hasAttribute("data-mttest")) {
      const t = await api("/api/meters/test", { method: "POST", body: JSON.stringify(m) });
      toast(t.ok ? `Quelle funktioniert: ${mtNum(t.value, 3)} ${m.unit}` : `Quelle nicht lesbar: ${t.error}`, !t.ok);
    } else if (e.target.hasAttribute("data-mtdel")) {
      if (!confirm("Zähler mit allen Ablesungen löschen?")) return;
      await api(`/api/meters/${id}`, { method: "DELETE" }); renderMeters();
    }
  } catch (err) { toast(err.message, true); }
});
document.querySelectorAll("[data-mtnew]").forEach((b) => (b.onclick = async () => {
  const presets = {
    ams: { name: "Strom", kind: "strom", unit: "kWh", source: "ams", json_path: "ic", active: 1 },
    shelly: { name: "Wasser", kind: "wasser", unit: "m³", source: "http", url: "http://<shelly-ip>/rpc/Input.GetStatus?id=2", json_path: "counts.total",
      factor: 0.001, active: 0, note: "Shelly-Wasserzähler kommt noch" },
    ha: { name: "Zähler", kind: "strom", unit: "kWh", source: "homeassistant", active: 1 },
    manual: { name: "Zähler", kind: "wasser", unit: "m³", source: "manuell", active: 1 },
  };
  try { const m = await api("/api/meters", { method: "POST", body: JSON.stringify(presets[b.dataset.mtnew]) }); MT.open.add(m.id); renderMeters(); }
  catch (err) { toast(err.message, true); }
}));
