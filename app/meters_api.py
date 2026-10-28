"""Zähler (Strom, Wasser, Gas, Wärme): Zählerstände automatisch erfassen und die nächste Abrechnung hochrechnen.

Quellen
  ams            AMS-Leser (amsleser.no / AmsToMqttBridge): GET http://<ip>/data.json, Feld "ic" = Bezug kumuliert (kWh)
  http           beliebiges JSON, z.B. Shelly (Gen2: /rpc/Input.GetStatus?id=2 -> "counts.total" Impulse, Faktor 0.001 = m³)
  homeassistant  Sensor in Home Assistant (HOMEASSISTANT_URL / HOMEASSISTANT_TOKEN in der .env)
  manuell        Zählerstände von Hand

Zuordnung
  Zähler gehören einem Haus (= allgemein, z.B. Allgemeinstrom) oder einer Wohnung. In der Nebenkostenabrechnung erscheint
  der Allgemein-Zähler bei seiner Kategorie in Klammern als „(erwartet CHF …)“ – abgerechnet wird weiterhin nach den Rechnungen.

Prognose für die laufende Abrechnungsperiode
  Verbrauch bisher = letzter Stand − Stand zu Periodenbeginn (Stand der letzten Rechnung oder erste Ablesung)
  Rest der Periode = Verbrauch derselben Zeit im Vorjahr (falls Ablesungen vorhanden), sonst Tagesdurchschnitt × Resttage
  Rechnung ≈ Verbrauch × Tarif + Grundgebühr × Monate;  Saldo = Rechnung − Akonto (bezahlt + noch fällig, aus dem Vertrag)
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response

from . import contracts_api as ct
from . import main as core

router = APIRouter()
HA_URL = os.environ.get("HOMEASSISTANT_URL", "").rstrip("/")
HA_TOKEN = os.environ.get("HOMEASSISTANT_TOKEN", "")
POLL_MINUTES = int(os.environ.get("METER_POLL_MINUTES", "60"))
KINDS = {"strom": ("kWh", "⚡"), "wasser": ("m³", "💧"), "gas": ("kWh", "🔥"), "waerme": ("kWh", "♨️")}

with core.db() as _con:
    _con.executescript(
        """
        CREATE TABLE IF NOT EXISTS meters (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'strom', unit TEXT,
            source TEXT NOT NULL DEFAULT 'manuell', url TEXT, json_path TEXT, ha_entity TEXT,
            factor REAL NOT NULL DEFAULT 1, value_offset REAL NOT NULL DEFAULT 0,
            tariff REAL, base_fee REAL, contract_id INTEGER, period_start_month INTEGER NOT NULL DEFAULT 1,
            start_reading REAL, start_date TEXT, active INTEGER NOT NULL DEFAULT 1, note TEXT,
            last_error TEXT, last_try REAL
        );
        CREATE TABLE IF NOT EXISTS meter_readings (
            meter_id INTEGER NOT NULL, ts TEXT NOT NULL, value REAL NOT NULL, source TEXT,
            PRIMARY KEY (meter_id, ts)
        );
        """
    )
    _cols = {r[1] for r in _con.execute("PRAGMA table_info(meters)")}
    for _c in ("property_id INTEGER", "nk_category_id INTEGER", "unit_id INTEGER"):
        if _c.split()[0] not in _cols:
            _con.execute(f"ALTER TABLE meters ADD COLUMN {_c}")

FIELDS = ["name", "kind", "unit", "source", "url", "json_path", "ha_entity", "factor", "value_offset", "tariff", "base_fee",
          "contract_id", "period_start_month", "start_reading", "start_date", "active", "note",
          "property_id", "unit_id", "nk_category_id"]


# ---------------------------------------------------------------- Ablesen
def _json_path(data, path: str):
    cur = data
    for part in (path or "").split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(part)
    return cur


async def fetch_value(m: dict) -> float:
    src = m["source"]
    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as cl:
        if src in ("ams", "http"):
            url = (m.get("url") or "").strip()
            if not url:
                raise ValueError("Adresse fehlt")
            if not url.startswith("http"):
                url = "http://" + url
            if src == "ams" and not url.rstrip("/").endswith(".json"):
                url = url.rstrip("/") + "/data.json"
            r = await cl.get(url)
            r.raise_for_status()
            raw = _json_path(r.json(), m.get("json_path") or ("ic" if src == "ams" else ""))
        elif src == "homeassistant":
            if not HA_URL or not HA_TOKEN:
                raise ValueError("HOMEASSISTANT_URL / HOMEASSISTANT_TOKEN fehlen in der .env")
            r = await cl.get(f"{HA_URL}/api/states/{m.get('ha_entity')}", headers={"Authorization": f"Bearer {HA_TOKEN}"})
            if r.status_code == 404:
                raise ValueError(f"Sensor {m.get('ha_entity')} gibt es in Home Assistant nicht")
            r.raise_for_status()
            raw = r.json().get("state")
        else:
            raise ValueError("manueller Zähler")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"kein Zahlenwert ({raw!r})")
    return round(val * float(m.get("factor") or 1) + float(m.get("value_offset") or 0), 4)


def _store(meter_id: int, value: float, source: str, ts: Optional[datetime] = None):
    ts = (ts or datetime.now()).replace(microsecond=0).isoformat()
    with core.db() as con:
        con.execute("INSERT OR REPLACE INTO meter_readings(meter_id, ts, value, source) VALUES (?, ?, ?, ?)", (meter_id, ts, value, source))


async def read_meter(m: dict, force: bool = False) -> Optional[float]:
    if m["source"] == "manuell" or not m["active"]:
        return None
    if not force and m.get("last_try") and time.time() - m["last_try"] < 10 * 60:
        return None
    err = None
    val = None
    try:
        val = await fetch_value(m)
        _store(m["id"], val, m["source"])
    except Exception as e:  # Zähler nicht erreichbar: merken, nicht abbrechen
        err = f"{type(e).__name__}: {e}"[:200]
    with core.db() as con:
        con.execute("UPDATE meters SET last_error = ?, last_try = ? WHERE id = ?", (err, time.time(), m["id"]))
    return val


async def poll_all():
    with core.db() as con:
        meters = [dict(r) for r in con.execute("SELECT * FROM meters WHERE active = 1 AND source != 'manuell'")]
    for m in meters:
        await read_meter(m)


async def _poller():
    while True:
        try:
            await poll_all()
        except Exception:
            pass
        await asyncio.sleep(max(5, POLL_MINUTES) * 60)


def start_poller():
    task = asyncio.get_event_loop().create_task(_poller())
    _TASKS.add(task)


_TASKS: set = set()


# ---------------------------------------------------------------- Prognose
def _period(m: dict, today: date) -> tuple[date, date]:
    sm = int(m.get("period_start_month") or 1)
    start = date(today.year if today.month >= sm else today.year - 1, sm, 1)
    return start, ct._add_months(start, 12) - timedelta(days=1)


def _readings(con, mid: int, since: Optional[str] = None) -> list[tuple[datetime, float]]:
    q = "SELECT ts, value FROM meter_readings WHERE meter_id = ?" + (" AND ts >= ?" if since else "") + " ORDER BY ts"
    return [(datetime.fromisoformat(r["ts"]), r["value"]) for r in con.execute(q, (mid, since) if since else (mid,))]


def _near(rs: list[tuple[datetime, float]], d: datetime, tol_days: int = 6) -> Optional[tuple[datetime, float]]:
    best = min(rs, key=lambda r: abs((r[0] - d).total_seconds()), default=None)
    return best if best and abs((best[0] - d).days) <= tol_days else None


def consumption(m: dict, d0: date, d1: date) -> dict:
    """Verbrauch zwischen Beginn d0 und Ende d1 (inkl.) für die Nebenkosten. Fehlt der Endstand (Periode läuft noch),
    wird mit dem Tagesdurchschnitt hochgerechnet; fehlt der Anfangsstand, ist das Ergebnis unvollständig."""
    a_dt, b_dt = datetime(d0.year, d0.month, d0.day), datetime(d1.year, d1.month, d1.day) + timedelta(days=1)
    with core.db() as con:
        rs = _readings(con, m["id"], (d0 - timedelta(days=60)).isoformat())
    out = {"meter_id": m["id"], "name": m["name"], "unit": m.get("unit") or KINDS.get(m["kind"], ("",))[0], "tariff": m.get("tariff"),
           "base_fee": m.get("base_fee"), "complete": False, "estimated": False}
    start = _near(rs, a_dt, 3)
    if not start and m.get("start_reading") is not None and m.get("start_date"):
        sd = date.fromisoformat(m["start_date"])
        if abs((sd - d0).days) <= 3:
            start = (datetime(sd.year, sd.month, sd.day), float(m["start_reading"]))
    if not start:
        out["missing"] = f"kein Zählerstand um den {d0.strftime('%d.%m.%Y')}"
        return out
    end = _near(rs, b_dt, 3)
    if not end:
        last = rs[-1] if rs else None
        if not last or last[0] <= start[0] or last[0] > b_dt:
            out["missing"] = f"kein Zählerstand um den {d1.strftime('%d.%m.%Y')}"
            return out
        days = (last[0] - start[0]).total_seconds() / 86400
        if days < 7:
            out["missing"] = "zu wenig Ablesungen für eine Hochrechnung"
            return out
        daily = (last[1] - start[1]) / days
        end = (b_dt, last[1] + daily * (b_dt - last[0]).total_seconds() / 86400)
        out["estimated"] = True
    qty = end[1] - start[1]
    if qty < 0:
        out["missing"] = "Endstand kleiner als Anfangsstand"
        return out
    months, x = 0, d0
    while ct._add_months(x, 1) - timedelta(days=1) <= d1:
        months, x = months + 1, ct._add_months(x, 1)
    months += ((d1 - x).days + 1) / 30.4375 if x <= d1 else 0
    amount = None
    if m.get("tariff") is not None:
        amount = round(qty * float(m["tariff"]) + float(m.get("base_fee") or 0) * months, 2)
    out.update(complete=amount is not None, start=[start[0].isoformat(), round(start[1], 3)], end=[end[0].isoformat(), round(end[1], 3)],
               quantity=round(qty, 2), months=round(months, 2), amount=amount)
    if amount is None:
        out["missing"] = "Tarif fehlt beim Zähler"
    return out


def akonto(contract_id: Optional[int], p0: date, p1: date, today: date) -> Optional[dict]:
    if not contract_id:
        return None
    with core.db() as con:
        c = con.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,)).fetchone()
        if not c:
            return None
        c = dict(c)
        versions = ct._versions(con, contract_id)
        pays = [dict(r) for r in con.execute(
            "SELECT t.date, t.amount FROM contract_payments cp JOIN transactions t ON t.id = cp.tx_id "
            "WHERE cp.contract_id = ? AND t.date >= ? AND t.date <= ? ORDER BY t.date", (contract_id, p0.isoformat(), p1.isoformat()))]
    paid = round(-sum(p["amount"] for p in pays), 2)
    paid_months = {p["date"][:7] for p in pays}
    anchor = ct._anchor(c, versions)
    remaining, n = 0.0, 0
    mth = date(today.year, today.month, 1)
    while mth <= p1:
        if ct.is_due(c, versions, mth) and mth.strftime("%Y-%m") not in paid_months:
            due = date(mth.year, mth.month, min(anchor.day if anchor else 1, 28))
            if due >= today:
                v = ct._version_at(versions, mth)
                remaining += ct._period_cost(c, v, None) or 0
                n += 1
        mth = ct._add_months(mth, 1)
    return {"contract": c["name"], "paid": paid, "payments": len(pays), "remaining": round(remaining, 2), "remaining_count": n,
            "total": round(paid + remaining, 2)}


def projection(m: dict, today: Optional[date] = None) -> dict:
    today = today or date.today()
    p0, p1 = _period(m, today)
    with core.db() as con:
        rs = _readings(con, m["id"], (p0 - timedelta(days=400)).isoformat())
    out: dict = {"period": [p0.isoformat(), p1.isoformat()], "status": "keine Daten"}
    if not rs:
        return out
    latest = rs[-1]
    out.update(latest_value=latest[1], latest_ts=latest[0].isoformat())
    start = None
    if m.get("start_reading") is not None and m.get("start_date"):
        sd = date.fromisoformat(m["start_date"])
        if abs((sd - p0).days) <= 45:
            start = (datetime(sd.year, sd.month, sd.day), float(m["start_reading"]))
    if start is None:
        inside = [r for r in rs if r[0].date() >= p0]
        start = _near(rs, datetime(p0.year, p0.month, p0.day), 10) or (inside[0] if inside else None)
    if not start:
        return out
    so_far = latest[1] - start[1]
    days = (latest[0] - start[0]).total_seconds() / 86400
    end_dt = datetime(p1.year, p1.month, p1.day, 23, 59)
    rest_days = max(0.0, (end_dt - latest[0]).total_seconds() / 86400)
    out.update(start_value=start[1], start_ts=start[0].isoformat(), so_far=round(so_far, 2), days=round(days, 1), rest_days=round(rest_days, 1))
    if days < 3 or so_far < 0:
        out["status"] = "zu wenig Daten" if so_far >= 0 else "Zählerstand kleiner als zu Beginn – Startwert prüfen"
        return out
    daily = so_far / days
    # Vorjahr: gleicher Restzeitraum ein Jahr früher
    py_a, py_b = _near(rs, latest[0] - timedelta(days=365)), _near(rs, end_dt - timedelta(days=365))
    if py_a and py_b and py_b[1] >= py_a[1] and rest_days > 1:
        rest, method = py_b[1] - py_a[1], "wie Vorjahr"
    else:
        rest, method = daily * rest_days, "Tagesdurchschnitt hochgerechnet"
    total = so_far + rest
    months = 12
    cost = None
    if m.get("tariff") is not None:
        cost = round(total * float(m["tariff"]) + float(m.get("base_fee") or 0) * months, 2)
    ak = akonto(m.get("contract_id"), p0, p1, today)
    out.update(status="ok", daily=round(daily, 3), rest=round(rest, 2), total=round(total, 1), method=method, cost=cost, akonto=ak,
               balance=round(cost - ak["total"], 2) if cost is not None and ak else None,
               progress=round(min(1.0, days / ((end_dt - start[0]).total_seconds() / 86400)), 3))
    # Verbrauch pro Monat (für das Diagramm)
    monthly = []
    mth = date(p0.year, p0.month, 1)
    while mth <= today and mth <= p1:
        nxt = ct._add_months(mth, 1)
        a = _near(rs, datetime(mth.year, mth.month, 1), 4)
        b = _near(rs, datetime(nxt.year, nxt.month, 1), 4) if nxt <= today else latest
        monthly.append({"month": mth.strftime("%Y-%m"), "value": round(b[1] - a[1], 2) if a and b and b[1] >= a[1] else None,
                        "partial": nxt > today})
        mth = nxt
    out["monthly"] = monthly
    return out


# ---------------------------------------------------------------- Endpunkte
def _meter(con, mid: int) -> dict:
    r = con.execute("SELECT * FROM meters WHERE id = ?", (mid,)).fetchone()
    if not r:
        raise HTTPException(404, "Zähler nicht gefunden")
    return dict(r)


@router.get("/api/meters")
async def meters_overview():
    with core.db() as con:
        meters = [dict(r) for r in con.execute("SELECT * FROM meters ORDER BY active DESC, kind, name")]
    for m in meters:
        await read_meter(m)
    with core.db() as con:
        meters = [dict(r) for r in con.execute("SELECT * FROM meters ORDER BY active DESC, kind, name")]
        contracts = [dict(r) for r in con.execute("SELECT id, name, kind FROM contracts WHERE active = 1 ORDER BY name")]
        props = [dict(r) for r in con.execute("SELECT id, name FROM properties ORDER BY name")]
        units = [dict(r) for r in con.execute("SELECT id, property_id, name FROM units ORDER BY sort, name")]
        cats = [dict(r) for r in con.execute("SELECT id, property_id, grp, name FROM cost_categories ORDER BY grp, sort, name")]
    meters.sort(key=lambda m: (m["property_id"] is None, m["property_id"] or 0, m["unit_id"] is not None, not m["active"], m["name"]))
    for m in meters:
        m["unit"] = m["unit"] or KINDS.get(m["kind"], ("", ""))[0]
        m["icon"] = KINDS.get(m["kind"], ("", "📟"))[1]
        m["projection"] = projection(m) if m["active"] else None
    return {"meters": meters, "contracts": contracts, "ha_configured": bool(HA_URL and HA_TOKEN), "poll_minutes": POLL_MINUTES,
            "properties": props, "units": units, "nk_categories": cats}


@router.post("/api/meters", status_code=201)
def add_meter(body: dict = Body(...)):
    data = {k: body[k] for k in FIELDS if k in body}
    data.setdefault("name", "Zähler")
    data.setdefault("unit", KINDS.get(data.get("kind", "strom"), ("kWh",))[0])
    with core.db() as con:
        cur = con.execute(f"INSERT INTO meters ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})", list(data.values()))
        return _meter(con, cur.lastrowid)


@router.put("/api/meters/{mid}")
async def update_meter(mid: int, body: dict = Body(...)):
    data = {k: (None if body[k] == "" else body[k]) for k in FIELDS if k in body}
    for k in ("active",):
        if k in data:
            data[k] = int(bool(data[k]))
    if data:
        with core.db() as con:
            _meter(con, mid)
            con.execute(f"UPDATE meters SET {', '.join(k + ' = ?' for k in data)}, last_try = NULL WHERE id = ?", [*data.values(), mid])
    with core.db() as con:
        m = _meter(con, mid)
    if {"source", "url", "json_path", "ha_entity", "factor", "value_offset", "active"} & set(data):
        await read_meter(m, force=True)
    return {"ok": True}


@router.delete("/api/meters/{mid}", status_code=204)
def delete_meter(mid: int):
    with core.db() as con:
        con.execute("DELETE FROM meter_readings WHERE meter_id = ?", (mid,))
        con.execute("DELETE FROM meters WHERE id = ?", (mid,))
    return Response(status_code=204)


@router.post("/api/meters/{mid}/read")
async def read_now(mid: int):
    with core.db() as con:
        m = _meter(con, mid)
    if m["source"] == "manuell":
        raise HTTPException(400, "Manueller Zähler – Stand von Hand erfassen")
    val = await read_meter(m, force=True)
    with core.db() as con:
        err = con.execute("SELECT last_error FROM meters WHERE id = ?", (mid,)).fetchone()[0]
    if val is None:
        raise HTTPException(502, f"Zähler nicht lesbar: {err}")
    return {"value": val}


@router.post("/api/meters/test")
async def test_source(body: dict = Body(...)):
    """Quelle ausprobieren, ohne zu speichern."""
    m = {"source": body.get("source"), "url": body.get("url"), "json_path": body.get("json_path"), "ha_entity": body.get("ha_entity"),
         "factor": body.get("factor") or 1, "value_offset": body.get("value_offset") or 0}
    try:
        return {"ok": True, "value": await fetch_value(m)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}


@router.post("/api/meters/{mid}/readings", status_code=201)
def add_reading(mid: int, body: dict = Body(...)):
    """Zählerstand von Hand (z.B. Wasserzähler ablesen, Stand auf der Rechnung)."""
    try:
        val = float(body["value"])
        ts = datetime.fromisoformat(body.get("date") or datetime.now().isoformat())
    except (KeyError, ValueError):
        raise HTTPException(400, "Datum und Zählerstand angeben")
    with core.db() as con:
        _meter(con, mid)
    _store(mid, val, "manuell", ts)
    return {"ok": True}


@router.get("/api/meters/{mid}/readings")
def list_readings(mid: int, days: int = 60):
    since = (datetime.now() - timedelta(days=days)).isoformat()
    with core.db() as con:
        return [dict(r) for r in con.execute("SELECT ts, value, source FROM meter_readings WHERE meter_id = ? AND ts >= ? ORDER BY ts DESC LIMIT 500",
                                             (mid, since))]
