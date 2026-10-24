"""Paperless Budget – Budget-Web-App auf Basis der Paperless-ngx REST-API.

Das Backend ist ein schlanker Proxy: Der Paperless-API-Token bleibt serverseitig,
Budgets, Einnahmen und Einstellungen liegen in einer lokalen SQLite-Datenbank.
Ausgaben = Paperless-Dokumente mit gesetztem Custom Field (monetary, Standard "Total").
Kategorien = Paperless-Tags, die in den Einstellungen als Budget-Kategorie markiert sind.
"""
import base64
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import bank, purpose as purp, vehicles as veh

# ---------------------------------------------------------------- Konfiguration
PAPERLESS_URL = os.environ.get("PAPERLESS_URL", "http://paperless:8000").rstrip("/")
PAPERLESS_TOKEN = os.environ.get("PAPERLESS_TOKEN", "")
PAPERLESS_PUBLIC_URL = os.environ.get("PAPERLESS_PUBLIC_URL", PAPERLESS_URL).rstrip("/")
PAPERLESS_API_VERSION = os.environ.get("PAPERLESS_API_VERSION", "")
PAPERLESS_VERIFY_SSL = os.environ.get("PAPERLESS_VERIFY_SSL", "true").lower() != "false"
AMOUNT_FIELD_NAME = os.environ.get("AMOUNT_FIELD_NAME", "Total")
CURRENCY = os.environ.get("CURRENCY", "CHF").upper()
DB_PATH = os.environ.get("DB_PATH", "/data/budget.db")
AUTH_USER = os.environ.get("BASIC_AUTH_USER", "")
AUTH_PASS = os.environ.get("BASIC_AUTH_PASSWORD", "")
META_TTL = int(os.environ.get("META_CACHE_SECONDS", "60"))
PAID_TAG_NAME = os.environ.get("PAID_TAG_NAME", "bezahlt")
PAID_FIELD_NAME = os.environ.get("PAID_FIELD_NAME", "Bezahlt am")
MATCH_BEFORE_DAYS = int(os.environ.get("MATCH_BEFORE_DAYS", "5"))
TRACCAR_URL = os.environ.get("TRACCAR_URL", "").rstrip("/")
TRACCAR_TOKEN = os.environ.get("TRACCAR_TOKEN", "")
TRACCAR_USER = os.environ.get("TRACCAR_USER", "")
TRACCAR_PASSWORD = os.environ.get("TRACCAR_PASSWORD", "")
TRACCAR_VERIFY_SSL = os.environ.get("TRACCAR_VERIFY_SSL", "true").lower() != "false"  # Rechnung darf so viele Tage NACH der Zahlung datiert sein

STATIC_DIR = Path(__file__).parent / "static"
DOC_FIELDS = "id,title,created,correspondent,document_type,tags,custom_fields"
DEFAULT_SETTINGS = {
    "category_tag_ids": [],         # Reihenfolge = Priorität, falls ein Dokument mehrere Kategorie-Tags hat
    "review_document_type_ids": [],  # "Erfassen"-Ansicht: nur diese Dokumenttypen (leer = alle)
    "review_tag_ids": [],            # "Erfassen"-Ansicht: nur Dokumente mit einem dieser Tags (leer = alle)
    "statement_document_type_ids": [],  # Kontoauszüge erkennen: Dokumenttypen
    "statement_tag_ids": [],            # Kontoauszüge erkennen: Tags
    "match_window_days": 60,            # Rechnung höchstens so viele Tage vor der Zahlung
    "fuel_correspondent_ids": [],       # Tankkarten-Rechnungen erkennen: Korrespondenten (z.B. Vögtlin-Meyer)
    "fuel_tag_ids": [],                 # ... oder Tags
    "exclude_tag_ids": [],              # Dokumente mit diesen Tags nie im Budget (z.B. "Geschäft")
}
EXCLUDE_REASONS = ["Geschäftlich", "Rückerstattet", "Von jemand anderem bezahlt", "Doppelt erfasst", "Kein Haushaltsausgabe"]

app = FastAPI(title="Paperless Budget")


# ---------------------------------------------------------------- Basic Auth (optional)
@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if AUTH_USER:
        ok = False
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, AUTH_USER) and secrets.compare_digest(pw, AUTH_PASS)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Budget"'})
    return await call_next(request)


# ---------------------------------------------------------------- Datenbank
@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS budgets (tag_id INTEGER PRIMARY KEY, amount REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS incomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                description TEXT NOT NULL,
                amount REAL NOT NULL,
                recurring INTEGER NOT NULL DEFAULT 0,
                end_date TEXT
            );
            CREATE TABLE IF NOT EXISTS statements (
                doc_id INTEGER PRIMARY KEY, title TEXT, parsed_at TEXT, tx_count INTEGER
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                statement_doc_id INTEGER NOT NULL,
                hash TEXT NOT NULL UNIQUE,
                date TEXT NOT NULL,
                text TEXT NOT NULL,
                amount REAL NOT NULL,              -- negativ = Belastung
                sign_source TEXT,
                status TEXT NOT NULL DEFAULT 'open', -- open | matched | ignored
                matched_doc_id INTEGER,
                matched_title TEXT,
                category_tag_id INTEGER,
                merchant TEXT
            );
            CREATE INDEX IF NOT EXISTS tx_date ON transactions(date);
            CREATE TABLE IF NOT EXISTS vehicles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                energy TEXT NOT NULL DEFAULT 'Benzin',   -- Benzin | Diesel | Elektro | Hybrid
                traccar_device_id INTEGER,
                tag_id INTEGER,                          -- Paperless-Tag für Belege dieses Fahrzeugs
                card_keys TEXT,                          -- Tankkarten-Nr. (kommagetrennt, Endziffern genügen)
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS fuel_docs (doc_id INTEGER PRIMARY KEY, title TEXT, parsed_at TEXT, entries INTEGER);
            CREATE TABLE IF NOT EXISTS fuel_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id INTEGER,                          -- NULL = manuell erfasst
                hash TEXT UNIQUE,
                date TEXT NOT NULL,
                time TEXT,
                card TEXT,
                description TEXT,
                quantity REAL NOT NULL,
                unit TEXT NOT NULL DEFAULT 'L',
                amount REAL NOT NULL,
                vehicle_id INTEGER                       -- fest zugeordnet (manuell); sonst über die Karte
            );
            CREATE TABLE IF NOT EXISTS traccar_km (
                device_id INTEGER NOT NULL, month TEXT NOT NULL, km REAL NOT NULL, fetched_at REAL NOT NULL,
                PRIMARY KEY (device_id, month)
            );
            -- Liegenschaften
            CREATE TABLE IF NOT EXISTS properties (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, address TEXT,
                owner_name TEXT, owner_address TEXT, owner_iban TEXT,
                start_month INTEGER NOT NULL DEFAULT 1, room_pct REAL NOT NULL DEFAULT 70,
                room_key_id INTEGER, water_key_id INTEGER, tag_id INTEGER,
                exclude_from_budget INTEGER NOT NULL DEFAULT 1, intro TEXT, closing TEXT, due_days INTEGER DEFAULT 30
            );
            CREATE TABLE IF NOT EXISTS units (
                id INTEGER PRIMARY KEY AUTOINCREMENT, property_id INTEGER NOT NULL, name TEXT NOT NULL,
                area REAL NOT NULL DEFAULT 0, owner_occupied INTEGER NOT NULL DEFAULT 0, sort INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS dist_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT, property_id INTEGER NOT NULL, name TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'percent'                -- percent | area | equal
            );
            CREATE TABLE IF NOT EXISTS key_shares (
                key_id INTEGER NOT NULL, unit_id INTEGER NOT NULL, percent REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (key_id, unit_id)
            );
            CREATE TABLE IF NOT EXISTS cost_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, property_id INTEGER NOT NULL, grp TEXT NOT NULL, name TEXT NOT NULL,
                key_id INTEGER, heating INTEGER NOT NULL DEFAULT 0, unit_label TEXT, sort INTEGER DEFAULT 0,
                keywords TEXT                                        -- automatisch zuordnen (Korrespondent/Titel/Zweck)
            );
            CREATE TABLE IF NOT EXISTS tenancies (
                id INTEGER PRIMARY KEY AUTOINCREMENT, unit_id INTEGER NOT NULL, tenant_name TEXT NOT NULL,
                tenant_address TEXT, salutation TEXT, start TEXT NOT NULL, end TEXT,
                rent REAL NOT NULL DEFAULT 0, akonto REAL NOT NULL DEFAULT 0,
                match_text TEXT                                      -- Stichworte im Buchungstext der Mietzahlung
            );
            CREATE TABLE IF NOT EXISTS fixed_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, property_id INTEGER NOT NULL, category_id INTEGER NOT NULL,
                unit_id INTEGER, name TEXT, amount REAL NOT NULL, interval TEXT NOT NULL DEFAULT 'jahr',  -- jahr | monat
                valid_from TEXT, valid_to TEXT
            );
            CREATE TABLE IF NOT EXISTS manual_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, property_id INTEGER NOT NULL, category_id INTEGER NOT NULL,
                unit_id INTEGER, date TEXT NOT NULL, description TEXT, amount REAL NOT NULL, quantity REAL
            );
            CREATE TABLE IF NOT EXISTS rent_payments (
                tx_id INTEGER PRIMARY KEY, tenancy_id INTEGER NOT NULL, month TEXT NOT NULL,
                rent_part REAL NOT NULL DEFAULT 0, akonto_part REAL NOT NULL DEFAULT 0, auto INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS nk_statements (
                id INTEGER PRIMARY KEY AUTOINCREMENT, property_id INTEGER, unit_id INTEGER, tenancy_id INTEGER,
                period_start TEXT, period_end TEXT, total REAL, akonto REAL, saldo REAL, created_at TEXT,
                docx TEXT, pdf TEXT, paperless_task TEXT
            );
            -- Projekte
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, budget REAL, tag_id INTEGER,
                status TEXT NOT NULL DEFAULT 'aktiv', exclude_from_budget INTEGER NOT NULL DEFAULT 0,
                start TEXT, end TEXT, note TEXT
            );
            -- Zuordnung von Belegen/Bankbuchungen (ganz oder Teil) zu Liegenschaft oder Projekt
            CREATE TABLE IF NOT EXISTS allocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,          -- doc | tx
                source_id INTEGER NOT NULL,
                target_type TEXT NOT NULL,          -- property | project
                target_id INTEGER NOT NULL,
                category_id INTEGER, unit_id INTEGER,
                percent REAL, amount REAL, quantity REAL, note TEXT
            );
            CREATE INDEX IF NOT EXISTS alloc_src ON allocations(source_type, source_id);
            CREATE TABLE IF NOT EXISTS doc_purpose (
                doc_id INTEGER PRIMARY KEY,
                purpose TEXT,
                source TEXT NOT NULL,                  -- auto | manuell | none
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS doc_flags (
                doc_id INTEGER PRIMARY KEY,
                excluded INTEGER NOT NULL DEFAULT 0,   -- 1 = im Budget nicht berücksichtigen
                reason TEXT,                           -- Grund / Zweck, z.B. "Geschäftlich"
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                category_tag_id INTEGER,
                ignore INTEGER NOT NULL DEFAULT 0
            );
            """
        )


init_db()


def migrate_db():
    """Spalten nachrüsten, die in älteren Versionen fehlen."""
    wanted = {"transactions": {"card": "TEXT", "vehicle_id": "INTEGER", "merchant": "TEXT"}, "rules": {"vehicle_id": "INTEGER"}}
    with db() as con:
        for table, cols in wanted.items():
            have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


migrate_db()


def get_settings() -> dict:
    out = dict(DEFAULT_SETTINGS)
    with db() as con:
        for row in con.execute("SELECT key, value FROM settings"):
            out[row["key"]] = json.loads(row["value"])
    return out


def get_budgets() -> dict[int, float]:
    with db() as con:
        return {r["tag_id"]: r["amount"] for r in con.execute("SELECT tag_id, amount FROM budgets")}


# ---------------------------------------------------------------- Paperless-Client
_client: Optional[httpx.AsyncClient] = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        accept = "application/json" + (f"; version={PAPERLESS_API_VERSION}" if PAPERLESS_API_VERSION else "")
        _client = httpx.AsyncClient(
            base_url=PAPERLESS_URL,
            headers={"Authorization": f"Token {PAPERLESS_TOKEN}", "Accept": accept},
            timeout=30,
            verify=PAPERLESS_VERIFY_SSL,
        )
    return _client


async def pl_request(method: str, path: str, **kw):
    try:
        r = await client().request(method, path, **kw)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Paperless nicht erreichbar ({PAPERLESS_URL}): {e}")
    if r.status_code == 401 or r.status_code == 403:
        raise HTTPException(502, "Paperless lehnt den API-Token ab (401/403). PAPERLESS_TOKEN prüfen.")
    if r.status_code >= 400:
        raise HTTPException(502, f"Paperless-Fehler {r.status_code}: {r.text[:400]}")
    return r.json() if r.content else None


async def pl_all(path: str, params: Optional[dict] = None) -> list:
    """Alle Seiten einer Paperless-Liste holen (über Seitennummern, nicht über 'next'-URLs)."""
    params = dict(params or {})
    params["page_size"] = 100
    page, out = 1, []
    while True:
        params["page"] = page
        data = await pl_request("GET", path, params=params)
        out.extend(data.get("results", []))
        if not data.get("next"):
            return out
        page += 1


_meta_cache: dict = {"t": 0.0, "data": None}


async def get_meta(force: bool = False) -> dict:
    if not force and _meta_cache["data"] and time.time() - _meta_cache["t"] < META_TTL:
        return _meta_cache["data"]
    tags = await pl_all("/api/tags/")
    correspondents = await pl_all("/api/correspondents/")
    doc_types = await pl_all("/api/document_types/")
    fields = await pl_all("/api/custom_fields/")
    amount = next((f for f in fields if f["name"].strip().lower() == AMOUNT_FIELD_NAME.lower()), None)
    paid_field = next((f for f in fields if f["name"].strip().lower() == PAID_FIELD_NAME.lower()), None)
    paid_tag = next((t for t in tags if t["name"].strip().lower() == PAID_TAG_NAME.lower()), None)
    data = {
        "tags": [{"id": t["id"], "name": t["name"], "color": t.get("color") or "#888888"} for t in tags],
        "correspondents": [{"id": c["id"], "name": c["name"]} for c in correspondents],
        "document_types": [{"id": d["id"], "name": d["name"]} for d in doc_types],
        "amount_field": (
            {"id": amount["id"], "name": amount["name"], "data_type": amount.get("data_type")} if amount else None
        ),
        "amount_field_name": AMOUNT_FIELD_NAME,
        "paid_tag": {"id": paid_tag["id"], "name": paid_tag["name"]} if paid_tag else None,
        "paid_field": {"id": paid_field["id"], "name": paid_field["name"]} if paid_field else None,
        "paid_tag_name": PAID_TAG_NAME,
        "paid_field_name": PAID_FIELD_NAME,
        "currency": CURRENCY,
        "public_url": PAPERLESS_PUBLIC_URL,
    }
    _meta_cache.update(t=time.time(), data=data)
    return data


def require_amount_field(meta: dict) -> int:
    if not meta["amount_field"]:
        raise HTTPException(
            409, f"Custom Field '{AMOUNT_FIELD_NAME}' existiert in Paperless nicht – in der App unter 'Übersicht' anlegen."
        )
    return meta["amount_field"]["id"]


# ---------------------------------------------------------------- Hilfsfunktionen
_num = re.compile(r"-?\d+(?:[.,]\d+)?")


def parse_amount(value) -> Optional[float]:
    """Paperless liefert monetary-Werte als 'CHF12.50' (oder ältere Versionen: '12.50')."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _num.search(str(value))
    return float(m.group().replace(",", ".")) if m else None


def month_range(month: str) -> tuple[date, date]:
    try:
        y, m = (int(x) for x in month.split("-"))
        start = date(y, m, 1)
    except Exception:
        raise HTTPException(400, "Monat im Format JJJJ-MM angeben")
    end = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return start, end


def shift_month(d: date, n: int) -> date:
    idx = d.year * 12 + (d.month - 1) + n
    return date(idx // 12, idx % 12 + 1, 1)


def get_flags() -> dict[int, dict]:
    with db() as con:
        return {r["doc_id"]: dict(r) for r in con.execute("SELECT * FROM doc_flags WHERE excluded = 1")}


def exclusion(doc_id: int, tags: list[int], flags: dict, settings: dict, tag_names: dict) -> tuple[bool, Optional[str], str]:
    """(ausgeschlossen?, Grund, Quelle 'manuell'|'tag'|'')"""
    f = flags.get(doc_id)
    if f:
        return True, f.get("reason"), "manuell"
    hit = next((t for t in settings.get("exclude_tag_ids") or [] if t in tags), None)
    if hit is not None:
        return True, tag_names.get(hit, "Tag"), "tag"
    return False, None, ""


def normalize(doc: dict, amount_field_id: Optional[int], categories: list[int], excl: Optional[tuple] = None) -> dict:
    amount, has_field = None, False
    for cf in doc.get("custom_fields") or []:
        if cf.get("field") == amount_field_id:
            has_field = True
            amount = parse_amount(cf.get("value"))
    tags = doc.get("tags") or []
    category = next((t for t in categories if t in tags), None)
    return {
        "id": doc["id"],
        "title": doc.get("title") or f"Dokument {doc['id']}",
        "date": str(doc.get("created") or "")[:10],
        "correspondent": doc.get("correspondent"),
        "document_type": doc.get("document_type"),
        "tags": tags,
        "amount": amount,
        "has_amount_field": has_field,
        "category": category,
        "url": f"{PAPERLESS_PUBLIC_URL}/documents/{doc['id']}/details",
        "content": (doc.get("content") or "")[:400] if "content" in doc else None,
        "excluded": bool(excl and excl[0]),
        "exclude_reason": excl[1] if excl else None,
        "exclude_source": excl[2] if excl else "",
    }


async def attach_purposes(docs: list[dict]) -> list[dict]:
    """Zweck je Dokument ergänzen: aus Cache, sonst aus dem OCR-Text lesen und speichern."""
    if not docs:
        return docs
    ids = [d["id"] for d in docs]
    with db() as con:
        q = ",".join("?" * len(ids))
        cached = {r["doc_id"]: dict(r) for r in con.execute(f"SELECT * FROM doc_purpose WHERE doc_id IN ({q})", ids)}
        fuel = {r["doc_id"]: r["entries"] for r in con.execute(f"SELECT doc_id, entries FROM fuel_docs WHERE doc_id IN ({q})", ids)}
    missing = [i for i in ids if i not in cached or (i in fuel and cached[i]["source"] != "manuell")]
    if missing:
        found: dict[int, Optional[str]] = {}
        need_text = [i for i in missing if i not in fuel]
        for k in range(0, len(need_text), 50):
            chunk = need_text[k:k + 50]
            for d in await pl_all("/api/documents/", {"id__in": ",".join(map(str, chunk)), "fields": "id,content"}):
                found[d["id"]] = purp.extract_purpose(d.get("content") or "")
        for i in missing:
            if i in fuel:
                found[i] = f"Treibstoff · {fuel[i]} Tankvorgänge"
        now = datetime.now().isoformat(timespec="seconds")
        with db() as con:
            for i in missing:
                v = found.get(i)
                con.execute("INSERT OR REPLACE INTO doc_purpose(doc_id, purpose, source, updated_at) VALUES (?, ?, ?, ?)",
                            (i, v, "auto" if v else "none", now))
                cached[i] = {"purpose": v, "source": "auto" if v else "none"}
    for d in docs:
        c = cached.get(d["id"]) or {}
        d["purpose"] = c.get("purpose")
        d["purpose_source"] = c.get("source")
    return docs


class PurposeIn(BaseModel):
    purpose: Optional[str] = None


@app.put("/api/documents/{doc_id}/purpose")
def set_purpose(doc_id: int, body: PurposeIn):
    v = (body.purpose or "").strip() or None
    with db() as con:
        con.execute("INSERT OR REPLACE INTO doc_purpose(doc_id, purpose, source, updated_at) VALUES (?, ?, 'manuell', ?)",
                    (doc_id, v, datetime.now().isoformat(timespec="seconds")))
    return {"doc_id": doc_id, "purpose": v, "purpose_source": "manuell"}


@app.delete("/api/documents/{doc_id}/purpose")
async def reset_purpose(doc_id: int):
    """Manuellen Zweck verwerfen und neu aus der Rechnung lesen."""
    with db() as con:
        con.execute("DELETE FROM doc_purpose WHERE doc_id = ?", (doc_id,))
    d = (await attach_purposes([{"id": doc_id}]))[0]
    return {"doc_id": doc_id, "purpose": d["purpose"], "purpose_source": d["purpose_source"]}


async def _excl_ctx() -> tuple[dict, dict, dict]:
    meta = await get_meta()
    return get_flags(), get_settings(), {t["id"]: t["name"] for t in meta["tags"]}


def normalize_all(docs: list[dict], fid: int, cats: list[int], ctx: tuple) -> list[dict]:
    flags, st, names = ctx
    return [normalize(d, fid, cats, exclusion(d["id"], d.get("tags") or [], flags, st, names)) for d in docs]


def alloc_targets() -> dict:
    with db() as con:
        return {
            "property": {r["id"]: dict(r) for r in con.execute("SELECT id, name, tag_id, exclude_from_budget FROM properties")},
            "project": {r["id"]: dict(r) for r in con.execute("SELECT id, name, tag_id, exclude_from_budget FROM projects")},
        }


def alloc_amount(a: dict, full: float) -> float:
    if a.get("amount") is not None:
        return round(float(a["amount"]), 2)
    return round(full * float(a.get("percent") if a.get("percent") is not None else 100) / 100, 2)


def attach_allocations(items: list[dict], source_type: str, amount_key: str = "amount", targets: Optional[dict] = None) -> None:
    """items[*]['allocations'] (explizit + implizit über Tag) und ['budget_reduction'] ergänzen."""
    if not items:
        return
    targets = targets or alloc_targets()
    ids = [i["id"] for i in items]
    with db() as con:
        q = ",".join("?" * len(ids))
        rows = [dict(r) for r in con.execute(
            f"SELECT * FROM allocations WHERE source_type = ? AND source_id IN ({q})", [source_type, *ids])]
    by_src: dict[int, list] = {}
    for r in rows:
        by_src.setdefault(r["source_id"], []).append(r)
    for it in items:
        full = float(it.get(amount_key) or 0)
        out, explicit_targets = [], set()
        for a in by_src.get(it["id"], []):
            t = targets[a["target_type"]].get(a["target_id"])
            if not t:
                continue
            explicit_targets.add((a["target_type"], a["target_id"]))
            out.append({**a, "target_name": t["name"], "value": alloc_amount(a, full), "implicit": False,
                        "excludes_budget": bool(t["exclude_from_budget"])})
        for tt in ("property", "project"):
            for t in targets[tt].values():
                if t["tag_id"] and t["tag_id"] in (it.get("tags") or []) and (tt, t["id"]) not in explicit_targets:
                    out.append({"id": None, "target_type": tt, "target_id": t["id"], "target_name": t["name"], "value": round(full, 2),
                                "percent": 100, "amount": None, "category_id": None, "unit_id": None, "quantity": None,
                                "implicit": True, "excludes_budget": bool(t["exclude_from_budget"])})
        it["allocations"] = out
        it["budget_reduction"] = round(min(full, sum(a["value"] for a in out if a["excludes_budget"])), 2)


def apply_budget_reduction(items: list[dict], kind_label: str = "") -> None:
    """Anteile, die einer Liegenschaft / einem Projekt 'ausserhalb des Haushaltsbudgets' gehören, abziehen."""
    for it in items:
        red = it.get("budget_reduction") or 0
        if red <= 0:
            continue
        it["amount_full"] = it["amount"]
        it["amount"] = round(it["amount"] - red, 2)
        names = ", ".join(sorted({a["target_name"] for a in it["allocations"] if a["excludes_budget"]}))
        if it["amount"] <= 0.005 and not it.get("excluded"):
            it["amount"] = 0.0
            it["excluded"], it["exclude_reason"], it["exclude_source"] = True, names, "alloc"
        elif it["amount"] > 0.005:
            it["alloc_note"] = f"ohne {red:.2f} für {names}"


async def fetch_expenses(start: date, end: date, include_excluded: bool = False, budget_view: bool = False) -> list[dict]:
    """Dokumente mit Betrag im Zeitraum. Standard: ohne die als 'nicht berücksichtigen' markierten.
    budget_view: Anteile für Liegenschaften/Projekte ausserhalb des Haushaltsbudgets abziehen."""
    meta = await get_meta()
    fid = require_amount_field(meta)
    cats = get_settings()["category_tag_ids"]
    docs = await pl_all(
        "/api/documents/",
        {
            "created__gte": start.isoformat(),
            "created__lt": end.isoformat(),
            "custom_fields__id__all": fid,
            "fields": DOC_FIELDS,
            "ordering": "-created",
        },
    )
    out = normalize_all(docs, fid, cats, await _excl_ctx())
    # Datum zusätzlich lokal prüfen (robust gegenüber Zeitzonen/Versionen)
    out = [d for d in out if d["amount"] is not None and start.isoformat() <= d["date"] < end.isoformat()]
    attach_allocations(out, "doc")
    if budget_view:
        apply_budget_reduction(out)
    return [d for d in out if include_excluded or not d["excluded"]]


def incomes_between(start: date, end: date) -> list[dict]:
    """Einmalige Einnahmen im Zeitraum + wiederkehrende, die im Zeitraum aktiv sind."""
    s, e = start.isoformat(), end.isoformat()
    with db() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM incomes ORDER BY date DESC")]
    out = []
    for r in rows:
        if r["recurring"]:
            if r["date"] < e and (not r["end_date"] or r["end_date"] >= s):
                out.append(r)
        elif s <= r["date"] < e:
            out.append(r)
    return out


def bank_expenses(start: date, end: date, categories: list[int]) -> list[dict]:
    """Belastungen aus Kontoauszügen ohne zugeordnete Rechnung -> zählen als Ausgabe."""
    with db() as con:
        rows = con.execute(
            "SELECT * FROM transactions WHERE status = 'open' AND amount < 0 AND date >= ? AND date < ?",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    items = [
        {
            "id": r["id"],
            "title": r["text"],
            "date": r["date"],
            "amount": -r["amount"],
            "category": r["category_tag_id"] if r["category_tag_id"] in categories else None,
            "source": "bank",
        }
        for r in rows
    ]
    attach_allocations(items, "tx")
    apply_budget_reduction(items)
    return [i for i in items if not i.get("excluded")]


# ---------------------------------------------------------------- API: Meta & Setup
@app.get("/api/health")
async def health():
    meta = await get_meta(force=True)
    return {"ok": True, "paperless": PAPERLESS_URL, "amount_field": meta["amount_field"]}


@app.get("/api/meta")
async def meta(refresh: bool = False):
    return await get_meta(force=refresh)


@app.post("/api/setup/amount-field")
async def create_amount_field():
    m = await get_meta(force=True)
    if m["amount_field"]:
        return m["amount_field"]
    await pl_request(
        "POST",
        "/api/custom_fields/",
        json={"name": AMOUNT_FIELD_NAME, "data_type": "monetary", "extra_data": {"default_currency": CURRENCY}},
    )
    return (await get_meta(force=True))["amount_field"]


# ---------------------------------------------------------------- API: Einstellungen & Budgets
class SettingsIn(BaseModel):
    category_tag_ids: Optional[list[int]] = None
    review_document_type_ids: Optional[list[int]] = None
    review_tag_ids: Optional[list[int]] = None
    statement_document_type_ids: Optional[list[int]] = None
    statement_tag_ids: Optional[list[int]] = None
    match_window_days: Optional[int] = None
    fuel_correspondent_ids: Optional[list[int]] = None
    fuel_tag_ids: Optional[list[int]] = None
    exclude_tag_ids: Optional[list[int]] = None


@app.get("/api/settings")
def read_settings():
    return get_settings()


@app.put("/api/settings")
def write_settings(body: SettingsIn):
    with db() as con:
        for k, v in body.model_dump(exclude_unset=True).items():
            if v is None:
                continue
            con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v)))
    return get_settings()


class BudgetsIn(BaseModel):
    budgets: dict[int, Optional[float]]


@app.get("/api/budgets")
def read_budgets():
    return get_budgets()


@app.put("/api/budgets")
def write_budgets(body: BudgetsIn):
    with db() as con:
        for tag_id, amount in body.budgets.items():
            if amount is None:
                con.execute("DELETE FROM budgets WHERE tag_id = ?", (tag_id,))
            else:
                con.execute("INSERT OR REPLACE INTO budgets(tag_id, amount) VALUES (?, ?)", (tag_id, amount))
    return get_budgets()


# ---------------------------------------------------------------- API: Einnahmen
class IncomeIn(BaseModel):
    date: date
    description: str
    amount: float
    recurring: bool = False
    end_date: Optional[date] = None


@app.get("/api/incomes")
def list_incomes():
    with db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM incomes ORDER BY recurring DESC, date DESC")]


@app.post("/api/incomes", status_code=201)
def add_income(body: IncomeIn):
    with db() as con:
        cur = con.execute(
            "INSERT INTO incomes(date, description, amount, recurring, end_date) VALUES (?, ?, ?, ?, ?)",
            (
                body.date.isoformat(),
                body.description.strip(),
                body.amount,
                int(body.recurring),
                body.end_date.isoformat() if body.end_date else None,
            ),
        )
        return dict(con.execute("SELECT * FROM incomes WHERE id = ?", (cur.lastrowid,)).fetchone())


@app.put("/api/incomes/{income_id}")
def update_income(income_id: int, body: IncomeIn):
    with db() as con:
        con.execute(
            "UPDATE incomes SET date=?, description=?, amount=?, recurring=?, end_date=? WHERE id=?",
            (
                body.date.isoformat(),
                body.description.strip(),
                body.amount,
                int(body.recurring),
                body.end_date.isoformat() if body.end_date else None,
                income_id,
            ),
        )
        row = con.execute("SELECT * FROM incomes WHERE id = ?", (income_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Einnahme nicht gefunden")
    return dict(row)


@app.delete("/api/incomes/{income_id}", status_code=204)
def delete_income(income_id: int):
    with db() as con:
        con.execute("DELETE FROM incomes WHERE id = ?", (income_id,))
    return Response(status_code=204)


# ---------------------------------------------------------------- API: Ausgaben & Auswertungen
@app.get("/api/expenses")
async def expenses(month: str, all: bool = False):
    """Ausgaben-Belege des Monats mit Zweck. all=true: auch die nicht berücksichtigten (excluded=true)."""
    start, end = month_range(month)
    return await attach_purposes(await fetch_expenses(start, end, include_excluded=all, budget_view=True))


@app.get("/api/month-items")
async def month_items(month: str):
    """Alle Ausgaben des Monats für die Übersicht: Belege (auch nicht berücksichtigte) + Bankbuchungen ohne Beleg."""
    start, end = month_range(month)
    st = get_settings()
    docs = await attach_purposes(await fetch_expenses(start, end, include_excluded=True, budget_view=True))
    items = [{**d, "kind": "doc"} for d in docs]
    with db() as con:
        rows = con.execute(
            "SELECT * FROM transactions WHERE status IN ('open','ignored') AND amount < 0 AND date >= ? AND date < ?",
            (start.isoformat(), end.isoformat())).fetchall()
    bank_items = []
    for r in rows:
        bank_items.append({
            "id": r["id"], "kind": "bank", "title": "Bankbuchung ohne Beleg", "date": r["date"],
            "amount": -r["amount"], "correspondent": None,
            "category": r["category_tag_id"] if r["category_tag_id"] in st["category_tag_ids"] else None,
            "purpose": r["merchant"] or r["text"][:60], "purpose_source": "bank",
            "excluded": r["status"] == "ignored", "exclude_reason": "ignoriert" if r["status"] == "ignored" else None,
            "exclude_source": "bank", "url": None,
        })
    attach_allocations(bank_items, "tx")
    apply_budget_reduction(bank_items)
    items += bank_items
    items.sort(key=lambda x: (x["date"], x["amount"]), reverse=True)
    return items


@app.get("/api/summary")
async def summary(month: str):
    start, end = month_range(month)
    meta = await get_meta()
    cats = get_settings()["category_tag_ids"]
    all_docs = await fetch_expenses(start, end, include_excluded=True, budget_view=True)
    paperless_docs = [d for d in all_docs if not d["excluded"]]
    excluded_docs = [d for d in all_docs if d["excluded"]]
    bank_items = bank_expenses(start, end, cats)
    docs = paperless_docs + bank_items
    budgets = get_budgets()
    tag_by_id = {t["id"]: t for t in meta["tags"]}

    spent: dict[Optional[int], float] = {}
    count: dict[Optional[int], int] = {}
    for d in docs:
        spent[d["category"]] = spent.get(d["category"], 0.0) + d["amount"]
        count[d["category"]] = count.get(d["category"], 0) + 1

    categories = [
        {
            "tag_id": t,
            "name": tag_by_id.get(t, {}).get("name", f"Tag {t}"),
            "color": tag_by_id.get(t, {}).get("color", "#888888"),
            "spent": round(spent.get(t, 0.0), 2),
            "count": count.get(t, 0),
            "budget": budgets.get(t),
        }
        for t in cats
    ]
    incomes = incomes_between(start, end)
    total_spent = round(sum(spent.values()), 2)
    total_income = round(sum(i["amount"] for i in incomes), 2)
    return {
        "month": month,
        "categories": categories,
        "uncategorized": {"spent": round(spent.get(None, 0.0), 2), "count": count.get(None, 0)},
        "total_spent": total_spent,
        "total_budget": round(sum(b for t, b in budgets.items() if t in cats), 2),
        "total_income": total_income,
        "balance": round(total_income - total_spent, 2),
        "incomes": incomes,
        "document_count": len(paperless_docs),
        "bank_count": len(bank_items),
        "excluded": {"count": len(excluded_docs), "amount": round(sum(d.get("amount_full", d["amount"]) for d in excluded_docs), 2)},
    }


@app.get("/api/trend")
async def trend(until: Optional[str] = None, months: int = 12):
    months = max(1, min(months, 36))
    last_start, _ = month_range(until or date.today().strftime("%Y-%m"))
    first = shift_month(last_start, -(months - 1))
    end = shift_month(last_start, 1)
    cats = get_settings()["category_tag_ids"]
    docs = await fetch_expenses(first, end, budget_view=True) + bank_expenses(first, end, cats)

    series = []
    for i in range(months):
        ms = shift_month(first, i)
        me = shift_month(ms, 1)
        key = ms.strftime("%Y-%m")
        month_docs = [d for d in docs if d["date"].startswith(key)]
        by_cat = {str(t): 0.0 for t in cats}
        by_cat["none"] = 0.0
        for d in month_docs:
            k = str(d["category"]) if d["category"] is not None else "none"
            by_cat[k] = round(by_cat.get(k, 0.0) + d["amount"], 2)
        series.append(
            {
                "month": key,
                "spent": round(sum(d["amount"] for d in month_docs), 2),
                "income": round(sum(i["amount"] for i in incomes_between(ms, me)), 2),
                "by_category": by_cat,
            }
        )
    return series


@app.get("/api/review")
async def review(month: str):
    """Dokumente im Monat ohne Betrag – zum Nacherfassen."""
    start, end = month_range(month)
    meta = await get_meta()
    fid = require_amount_field(meta)
    st = get_settings()
    params = {
        "created__gte": start.isoformat(),
        "created__lt": end.isoformat(),
        "fields": DOC_FIELDS + ",content",
        "truncate_content": "true",
        "ordering": "-created",
    }
    if st["review_document_type_ids"]:
        params["document_type__id__in"] = ",".join(map(str, st["review_document_type_ids"]))
    if st["review_tag_ids"]:
        params["tags__id__in"] = ",".join(map(str, st["review_tag_ids"]))
    docs = normalize_all(await pl_all("/api/documents/", params), fid, st["category_tag_ids"], await _excl_ctx())
    return await attach_purposes([d for d in docs if d["amount"] is None and not d["excluded"]])


@app.get("/api/excluded")
async def excluded_docs(month: str):
    """Im Monat als 'nicht berücksichtigen' markierte Dokumente (mit und ohne Betrag)."""
    start, end = month_range(month)
    meta = await get_meta()
    fid = require_amount_field(meta)
    st = get_settings()
    ctx = await _excl_ctx()
    params = {"created__gte": start.isoformat(), "created__lt": end.isoformat(), "fields": DOC_FIELDS, "ordering": "-created"}
    flagged = set(ctx[0])
    out = []
    for d in normalize_all(await pl_all("/api/documents/", params), fid, st["category_tag_ids"], ctx):
        if d["excluded"] and (d["id"] in flagged or d["amount"] is not None):
            out.append(d)
    return await attach_purposes(out)


class FlagIn(BaseModel):
    excluded: bool
    reason: Optional[str] = None


@app.put("/api/documents/{doc_id}/flag")
def flag_document(doc_id: int, body: FlagIn):
    with db() as con:
        if body.excluded:
            con.execute(
                "INSERT OR REPLACE INTO doc_flags(doc_id, excluded, reason, updated_at) VALUES (?, 1, ?, ?)",
                (doc_id, (body.reason or "").strip() or None, datetime.now().isoformat(timespec="seconds")),
            )
        else:
            con.execute("DELETE FROM doc_flags WHERE doc_id = ?", (doc_id,))
    return {"doc_id": doc_id, "excluded": body.excluded, "reason": body.reason}


@app.get("/api/exclude-reasons")
def exclude_reasons():
    with db() as con:
        used = [r[0] for r in con.execute(
            "SELECT reason FROM doc_flags WHERE reason IS NOT NULL GROUP BY reason ORDER BY COUNT(*) DESC LIMIT 20")]
    return list(dict.fromkeys(used + EXCLUDE_REASONS))


class DocUpdate(BaseModel):
    amount: Optional[float] = None        # None = Betrag entfernen (nur wenn Feld mitgeschickt)
    category_tag_id: Optional[int] = None  # None = Kategorie-Tag entfernen (nur wenn Feld mitgeschickt)


@app.patch("/api/documents/{doc_id}")
async def update_document(doc_id: int, body: DocUpdate):
    """Betrag und/oder Kategorie-Tag in Paperless zurückschreiben."""
    meta = await get_meta()
    fid = require_amount_field(meta)
    cats = get_settings()["category_tag_ids"]
    doc = await pl_request("GET", f"/api/documents/{doc_id}/")
    payload: dict = {}

    if "amount" in body.model_fields_set:
        # Paperless ersetzt beim PATCH die komplette custom_fields-Liste -> bestehende Felder übernehmen
        cfs = [{"field": cf["field"], "value": cf.get("value")} for cf in doc.get("custom_fields") or [] if cf["field"] != fid]
        if body.amount is not None:
            cfs.append({"field": fid, "value": f"{CURRENCY}{body.amount:.2f}"})
        payload["custom_fields"] = cfs

    if "category_tag_id" in body.model_fields_set:
        tags = [t for t in doc.get("tags") or [] if t not in cats]
        if body.category_tag_id is not None:
            tags.append(body.category_tag_id)
        payload["tags"] = tags

    if not payload:
        raise HTTPException(400, "Nichts zu ändern")
    updated = await pl_request("PATCH", f"/api/documents/{doc_id}/", json=payload)
    return normalize(updated, fid, cats)


# ---------------------------------------------------------------- Kontoauszüge & Abgleich
async def patch_doc(doc_id: int, fields: Optional[dict] = None, add_tags=(), remove_tags=()) -> dict:
    """Dokument ändern und dabei alle übrigen Custom Fields und Tags erhalten.
    fields: {field_id: wert}; wert None entfernt das Feld."""
    doc = await pl_request("GET", f"/api/documents/{doc_id}/")
    payload: dict = {}
    if fields:
        cfs = [{"field": cf["field"], "value": cf.get("value")} for cf in doc.get("custom_fields") or [] if cf["field"] not in fields]
        cfs += [{"field": f, "value": v} for f, v in fields.items() if v is not None]
        payload["custom_fields"] = cfs
    if add_tags or remove_tags:
        tags = [t for t in doc.get("tags") or [] if t not in remove_tags]
        tags += [t for t in add_tags if t not in tags]
        payload["tags"] = tags
    if not payload:
        return doc
    return await pl_request("PATCH", f"/api/documents/{doc_id}/", json=payload)


async def ensure_paid_targets() -> tuple[int, int]:
    """Tag 'bezahlt' und Datumsfeld 'Bezahlt am' in Paperless anlegen, falls sie fehlen."""
    m = await get_meta()
    created = False
    if not m["paid_tag"]:
        await pl_request("POST", "/api/tags/", json={"name": PAID_TAG_NAME, "matching_algorithm": 0, "color": "#2e7d32"})
        created = True
    if not m["paid_field"]:
        await pl_request("POST", "/api/custom_fields/", json={"name": PAID_FIELD_NAME, "data_type": "date"})
        created = True
    if created:
        m = await get_meta(force=True)
    return m["paid_tag"]["id"], m["paid_field"]["id"]


def statement_filter(st: dict) -> dict:
    if not st["statement_document_type_ids"] and not st["statement_tag_ids"]:
        raise HTTPException(409, "Noch nicht festgelegt, welche Dokumente Kontoauszüge sind – unter Einstellungen wählen.")
    p = {"fields": "id,title,created,correspondent", "ordering": "-created"}
    if st["statement_document_type_ids"]:
        p["document_type__id__in"] = ",".join(map(str, st["statement_document_type_ids"]))
    if st["statement_tag_ids"]:
        p["tags__id__in"] = ",".join(map(str, st["statement_tag_ids"]))
    return p


def rule_matches(keyword: str, hay: str) -> bool:
    """Stichwort; mehrere Begriffe mit '+' müssen alle vorkommen, z.B. 'Tankomat + 1234'."""
    terms = [t.strip().lower() for t in keyword.split("+") if t.strip()]
    return bool(terms) and all(t in hay for t in terms)


def apply_rules(con, only_ids: Optional[list[int]] = None) -> int:
    """Regeln auf offene Belastungen anwenden: Kategorie/ignorieren (falls noch keine Kategorie) und Fahrzeug."""
    rules = [dict(r) for r in con.execute("SELECT * FROM rules ORDER BY length(keyword) DESC")]
    if not rules:
        return 0
    q = ("SELECT id, text, merchant, card, category_tag_id, vehicle_id FROM transactions "
         "WHERE status = 'open' AND amount < 0 AND (category_tag_id IS NULL OR vehicle_id IS NULL)")
    n = 0
    for r in con.execute(q).fetchall():
        if only_ids is not None and r["id"] not in only_ids:
            continue
        hay = f"{r['merchant'] or ''} {r['text']} karte {r['card'] or ''}".lower()
        hits = [x for x in rules if rule_matches(x["keyword"], hay)]
        changed = False
        if r["vehicle_id"] is None:
            vr = next((x for x in hits if x["vehicle_id"]), None)
            if vr:
                con.execute("UPDATE transactions SET vehicle_id = ? WHERE id = ?", (vr["vehicle_id"], r["id"]))
                changed = True
        if r["category_tag_id"] is None:
            cr = next((x for x in hits if x["ignore"] or x["category_tag_id"]), None)
            if cr and cr["ignore"]:
                con.execute("UPDATE transactions SET status = 'ignored' WHERE id = ?", (r["id"],))
                changed = True
            elif cr:
                con.execute("UPDATE transactions SET category_tag_id = ? WHERE id = ?", (cr["category_tag_id"], r["id"]))
                changed = True
        n += changed
    return n


@app.get("/api/statements")
async def list_statements():
    st = get_settings()
    docs = await pl_all("/api/documents/", statement_filter(st))
    with db() as con:
        parsed = {r["doc_id"]: dict(r) for r in con.execute("SELECT * FROM statements")}
    return [
        {
            "id": d["id"],
            "title": d.get("title"),
            "date": str(d.get("created") or "")[:10],
            "url": f"{PAPERLESS_PUBLIC_URL}/documents/{d['id']}/details",
            "parsed_at": parsed.get(d["id"], {}).get("parsed_at"),
            "tx_count": parsed.get(d["id"], {}).get("tx_count"),
        }
        for d in docs
    ]


async def parse_one(doc_id: int) -> dict:
    doc = await pl_request("GET", f"/api/documents/{doc_id}/")
    bookings = bank.parse_statement(doc.get("content") or "", doc_id)
    added = 0
    with db() as con:
        existing = {r["hash"]: dict(r) for r in con.execute("SELECT * FROM transactions WHERE statement_doc_id = ?", (doc_id,))}
        new_hashes = {b.hash for b in bookings}
        # nicht mehr gefundene, unbearbeitete Buchungen dieses Auszugs entfernen
        for h, r in existing.items():
            if h not in new_hashes and r["status"] == "open" and r["category_tag_id"] is None:
                con.execute("DELETE FROM transactions WHERE id = ?", (r["id"],))
        new_ids = []
        for b in bookings:
            cur = con.execute(
                "INSERT OR IGNORE INTO transactions(statement_doc_id, hash, date, text, amount, sign_source, merchant, card) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (doc_id, b.hash, b.date, b.text, b.amount, b.sign_source, b.merchant, b.card),
            )
            if cur.rowcount:
                added += 1
                new_ids.append(cur.lastrowid)
        apply_rules(con, new_ids)
        from .re_api import auto_assign_rent
        auto_assign_rent(con)
        con.execute(
            "INSERT OR REPLACE INTO statements(doc_id, title, parsed_at, tx_count) VALUES (?, ?, ?, ?)",
            (doc_id, doc.get("title"), datetime.now().isoformat(timespec="seconds"), len(bookings)),
        )
    unchecked = sum(1 for b in bookings if b.sign_source == "stichwort")
    return {"doc_id": doc_id, "title": doc.get("title"), "found": len(bookings), "added": added, "unchecked": unchecked}


@app.post("/api/statements/{doc_id}/parse")
async def parse_statement_doc(doc_id: int):
    return await parse_one(doc_id)


@app.post("/api/statements/parse-new")
async def parse_new_statements():
    statements = await list_statements()
    results = [await parse_one(s["id"]) for s in statements if not s["parsed_at"]]
    return {"parsed": len(results), "added": sum(r["added"] for r in results), "results": results}


async def _transactions(month: str) -> list[dict]:
    start, end = month_range(month)
    st = get_settings()
    window = int(st.get("match_window_days") or 60)
    meta = await get_meta()
    require_amount_field(meta)
    with db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM transactions WHERE date >= ? AND date < ? ORDER BY date, id", (start.isoformat(), end.isoformat()))]
        taken = {r[0] for r in con.execute("SELECT matched_doc_id FROM transactions WHERE matched_doc_id IS NOT NULL")}
    need = any(r["status"] == "open" and r["amount"] < 0 for r in rows)
    invoices = await fetch_expenses(start - timedelta(days=window), end + timedelta(days=MATCH_BEFORE_DAYS + 1),
                                    include_excluded=True) if need else []
    paid_tag = meta["paid_tag"]["id"] if meta["paid_tag"] else None
    free = [d for d in invoices if d["id"] not in taken and (paid_tag is None or paid_tag not in d["tags"])]
    corr = {c["id"]: c["name"] for c in meta["correspondents"]}
    with db() as con:
        rent = {r["tx_id"]: dict(r) for r in con.execute(
            "SELECT rp.tx_id, rp.month, t.tenant_name FROM rent_payments rp JOIN tenancies t ON t.id = rp.tenancy_id")}
    for t in rows:
        t["abs_amount"] = -t["amount"] if t["amount"] < 0 else t["amount"]
    attach_allocations(rows, "tx", amount_key="abs_amount")
    for t in rows:
        t["rent"] = rent.get(t["id"])
        t["candidates"], t["unique"] = [], False
        if t["status"] == "open" and t["amount"] < 0:
            c = bank.candidates(t, free, corr, window, MATCH_BEFORE_DAYS)[:5]
            t["candidates"] = [
                {k: x[k] for k in ("id", "title", "date", "amount", "correspondent", "score", "days", "name_hit", "url", "excluded")}
                for x in c
            ]
            t["unique"] = bool(c) and bank.is_unique(c)
        t["matched_url"] = f"{PAPERLESS_PUBLIC_URL}/documents/{t['matched_doc_id']}/details" if t["matched_doc_id"] else None
        t["statement_url"] = f"{PAPERLESS_PUBLIC_URL}/documents/{t['statement_doc_id']}/details"
    return rows


@app.get("/api/transactions")
async def transactions(month: str):
    return await _transactions(month)


def _tx(tx_id: int) -> dict:
    with db() as con:
        r = con.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Buchung nicht gefunden")
    return dict(r)


async def do_match(tx_id: int, doc_id: int) -> dict:
    tx = _tx(tx_id)
    if tx["amount"] >= 0:
        raise HTTPException(400, "Nur Belastungen können einer Rechnung zugeordnet werden")
    tag_id, field_id = await ensure_paid_targets()
    doc = await patch_doc(doc_id, fields={field_id: tx["date"]}, add_tags=[tag_id])
    with db() as con:
        con.execute(
            "UPDATE transactions SET status = 'matched', matched_doc_id = ?, matched_title = ?, category_tag_id = NULL WHERE id = ?",
            (doc_id, doc.get("title"), tx_id),
        )
    return _tx(tx_id)


class MatchIn(BaseModel):
    doc_id: int


@app.post("/api/transactions/{tx_id}/match")
async def match_tx(tx_id: int, body: MatchIn):
    return await do_match(tx_id, body.doc_id)


@app.post("/api/transactions/{tx_id}/unmatch")
async def unmatch_tx(tx_id: int):
    tx = _tx(tx_id)
    if tx["matched_doc_id"]:
        with db() as con:
            others = con.execute(
                "SELECT COUNT(*) FROM transactions WHERE matched_doc_id = ? AND id != ?", (tx["matched_doc_id"], tx_id)
            ).fetchone()[0]
        if not others:
            m = await get_meta()
            fields = {m["paid_field"]["id"]: None} if m["paid_field"] else None
            remove = [m["paid_tag"]["id"]] if m["paid_tag"] else []
            await patch_doc(tx["matched_doc_id"], fields=fields, remove_tags=remove)
    with db() as con:
        con.execute("UPDATE transactions SET status = 'open', matched_doc_id = NULL, matched_title = NULL WHERE id = ?", (tx_id,))
    return _tx(tx_id)


@app.post("/api/transactions/match-unique")
async def match_unique(month: str):
    """Alle eindeutigen Vorschläge des Monats bestätigen."""
    used, done = set(), []
    for t in await _transactions(month):
        if not t["unique"]:
            continue
        best = t["candidates"][0]
        if best["id"] in used:
            continue
        used.add(best["id"])
        await do_match(t["id"], best["id"])
        done.append({"tx": t["id"], "doc": best["id"]})
    return {"matched": len(done), "pairs": done}


class TxUpdate(BaseModel):
    status: Optional[str] = None          # open | ignored
    category_tag_id: Optional[int] = None
    vehicle_id: Optional[int] = None
    flip_sign: bool = False


@app.patch("/api/transactions/{tx_id}")
def update_tx(tx_id: int, body: TxUpdate):
    tx = _tx(tx_id)
    with db() as con:
        if body.status in ("open", "ignored") and tx["status"] != "matched":
            con.execute("UPDATE transactions SET status = ? WHERE id = ?", (body.status, tx_id))
        if "category_tag_id" in body.model_fields_set:
            con.execute("UPDATE transactions SET category_tag_id = ? WHERE id = ?", (body.category_tag_id, tx_id))
        if "vehicle_id" in body.model_fields_set:
            con.execute("UPDATE transactions SET vehicle_id = ? WHERE id = ?", (body.vehicle_id, tx_id))
        if body.flip_sign and tx["status"] != "matched":
            con.execute("UPDATE transactions SET amount = -amount, sign_source = 'manuell' WHERE id = ?", (tx_id,))
    return _tx(tx_id)


@app.get("/api/open-invoices")
async def open_invoices(days: int = 120, grace: int = 10):
    """Rechnungen mit Betrag, die (noch) keiner Zahlung zugeordnet sind."""
    meta = await get_meta()
    st = get_settings()
    today = date.today()
    docs = await fetch_expenses(today - timedelta(days=days), today - timedelta(days=grace) + timedelta(days=1))
    with db() as con:
        taken = {r[0] for r in con.execute("SELECT matched_doc_id FROM transactions WHERE matched_doc_id IS NOT NULL")}
    paid_tag = meta["paid_tag"]["id"] if meta["paid_tag"] else None
    types = set(st["review_document_type_ids"])
    return [
        d for d in docs
        if d["id"] not in taken and (paid_tag is None or paid_tag not in d["tags"]) and (not types or d["document_type"] in types)
    ]


# ---------------------------------------------------------------- Regeln (Händler -> Kategorie)
class RuleIn(BaseModel):
    keyword: str
    category_tag_id: Optional[int] = None
    vehicle_id: Optional[int] = None
    ignore: bool = False


@app.get("/api/rules")
def list_rules():
    with db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM rules ORDER BY keyword")]


@app.post("/api/rules", status_code=201)
def add_rule(body: RuleIn):
    kw = body.keyword.strip()
    if len(kw) < 3:
        raise HTTPException(400, "Stichwort mit mindestens 3 Zeichen angeben")
    if not body.ignore and body.category_tag_id is None and body.vehicle_id is None:
        raise HTTPException(400, "Kategorie, Fahrzeug oder 'ignorieren' wählen")
    with db() as con:
        cur = con.execute("INSERT INTO rules(keyword, category_tag_id, ignore, vehicle_id) VALUES (?, ?, ?, ?)",
                          (kw, body.category_tag_id, int(body.ignore), body.vehicle_id))
        applied = apply_rules(con)
        rule = dict(con.execute("SELECT * FROM rules WHERE id = ?", (cur.lastrowid,)).fetchone())
    return {**rule, "applied": applied}


@app.delete("/api/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int):
    with db() as con:
        con.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    return Response(status_code=204)


# ---------------------------------------------------------------- Fahrzeuge (Kosten, Traccar-km, Verbrauch)
class VehicleIn(BaseModel):
    name: str
    energy: str = "Benzin"
    traccar_device_id: Optional[int] = None
    tag_id: Optional[int] = None
    card_keys: Optional[str] = None
    active: bool = True


def list_vehicles_db(active_only: bool = False) -> list[dict]:
    with db() as con:
        q = "SELECT * FROM vehicles" + (" WHERE active = 1" if active_only else "") + " ORDER BY name"
        return [dict(r) for r in con.execute(q)]


@app.get("/api/vehicles")
def get_vehicles():
    return list_vehicles_db()


@app.post("/api/vehicles", status_code=201)
def add_vehicle(body: VehicleIn):
    with db() as con:
        cur = con.execute(
            "INSERT INTO vehicles(name, energy, traccar_device_id, tag_id, card_keys, active) VALUES (?, ?, ?, ?, ?, ?)",
            (body.name.strip(), body.energy, body.traccar_device_id, body.tag_id, body.card_keys, int(body.active)),
        )
        return dict(con.execute("SELECT * FROM vehicles WHERE id = ?", (cur.lastrowid,)).fetchone())


@app.put("/api/vehicles/{vid}")
def update_vehicle(vid: int, body: VehicleIn):
    with db() as con:
        con.execute(
            "UPDATE vehicles SET name=?, energy=?, traccar_device_id=?, tag_id=?, card_keys=?, active=? WHERE id=?",
            (body.name.strip(), body.energy, body.traccar_device_id, body.tag_id, body.card_keys, int(body.active), vid),
        )
        row = con.execute("SELECT * FROM vehicles WHERE id = ?", (vid,)).fetchone()
    if not row:
        raise HTTPException(404, "Fahrzeug nicht gefunden")
    return dict(row)


@app.delete("/api/vehicles/{vid}", status_code=204)
def delete_vehicle(vid: int):
    with db() as con:
        con.execute("DELETE FROM vehicles WHERE id = ?", (vid,))
        con.execute("UPDATE transactions SET vehicle_id = NULL WHERE vehicle_id = ?", (vid,))
        con.execute("UPDATE rules SET vehicle_id = NULL WHERE vehicle_id = ?", (vid,))
        con.execute("UPDATE fuel_entries SET vehicle_id = NULL WHERE vehicle_id = ?", (vid,))
    return Response(status_code=204)


# --- Traccar
_tc: Optional[httpx.AsyncClient] = None


def traccar_client() -> httpx.AsyncClient:
    global _tc
    if not TRACCAR_URL:
        raise HTTPException(409, "Traccar ist nicht konfiguriert (TRACCAR_URL in .env).")
    if _tc is None:
        headers = {"Accept": "application/json"}
        auth = None
        if TRACCAR_TOKEN:
            headers["Authorization"] = f"Bearer {TRACCAR_TOKEN}"
        elif TRACCAR_USER:
            auth = (TRACCAR_USER, TRACCAR_PASSWORD)
        _tc = httpx.AsyncClient(base_url=TRACCAR_URL, headers=headers, auth=auth, timeout=30, verify=TRACCAR_VERIFY_SSL)
    return _tc


async def tc_get(path: str, params: Optional[dict] = None):
    try:
        r = await traccar_client().get(path, params=params)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Traccar nicht erreichbar ({TRACCAR_URL}): {e}")
    if r.status_code in (401, 403):
        raise HTTPException(502, "Traccar lehnt die Anmeldung ab (TRACCAR_TOKEN bzw. TRACCAR_USER/PASSWORD prüfen).")
    if r.status_code >= 400:
        raise HTTPException(502, f"Traccar-Fehler {r.status_code}: {r.text[:300]}")
    return r.json()


@app.get("/api/traccar/devices")
async def traccar_devices():
    devices = await tc_get("/api/devices")
    return [{"id": d["id"], "name": d.get("name"), "uniqueId": d.get("uniqueId")} for d in devices]


async def traccar_month_km(device_id: int, month: str) -> float:
    """Gefahrene km eines Geräts in einem Monat (vergangene Monate werden dauerhaft zwischengespeichert)."""
    start, end = month_range(month)
    today = date.today()
    if start > today:
        return 0.0
    current = start <= today < end
    with db() as con:
        row = con.execute("SELECT km, fetched_at FROM traccar_km WHERE device_id = ? AND month = ?", (device_id, month)).fetchone()
    if row and (not current or time.time() - row["fetched_at"] < 3600):
        return row["km"]
    to = min(end, today + timedelta(days=1))
    data = await tc_get("/api/reports/summary", {
        "deviceId": device_id, "from": f"{start.isoformat()}T00:00:00Z", "to": f"{to.isoformat()}T00:00:00Z",
    })
    km = round(sum(float(x.get("distance") or 0) for x in (data or [])) / 1000, 1)
    with db() as con:
        con.execute("INSERT OR REPLACE INTO traccar_km(device_id, month, km, fetched_at) VALUES (?, ?, ?, ?)",
                    (device_id, month, km, time.time()))
    return km


@app.post("/api/traccar/refresh")
def traccar_refresh():
    with db() as con:
        con.execute("DELETE FROM traccar_km")
    return {"ok": True}


# --- Tankkarten-Rechnungen
def is_fuel_doc(doc: dict, st: dict) -> bool:
    return (doc.get("correspondent") in set(st.get("fuel_correspondent_ids") or [])) or bool(
        set(doc.get("tags") or []) & set(st.get("fuel_tag_ids") or []))


@app.get("/api/fuel-docs")
async def fuel_docs():
    st = get_settings()
    if not st.get("fuel_correspondent_ids") and not st.get("fuel_tag_ids"):
        return []
    base = {"fields": "id,title,created,correspondent,tags", "ordering": "-created"}
    found: dict[int, dict] = {}
    if st.get("fuel_correspondent_ids"):
        for d in await pl_all("/api/documents/", {**base, "correspondent__id__in": ",".join(map(str, st["fuel_correspondent_ids"]))}):
            found[d["id"]] = d
    if st.get("fuel_tag_ids"):
        for d in await pl_all("/api/documents/", {**base, "tags__id__in": ",".join(map(str, st["fuel_tag_ids"]))}):
            found[d["id"]] = d
    with db() as con:
        parsed = {r["doc_id"]: dict(r) for r in con.execute("SELECT * FROM fuel_docs")}
    found = {k: d for k, d in found.items() if is_fuel_doc(d, st)}
    out = [{
        "id": d["id"], "title": d.get("title"), "date": str(d.get("created") or "")[:10],
        "url": f"{PAPERLESS_PUBLIC_URL}/documents/{d['id']}/details",
        "parsed_at": parsed.get(d["id"], {}).get("parsed_at"), "entries": parsed.get(d["id"], {}).get("entries"),
    } for d in found.values()]
    return sorted(out, key=lambda x: x["date"], reverse=True)


def vehicle_for_entry(e: dict, vehicles: list[dict]) -> Optional[int]:
    if e.get("vehicle_id"):
        return e["vehicle_id"]
    return next((v["id"] for v in vehicles if veh.card_matches(e.get("card") or "", v.get("card_keys") or "")), None)


async def parse_fuel_one(doc_id: int) -> dict:
    doc = await pl_request("GET", f"/api/documents/{doc_id}/")
    entries = veh.parse_fuel_invoice(doc.get("content") or "")
    added = 0
    with db() as con:
        hashes = {e.hash for e in entries}
        for r in con.execute("SELECT id, hash FROM fuel_entries WHERE doc_id = ?", (doc_id,)).fetchall():
            if r["hash"] not in hashes:
                con.execute("DELETE FROM fuel_entries WHERE id = ?", (r["id"],))
        for e in entries:
            cur = con.execute(
                "INSERT OR IGNORE INTO fuel_entries(doc_id, hash, date, time, card, description, quantity, unit, amount) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (doc_id, e.hash, e.date, e.time, e.card, e.description, e.quantity, e.unit, e.amount),
            )
            added += cur.rowcount
        con.execute("INSERT OR REPLACE INTO fuel_docs(doc_id, title, parsed_at, entries) VALUES (?, ?, ?, ?)",
                    (doc_id, doc.get("title"), datetime.now().isoformat(timespec="seconds"), len(entries)))
    vehicles = list_vehicles_db()
    cards = {}
    for e in entries:
        c = cards.setdefault(e.card, {"card": e.card, "quantity": 0.0, "amount": 0.0, "vehicle_id": None})
        c["quantity"] = round(c["quantity"] + e.quantity, 2)
        c["amount"] = round(c["amount"] + e.amount, 2)
        c["vehicle_id"] = vehicle_for_entry({"card": e.card}, vehicles)
    return {"doc_id": doc_id, "title": doc.get("title"), "found": len(entries), "added": added, "cards": list(cards.values())}


@app.post("/api/fuel-docs/{doc_id}/parse")
async def parse_fuel_doc(doc_id: int):
    return await parse_fuel_one(doc_id)


@app.post("/api/fuel-docs/parse-new")
async def parse_new_fuel_docs():
    results = [await parse_fuel_one(d["id"]) for d in await fuel_docs() if not d["parsed_at"]]
    return {"parsed": len(results), "added": sum(r["added"] for r in results), "results": results}


class FuelIn(BaseModel):
    date: date
    vehicle_id: int
    quantity: float
    amount: float
    unit: str = "L"
    description: Optional[str] = None


@app.get("/api/fuel-entries")
def fuel_entries(month_from: str, month_to: str):
    start, _ = month_range(month_from)
    _, end = month_range(month_to)
    vehicles = list_vehicles_db()
    with db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM fuel_entries WHERE date >= ? AND date < ? ORDER BY date DESC, time DESC",
            (start.isoformat(), end.isoformat()))]
    for r in rows:
        r["resolved_vehicle_id"] = vehicle_for_entry(r, vehicles)
        r["doc_url"] = f"{PAPERLESS_PUBLIC_URL}/documents/{r['doc_id']}/details" if r["doc_id"] else None
    return rows


@app.post("/api/fuel-entries", status_code=201)
def add_fuel_entry(body: FuelIn):
    with db() as con:
        cur = con.execute(
            "INSERT INTO fuel_entries(doc_id, hash, date, description, quantity, unit, amount, vehicle_id) VALUES (NULL, NULL, ?, ?, ?, ?, ?, ?)",
            (body.date.isoformat(), body.description or "manuell", body.quantity, body.unit, body.amount, body.vehicle_id),
        )
        return dict(con.execute("SELECT * FROM fuel_entries WHERE id = ?", (cur.lastrowid,)).fetchone())


@app.delete("/api/fuel-entries/{eid}", status_code=204)
def delete_fuel_entry(eid: int):
    with db() as con:
        con.execute("DELETE FROM fuel_entries WHERE id = ? AND doc_id IS NULL", (eid,))
    return Response(status_code=204)


# --- Auswertung
FUEL_TX_RE = re.compile(
    r"tankomat|tankstelle|tankst|socar|migrol|shell|agrola|avia|esso|tamoil|\bbp\b|\beni\b|ruedi r|coop pronto|"
    r"ladestation|ladesäule|ionity|swisscharge|move mobility|evpass|tesla supercharger",
    re.I,
)


@app.get("/api/vehicles/report")
async def vehicle_report(until: Optional[str] = None, months: int = 12):
    months = max(1, min(months, 36))
    last_start, _ = month_range(until or date.today().strftime("%Y-%m"))
    first = shift_month(last_start, -(months - 1))
    end = shift_month(last_start, 1)
    keys = [shift_month(first, i).strftime("%Y-%m") for i in range(months)]
    vehicles = list_vehicles_db(active_only=True)
    st = get_settings()
    meta = await get_meta()

    docs = await fetch_expenses(first, end) if meta["amount_field"] and vehicles else []
    with db() as con:
        fuel_doc_ids = {r[0] for r in con.execute("SELECT doc_id FROM fuel_docs")}
        fuel = [dict(r) for r in con.execute(
            "SELECT * FROM fuel_entries WHERE date >= ? AND date < ?", (first.isoformat(), end.isoformat()))]
        txs = [dict(r) for r in con.execute(
            "SELECT * FROM transactions WHERE vehicle_id IS NOT NULL AND status = 'open' AND amount < 0 AND date >= ? AND date < ?",
            (first.isoformat(), end.isoformat()))]
    for e in fuel:
        e["vid"] = vehicle_for_entry(e, vehicles)

    traccar_error = None
    out = []
    for v in vehicles:
        series = {k: {"month": k, "fuel_cost": 0.0, "quantity": 0.0, "bank_fuel": 0.0, "doc_cost": 0.0, "bank_cost": 0.0, "km": None}
                  for k in keys}
        items = []
        for e in fuel:
            if e["vid"] == v["id"]:
                m = series[e["date"][:7]]
                m["fuel_cost"] += e["amount"]
                m["quantity"] += e["quantity"]
        for d in docs:
            if v["tag_id"] and v["tag_id"] in d["tags"] and d["id"] not in fuel_doc_ids and not is_fuel_doc(d, st):
                series[d["date"][:7]]["doc_cost"] += d["amount"]
                items.append({"date": d["date"], "title": d["title"], "amount": d["amount"], "source": "Beleg", "url": d["url"]})
        for t in txs:
            if t["vehicle_id"] == v["id"]:
                is_fuel = bool(FUEL_TX_RE.search(f"{t['merchant'] or ''} {t['text']}"))
                series[t["date"][:7]]["bank_fuel" if is_fuel else "bank_cost"] += -t["amount"]
                items.append({"date": t["date"], "title": t["merchant"] or t["text"][:60], "amount": -t["amount"],
                              "source": "Tanken (Bank)" if is_fuel else "Bank", "url": None})
        if v["traccar_device_id"] and TRACCAR_URL:
            try:
                for k in keys:
                    series[k]["km"] = await traccar_month_km(v["traccar_device_id"], k)
            except HTTPException as e:
                traccar_error = e.detail

        rows = list(series.values())
        for m in rows:
            for f in ("fuel_cost", "quantity", "bank_fuel", "doc_cost", "bank_cost"):
                m[f] = round(m[f], 2)
            m["fuel_total"] = round(m["fuel_cost"] + m["bank_fuel"], 2)
            m["cost"] = round(m["fuel_total"] + m["doc_cost"] + m["bank_cost"], 2)
        km_known = [m["km"] for m in rows if m["km"] is not None]
        km = round(sum(km_known), 1) if km_known else None
        tot = {f: round(sum(m[f] for m in rows), 2)
               for f in ("fuel_cost", "quantity", "bank_fuel", "fuel_total", "doc_cost", "bank_cost", "cost")}
        # Verbrauch nur aus Monaten, in denen alle Tankvorgänge eine Mengenangabe haben (sonst zu tief)
        full = [m for m in rows if m["quantity"] > 0 and m["km"] and m["bank_fuel"] == 0]
        basis = full or [m for m in rows if m["quantity"] > 0 and m["km"]]
        cons = veh.consumption(sum(m["quantity"] for m in basis), sum(m["km"] for m in basis)) if basis else None
        unit = "kWh" if v["energy"] == "Elektro" else "L"
        out.append({
            **v, "unit": unit, "months": rows, **tot, "km": km,
            "cost_per_km": veh.per_km(tot["cost"], km or 0),
            "fuel_per_km": veh.per_km(tot["fuel_total"], km or 0),
            "consumption": cons,
            "consumption_months": len(basis),
            "consumption_complete": bool(full),
            "price_per_unit": round(tot["fuel_cost"] / tot["quantity"], 3) if tot["quantity"] else None,
            "items": sorted(items, key=lambda x: x["date"], reverse=True)[:30],
        })
    return {"months": keys, "vehicles": out, "traccar_configured": bool(TRACCAR_URL), "traccar_error": traccar_error}


# ---------------------------------------------------------------- Liegenschaften & Projekte
from .re_api import router as re_router  # noqa: E402

app.include_router(re_router)

# ---------------------------------------------------------------- Frontend
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
