"use strict";
/* Einstellungen: Rubriken mit Seitennavigation, jede Änderung wird sofort gespeichert. */

const SET = { panel: null, status: null, openVeh: new Set() };
try { SET.panel = localStorage.getItem("setPanel"); } catch {}

const panelAllowed = (name) => !S.me?.users_enabled ? name !== "account" : S.me.is_admin ? true : name === "account";
const setPanelsEls = () => [...document.querySelectorAll(".set-panel")].filter((p) => panelAllowed(p.dataset.panel));
const relTime = (iso) => {
  if (!iso) return null;
  const d = new Date(iso), days = Math.floor((Date.now() - d) / 86400000);
  const hm = d.toLocaleTimeString("de-CH", { hour: "2-digit", minute: "2-digit" });
  return days <= 0 && d.toDateString() === new Date().toDateString() ? `heute ${hm}` : days < 2 ? `gestern ${hm}` : d.toLocaleDateString("de-CH");
};
const selOpts = (items, cur, empty) => (empty != null ? `<option value="">${esc(empty)}</option>` : "") +
  items.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(cur ?? "") ? "selected" : ""}>${esc(l)}</option>`).join("");

async function saveSetting(obj, msg = "Gespeichert") {
  try {
    S.settings = await api("/api/settings", { method: "PUT", body: JSON.stringify(obj) });
    if (Object.keys(obj).some((k) => k.endsWith("_name"))) S.meta = await api("/api/meta?refresh=true");
    toast(msg);
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------ Navigation
function buildSetNav() {
  const warn = SET.status?.warnings > 0;
  const q = ($("#setSearch").value || "").trim().toLowerCase();
  $("#setLinks").innerHTML = setPanelsEls().map((p) => {
    const hit = !q || (p.dataset.title + " " + p.textContent).toLowerCase().includes(q);
    return `<a href="#" data-p="${p.dataset.panel}" class="${p.dataset.panel === SET.panel ? "active" : ""} ${hit ? "" : "dim"}">
      <span><span class="ico">${p.dataset.ico}</span>${esc(p.dataset.title)}</span>${p.dataset.panel === "status" && warn ? `<span class="set-dot" title="Etwas fehlt"></span>` : ""}</a>`;
  }).join("");
  $("#setMobile").innerHTML = setPanelsEls().map((p) => `<option value="${p.dataset.panel}" ${p.dataset.panel === SET.panel ? "selected" : ""}>${p.dataset.ico} ${esc(p.dataset.title)}${p.dataset.panel === "status" && warn ? " •" : ""}</option>`).join("");
}
async function showSetPanel(name) {
  if (!setPanelsEls().some((p) => p.dataset.panel === name)) name = panelAllowed("status") ? "status" : "account";
  SET.panel = name;
  try { localStorage.setItem("setPanel", name); } catch {}
  document.querySelectorAll(".set-panel").forEach((p) => (p.hidden = p.dataset.panel !== name));
  buildSetNav();
  const f = { status: setStatus, budget: setBudget, docs: setDocs, bank: setBank, plan: setPlan, vehicles: setVehicles,
    contracts: setContracts, realestate: setRealestate, data: setData, users: setUsers, account: setAccount }[name];
  try { await f(); } catch (e) { toast(e.message, true); }
}
async function renderSettings() {
  buildSetNav();
  await showSetPanel(SET.panel || "status");
  if (panelAllowed("status") && SET.panel !== "status" && !SET.status) api("/api/status").then((s) => { SET.status = s; buildSetNav(); }).catch(() => {});
}
$("#setLinks").addEventListener("click", (e) => { const a = e.target.closest("[data-p]"); if (a) { e.preventDefault(); showSetPanel(a.dataset.p); } });
$("#setMobile").onchange = (e) => showSetPanel(e.target.value);
$("#setSearch").oninput = () => {
  buildSetNav();
  const q = $("#setSearch").value.trim().toLowerCase();
  const cur = setPanelsEls().find((p) => p.dataset.panel === SET.panel);
  if (q && cur && !(cur.dataset.title + " " + cur.textContent).toLowerCase().includes(q)) {
    const first = setPanelsEls().find((p) => (p.dataset.title + " " + p.textContent).toLowerCase().includes(q));
    if (first) showSetPanel(first.dataset.panel);
  }
};
document.addEventListener("click", (e) => {
  const g = e.target.closest("[data-gosettings]"); if (!g) return;
  e.preventDefault(); SET.panel = g.dataset.gosettings; setView("settings");
});

// ------------------------------------------------------------ Verbindung & Status
async function setStatus() {
  $("#setStatus").innerHTML = `<div class="muted small">Prüfe Verbindungen …</div>`;
  const s = SET.status = await api("/api/status");
  buildSetNav();
  const pl = s.paperless, tc = s.traccar;
  const check = (c) => `<div class="st-row"><span>${esc(c.label)}</span>${c.ok ? `<span class="st-ok">✓ vorhanden</span>`
    : `<span><span class="st-wa">fehlt</span> <button data-create="${c.create}" data-name="${esc(S.settings[c.key])}">anlegen</button></span>`}</div>`;
  const dev = (d) => {
    const age = d.last_update ? (Date.now() - new Date(d.last_update)) / 86400000 : null;
    return `<div class="st-row"><span>${esc(d.name)}</span>${d.last_update ? `<span class="${age > 7 ? "st-wa" : "muted"}">${age > 7 ? `seit ${relTime(d.last_update)} keine Meldung` : `letzte Meldung ${relTime(d.last_update)}`}</span>` : `<span class="muted">noch nie gemeldet</span>`}</div>`;
  };
  $("#setStatus").classList.remove("muted", "small");
  $("#setStatus").innerHTML = `
    <div class="st-grid">
      <div class="card"><div class="cardhead"><h3>Paperless</h3>${pl.ok ? `<span class="st-ok">● verbunden</span>` : `<span class="st-bad">● nicht erreichbar</span>`}</div>
        <div class="st-row"><span>Server</span><span class="muted">${esc(pl.url)}</span></div>
        ${pl.ok ? pl.checks.map(check).join("") + (pl.documents_with_amount != null ? `<div class="st-row"><span>Dokumente mit Betrag</span><span class="muted">${fmtN(pl.documents_with_amount)}</span></div>` : "")
          : `<div class="alert" style="margin:8px 0">${esc(pl.error || "")}</div>`}
      </div>
      <div class="card"><div class="cardhead"><h3>Traccar</h3>${!tc.configured ? `<span class="muted">nicht eingerichtet</span>` : tc.ok ? `<span class="st-ok">● verbunden</span>` : `<span class="st-bad">● nicht erreichbar</span>`}</div>
        ${!tc.configured ? `<p class="muted small">Für Kilometer pro Fahrzeug <code>TRACCAR_URL</code> und Zugangsdaten in der <code>.env</code> setzen.</p>`
          : `<div class="st-row"><span>Server</span><span class="muted">${esc(tc.url)}</span></div>` + (tc.ok ? tc.devices.map(dev).join("") || `<div class="muted small">Keine Geräte.</div>` : `<div class="alert" style="margin:8px 0">${esc(tc.error || "")}</div>`)}
      </div>
    </div>
    <div class="card"><h3>App</h3>
      <div class="st-row"><span>Version</span><span class="muted">${esc(s.version)}</span></div>
      <div class="st-row"><span>Letzter Kontoauszug eingelesen</span><span class="muted">${s.last_statement ? `${relTime(s.last_statement.parsed_at)} · ${esc(s.last_statement.title || "")}` : "noch keiner"}</span></div>
      <div class="st-row"><span>Gespeichert</span><span class="muted">${fmtN(s.counts.transactions)} Buchungen · ${s.counts.rules} Regeln · ${s.counts.vehicles} Fahrzeuge · ${s.counts.properties} Häuser · ${s.counts.contracts} Verträge</span></div>
      <div class="st-row"><span>Letztes Backup</span><span>${s.last_backup ? `<span class="muted">${relTime(s.last_backup)}</span>` : `<span class="st-wa">noch nie</span>`} <a class="btnlink" href="/api/backup" data-backup>Backup herunterladen</a></span></div>
      <div class="envnote">Adressen, Token und Passwörter stehen in der <code>.env</code> auf dem Server. Alles andere stellst du hier ein – Änderungen werden sofort gespeichert.</div>
    </div>`;
}
$("#setStatus").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-create]");
  if (b) {
    b.disabled = true;
    try { S.meta = await api("/api/setup/create", { method: "POST", body: JSON.stringify({ kind: b.dataset.create, name: b.dataset.name }) }); toast("In Paperless angelegt"); await setStatus(); }
    catch (err) { toast(err.message, true); b.disabled = false; }
  }
  if (e.target.closest("[data-backup]")) setTimeout(() => { SET.status = null; }, 500);
});

// ------------------------------------------------------------ Budget
function drawCatSettings() {
  const q = $("#tagSearch").value.trim().toLowerCase();
  const match = (t) => !q || t.name.toLowerCase().includes(q);
  const cats = S.settings.category_tag_ids;
  const on = cats.map(tag).filter(Boolean).filter(match);
  const off = S.meta.tags.filter((t) => !cats.includes(t.id) && match(t)).sort((a, b) => a.name.localeCompare(b.name, "de"));
  const row = (t, active, i) => `<div class="cs-row ${active ? "" : "off"}" data-id="${t.id}">
      <input type="checkbox" class="cs-on" ${active ? "checked" : ""}>
      <span class="cs-name"><span class="dot" style="background:${esc(t.color)}"></span>${esc(t.name)}</span>
      <span class="cs-budget"><input type="number" step="10" min="0" placeholder="Budget/Monat" value="${S.budgets[t.id] ?? ""}"></span>
      <span class="cs-move"><button data-mv="-1" ${i === 0 ? "disabled" : ""}>↑</button><button data-mv="1" ${i === on.length - 1 ? "disabled" : ""}>↓</button></span>
    </div>`;
  $("#catSettings").innerHTML = (on.length ? on.map((t, i) => row(t, true, i)).join("") : `<div class="empty">Noch keine Kategorie gewählt.</div>`) +
    (off.length ? `<div class="cs-sep">Weitere Tags</div>` + off.map((t) => row(t, false, -1)).join("") : "");
}
const chipsHtml = (items, ids, key) => items.map((x) => `<label class="chip ${ids.includes(x.id) ? "on" : ""}"><input type="checkbox" data-skey="${key}" value="${x.id}" ${ids.includes(x.id) ? "checked" : ""}>${esc(x.name)}</label>`).join("")
  || `<span class="muted small">keine vorhanden</span>`;
function setBudget() {
  drawCatSettings();
  $("#exclTags").innerHTML = chipsHtml(S.meta.tags, S.settings.exclude_tag_ids || [], "exclude_tag_ids");
  $("#setReasons").value = (S.settings.exclude_reasons || []).join("\n");
}
$("#tagSearch").oninput = drawCatSettings;
$("#catSettings").addEventListener("change", async (e) => {
  const r = e.target.closest(".cs-row"); if (!r) return; const id = Number(r.dataset.id);
  if (e.target.classList.contains("cs-on")) {
    const cats = S.settings.category_tag_ids;
    await saveSetting({ category_tag_ids: e.target.checked ? [...cats, id] : cats.filter((x) => x !== id) });
    drawCatSettings();
  } else if (e.target.type === "number") {
    try { S.budgets = await api("/api/budgets", { method: "PUT", body: JSON.stringify({ budgets: { [id]: e.target.value === "" ? null : Number(e.target.value) } }) }); toast("Budget gespeichert"); }
    catch (err) { toast(err.message, true); }
  }
});
$("#catSettings").addEventListener("click", async (e) => {
  const mv = e.target.dataset.mv; if (!mv) return;
  const cats = [...S.settings.category_tag_ids];
  const id = Number(e.target.closest(".cs-row").dataset.id), i = cats.indexOf(id), j = i + Number(mv);
  if (j < 0 || j >= cats.length) return;
  [cats[i], cats[j]] = [cats[j], cats[i]];
  await saveSetting({ category_tag_ids: cats }, "Reihenfolge gespeichert"); drawCatSettings();
});
// alle Chip-Gruppen: Liste der markierten IDs sofort speichern
$("#setPanels").addEventListener("change", async (e) => {
  const key = e.target.dataset.skey; if (!key) return;
  e.target.closest(".chip").classList.toggle("on", e.target.checked);
  const ids = [...e.target.closest(".chips").querySelectorAll(`input[data-skey="${key}"]:checked`)].map((x) => Number(x.value));
  await saveSetting({ [key]: ids });
});
const lines = (v) => v.split("\n").map((x) => x.trim()).filter(Boolean);
$("#setReasons").onchange = (e) => saveSetting({ exclude_reasons: lines(e.target.value) });

// ------------------------------------------------------------ Belege & Erfassen
function setDocs() {
  const cur = S.settings.amount_field_name;
  const fields = S.meta.custom_fields || [];
  const opts = fields.filter((f) => f.data_type === "monetary").map((f) => [f.name, f.name]);
  if (!opts.some(([v]) => v === cur)) opts.unshift([cur, `${cur} (fehlt in Paperless)`]);
  $("#setAmountField").innerHTML = selOpts(opts, cur);
  $("#reviewTypes").innerHTML = chipsHtml(S.meta.document_types, S.settings.review_document_type_ids || [], "review_document_type_ids");
  $("#reviewTags").innerHTML = chipsHtml(S.meta.tags, S.settings.review_tag_ids || [], "review_tag_ids");
}
$("#setAmountField").onchange = (e) => saveSetting({ amount_field_name: e.target.value });

// ------------------------------------------------------------ Bank & Abgleich
async function setBank() {
  $("#stTypes").innerHTML = chipsHtml(S.meta.document_types, S.settings.statement_document_type_ids || [], "statement_document_type_ids");
  $("#stTags").innerHTML = chipsHtml(S.meta.tags, S.settings.statement_tag_ids || [], "statement_tag_ids");
  $("#matchWindow").value = S.settings.match_window_days || 60;
  const tagOpts = (cur) => { const o = S.meta.tags.map((t) => [t.name, t.name]); if (!o.some(([v]) => v === cur)) o.unshift([cur, `${cur} (wird beim ersten Treffer angelegt)`]); return o; };
  const dateFields = (cur) => { const o = (S.meta.custom_fields || []).filter((f) => f.data_type === "date").map((f) => [f.name, f.name]); if (!o.some(([v]) => v === cur)) o.unshift([cur, `${cur} (wird beim ersten Treffer angelegt)`]); return o; };
  $("#setPaidTag").innerHTML = selOpts(tagOpts(S.settings.paid_tag_name), S.settings.paid_tag_name);
  $("#setPaidField").innerHTML = selOpts(dateFields(S.settings.paid_field_name), S.settings.paid_field_name);
  drawRules(await api("/api/rules"));
}
$("#matchWindow").onchange = (e) => saveSetting({ match_window_days: Number(e.target.value) || 60 });
$("#setPaidTag").onchange = (e) => saveSetting({ paid_tag_name: e.target.value });
$("#setPaidField").onchange = (e) => saveSetting({ paid_field_name: e.target.value });

// ------------------------------------------------------------ Vormerkungen & Lohn
function setPlan() {
  const cur = S.settings.order_tag_name;
  const o = S.meta.tags.map((t) => [t.name, t.name]);
  const missing = !o.some(([v]) => v === cur);
  if (missing) o.unshift([cur, `${cur} (fehlt in Paperless)`]);
  $("#setOrderTag").innerHTML = selOpts(o, cur);
  $("#setOrderTagCreate").hidden = !missing;
  $("#setExpectDays").value = S.settings.order_expect_days || 30;
  $("#psTypes").innerHTML = chipsHtml(S.meta.document_types, S.settings.payslip_document_type_ids || [], "payslip_document_type_ids");
  $("#psTags").innerHTML = chipsHtml(S.meta.tags, S.settings.payslip_tag_ids || [], "payslip_tag_ids");
}
$("#setOrderTag").onchange = async (e) => { await saveSetting({ order_tag_name: e.target.value }); setPlan(); };
$("#setOrderTagCreate").onclick = async () => {
  try { S.meta = await api("/api/setup/create", { method: "POST", body: JSON.stringify({ kind: "tag", name: S.settings.order_tag_name }) }); toast("Tag angelegt"); setPlan(); }
  catch (e) { toast(e.message, true); }
};
$("#setExpectDays").onchange = (e) => saveSetting({ order_expect_days: Number(e.target.value) || 30 });

// ------------------------------------------------------------ Fahrzeuge
async function setVehicles() {
  if (!S.tcDevices) S.tcDevices = await api("/api/traccar/devices").catch((e) => { S.tcError = e.message; return []; });
  drawVehicleCards();
  $("#fuelCorr").innerHTML = chipsHtml(S.meta.correspondents, S.settings.fuel_correspondent_ids || [], "fuel_correspondent_ids");
  $("#fuelTags").innerHTML = chipsHtml(S.meta.tags, S.settings.fuel_tag_ids || [], "fuel_tag_ids");
  $("#parkCorr").innerHTML = chipsHtml(S.meta.correspondents, S.settings.parking_correspondent_ids || [], "parking_correspondent_ids");
  $("#parkTags").innerHTML = chipsHtml(S.meta.tags, S.settings.parking_tag_ids || [], "parking_tag_ids");
}
function drawVehicleCards() {
  const devs = S.tcDevices || [];
  const devName = (id) => devs.find((d) => d.id === id)?.name;
  const card = (v) => {
    const dev = devs.find((d) => d.id === v.traccar_device_id);
    const devOpts = [...devs.map((d) => [d.id, d.name])];
    if (v.traccar_device_id && !dev) devOpts.push([v.traccar_device_id, `Gerät ${v.traccar_device_id}`]);
    return `<details class="vcard" data-vid="${v.id}" ${SET.openVeh.has(v.id) ? "open" : ""}>
      <summary><span><b>${v.energy === "Elektro" ? "🔌" : "🚗"} ${esc(v.name)}</b> <span class="muted small">${esc(v.energy)}${v.traccar_device_id ? ` · Traccar: ${esc(devName(v.traccar_device_id) || "Gerät " + v.traccar_device_id)}` : ""} · ${v.km_source === "gps" ? "km aus GPS" : "Traccar-Bericht"}${v.plates ? " · " + esc(v.plates) : ""}${v.active ? "" : " · inaktiv"}</span></span></summary>
      <div class="vb">
        <div class="field"><div class="lbl"><b>Name / Antrieb</b></div><div class="row"><input name="name" value="${esc(v.name)}"><select name="energy">${selOpts(ENERGIES.map((x) => [x, x]), v.energy)}</select></div></div>
        <div class="field"><div class="lbl"><b>Traccar-Gerät</b><span>${dev?.last_update ? `letzte Meldung ${relTime(dev.last_update)}` : S.tcError ? esc(S.tcError) : ""}</span></div><div><select name="traccar_device_id">${selOpts(devOpts, v.traccar_device_id, "– kein Gerät –")}</select></div></div>
        <div class="field"><div class="lbl"><b>Kilometer</b><span>GPS hilft, wenn der Kilometerzähler des Geräts falsch ist</span></div><div><select name="km_source">${selOpts([["report", "Traccar-Bericht"], ["gps", "aus GPS berechnen"]], v.km_source || "report")}</select></div></div>
        <div class="field"><div class="lbl"><b>Paperless-Tag</b><span>Service, Versicherung, Reparatur …</span></div><div><select name="tag_id">${selOpts(S.meta.tags.map((t) => [t.id, t.name]), v.tag_id, "– kein Tag –")}</select></div></div>
        <div class="field"><div class="lbl"><b>Tankkarten</b><span>Endziffern genügen, mehrere mit Komma</span></div><div><input name="card_keys" value="${esc(v.card_keys || "")}" placeholder="z.B. 4321, 8765"></div></div>
        <div class="field"><div class="lbl"><b>Kennzeichen / Badge-Nr.</b><span>für Parkingpay; mehrere mit Komma – auch die Parkingpay-Badge-Nr. fürs Parkhaus</span></div><div><input name="plates" value="${esc(v.plates || "")}" placeholder="z.B. AG 123456, 118200000000"></div></div>
        <div class="row" style="justify-content:space-between;margin-top:8px"><label class="check small"><input type="checkbox" name="active" ${v.active ? "checked" : ""}> aktiv</label><button class="linkbtn danger" data-vdel>Fahrzeug löschen</button></div>
      </div></details>`;
  };
  $("#vehSettings").innerHTML = (S.tcError && !devs.length ? `<div class="alert" style="margin:8px 0">Traccar: ${esc(S.tcError)} <button class="linkbtn" id="tcRetry">erneut versuchen</button></div>` : "")
    + (S.vehicles.map(card).join("") || `<div class="empty">Noch kein Fahrzeug.</div>`);
}
$("#vehSettings").addEventListener("toggle", (e) => {
  const d = e.target.closest?.("[data-vid]"); if (!d) return;
  const id = Number(d.dataset.vid); d.open ? SET.openVeh.add(id) : SET.openVeh.delete(id);
}, true);
$("#vehSettings").addEventListener("change", async (e) => {
  const d = e.target.closest("[data-vid]"); if (!d) return;
  const q = (n) => d.querySelector(`[name=${n}]`);
  if (!q("name").value.trim()) { toast("Name angeben", true); return; }
  const body = { name: q("name").value.trim(), energy: q("energy").value, traccar_device_id: q("traccar_device_id").value ? Number(q("traccar_device_id").value) : null,
    tag_id: q("tag_id").value ? Number(q("tag_id").value) : null, card_keys: q("card_keys").value || null, active: q("active").checked, km_source: q("km_source").value,
    plates: q("plates").value.trim() || null };
  try { await api(`/api/vehicles/${d.dataset.vid}`, { method: "PUT", body: JSON.stringify(body) }); S.vehicles = await api("/api/vehicles"); drawVehicleCards(); toast("Gespeichert"); }
  catch (err) { toast(err.message, true); }
});
$("#vehSettings").addEventListener("click", async (e) => {
  if (e.target.id === "tcRetry") { S.tcDevices = null; S.tcError = null; return setVehicles(); }
  if (!e.target.hasAttribute("data-vdel")) return;
  const d = e.target.closest("[data-vid]");
  if (!confirm("Fahrzeug löschen? Zuordnungen von Buchungen und Regeln werden entfernt.")) return;
  try { await api(`/api/vehicles/${d.dataset.vid}`, { method: "DELETE" }); S.vehicles = await api("/api/vehicles"); drawVehicleCards(); toast("Gelöscht"); }
  catch (err) { toast(err.message, true); }
});
$("#vehAdd").onclick = async () => {
  try {
    const v = await api("/api/vehicles", { method: "POST", body: JSON.stringify({ name: "Neues Fahrzeug", energy: "Benzin", active: true, km_source: "report" }) });
    S.vehicles = await api("/api/vehicles"); SET.openVeh.add(v.id); drawVehicleCards();
    const inp = document.querySelector(`[data-vid="${v.id}"] [name=name]`); if (inp) { inp.focus(); inp.select(); }
  } catch (err) { toast(err.message, true); }
};

// ------------------------------------------------------------ Verträge
function setContracts() {
  $("#setNoticeDays").value = S.settings.contract_notice_days ?? 120;
  $("#setBadgeDays").value = S.settings.contract_badge_days ?? 30;
  $("#setCtCats").value = (S.settings.contract_categories || []).join("\n");
  $("#setLoanCats").value = (S.settings.loan_categories || []).join("\n");
  $("#setAboCats").value = (S.settings.abo_categories || []).join("\n");
}
$("#setNoticeDays").onchange = (e) => saveSetting({ contract_notice_days: Number(e.target.value) || 120 });
$("#setBadgeDays").onchange = (e) => saveSetting({ contract_badge_days: Number(e.target.value) || 30 });
$("#setCtCats").onchange = (e) => saveSetting({ contract_categories: lines(e.target.value) });
$("#setLoanCats").onchange = (e) => saveSetting({ loan_categories: lines(e.target.value) });
$("#setAboCats").onchange = (e) => saveSetting({ abo_categories: lines(e.target.value) });

// ------------------------------------------------------------ Immobilien & Projekte
async function setRealestate() {
  $("#setNkTag").value = S.settings.nk_tag_name || "";
  const [props, projects] = await Promise.all([api("/api/re/properties"), api("/api/re/projects")]);
  $("#setProps").classList.remove("muted", "small");
  $("#setProps").innerHTML = props.length ? props.map((p) => `<div class="st-row"><span><b>${esc(p.name)}</b> <span class="muted small">${esc(p.address || "")}</span></span>
      <span><a href="#" data-goprop="${p.id}" data-sub="setup">Einrichtung</a> · <a href="#" data-goprop="${p.id}" data-sub="statement">Abrechnung</a></span></div>`).join("")
    : `<div class="muted small">Noch kein Haus – unter <a href="#" data-goto="realestate">Immobilien</a> anlegen.</div>`;
  $("#setProjects").classList.remove("muted", "small");
  $("#setProjects").innerHTML = projects.length ? projects.map((p) => `<div class="st-row"><span><b>${esc(p.name)}</b> <span class="muted small">${esc(p.status || "")}${p.budget ? " · Budget " + fmt(p.budget) : ""}</span></span>
      <a href="#" data-goproj="${p.id}">öffnen</a></div>`).join("")
    : `<div class="muted small">Noch keine Projekte – unter <a href="#" data-goto="projects">Projekte</a> anlegen.</div>`;
}
$("#setNkTag").onchange = (e) => saveSetting({ nk_tag_name: e.target.value.trim() || "Nebenkostenabrechnung" });
$("#setPanels").addEventListener("click", (e) => {
  const p = e.target.closest("[data-goprop]"), j = e.target.closest("[data-goproj]");
  if (p) { e.preventDefault(); RE.pid = Number(p.dataset.goprop); RE.sub = p.dataset.sub; RE.year = null; setView("realestate"); }
  if (j) { e.preventDefault(); RE.prId = Number(j.dataset.goproj); setView("projects"); }
});

// ------------------------------------------------------------ Daten & Wartung
function setData() {
  $("#setLastBackup").textContent = S.settings.last_backup ? `zuletzt ${relTime(S.settings.last_backup)}` : "noch nie";
  const y = new Date().getFullYear();
  if (!$("#setExportYear").options.length) $("#setExportYear").innerHTML = [0, 1, 2, 3, 4].map((i) => `<option>${y - i}</option>`).join("");
  document.querySelectorAll("[data-export]").forEach((a) => (a.href = `/api/export/${a.dataset.export}?year=${$("#setExportYear").value}`));
}
$("#setExportYear").onchange = setData;
$("#setBackup").onclick = () => setTimeout(async () => { S.settings = await api("/api/settings"); SET.status = null; setData(); }, 1500);
$("#setRestore").onchange = async (e) => {
  const f = e.target.files[0]; if (!f) return;
  if (!confirm(`Backup „${f.name}“ einspielen? Alle Daten der App werden durch das Backup ersetzt. Die aktuelle Datenbank wird vorher gesichert.`)) { e.target.value = ""; return; }
  try {
    const r = await fetch("/api/restore", { method: "POST", body: f, headers: { "Content-Type": "application/octet-stream" } });
    const j = await r.json(); if (!r.ok) throw new Error(j.detail || r.statusText);
    toast("Backup eingespielt – lade neu …"); setTimeout(() => location.reload(), 1200);
  } catch (err) { toast(err.message, true); e.target.value = ""; }
};
$("#setRefreshMeta").onclick = async () => { await api("/api/maintenance/refresh", { method: "POST" }); S.meta = await api("/api/meta?refresh=true"); toast("Paperless-Daten neu geladen"); };
$("#setRefreshKm").onclick = async () => { await api("/api/traccar/refresh", { method: "POST" }); toast("Kilometer werden neu geladen"); };


// ------------------------------------------------------------ Benutzer & Rechte (nur Admin) / Mein Konto
const LVL = [["none", "–"], ["view", "ansehen"], ["edit", "bearbeiten"]];
async function setUsers() {
  const me = S.me || {};
  if (!me.users_enabled) {
    $("#setUsers").innerHTML = `<div class="card"><h3>Benutzerverwaltung einschalten</h3>
      <p class="muted small">Im Moment ist die App ohne Anmeldung offen. Lege zuerst <b>deinen eigenen Zugang als Administrator</b> an – danach
        meldet sich jeder mit Benutzername und Passwort an, und du kannst weitere Benutzer mit eingeschränkten Rechten anlegen.</p>
      <form id="firstAdmin" class="form" style="max-width:420px">
        <label>Name<input name="name" placeholder="z.B. Peter" required></label>
        <label>Benutzername<input name="username" autocomplete="username" required></label>
        <label>Passwort (mind. 8 Zeichen)<input name="password" type="password" autocomplete="new-password" minlength="8" required></label>
        <label>Passwort wiederholen<input name="password2" type="password" autocomplete="new-password" minlength="8" required></label>
        <div class="row"><button class="primary">Administrator anlegen und anmelden</button></div>
      </form></div>`;
    $("#firstAdmin").onsubmit = async (e) => {
      e.preventDefault(); const f = e.target;
      if (f.password.value !== f.password2.value) { toast("Die Passwörter stimmen nicht überein", true); return; }
      try { await api("/api/users", { method: "POST", body: JSON.stringify({ name: f.name.value, username: f.username.value, password: f.password.value }) });
        toast("Administrator angelegt"); setTimeout(() => location.reload(), 600); }
      catch (err) { toast(err.message, true); }
    };
    return;
  }
  const users = await api("/api/users");
  const areas = me.areas || [];
  const sel = (u, a) => `<select data-perm="${a}" ${u.is_admin ? "disabled" : ""}>${LVL.map(([k, l]) => `<option value="${k}" ${(u.is_admin ? "edit" : (u.perms || {})[a] || "none") === k ? "selected" : ""}>${l}</option>`).join("")}</select>`;
  $("#setUsers").innerHTML = `<div class="card"><div class="cardhead"><h3>Benutzer & Rechte</h3><button id="userAdd">+ Benutzer</button></div>
    <p class="muted small">Pro Bereich: <b>–</b> = nicht sichtbar, <b>ansehen</b> = nur lesen, <b>bearbeiten</b> = ändern. Administratoren dürfen alles,
      inkl. Einstellungen, Benutzer und Backup. Die Rechte prüft der Server bei jeder Anfrage. Ohne Recht auf „Einnahmen &amp; Lohn“ sind Einnahmen auch in der Übersicht ausgeblendet.</p>
    <div class="tablewrap"><table class="etable users"><thead><tr><th>Name</th><th>Benutzer</th><th>Admin</th>${areas.map(([, l]) => `<th style="min-width:110px">${esc(l.split(" (")[0])}</th>`).join("")}<th>aktiv</th><th></th></tr></thead>
    <tbody>${users.map((u) => `<tr data-uid="${u.id}">
      <td><input data-uf="name" value="${esc(u.name || "")}"></td><td><input data-uf="username" value="${esc(u.username)}"></td>
      <td><input type="checkbox" data-uf="is_admin" ${u.is_admin ? "checked" : ""}></td>
      ${areas.map(([a]) => `<td>${sel(u, a)}</td>`).join("")}
      <td><input type="checkbox" data-uf="active" ${u.active ? "checked" : ""}></td>
      <td class="row"><button class="linkbtn" data-upw>Passwort</button>${me.user && me.user.id === u.id ? "" : `<button class="linkbtn danger" data-udel>löschen</button>`}</td></tr>`).join("")}</tbody></table></div></div>`;
}
$("#setUsers").addEventListener("change", async (e) => {
  const tr = e.target.closest("[data-uid]"); if (!tr) return;
  const id = tr.dataset.uid, el = e.target;
  let body;
  if (el.dataset.perm) {
    const perms = {}; tr.querySelectorAll("[data-perm]").forEach((s) => (perms[s.dataset.perm] = s.value)); body = { perms };
  } else if (el.dataset.uf) body = { [el.dataset.uf]: el.type === "checkbox" ? el.checked : el.value };
  else return;
  try { await api(`/api/users/${id}`, { method: "PUT", body: JSON.stringify(body) }); toast("Gespeichert"); if (el.dataset.uf === "is_admin") setUsers(); }
  catch (err) { toast(err.message, true); setUsers(); }
});
$("#setUsers").addEventListener("click", async (e) => {
  if (e.target.id === "userAdd") {
    const n = await openDialog({ title: "Neuer Benutzer", label: "Name", ok: "Weiter" }); if (!n || !n.value) return;
    const u = await openDialog({ title: "Benutzername", label: "zum Anmelden", value: n.value.toLowerCase().replace(/[^a-z0-9._-]/g, ""), ok: "Weiter" }); if (!u || !u.value) return;
    const p = await openDialog({ title: "Passwort", label: "mind. 8 Zeichen – dem Benutzer mitteilen, er kann es unter „Konto“ ändern", ok: "Anlegen" }); if (!p || !p.value) return;
    try { await api("/api/users", { method: "POST", body: JSON.stringify({ name: n.value, username: u.value, password: p.value, perms: { budget: "view" } }) });
      toast("Benutzer angelegt – jetzt Rechte festlegen"); setUsers(); }
    catch (err) { toast(err.message, true); }
    return;
  }
  const tr = e.target.closest("[data-uid]"); if (!tr) return;
  try {
    if (e.target.hasAttribute("data-upw")) {
      const p = await openDialog({ title: "Neues Passwort", label: "mind. 8 Zeichen", ok: "Setzen" }); if (!p || !p.value) return;
      await api(`/api/users/${tr.dataset.uid}`, { method: "PUT", body: JSON.stringify({ password: p.value }) }); toast("Passwort gesetzt – der Benutzer wird abgemeldet");
    } else if (e.target.hasAttribute("data-udel")) {
      if (!confirm("Benutzer löschen?")) return;
      await api(`/api/users/${tr.dataset.uid}`, { method: "DELETE" }); setUsers();
    }
  } catch (err) { toast(err.message, true); }
});

function setAccount() {
  const me = S.me || {};
  const lbl = { none: "kein Zugriff", view: "ansehen", edit: "bearbeiten" };
  $("#myPerms").innerHTML = me.is_admin ? "Administrator – alle Bereiche bearbeiten, Einstellungen und Benutzer verwalten."
    : (me.areas || []).map(([a, l]) => `${esc(l)}: <b>${lbl[(me.perms || {})[a] || "none"]}</b>`).join("<br>");
}
$("#pwForm").onsubmit = async (e) => {
  e.preventDefault(); const f = e.target;
  if (f.new.value !== f.new2.value) { toast("Die neuen Passwörter stimmen nicht überein", true); return; }
  try { await api("/api/me/password", { method: "POST", body: JSON.stringify({ old: f.old.value, new: f.new.value }) }); f.reset(); toast("Passwort geändert"); }
  catch (err) { toast(err.message, true); }
};
$("#logoutBtn").onclick = async (e) => { e.preventDefault(); try { await api("/api/logout", { method: "POST" }); } catch {} location.href = "/login.html"; };
