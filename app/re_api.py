"""API: Liegenschaften (Nebenkosten, Mietzinseingänge) und Projekte."""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse, Response

from . import main as core
from . import realestate as rx

router = APIRouter()

with core.db() as _con:
    _con.executescript(
        """
        CREATE TABLE IF NOT EXISTS heating_stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT, property_id INTEGER NOT NULL,
            date TEXT NOT NULL, liters REAL NOT NULL DEFAULT 0, price REAL,   -- CHF pro 100 Liter
            note TEXT
        );
        """
    )
    for _t, _c, _ty in [("properties", "oil_category_id", "INTEGER"), ("nk_statements", "interim", "INTEGER")]:
        if _c not in {r["name"] for r in _con.execute(f"PRAGMA table_info({_t})")}:
            _con.execute(f"ALTER TABLE {_t} ADD COLUMN {_c} {_ty}")
FILES_DIR = Path(os.environ.get("FILES_DIR", str(Path(core.DB_PATH).parent / "abrechnungen")))

# ---------------------------------------------------------------- generisches CRUD
ENT: dict[str, tuple[str, list[str]]] = {
    "properties": ("properties", ["name", "address", "owner_name", "owner_address", "owner_iban", "start_month", "room_pct",
                                  "room_key_id", "water_key_id", "tag_id", "exclude_from_budget", "intro", "closing", "due_days", "oil_category_id"]),
    "units": ("units", ["property_id", "name", "area", "owner_occupied", "sort"]),
    "keys": ("dist_keys", ["property_id", "name", "type"]),
    "categories": ("cost_categories", ["property_id", "grp", "name", "key_id", "heating", "unit_label", "sort", "keywords"]),
    "tenancies": ("tenancies", ["unit_id", "tenant_name", "tenant_address", "salutation", "start", "end", "rent", "akonto", "match_text"]),
    "fixed-costs": ("fixed_costs", ["property_id", "category_id", "unit_id", "name", "amount", "interval", "valid_from", "valid_to"]),
    "manual-costs": ("manual_costs", ["property_id", "category_id", "unit_id", "date", "description", "amount", "quantity"]),
    "heating-stock": ("heating_stock", ["property_id", "date", "liters", "price", "note"]),
    "projects": ("projects", ["name", "budget", "tag_id", "status", "exclude_from_budget", "start", "end", "note"]),
}


def _clean(fields: list[str], body: dict) -> dict:
    out = {}
    for k in fields:
        if k in body:
            v = body[k]
            if isinstance(v, bool):
                v = int(v)
            if v == "":
                v = None
            out[k] = v
    return out


def _row(table: str, rid: int) -> dict:
    with core.db() as con:
        r = con.execute(f"SELECT * FROM {table} WHERE id = ?", (rid,)).fetchone()
    if not r:
        raise HTTPException(404, "Nicht gefunden")
    return dict(r)


@router.get("/api/re/{ent}")
def re_list(ent: str, property_id: Optional[int] = None):
    table, fields = ENT.get(ent) or (None, None)
    if not table:
        raise HTTPException(404)
    with core.db() as con:
        if property_id is not None and "property_id" in fields:
            return [dict(r) for r in con.execute(f"SELECT * FROM {table} WHERE property_id = ? ORDER BY id", (property_id,))]
        return [dict(r) for r in con.execute(f"SELECT * FROM {table} ORDER BY id")]


@router.post("/api/re/{ent}", status_code=201)
def re_create(ent: str, body: dict = Body(...)):
    table, fields = ENT.get(ent) or (None, None)
    if not table:
        raise HTTPException(404)
    data = _clean(fields, body)
    if not data:
        raise HTTPException(400, "Keine Daten")
    with core.db() as con:
        cur = con.execute(f"INSERT INTO {table} ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})", list(data.values()))
        rid = cur.lastrowid
    return _row(table, rid)


@router.put("/api/re/{ent}/{rid}")
def re_update(ent: str, rid: int, body: dict = Body(...)):
    table, fields = ENT.get(ent) or (None, None)
    if not table:
        raise HTTPException(404)
    data = _clean(fields, body)
    if data:
        with core.db() as con:
            con.execute(f"UPDATE {table} SET {', '.join(k + ' = ?' for k in data)} WHERE id = ?", [*data.values(), rid])
    return _row(table, rid)


@router.delete("/api/re/{ent}/{rid}", status_code=204)
def re_delete(ent: str, rid: int):
    table, _ = ENT.get(ent) or (None, None)
    if not table:
        raise HTTPException(404)
    with core.db() as con:
        if ent == "properties":
            units = [r[0] for r in con.execute("SELECT id FROM units WHERE property_id = ?", (rid,))]
            keys = [r[0] for r in con.execute("SELECT id FROM dist_keys WHERE property_id = ?", (rid,))]
            for u in units:
                con.execute("DELETE FROM tenancies WHERE unit_id = ?", (u,))
            for k in keys:
                con.execute("DELETE FROM key_shares WHERE key_id = ?", (k,))
            for t in ("units", "dist_keys", "cost_categories", "fixed_costs", "manual_costs", "heating_stock"):
                con.execute(f"DELETE FROM {t} WHERE property_id = ?", (rid,))
            con.execute("DELETE FROM allocations WHERE target_type = 'property' AND target_id = ?", (rid,))
        elif ent == "units":
            con.execute("DELETE FROM tenancies WHERE unit_id = ?", (rid,))
            con.execute("DELETE FROM key_shares WHERE unit_id = ?", (rid,))
        elif ent == "keys":
            con.execute("DELETE FROM key_shares WHERE key_id = ?", (rid,))
            con.execute("UPDATE cost_categories SET key_id = NULL WHERE key_id = ?", (rid,))
        elif ent == "categories":
            con.execute("UPDATE allocations SET category_id = NULL WHERE category_id = ?", (rid,))
        elif ent == "projects":
            con.execute("DELETE FROM allocations WHERE target_type = 'project' AND target_id = ?", (rid,))
        elif ent == "tenancies":
            con.execute("DELETE FROM rent_payments WHERE tenancy_id = ?", (rid,))
        con.execute(f"DELETE FROM {table} WHERE id = ?", (rid,))
    return Response(status_code=204)


@router.put("/api/re/keys/{kid}/shares")
def set_shares(kid: int, body: dict = Body(...)):
    shares = body.get("shares") or {}
    with core.db() as con:
        con.execute("DELETE FROM key_shares WHERE key_id = ?", (kid,))
        for uid, pct in shares.items():
            if pct not in (None, ""):
                con.execute("INSERT INTO key_shares(key_id, unit_id, percent) VALUES (?, ?, ?)", (kid, int(uid), float(pct)))
    return {"key_id": kid, "shares": shares}


@router.get("/api/properties-full")
def properties_full():
    with core.db() as con:
        props = [dict(r) for r in con.execute("SELECT * FROM properties ORDER BY name")]
        for p in props:
            pid = p["id"]
            p["units"] = [dict(r) for r in con.execute("SELECT * FROM units WHERE property_id = ? ORDER BY sort, id", (pid,))]
            p["keys"] = [dict(r) for r in con.execute("SELECT * FROM dist_keys WHERE property_id = ? ORDER BY id", (pid,))]
            for k in p["keys"]:
                k["shares"] = {r["unit_id"]: r["percent"] for r in con.execute("SELECT * FROM key_shares WHERE key_id = ?", (k["id"],))}
            p["categories"] = [dict(r) for r in con.execute(
                "SELECT * FROM cost_categories WHERE property_id = ? ORDER BY sort, grp, name", (pid,))]
            uids = [u["id"] for u in p["units"]] or [-1]
            p["tenancies"] = [dict(r) for r in con.execute(
                f"SELECT * FROM tenancies WHERE unit_id IN ({','.join('?' * len(uids))}) ORDER BY start DESC", uids)]
            p["fixed_costs"] = [dict(r) for r in con.execute("SELECT * FROM fixed_costs WHERE property_id = ? ORDER BY id", (pid,))]
    return props


@router.post("/api/properties/{pid}/defaults")
def property_defaults(pid: int):
    """Verteilschlüssel und Kostenarten wie in einer typischen Heiz- und Nebenkostenabrechnung anlegen."""
    with core.db() as con:
        if con.execute("SELECT COUNT(*) FROM cost_categories WHERE property_id = ?", (pid,)).fetchone()[0]:
            raise HTTPException(409, "Es gibt bereits Kostenarten für dieses Haus.")
        units = [r[0] for r in con.execute("SELECT id FROM units WHERE property_id = ?", (pid,))]
        keys = {}
        for name, typ in [("Raumheizung", "percent"), ("Warmwasser", "percent"), ("Fläche", "area"), ("Gleichmässig", "equal")]:
            keys[name] = con.execute("INSERT INTO dist_keys(property_id, name, type) VALUES (?, ?, ?)", (pid, name, typ)).lastrowid
            if typ == "percent" and units:
                for u in units:
                    con.execute("INSERT INTO key_shares(key_id, unit_id, percent) VALUES (?, ?, ?)",
                                (keys[name], u, round(100 / len(units), 2)))
        cats = [
            ("Heizkosten", "Heizöl", None, 1, "Liter", 1, "heizöl, heizoel, brennstoff"),
            ("Heizkosten", "Strom Allgemein", None, 1, "kWh", 2, "strom, elektrizität, elektrizitaet"),
            ("Heiznebenkosten", "Kaminfeger", None, 1, "", 3, "kaminfeger"),
            ("Heiznebenkosten", "Feuerungskontrolle", None, 1, "", 4, "feuerungskontrolle"),
            ("Heiznebenkosten", "Wartung Heizung", None, 1, "", 5, "wartung heizung, heizungsservice, brenner"),
            ("Wasser", "Wasser / Abwasser", keys["Fläche"], 0, "m³", 10, "wasser, abwasser"),
            ("Unterhalt & Entsorgung", "Grüngut", keys["Gleichmässig"], 0, "", 20, "grüngut, gruengut"),
            ("Unterhalt & Entsorgung", "Sperrgut / Kehricht", keys["Gleichmässig"], 0, "", 21, "sperrgut, kehricht, entsorgung"),
            ("Unterhalt & Entsorgung", "Facilitymanagement / Hauswart", keys["Gleichmässig"], 0, "", 22, "facility, hauswart"),
        ]
        for grp, name, key, heating, ul, sort, kw in cats:
            con.execute("INSERT INTO cost_categories(property_id, grp, name, key_id, heating, unit_label, sort, keywords) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (pid, grp, name, key, heating, ul, sort, kw))
        con.execute("UPDATE properties SET room_key_id = ?, water_key_id = ? WHERE id = ?", (keys["Raumheizung"], keys["Warmwasser"], pid))
    return properties_full()


# ---------------------------------------------------------------- Zuordnungen
@router.post("/api/allocations", status_code=201)
def add_allocation(body: dict = Body(...)):
    f = ["source_type", "source_id", "target_type", "target_id", "category_id", "unit_id", "percent", "amount", "quantity", "note"]
    data = _clean(f, body)
    if data.get("source_type") not in ("doc", "tx") or data.get("target_type") not in ("property", "project"):
        raise HTTPException(400, "source_type doc|tx und target_type property|project angeben")
    if data.get("amount") is None and data.get("percent") is None:
        data["percent"] = 100
    with core.db() as con:
        rid = con.execute(f"INSERT INTO allocations ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                          list(data.values())).lastrowid
    return _row("allocations", rid)


@router.put("/api/allocations/{aid}")
def update_allocation(aid: int, body: dict = Body(...)):
    data = _clean(["target_type", "target_id", "category_id", "unit_id", "percent", "amount", "quantity", "note"], body)
    if "amount" in data and data["amount"] is not None:
        data["percent"] = None
    elif "percent" in data and data["percent"] is not None:
        data["amount"] = None
    with core.db() as con:
        if data:
            con.execute(f"UPDATE allocations SET {', '.join(k + ' = ?' for k in data)} WHERE id = ?", [*data.values(), aid])
    return _row("allocations", aid)


@router.delete("/api/allocations/{aid}", status_code=204)
def delete_allocation(aid: int):
    with core.db() as con:
        con.execute("DELETE FROM allocations WHERE id = ?", (aid,))
    return Response(status_code=204)


async def _docs_by_ids(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    meta = await core.get_meta()
    fid = core.require_amount_field(meta)
    cats = core.get_settings()["category_tag_ids"]
    out = []
    for k in range(0, len(ids), 50):
        chunk = ids[k:k + 50]
        docs = await core.pl_all("/api/documents/", {"id__in": ",".join(map(str, chunk)), "fields": core.DOC_FIELDS})
        out += core.normalize_all(docs, fid, cats, await core._excl_ctx())
    return out


async def _docs_by_tag(tag_id: int, p0: Optional[date], p1: Optional[date]) -> list[dict]:
    meta = await core.get_meta()
    fid = core.require_amount_field(meta)
    cats = core.get_settings()["category_tag_ids"]
    params: dict[str, Any] = {"tags__id__all": tag_id, "fields": core.DOC_FIELDS, "ordering": "-created"}
    if p0:
        params["created__gte"] = p0.isoformat()
    if p1:
        params["created__lt"] = (p1 + timedelta(days=1)).isoformat()
    return core.normalize_all(await core.pl_all("/api/documents/", params), fid, cats, await core._excl_ctx())


QTY_RE = re.compile(r"(?<![\d.,'’])(\d{1,3}(?:['’]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*(liter|ltr|l\b|kwh|m3|m³)", re.I)


def _guess_quantity(text: str, unit_label: str) -> Optional[float]:
    """Menge (z.B. '2'700 Liter') aus Titel/Zweck lesen, passend zur Einheit der Kostenart."""
    if not unit_label:
        return None
    want = unit_label.lower().replace("³", "3")
    for m in QTY_RE.finditer(text or ""):
        unit = m.group(2).lower().replace("³", "3")
        if (want.startswith("l") and unit.startswith("l")) or unit == want:
            return float(re.sub(r"['’]", "", m.group(1)).replace(",", "."))
    return None


def _guess_category(text: str, categories: list[dict]) -> Optional[int]:
    t = (text or "").lower()
    best, best_len = None, 0
    for c in categories:
        for kw in (c.get("keywords") or "").split(","):
            kw = kw.strip().lower()
            if len(kw) >= 3 and kw in t and len(kw) > best_len:
                best, best_len = c["id"], len(kw)
    return best


async def target_lines(target_type: str, tid: int, p0: Optional[date] = None, p1: Optional[date] = None,
                       categories: Optional[list[dict]] = None) -> list[dict]:
    """Belege und Bankbuchungen, die einer Liegenschaft / einem Projekt zugeordnet sind (explizit oder über Tag)."""
    table = "properties" if target_type == "property" else "projects"
    target = _row(table, tid)
    in_period = lambda d: (not p0 or d >= p0.isoformat()) and (not p1 or d <= p1.isoformat())
    with core.db() as con:
        allocs = [dict(r) for r in con.execute(
            "SELECT * FROM allocations WHERE target_type = ? AND target_id = ?", (target_type, tid))]
        tx_ids = [a["source_id"] for a in allocs if a["source_type"] == "tx"]
        txs = {r["id"]: dict(r) for r in con.execute(
            f"SELECT * FROM transactions WHERE id IN ({','.join('?' * len(tx_ids)) or 'NULL'})", tx_ids)} if tx_ids else {}
    doc_alloc_ids = sorted({a["source_id"] for a in allocs if a["source_type"] == "doc"})
    docs = {d["id"]: d for d in await _docs_by_ids(doc_alloc_ids)}
    tag_docs = await _docs_by_tag(target["tag_id"], p0, p1) if target.get("tag_id") else []
    explicit_docs = set(doc_alloc_ids)
    for d in tag_docs:
        docs.setdefault(d["id"], d)
    await core.attach_purposes(list(docs.values()))
    cats = categories or []
    corr = {c["id"]: c["name"] for c in (await core.get_meta())["correspondents"]}

    lines = []
    for a in allocs:
        if a["source_type"] == "doc":
            d = docs.get(a["source_id"])
            if not d or d["amount"] is None or not in_period(d["date"]):
                continue
            text = f"{corr.get(d['correspondent'], '')} {d['title']} {d.get('purpose') or ''}"
            cat = a["category_id"] or (_guess_category(text, cats) if cats else None)
            lines.append({"key": f"a{a['id']}", "source": "Beleg", "source_type": "doc", "source_id": d["id"], "alloc_id": a["id"],
                          "date": d["date"], "title": d["title"], "purpose": d.get("purpose"), "correspondent": d["correspondent"],
                          "full_amount": d["amount"], "amount": core.alloc_amount(a, d["amount"]), "percent": a["percent"],
                          "fixed_amount": a["amount"], "category_id": cat, "category_guessed": not a["category_id"] and cat is not None,
                          "unit_id": a["unit_id"], "quantity": a["quantity"], "url": d["url"], "implicit": False})
        else:
            t = txs.get(a["source_id"])
            if not t or not in_period(t["date"]):
                continue
            full = -t["amount"]
            cat = a["category_id"] or (_guess_category(f"{t['merchant']} {t['text']}", cats) if cats else None)
            lines.append({"key": f"a{a['id']}", "source": "Bank", "source_type": "tx", "source_id": t["id"], "alloc_id": a["id"],
                          "date": t["date"], "title": t["merchant"] or t["text"][:60], "purpose": "Bankbuchung", "correspondent": None,
                          "full_amount": full, "amount": core.alloc_amount(a, full), "percent": a["percent"], "fixed_amount": a["amount"],
                          "category_id": cat, "category_guessed": not a["category_id"] and cat is not None,
                          "unit_id": a["unit_id"], "quantity": a["quantity"], "url": None, "implicit": False})
    for d in tag_docs:
        if d["id"] in explicit_docs or d["amount"] is None or not in_period(d["date"]):
            continue
        text = f"{corr.get(d['correspondent'], '')} {d['title']} {d.get('purpose') or ''}"
        cat = _guess_category(text, cats) if cats else None
        lines.append({"key": f"t{d['id']}", "source": "Beleg", "source_type": "doc", "source_id": d["id"], "alloc_id": None,
                      "date": d["date"], "title": d["title"], "purpose": d.get("purpose"), "correspondent": d["correspondent"],
                      "full_amount": d["amount"], "amount": d["amount"], "percent": 100, "fixed_amount": None,
                      "category_id": cat, "category_guessed": cat is not None, "unit_id": None, "quantity": None,
                      "url": d["url"], "implicit": True})
    ul = {c["id"]: c.get("unit_label") or "" for c in cats}
    for l in lines:
        if l["source_type"] == "doc" and l.get("quantity") is None and l.get("category_id"):
            q = _guess_quantity(f"{l['title']} {l.get('purpose') or ''}", ul.get(l["category_id"], ""))
            if q:
                l["quantity"], l["quantity_guessed"] = q, True
    lines.sort(key=lambda x: x["date"], reverse=True)
    return lines


# ---------------------------------------------------------------- Nebenkosten
def _load_property(pid: int) -> dict:
    p = next((x for x in properties_full() if x["id"] == pid), None)
    if not p:
        raise HTTPException(404, "Haus nicht gefunden")
    return p


def _period(p: dict, year: int) -> tuple[date, date]:
    return rx.period_for(int(p["start_month"] or 1), year)


async def property_costs(pid: int, year: int, period: Optional[tuple[date, date]] = None) -> tuple[dict, tuple[date, date], list[dict]]:
    p = _load_property(pid)
    p0, p1 = period or _period(p, year)
    await core.get_meta()
    lines = await target_lines("property", pid, p0, p1, p["categories"])
    days_total = (p1 - p0).days + 1
    for f in p["fixed_costs"]:
        v0 = date.fromisoformat(f["valid_from"]) if f["valid_from"] else p0
        v1 = date.fromisoformat(f["valid_to"]) if f["valid_to"] else p1
        days = rx.overlap_days(p0, p1, v0, v1)
        if not days:
            continue
        amount = f["amount"] * (days / days_total) * (12 if f["interval"] == "monat" else 1)
        lines.append({"key": f"f{f['id']}", "source": "Fix", "source_type": "fixed", "source_id": f["id"], "alloc_id": None,
                      "date": max(p0, v0).isoformat(), "title": f["name"] or "Fixkosten",
                      "purpose": f"{f['amount']:.2f} pro {'Monat' if f['interval'] == 'monat' else 'Jahr'}"
                                 + ("" if days == days_total else f" · {days} von {days_total} Tagen"),
                      "full_amount": rx.r2(amount), "amount": rx.r2(amount), "category_id": f["category_id"], "category_guessed": False,
                      "unit_id": f["unit_id"], "quantity": None, "url": None, "implicit": False})
    with core.db() as con:
        for m in con.execute("SELECT * FROM manual_costs WHERE property_id = ? AND date >= ? AND date <= ? ORDER BY date",
                             (pid, p0.isoformat(), p1.isoformat())):
            m = dict(m)
            lines.append({"key": f"m{m['id']}", "source": "Manuell", "source_type": "manual", "source_id": m["id"], "alloc_id": None,
                          "date": m["date"], "title": m["description"] or "Manuelle Kosten", "purpose": None,
                          "full_amount": m["amount"], "amount": m["amount"], "category_id": m["category_id"], "category_guessed": False,
                          "unit_id": m["unit_id"], "quantity": m["quantity"], "url": None, "implicit": False})
    lines.sort(key=lambda x: x["date"], reverse=True)
    return p, (p0, p1), lines


@router.get("/api/properties/{pid}/costs")
async def get_property_costs(pid: int, year: int):
    p, (p0, p1), lines = await property_costs(pid, year)
    return {"period": [p0.isoformat(), p1.isoformat()], "lines": lines,
            "total": rx.r2(sum(l["amount"] for l in lines)),
            "unassigned": sum(1 for l in lines if not l["category_id"])}


def _stay(tenancy: Optional[dict], p0: date, p1: date) -> tuple[date, date]:
    if not tenancy:
        return p0, p1
    s = date.fromisoformat(tenancy["start"])
    e = date.fromisoformat(tenancy["end"]) if tenancy.get("end") else p1
    return s, e


def _months_of(p0: date, p1: date) -> list[str]:
    out, d = [], date(p0.year, p0.month, 1)
    while d <= p1:
        out.append(d.strftime("%Y-%m"))
        d = date(d.year + (d.month == 12), 1 if d.month == 12 else d.month + 1, 1)
    return out


def _month_in_stay(m: str, s: date, e: date) -> bool:
    """Monat zählt (Miete/Akonto fällig), wenn das Mietverhältnis mindestens den halben Monat abdeckt."""
    y, mo = map(int, m.split("-"))
    m0 = date(y, mo, 1)
    m1 = date(y + (mo == 12), 1 if mo == 12 else mo + 1, 1) - timedelta(days=1)
    return rx.overlap_days(m0, m1, s, e) >= ((m1 - m0).days + 1) / 2


def _akonto(tenancy: Optional[dict], p0: date, p1: date) -> tuple[float, float]:
    """(erhalten laut Mietzinseingängen, erwartet laut Mietvertrag)"""
    if not tenancy:
        return 0.0, 0.0
    s, e = _stay(tenancy, p0, p1)
    months = [m for m in _months_of(p0, p1) if _month_in_stay(m, s, e)]
    expected = rx.r2((tenancy["akonto"] or 0) * len(months))
    with core.db() as con:
        rows = con.execute("SELECT month, akonto_part FROM rent_payments WHERE tenancy_id = ?", (tenancy["id"],)).fetchall()
    received = rx.r2(sum(r["akonto_part"] for r in rows if r["month"] in months))
    return received, expected


def _reading(pid: int, d: date, tol: int = 20) -> Optional[dict]:
    """Tankstand-Ablesung am nächsten zum Stichtag (höchstens `tol` Tage daneben)."""
    with core.db() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM heating_stock WHERE property_id = ?", (pid,))]
    best = min(rows, key=lambda r: abs((date.fromisoformat(r["date"]) - d).days), default=None)
    return best if best and abs((date.fromisoformat(best["date"]) - d).days) <= tol else None


def _oil(p: dict, p0: date, p1: date, lines: list[dict]) -> tuple[list[dict], Optional[dict]]:
    """Heizöl nach Verbrauch statt nach Einkauf: Anfangsbestand + Einkäufe − Endbestand."""
    cid = p.get("oil_category_id")
    if not cid:
        return lines, None
    start, end = _reading(p["id"], p0), _reading(p["id"], p1 + timedelta(days=1)) or _reading(p["id"], p1)
    buys = [l for l in lines if l["category_id"] == cid and not l.get("unit_id")]
    info = {"category_id": cid, "start": start, "end": end, "complete": bool(start and end),
            "purchases": {"liters": rx.r2(sum(l.get("quantity") or 0 for l in buys)), "amount": rx.r2(sum(l["amount"] for l in buys)),
                          "count": len(buys)}}
    if not info["complete"]:
        return lines, info
    val = lambda r: rx.r2((r["liters"] or 0) * (r["price"] or 0) / 100)
    start_v, end_v = val(start), val(end)
    liters = rx.r2((start["liters"] or 0) + info["purchases"]["liters"] - (end["liters"] or 0))
    amount = rx.r2(start_v + info["purchases"]["amount"] - end_v)
    info.update(start_value=start_v, end_value=end_v, liters=liters, amount=amount,
                price=rx.r2(amount / liters * 100) if liters else None)
    rest = [l for l in lines if l not in buys]
    rest.append({"key": "oil", "source": "Verbrauch", "source_type": "oil", "source_id": None, "alloc_id": None, "date": p1.isoformat(),
                 "title": "Heizöl-Verbrauch", "purpose": None, "full_amount": amount, "amount": amount, "category_id": cid,
                 "category_guessed": False, "unit_id": None, "quantity": liters, "url": None, "implicit": False})
    return rest, info


def _meters_nk(p: dict, p0: date, p1: date) -> list[dict]:
    """Allgemein-Zähler des Hauses (ohne Wohnung): Verbrauch der Periode × Tarif als Hinweis „erwartet“ – ersetzt nichts."""
    from . import meters_api as mt   # spät importieren (meters_api -> contracts_api -> re_api)
    with core.db() as con:
        meters = [dict(r) for r in con.execute("SELECT * FROM meters WHERE property_id = ? AND unit_id IS NULL AND active = 1", (p["id"],))]
    out = []
    for m in meters:
        c = mt.consumption(m, p0, p1)
        cid = m.get("nk_category_id")
        if not cid and m["kind"] == "strom":   # ohne Angabe: Kategorie mit „Strom“ im Namen
            cid = next((x["id"] for x in p["categories"] if "strom" in x["name"].lower()), None)
        cat = next((x for x in p["categories"] if x["id"] == cid), None)
        c.update(category_id=cid, category=cat["name"] if cat else None, grp=cat["grp"] if cat else None)
        out.append(c)
    return out


async def _statement(pid: int, year: int, tenancy_id: Optional[int], unit_id: Optional[int], akonto: Optional[float],
                     period_from: Optional[str] = None, period_to: Optional[str] = None):
    interim = bool(period_from or period_to)
    period = None
    if interim:
        base = _period(_load_property(pid), year)
        period = (date.fromisoformat(period_from) if period_from else base[0], date.fromisoformat(period_to) if period_to else base[1])
        if period[1] < period[0]:
            raise HTTPException(400, "Ende liegt vor dem Beginn")
    p, (p0, p1), lines = await property_costs(pid, year, period)
    lines, oil = _oil(p, p0, p1, lines)
    meters = _meters_nk(p, p0, p1)
    tenancy = next((t for t in p["tenancies"] if t["id"] == tenancy_id), None) if tenancy_id else None
    uid = tenancy["unit_id"] if tenancy else unit_id
    if not uid or not any(u["id"] == uid for u in p["units"]):
        raise HTTPException(400, "Wohnung oder Mietverhältnis wählen")
    units = [rx.Unit(u["id"], u["name"], u["area"] or 0, bool(u["owner_occupied"])) for u in p["units"]]
    keys = [rx.Key(k["id"], k["name"], k["type"], {int(a): b for a, b in k["shares"].items()}) for k in p["keys"]]
    cats = [rx.Category(c["id"], c["grp"], c["name"], c["key_id"], bool(c["heating"]), c["unit_label"] or "", c["sort"] or 0)
            for c in p["categories"]]
    costs = [rx.Cost(l["category_id"], l["amount"], l["title"], l.get("quantity"), l.get("unit_id"), l["source"],
                     date.fromisoformat(l["date"])) for l in lines if l["category_id"]]
    received, expected = _akonto(tenancy, p0, p1)
    used = akonto if akonto is not None else (received if received else expected)
    st = rx.compute_statement(units=units, keys=keys, categories=cats, costs=costs, unit_id=uid, period=(p0, p1),
                              stay=_stay(tenancy, p0, p1), room_pct=p["room_pct"] or 70, room_key_id=p["room_key_id"],
                              water_key_id=p["water_key_id"], akonto=used)
    for m in meters:     # erwartete Kosten laut Zähler in Klammern hinter die Kategorie
        if m.get("amount") is None or not m.get("category"):
            continue
        tag = f" (erwartet {rx.chf(m['amount'])})"
        for r in st["heating"]["rows"]:
            if r["name"] == m["category"] and r["grp"] == m["grp"]:
                r["name"] += tag
        for g in st["groups"]:
            for r in g["rows"]:
                if g["name"] == m["grp"] and r["name"] == m["category"] and not r.get("direct"):
                    r["name"] += tag
    st.update({"akonto_received": received, "akonto_expected": expected, "interim": interim, "oil": oil, "meters": meters,
               "unassigned": [l for l in lines if not l["category_id"]], "tenancy": tenancy})
    return p, st


@router.get("/api/properties/{pid}/statement")
async def statement_preview(pid: int, year: int, tenancy_id: Optional[int] = None, unit_id: Optional[int] = None,
                            akonto: Optional[float] = None, period_from: Optional[str] = None, period_to: Optional[str] = None):
    _, st = await _statement(pid, year, tenancy_id, unit_id, akonto, period_from, period_to)
    return st


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")[:80]


async def _ensure_tag(name: str) -> int:
    meta = await core.get_meta(force=True)
    t = next((t for t in meta["tags"] if t["name"].lower() == name.lower()), None)
    if t:
        return t["id"]
    res = await core.pl_request("POST", "/api/tags/", json={"name": name, "matching_algorithm": 0, "color": "#6a1b9a"})
    await core.get_meta(force=True)
    return res["id"]


@router.post("/api/properties/{pid}/statement/generate")
async def statement_generate(pid: int, body: dict = Body(...)):
    year = int(body["year"])
    p, st = await _statement(pid, year, body.get("tenancy_id"), body.get("unit_id"), body.get("akonto"),
                             body.get("period_from"), body.get("period_to"))
    tenancy = st["tenancy"]
    unit = next(u for u in p["units"] if u["id"] == st["unit"]["id"])
    inv = date.fromisoformat(body["invoice_date"]) if body.get("invoice_date") else date.today()
    tenant = ({"name": tenancy["tenant_name"], "address": tenancy["tenant_address"], "salutation": tenancy["salutation"]}
              if tenancy else {"name": p["owner_name"] or "", "address": p["owner_address"] or "", "salutation": "Eigenanteil"})
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    p0, p1 = st["period"]
    base = _safe(f"{'Zwischenabrechnung_' + p0 + '_' + p1 if st['interim'] else 'Nebenkosten_' + p0[:4] + '-' + p1[2:4]}_{unit['name']}_{tenant['name']}")
    docx_path = FILES_DIR / f"{base}.docx"
    rx.build_docx(str(docx_path), st, owner={"name": p["owner_name"] or "", "address": p["owner_address"] or "", "iban": p["owner_iban"]},
                  tenant=tenant, prop={"name": p["name"], "address": p["address"] or p["name"]}, invoice_date=inv,
                  due_days=int(p.get("due_days") or 30), intro=p.get("intro"), closing=p.get("closing"))
    pdf_path, warnings = None, []
    try:
        await asyncio.to_thread(subprocess.run, ["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(FILES_DIR),
                                                 str(docx_path)], check=True, capture_output=True, timeout=120)
        pdf_path = FILES_DIR / f"{base}.pdf"
        if not pdf_path.exists():
            pdf_path = None
    except Exception as e:  # LibreOffice fehlt oder Fehler
        warnings.append(f"PDF konnte nicht erzeugt werden ({type(e).__name__}).")
    task = None
    if body.get("upload", True) and pdf_path:
        try:
            tags = [await _ensure_tag(core.get_settings()["nk_tag_name"])] + ([p["tag_id"]] if p.get("tag_id") else [])
            title = (f"Zwischenabrechnung Nebenkosten {p0} – {p1} {unit['name']} – {tenant['name']}" if st["interim"]
                     else f"Nebenkostenabrechnung {p0[:4]}/{p1[:4]} {unit['name']} – {tenant['name']}")
            data = {"title": title, "created": inv.isoformat(), "tags": [str(t) for t in tags]}
            r = await core.client().post("/api/documents/post_document/", data=data,
                                         files={"document": (pdf_path.name, pdf_path.read_bytes(), "application/pdf")})
            if r.status_code >= 400:
                warnings.append(f"Upload zu Paperless fehlgeschlagen ({r.status_code}).")
            else:
                task = r.text.strip('"')
        except Exception as e:
            warnings.append(f"Upload zu Paperless fehlgeschlagen ({e}).")
    with core.db() as con:
        con.execute("INSERT INTO nk_statements(property_id, unit_id, tenancy_id, period_start, period_end, total, akonto, saldo, "
                    "created_at, docx, pdf, paperless_task, interim) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (pid, unit["id"], tenancy["id"] if tenancy else None, p0, p1, st["total"], st["akonto"], st["saldo"],
                     datetime.now().isoformat(timespec="seconds"), docx_path.name, pdf_path.name if pdf_path else None, task,
                     1 if st["interim"] else 0))
    return {"docx": f"/api/files/{docx_path.name}", "pdf": f"/api/files/{pdf_path.name}" if pdf_path else None,
            "paperless_task": task, "warnings": warnings, "total": st["total"], "saldo": st["saldo"]}


@router.get("/api/properties/{pid}/summary")
async def property_summary(pid: int, year: int):
    """Umlegbare Kosten der Periode verteilt auf Mietverhältnisse, Eigennutzung und Leerstand."""
    p = _load_property(pid)
    p0, p1 = _period(p, year)
    rows = []
    for u in p["units"]:
        tens = [t for t in p["tenancies"] if t["unit_id"] == u["id"] and rx.overlap_days(p0, p1, *_stay(t, p0, p1))]
        covered = 0
        for t in tens:
            _, st = await _statement(pid, year, t["id"], None, None)
            covered += st["days"]
            rows.append({"unit_id": u["id"], "unit": u["name"], "type": "Mieter", "name": t["tenant_name"], "tenancy_id": t["id"],
                         "days": st["days"], "total": st["total"], "akonto": st["akonto"], "saldo": st["saldo"]})
        if u["owner_occupied"] and not tens:
            _, st = await _statement(pid, year, None, u["id"], None)
            covered = st["days"]
            rows.append({"unit_id": u["id"], "unit": u["name"], "type": "Eigennutzung", "name": p["owner_name"] or "Eigentümer",
                         "tenancy_id": None, "days": st["days"], "total": st["total"], "akonto": 0, "saldo": st["total"]})
        days_total = (p1 - p0).days + 1
        if covered < days_total:
            _, st = await _statement(pid, year, None, u["id"], None)
            # nur verteilte Kosten; direkt belastete Positionen gehören dem jeweiligen Mieter
            direct = sum(r["unit_amount"] for g in st["groups"] for r in g["rows"] if r.get("direct"))
            share = rx.r2((st["subtotal"] - direct) * (days_total - covered) / days_total)
            rows.append({"unit_id": u["id"], "unit": u["name"], "type": "Leerstand", "name": "ohne Mieter", "tenancy_id": None,
                         "days": days_total - covered, "total": share, "akonto": 0, "saldo": share})
    _, _, lines = await property_costs(pid, year)
    umlegbar = rx.r2(sum(l["amount"] for l in lines if l["category_id"]))
    return {"period": [p0.isoformat(), p1.isoformat()], "rows": rows, "umlegbar": umlegbar,
            "nicht_umlegbar": rx.r2(sum(l["amount"] for l in lines if not l["category_id"])),
            "verteilt": rx.r2(sum(r["total"] for r in rows))}


@router.get("/api/nk-statements")
def nk_history(property_id: int):
    with core.db() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM nk_statements WHERE property_id = ? ORDER BY id DESC", (property_id,))]
    latest = _latest_ids(rows)
    for r in rows:
        r["deletable"] = r["id"] in latest
    return rows


def _latest_ids(rows: list[dict]) -> set:
    """Pro Mietverhältnis (bzw. Wohnung beim Eigenanteil) nur die zuletzt erzeugte Abrechnung."""
    best: dict = {}
    for r in rows:
        key = ("t", r["tenancy_id"]) if r["tenancy_id"] else ("u", r["unit_id"])
        if key not in best or r["id"] > best[key]:
            best[key] = r["id"]
    return set(best.values())


@router.delete("/api/nk-statements/{sid}")
async def nk_delete(sid: int, paperless: bool = False):
    """Abrechnung löschen: Eintrag, Word-/PDF-Datei (falls von keiner anderen Abrechnung genutzt) und optional das Paperless-Dokument."""
    with core.db() as con:
        r = con.execute("SELECT * FROM nk_statements WHERE id = ?", (sid,)).fetchone()
        if not r:
            raise HTTPException(404, "Abrechnung nicht gefunden")
        r = dict(r)
        same = [dict(x) for x in con.execute("SELECT id, tenancy_id, unit_id FROM nk_statements WHERE property_id = ?", (r["property_id"],))]
        if sid not in _latest_ids(same):
            raise HTTPException(409, "Nur die zuletzt erzeugte Abrechnung dieses Mietverhältnisses kann gelöscht werden.")
        con.execute("DELETE FROM nk_statements WHERE id = ?", (sid,))
        others = {x for row in con.execute("SELECT docx, pdf FROM nk_statements") for x in row if x}
    for name in (r["docx"], r["pdf"]):
        if name and name not in others:
            (FILES_DIR / name).unlink(missing_ok=True)
    result = {"ok": True, "paperless": None}
    if paperless:
        doc_id = None
        if r.get("paperless_task"):
            try:
                tasks = await core.pl_request("GET", "/api/tasks/", params={"task_id": r["paperless_task"]})
                tasks = tasks if isinstance(tasks, list) else tasks.get("results", [])
                doc_id = next((t.get("related_document") for t in tasks if t.get("related_document")), None)
            except HTTPException:
                doc_id = None
        if doc_id:
            try:
                await core.pl_request("DELETE", f"/api/documents/{int(doc_id)}/")
                result["paperless"] = "gelöscht"
            except HTTPException as e:
                result["paperless"] = f"nicht gelöscht ({e.detail})"
        else:
            result["paperless"] = "Dokument in Paperless nicht gefunden – bitte dort von Hand löschen"
    return result


@router.get("/api/files/{name}")
def get_file(name: str):
    path = (FILES_DIR / _safe(Path(name).stem)).with_suffix(Path(name).suffix)
    if path.suffix not in (".docx", ".pdf") or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, filename=path.name)


# ---------------------------------------------------------------- Mietzinseingänge
def _kw_match(match_text: str, hay: str) -> bool:
    hay = hay.lower()
    for kw in (match_text or "").split(","):
        terms = [t.strip().lower() for t in kw.split("+") if t.strip()]
        if terms and all(t in hay for t in terms):
            return True
    return False


def _rent_month(d: str) -> str:
    """Mieten werden im Voraus bezahlt: Zahlung ab dem 20. gilt für den Folgemonat."""
    dt = date.fromisoformat(d)
    if dt.day >= 20:
        dt = date(dt.year + (dt.month == 12), 1 if dt.month == 12 else dt.month + 1, 1)
    return dt.strftime("%Y-%m")


def _rent_fits(amount: float, ten: dict) -> bool:
    """Betrag passt zur Monatsmiete (Miete + Akonto) – auch zwei oder drei Monate auf einmal; Toleranz 2 %, mind. CHF 2."""
    exp = (ten["rent"] or 0) + (ten["akonto"] or 0)
    if exp <= 0:
        return False
    return any(abs(amount - k * exp) <= max(2.0, 0.02 * k * exp) for k in (1, 2, 3))


def _in_tenancy(d: str, ten: dict) -> bool:
    s = date.fromisoformat(ten["start"]) - timedelta(days=31)
    e = (date.fromisoformat(ten["end"]) if ten["end"] else date(2999, 1, 1)) + timedelta(days=31)
    return s.isoformat() <= d <= e.isoformat()


def _rent_auto_ok(t, ten: dict) -> bool:
    """Automatisch nur, wenn Stichwort UND Betrag passen und die Zahlung in die Mietdauer fällt."""
    hay = f"{t['merchant'] or ''} {t['text']}"
    return _in_tenancy(t["date"], ten) and _kw_match(ten["match_text"], hay) and _rent_fits(t["amount"], ten)


def _rent_parts(amount: float, ten) -> tuple[float, float]:
    exp = (ten["rent"] or 0) + (ten["akonto"] or 0)
    ak = ten["akonto"] or 0
    akonto_part = rx.r2(amount * ak / exp) if exp else 0
    return rx.r2(amount - akonto_part), akonto_part


def auto_assign_rent(con) -> int:
    tenancies = {r["id"]: dict(r) for r in con.execute("SELECT * FROM tenancies WHERE match_text IS NOT NULL AND match_text != ''")}
    # früher automatisch zugeordnete Buchungen, die die Regeln nicht (mehr) erfüllen, wieder lösen
    for r in con.execute("SELECT rp.tx_id, rp.tenancy_id, t.* FROM rent_payments rp JOIN transactions t ON t.id = rp.tx_id "
                         "WHERE rp.auto = 1 AND rp.tenancy_id != 0").fetchall():
        ten = tenancies.get(r["tenancy_id"])
        if not ten or not _rent_auto_ok(r, ten):
            con.execute("DELETE FROM rent_payments WHERE tx_id = ?", (r["tx_id"],))
    if not tenancies:
        return 0
    taken = {r[0] for r in con.execute("SELECT tx_id FROM rent_payments")}
    n = 0
    for t in con.execute("SELECT * FROM transactions WHERE amount > 0 AND status != 'ignored' ORDER BY date").fetchall():
        if t["id"] in taken:
            continue
        ten = next((x for x in tenancies.values() if _rent_auto_ok(t, x)), None)
        if not ten:
            continue
        rent_part, akonto_part = _rent_parts(t["amount"], ten)
        con.execute("INSERT INTO rent_payments(tx_id, tenancy_id, month, rent_part, akonto_part, auto) VALUES (?, ?, ?, ?, ?, 1)",
                    (t["id"], ten["id"], _rent_month(t["date"]), rent_part, akonto_part))
        n += 1
    return n


@router.get("/api/properties/{pid}/rent")
def rent_overview(pid: int, year: int):
    p = _load_property(pid)
    p0, p1 = _period(p, year)
    months = _months_of(p0, p1)
    today_m = date.today().strftime("%Y-%m")
    with core.db() as con:
        auto_assign_rent(con)
        pays = [dict(r) for r in con.execute(
            "SELECT rp.*, t.date, t.amount, t.merchant, t.text FROM rent_payments rp JOIN transactions t ON t.id = rp.tx_id "
            "WHERE rp.tenancy_id != 0")]
        taken = {r[0] for r in con.execute("SELECT tx_id FROM rent_payments")}
        credits = [dict(r) for r in con.execute(
            "SELECT id, date, amount, merchant, text FROM transactions WHERE amount > 0 AND status != 'ignored' "
            "AND date >= ? AND date <= ? ORDER BY date DESC", ((p0 - timedelta(days=15)).isoformat(), p1.isoformat()))]
    unit_name = {u["id"]: u["name"] for u in p["units"]}
    out = []
    for ten in p["tenancies"]:
        s, e = _stay(ten, p0, p1)
        if rx.overlap_days(p0, p1, s, e) == 0:
            continue
        rows, arrears = [], 0.0
        for m in months:
            due = (ten["rent"] or 0) + (ten["akonto"] or 0) if _month_in_stay(m, s, e) else 0
            mp = [x for x in pays if x["tenancy_id"] == ten["id"] and x["month"] == m]
            paid = rx.r2(sum(x["amount"] for x in mp))
            if m <= today_m:
                arrears += due - paid
            rows.append({"month": m, "due": due, "paid": paid,
                         "payments": [{"tx_id": x["tx_id"], "date": x["date"], "amount": x["amount"], "auto": x["auto"]} for x in mp]})
        bookings = sorted(({"tx_id": x["tx_id"], "date": x["date"], "month": x["month"], "amount": x["amount"], "auto": x["auto"],
                            "text": f"{x['merchant'] or ''} {x['text']}".strip()[:140]}
                           for x in pays if x["tenancy_id"] == ten["id"] and x["month"] in months), key=lambda x: x["date"], reverse=True)
        out.append({"tenancy": ten, "unit_name": unit_name.get(ten["unit_id"]), "months": rows, "bookings": bookings,
                    "due_total": rx.r2(sum(r["due"] for r in rows)), "paid_total": rx.r2(sum(r["paid"] for r in rows)),
                    "arrears": rx.r2(arrears)})
    open_credits = [c for c in credits if c["id"] not in taken][:60]
    for c in open_credits:    # Vorschlag: Betrag passt zu einem Mietverhältnis (Stichwort fehlt oder passt nicht)
        hay = f"{c['merchant'] or ''} {c['text']}"
        fit = [t for t in p["tenancies"] if _in_tenancy(c["date"], t) and _rent_fits(c["amount"], t)]
        if fit:
            kw = [t for t in fit if t.get("match_text") and _kw_match(t["match_text"], hay)]
            c["suggest"] = (kw or fit)[0]["id"]
    return {"months": months, "tenancies": out, "unassigned_credits": open_credits}


@router.post("/api/rent-payments")
def assign_rent(body: dict = Body(...)):
    tx_id, ten_id, month = int(body["tx_id"]), int(body["tenancy_id"]), body.get("month")
    with core.db() as con:
        t = con.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        ten = con.execute("SELECT * FROM tenancies WHERE id = ?", (ten_id,)).fetchone()
        if not t or not ten:
            raise HTTPException(404)
        rent_part, akonto_part = _rent_parts(t["amount"], ten)
        con.execute("INSERT OR REPLACE INTO rent_payments(tx_id, tenancy_id, month, rent_part, akonto_part, auto) VALUES (?, ?, ?, ?, ?, 0)",
                    (tx_id, ten_id, month or _rent_month(t["date"]), rent_part, akonto_part))
    return {"ok": True}


@router.delete("/api/rent-payments/{tx_id}", status_code=204)
def unassign_rent(tx_id: int):
    """Zuordnung lösen; die Buchung wird nicht mehr automatisch als Miete erkannt."""
    with core.db() as con:
        con.execute("INSERT OR REPLACE INTO rent_payments(tx_id, tenancy_id, month, rent_part, akonto_part, auto) "
                    "VALUES (?, 0, '', 0, 0, 0)", (tx_id,))
    return Response(status_code=204)


# ---------------------------------------------------------------- Projekte
@router.get("/api/projects-overview")
async def projects_overview():
    with core.db() as con:
        projects = [dict(r) for r in con.execute("SELECT * FROM projects ORDER BY status, name")]
    await core.get_meta()
    for pr in projects:
        lines = await target_lines("project", pr["id"])
        pr["total"] = rx.r2(sum(l["amount"] for l in lines))
        pr["count"] = len(lines)
    return projects


@router.get("/api/projects/{pid}/items")
async def project_items(pid: int):
    await core.get_meta()
    lines = await target_lines("project", pid)
    return {"project": _row("projects", pid), "lines": lines, "total": rx.r2(sum(l["amount"] for l in lines))}


# ---------------------------------------------------------------- Suche (Belege zum Zuordnen)
@router.get("/api/doc-search")
async def doc_search(q: str, limit: int = 25):
    meta = await core.get_meta()
    fid = core.require_amount_field(meta)
    cats = core.get_settings()["category_tag_ids"]
    data = await core.pl_request("GET", "/api/documents/", params={"query": q, "page_size": limit, "fields": core.DOC_FIELDS})
    docs = core.normalize_all(data.get("results", []), fid, cats, await core._excl_ctx())
    await core.attach_purposes(docs)
    core.attach_allocations(docs, "doc")
    like = f"%{q}%"
    with core.db() as con:
        txs = [dict(r) for r in con.execute(
            "SELECT id, date, amount, merchant, text FROM transactions WHERE amount < 0 AND (merchant LIKE ? OR text LIKE ?) "
            "ORDER BY date DESC LIMIT ?", (like, like, limit))]
    items = [{"kind": "doc", "id": d["id"], "date": d["date"], "title": d["title"], "purpose": d.get("purpose"),
              "amount": d["amount"], "url": d["url"], "allocations": d["allocations"]} for d in docs]
    tx_items = [{"kind": "tx", "id": t["id"], "date": t["date"], "title": t["merchant"] or t["text"][:60], "purpose": "Bankbuchung",
                 "amount": -t["amount"], "url": None} for t in txs]
    core.attach_allocations(tx_items, "tx")
    return items + tx_items
