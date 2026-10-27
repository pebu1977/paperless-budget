"""API: Verträge (Versicherungen, Abos, Telefon, Energie …) und Kredite (Hypothek, Leasing, Darlehen).

Ein Vertrag hat Versionen (Preis-/Konditionsänderungen ab Datum, optional mit Paperless-Beleg),
Zahlungen (Bankbuchungen, automatisch über Stichworte oder manuell zugeordnet) und Dokumente
(Paperless, über Korrespondent und/oder Tag).
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response

from . import main as core
from . import re_api

router = APIRouter()

INTERVAL_MONTHS = {"monat": 1, "quartal": 3, "halbjahr": 6, "jahr": 12, "einmalig": 0}

with core.db() as _con:
    _con.executescript(
        """
        CREATE TABLE IF NOT EXISTS contracts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL DEFAULT 'vertrag',      -- vertrag | kredit
            name TEXT NOT NULL, category TEXT, provider TEXT, contract_no TEXT,
            correspondent_id INTEGER, tag_id INTEGER,  -- Paperless: Dokumente zum Vertrag
            start TEXT, end TEXT,                      -- Beginn / festes Ende (leer = unbefristet)
            notice_months INTEGER,                     -- Kündigungsfrist in Monaten
            renew_months INTEGER,                      -- verlängert sich automatisch um x Monate
            match_text TEXT,                           -- Stichworte im Buchungstext (Komma = oder, + = und)
            principal REAL,                            -- Kredit: Anfangsbetrag
            active INTEGER NOT NULL DEFAULT 1, note TEXT
        );
        CREATE TABLE IF NOT EXISTS contract_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL,
            valid_from TEXT NOT NULL, amount REAL, interval TEXT NOT NULL DEFAULT 'monat',
            interest_rate REAL, amortization REAL,     -- Kredit: Zins % p.a., Amortisation pro Intervall
            doc_id INTEGER, note TEXT
        );
        CREATE TABLE IF NOT EXISTS contract_payments (
            tx_id INTEGER PRIMARY KEY, contract_id INTEGER NOT NULL, auto INTEGER NOT NULL DEFAULT 1  -- contract_id 0 = bewusst keiner
        );
        """
    )

re_api.ENT["contracts"] = ("contracts", ["kind", "name", "category", "provider", "contract_no", "correspondent_id", "tag_id",
                                         "start", "end", "notice_months", "renew_months", "match_text", "principal", "active", "note"])
re_api.ENT["contract-versions"] = ("contract_versions", ["contract_id", "valid_from", "amount", "interval", "interest_rate",
                                                         "amortization", "doc_id", "note"])


# ---------------------------------------------------------------- Rechnen
def _d(s: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(s[:10]) if s else None
    except ValueError:
        return None


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    last = (date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last))


def _mi(d: date) -> int:
    return d.year * 12 + d.month - 1


def _versions(con, cid: int) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM contract_versions WHERE contract_id = ? ORDER BY valid_from, id", (cid,))]


def _version_at(versions: list[dict], d: date) -> Optional[dict]:
    cur = None
    for v in versions:
        if (_d(v["valid_from"]) or date.min) <= d:
            cur = v
    return cur or (versions[0] if versions else None)


def _anchor(c: dict, versions: list[dict]) -> Optional[date]:
    return _d(c["start"]) or (_d(versions[0]["valid_from"]) if versions else None)


def loan_balance(c: dict, versions: list[dict], at: date) -> Optional[float]:
    """Restschuld nach Plan: Anfangsbetrag minus Amortisationen der fälligen Perioden bis zum Stichtag."""
    if c["principal"] is None:
        return None
    bal = float(c["principal"])
    anchor = _anchor(c, versions)
    if not anchor:
        return round(bal, 2)
    for i, v in enumerate(versions):
        m = INTERVAL_MONTHS.get(v["interval"], 1)
        if not v["amortization"] or not m:
            continue
        vf = max(_d(v["valid_from"]) or anchor, anchor)
        vt = _d(versions[i + 1]["valid_from"]) if i + 1 < len(versions) else None
        stop = min(at, vt - timedelta(days=1)) if vt else at
        # Fälligkeiten: anchor + k*m Monate (erste Zahlung eine Periode nach Beginn)
        k = 1
        while True:
            due = _add_months(anchor, k * m)
            if due > stop:
                break
            if due >= vf:
                bal -= v["amortization"]
            k += 1
    return round(max(bal, 0.0), 2)


def _period_cost(c: dict, v: Optional[dict], balance: Optional[float]) -> Optional[float]:
    """Betrag pro Intervall. Kredit ohne fixen Betrag: Zins auf Restschuld + Amortisation."""
    if not v:
        return None
    if v["amount"] is not None:
        return float(v["amount"])
    m = INTERVAL_MONTHS.get(v["interval"], 1)
    if c["kind"] == "kredit" and m and (v["interest_rate"] or v["amortization"]):
        return round((balance or 0) * (v["interest_rate"] or 0) / 100 * m / 12 + (v["amortization"] or 0), 2)
    return None


def _monthly(v: Optional[dict], per: Optional[float]) -> Optional[float]:
    if not v or per is None:
        return None
    m = INTERVAL_MONTHS.get(v["interval"], 1)
    return round(per / m, 2) if m else 0.0


def is_due(c: dict, versions: list[dict], month: date) -> bool:
    anchor = _anchor(c, versions)
    v = _version_at(versions, month)
    if not anchor or not v or _mi(month) < _mi(anchor):
        return False
    end = _d(c["end"])
    if end and _mi(month) > _mi(end):
        return False
    m = INTERVAL_MONTHS.get(v["interval"], 1)
    if m == 0:
        return _mi(month) == _mi(_d(v["valid_from"]) or anchor)
    return (_mi(month) - _mi(anchor)) % m == 0


def expected_in_year(c: dict, versions: list[dict], year: int, until: Optional[date] = None) -> float:
    total = 0.0
    for mo in range(1, 13):
        d = date(year, mo, 1)
        if until and d > until:
            break
        if is_due(c, versions, d):
            v = _version_at(versions, d)
            bal = loan_balance(c, versions, d) if c["kind"] == "kredit" else None
            total += _period_cost(c, v, bal) or 0
    return round(total, 2)


def term_info(c: dict, today: date) -> dict:
    """Laufzeitende und letzter Kündigungstermin."""
    start, end = _d(c["start"]), _d(c["end"])
    term_end = None
    if end and end >= today:
        term_end = end
    elif start and c["renew_months"]:
        t = start
        while True:
            nxt = _add_months(t, c["renew_months"])
            if nxt - timedelta(days=1) >= today:
                term_end = nxt - timedelta(days=1)
                break
            t = nxt
    elif end:
        term_end = end
    notice_by = None
    if term_end and c["notice_months"] is not None and (not end or end >= today or c["renew_months"]):
        notice_by = _add_months(term_end + timedelta(days=1), -int(c["notice_months"])) - timedelta(days=1)
        # Termin schon vorbei, Vertrag verlängert sich: nächsten Termin nehmen
        if notice_by < today and c["renew_months"]:
            term_end = _add_months(term_end + timedelta(days=1), c["renew_months"]) - timedelta(days=1)
            notice_by = _add_months(term_end + timedelta(days=1), -int(c["notice_months"])) - timedelta(days=1)
    return {"term_end": term_end.isoformat() if term_end else None,
            "notice_by": notice_by.isoformat() if notice_by else None,
            "notice_days": (notice_by - today).days if notice_by else None,
            "ended": bool(end and end < today and not c["renew_months"])}


def next_due(c: dict, versions: list[dict], today: date) -> Optional[str]:
    anchor = _anchor(c, versions)
    if not anchor:
        return None
    d = date(today.year, today.month, 1)
    for _ in range(25):
        if is_due(c, versions, d):
            day = date(d.year, d.month, min(anchor.day, 28))
            if day >= today:
                return day.isoformat()
        d = _add_months(d, 1)
    return None


def compute(c: dict, versions: list[dict], con, today: Optional[date] = None) -> dict:
    today = today or date.today()
    v = _version_at(versions, today)
    bal = loan_balance(c, versions, today) if c["kind"] == "kredit" else None
    per = _period_cost(c, v, bal)
    monthly = _monthly(v, per)
    pays = con.execute("SELECT SUM(t.amount), COUNT(*) FROM contract_payments cp JOIN transactions t ON t.id = cp.tx_id "
                       "WHERE cp.contract_id = ? AND t.date >= ?", (c["id"], f"{today.year}-01-01")).fetchone()
    last = con.execute("SELECT t.date, t.amount FROM contract_payments cp JOIN transactions t ON t.id = cp.tx_id "
                       "WHERE cp.contract_id = ? ORDER BY t.date DESC LIMIT 1", (c["id"],)).fetchone()
    out = {
        "version": v, "per_interval": per, "interval": v["interval"] if v else None,
        "monthly": monthly, "yearly": round(monthly * 12, 2) if monthly is not None else None,
        "next_due": next_due(c, versions, today) if c["active"] else None,
        "paid_ytd": round(-(pays[0] or 0), 2), "payments_ytd": pays[1],
        "expected_ytd": expected_in_year(c, versions, today.year, today),
        "last_payment": dict(last) if last else None,
        **term_info(c, today),
    }
    if c["kind"] == "kredit":
        rate = (v or {}).get("interest_rate") or 0
        out.update(balance=bal, interest_year=round((bal or 0) * rate / 100, 2), interest_rate=rate,
                   amortization_year=round(((v or {}).get("amortization") or 0) * 12 / (INTERVAL_MONTHS.get((v or {}).get("interval"), 1) or 12), 2)
                   if v else 0,
                   repaid=round((c["principal"] or 0) - (bal or 0), 2) if bal is not None else None)
    return out


# ---------------------------------------------------------------- Zahlungen automatisch zuordnen
def auto_assign_contracts(con) -> int:
    contracts = [dict(r) for r in con.execute("SELECT * FROM contracts WHERE match_text IS NOT NULL AND match_text != ''")]
    if not contracts:
        return 0
    vers = {c["id"]: _versions(con, c["id"]) for c in contracts}
    taken = {r[0] for r in con.execute("SELECT tx_id FROM contract_payments")}
    n = 0
    for t in con.execute("SELECT * FROM transactions WHERE amount < 0 AND status != 'ignored'").fetchall():
        if t["id"] in taken:
            continue
        hay = f"{t['merchant'] or ''} {t['text']}"
        td = _d(t["date"])
        hits = []
        for c in contracts:
            s = (_d(c["start"]) or date(1900, 1, 1)) - timedelta(days=31)
            e = (_d(c["end"]) or date(2999, 1, 1)) + timedelta(days=62)
            if not (s <= td <= e) or not re_api._kw_match(c["match_text"], hay):
                continue
            v = _version_at(vers[c["id"]], td)
            bal = loan_balance(c, vers[c["id"]], td) if c["kind"] == "kredit" else None
            per = _period_cost(c, v, bal)
            hits.append((abs(-t["amount"] - per) if per is not None else 1e9, c["id"]))
        if hits:
            con.execute("INSERT INTO contract_payments(tx_id, contract_id, auto) VALUES (?, ?, 1)", (t["id"], min(hits)[1]))
            n += 1
    return n


def contract_labels(con) -> dict[int, dict]:
    return {r["tx_id"]: {"id": r["cid"], "name": r["name"], "kind": r["kind"]} for r in con.execute(
        "SELECT cp.tx_id, c.id AS cid, c.name, c.kind FROM contract_payments cp JOIN contracts c ON c.id = cp.contract_id")}


# ---------------------------------------------------------------- Endpunkte
def _contract(con, cid: int) -> dict:
    r = con.execute("SELECT * FROM contracts WHERE id = ?", (cid,)).fetchone()
    if not r:
        raise HTTPException(404, "Vertrag nicht gefunden")
    return dict(r)


@router.get("/api/contracts-overview")
def contracts_overview():
    today = date.today()
    cfg = core.get_settings()
    with core.db() as con:
        auto_assign_contracts(con)
        items = []
        for c in [dict(r) for r in con.execute("SELECT * FROM contracts ORDER BY kind DESC, active DESC, category, name")]:
            c.update(compute(c, _versions(con, c["id"]), con, today))
            items.append(c)
    act = [c for c in items if c["active"] and not c["ended"]]
    con_ = [c for c in act if c["kind"] == "vertrag"]
    loans = [c for c in act if c["kind"] == "kredit"]
    abos = [c for c in act if c["kind"] == "abo"]
    s = lambda xs, k: round(sum(x.get(k) or 0 for x in xs), 2)
    by_cat: dict[str, float] = {}
    for c in con_:
        by_cat[c["category"] or "Sonstiges"] = round(by_cat.get(c["category"] or "Sonstiges", 0) + (c["monthly"] or 0), 2)
    return {
        "items": items,
        "totals": {"contracts_monthly": s(con_, "monthly"), "contracts_yearly": s(con_, "yearly"),
                   "loans_monthly": s(loans, "monthly"), "loans_yearly": s(loans, "yearly"),
                   "loans_balance": s(loans, "balance"), "loans_interest_year": s(loans, "interest_year"),
                   "abos_monthly": s(abos, "monthly"), "abos_yearly": s(abos, "yearly"), "abos_count": len(abos),
                   "paid_ytd": s(act, "paid_ytd"), "by_category": by_cat},
        "notices": sorted([{"id": c["id"], "name": c["name"], "notice_by": c["notice_by"], "notice_days": c["notice_days"],
                            "term_end": c["term_end"]} for c in act if c["notice_days"] is not None and 0 <= c["notice_days"] <= (cfg["contract_notice_days"] or 120)],
                          key=lambda x: x["notice_days"]),
        "categories": cfg["contract_categories"], "loan_categories": cfg["loan_categories"], "intervals": list(INTERVAL_MONTHS),
        "abo_categories": cfg.get("abo_categories") or [],
        "notice_days": cfg["contract_notice_days"], "badge_days": cfg["contract_badge_days"],
    }


@router.get("/api/contracts/{cid}")
def contract_detail(cid: int):
    today = date.today()
    pub = core.PAPERLESS_PUBLIC_URL
    with core.db() as con:
        auto_assign_contracts(con)
        c = _contract(con, cid)
        versions = _versions(con, cid)
        c.update(compute(c, versions, con, today))
        pays = [dict(r) for r in con.execute(
            "SELECT t.id AS tx_id, t.date, t.amount, t.merchant, t.text, t.matched_doc_id, cp.auto FROM contract_payments cp "
            "JOIN transactions t ON t.id = cp.tx_id WHERE cp.contract_id = ? ORDER BY t.date DESC", (cid,))]
    for v in versions:
        v["doc_url"] = f"{pub}/documents/{v['doc_id']}/details" if v["doc_id"] else None
        bal = loan_balance(c, versions, _d(v["valid_from"]) or today) if c["kind"] == "kredit" else None
        v["per_interval"] = _period_cost(c, v, bal)
        v["monthly"] = _monthly(v, v["per_interval"])
    for p in pays:
        p["doc_url"] = f"{pub}/documents/{p['matched_doc_id']}/details" if p["matched_doc_id"] else None
    years = sorted({int(p["date"][:4]) for p in pays} | {today.year}, reverse=True)[:6]
    per_year = [{"year": y, "paid": round(-sum(p["amount"] for p in pays if p["date"].startswith(str(y))), 2),
                 "count": sum(1 for p in pays if p["date"].startswith(str(y))),
                 "expected": expected_in_year(c, versions, y, today if y == today.year else None)} for y in years]
    schedule = []
    if c["kind"] == "kredit" and c["principal"] is not None:
        for y in range(today.year, today.year + 6):
            b = loan_balance(c, versions, date(y, 12, 31))
            schedule.append({"year": y, "balance_end": b})
            if not b:
                break
    return {"contract": c, "versions": versions, "payments": pays, "per_year": per_year, "schedule": schedule}


@router.delete("/api/contracts/{cid}", status_code=204)
def contract_delete(cid: int):
    with core.db() as con:
        con.execute("DELETE FROM contract_versions WHERE contract_id = ?", (cid,))
        con.execute("DELETE FROM contract_payments WHERE contract_id = ?", (cid,))
        con.execute("DELETE FROM contracts WHERE id = ?", (cid,))
    return Response(status_code=204)


@router.get("/api/contracts/{cid}/docs")
async def contract_docs(cid: int):
    """Paperless-Dokumente zum Vertrag (Korrespondent und/oder Tag) plus die in Versionen verknüpften."""
    with core.db() as con:
        c = _contract(con, cid)
        linked = {r[0] for r in con.execute("SELECT doc_id FROM contract_versions WHERE contract_id = ? AND doc_id IS NOT NULL", (cid,))}
    meta = await core.get_meta()
    fid = meta["amount_field"]["id"] if meta["amount_field"] else None
    raw: dict[int, dict] = {}
    params = {"page_size": 60, "ordering": "-created", "fields": core.DOC_FIELDS}
    if c["correspondent_id"] or c["tag_id"]:
        p = dict(params)
        if c["correspondent_id"]:
            p["correspondent__id__in"] = c["correspondent_id"]
        if c["tag_id"]:
            p["tags__id__all"] = c["tag_id"]
        for d in (await core.pl_request("GET", "/api/documents/", params=p)).get("results", []):
            raw[d["id"]] = d
    missing = [i for i in linked if i not in raw]
    if missing:
        p = dict(params, id__in=",".join(map(str, missing)))
        for d in (await core.pl_request("GET", "/api/documents/", params=p)).get("results", []):
            raw[d["id"]] = d
    docs = [core.normalize(d, fid, []) for d in raw.values()]
    await core.attach_purposes(docs)
    types = {t["id"]: t["name"] for t in meta["document_types"]}
    out = [{"id": d["id"], "date": d["date"], "title": d["title"], "purpose": d.get("purpose"), "amount": d["amount"],
            "url": d["url"], "type": types.get(d["document_type"]), "linked": d["id"] in linked} for d in docs]
    return sorted(out, key=lambda x: x["date"], reverse=True)


@router.post("/api/contracts/{cid}/version-from-doc")
async def version_from_doc(cid: int, body: dict = Body(...)):
    """Neue Version aus einem Paperless-Beleg: Datum und Betrag übernehmen, Intervall der aktuellen Version."""
    doc_id = int(body["doc_id"])
    meta = await core.get_meta()
    fid = meta["amount_field"]["id"] if meta["amount_field"] else None
    d = core.normalize(await core.pl_request("GET", f"/api/documents/{doc_id}/"), fid, [])
    with core.db() as con:
        _contract(con, cid)
        versions = _versions(con, cid)
        cur = versions[-1] if versions else None
        con.execute("INSERT INTO contract_versions(contract_id, valid_from, amount, interval, interest_rate, amortization, doc_id, note) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (cid, body.get("valid_from") or d["date"], d["amount"], (cur or {}).get("interval") or "monat",
                     (cur or {}).get("interest_rate"), (cur or {}).get("amortization"), doc_id, d["title"][:120]))
    return {"ok": True}


@router.get("/api/contracts/{cid}/tx-search")
def tx_search(cid: int, q: str = ""):
    """Belastungen zum manuellen Zuordnen (ohne bereits einem Vertrag zugeordnete)."""
    like = f"%{q}%"
    with core.db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT t.id, t.date, t.amount, t.merchant, t.text, cp.contract_id FROM transactions t "
            "LEFT JOIN contract_payments cp ON cp.tx_id = t.id "
            "WHERE t.amount < 0 AND t.status != 'ignored' AND (t.merchant LIKE ? OR t.text LIKE ?) "
            "AND (cp.contract_id IS NULL OR cp.contract_id = 0) ORDER BY t.date DESC LIMIT 40", (like, like))]
    return rows


@router.post("/api/contract-payments")
def assign_payment(body: dict = Body(...)):
    tx_id, cid = int(body["tx_id"]), int(body["contract_id"])
    with core.db() as con:
        if not con.execute("SELECT 1 FROM transactions WHERE id = ?", (tx_id,)).fetchone():
            raise HTTPException(404, "Buchung nicht gefunden")
        _contract(con, cid)
        con.execute("INSERT OR REPLACE INTO contract_payments(tx_id, contract_id, auto) VALUES (?, ?, 0)", (tx_id, cid))
    return {"ok": True}


@router.delete("/api/contract-payments/{tx_id}", status_code=204)
def unassign_payment(tx_id: int):
    """Zuordnung lösen; die Buchung wird danach nicht mehr automatisch zugeordnet."""
    with core.db() as con:
        con.execute("INSERT OR REPLACE INTO contract_payments(tx_id, contract_id, auto) VALUES (?, 0, 0)", (tx_id,))
    return Response(status_code=204)


# ---------------------------------------------------------------- Abos: wiederkehrende Zahlungen erkennen
ABO_HINTS = re.compile(r"netflix|spotify|apple\.com|itunes|google|youtube|disney|amazon prime|prime video|audible|microsoft|office ?365|"
                       r"adobe|dropbox|icloud|openai|chatgpt|anthropic|claude|github|zattoo|teleboy|blue tv|sky|dazn|deezer|tidal|"
                       r"nzz|tages-?anzeiger|blick|watson|aargauer zeitung|20 ?minuten|zeitung|abo\b|abonnement|mitgliedschaft|"
                       r"fitness|migros fitness|basefit|update fitness|clever fit|mobility|publibike|tier\b|lime\b|swisspass|halbtax|ga\b",
                       re.I)
_NORM_DROP = re.compile(r"\d{2}\.\d{2}\.\d{2,4}|\b\d[\d.,:/-]*\b|kartenzahlung|einkauf|debit|maestro|mastercard|visa|twint|"
                        r"lastschrift|e-banking|zahlung|belastung|gutschrift|ch\d{2}[\d ]+|[^a-zäöüéèà& ]", re.I)


def _abo_key(t: dict) -> str:
    base = t.get("merchant") or (t.get("text") or "").split(" · ")[0]
    words = [w for w in _NORM_DROP.sub(" ", base.lower()).split() if len(w) > 1][:3]
    return " ".join(words)


def _cadence(days: list[int]) -> Optional[str]:
    if not days:
        return None
    med = sorted(days)[len(days) // 2]
    for name, lo, hi in (("monat", 26, 35), ("quartal", 84, 98), ("halbjahr", 165, 200), ("jahr", 330, 400)):
        if lo <= med <= hi and all(lo - 6 <= d <= hi + 6 for d in days):
            return name
    return None


@router.get("/api/abo-suggestions")
def abo_suggestions():
    """Belastungen, die regelmässig mit (fast) gleichem Betrag wiederkommen und noch keinem Vertrag/Abo zugeordnet sind."""
    cfg = core.get_settings()
    ignored = set(cfg.get("abo_ignore") or [])
    since = (date.today() - timedelta(days=430)).isoformat()
    with core.db() as con:
        assigned = {r[0] for r in con.execute("SELECT tx_id FROM contract_payments WHERE contract_id != 0")}
        txs = [dict(r) for r in con.execute(
            "SELECT id, date, amount, merchant, text, matched_title FROM transactions WHERE amount < 0 AND status != 'ignored' AND date >= ? "
            "ORDER BY date", (since,))]
        match_texts = [r[0] for r in con.execute("SELECT match_text FROM contracts WHERE match_text IS NOT NULL AND match_text != ''")]
    groups: dict[str, list] = {}
    for t in txs:
        if t["id"] in assigned:
            continue
        hay = f"{t['merchant'] or ''} {t['text']}"
        if any(re_api._kw_match(mt, hay) for mt in match_texts):
            continue
        k = _abo_key(t)
        if k and k not in ignored:
            groups.setdefault(k, []).append(t)
    out = []
    today = date.today()
    for k, ts in groups.items():
        # nach Betrag bündeln (±10 %, mind. 1 CHF) – ein Händler kann mehrere Abos haben
        clusters: list[list] = []
        for t in ts:
            a = -t["amount"]
            for cl in clusters:
                ref = -cl[0]["amount"]
                if abs(a - ref) <= max(1.0, ref * 0.10):
                    cl.append(t)
                    break
            else:
                clusters.append([t])
        for cl in clusters:
            dates = [date.fromisoformat(t["date"]) for t in cl]
            gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
            cad = _cadence(gaps)
            hint = bool(ABO_HINTS.search(f"{cl[0]['merchant'] or ''} {cl[0]['text']}"))
            if not ((cad == "monat" and len(cl) >= 3) or (cad in ("quartal", "halbjahr", "jahr") and len(cl) >= 2) or
                    (hint and (cad or len(cl) == 1))):
                continue
            months = INTERVAL_MONTHS.get(cad or "monat", 1)
            last = dates[-1]
            nxt = _add_months(last, months)
            if (today - last).days > months * 31 + 45:     # seit längerem nicht mehr belastet -> vermutlich gekündigt
                continue
            amt = round(-cl[-1]["amount"], 2)
            out.append({"key": k, "name": (cl[-1]["merchant"] or k).title() if not cl[-1]["merchant"] else cl[-1]["merchant"],
                        "amount": amt, "interval": cad or "monat", "count": len(cl), "first": cl[0]["date"], "last": cl[-1]["date"],
                        "next": nxt.isoformat(), "monthly": round(amt / months, 2), "certain": bool(cad), "hint": hint,
                        "tx_ids": [t["id"] for t in cl], "sample": cl[-1]["text"][:90]})
    return sorted(out, key=lambda x: (not x["certain"], -x["monthly"]))


@router.post("/api/abo-suggestions/accept")
def accept_abo(body: dict = Body(...)):
    """Vorschlag als Abo anlegen: Vertrag (kind=abo) mit Version, Stichwort und den bisherigen Zahlungen."""
    name, amount, interval = body["name"], float(body["amount"]), body.get("interval") or "monat"
    first = body.get("first") or date.today().isoformat()
    # Rhythmus an der letzten Zahlung ausrichten (Abbuchungsdatum kann sich verschieben), Beginn = davor liegende Fälligkeit
    m = INTERVAL_MONTHS.get(interval, 1) or 1
    if body.get("last"):
        anchor = date.fromisoformat(body["last"])
        while anchor > date.fromisoformat(first):
            anchor = _add_months(anchor, -m)
        first = anchor.isoformat()
    with core.db() as con:
        cur = con.execute("INSERT INTO contracts(kind, name, category, provider, start, match_text, active, notice_months, renew_months) "
                          "VALUES ('abo', ?, ?, ?, ?, ?, 1, ?, ?)",
                          (name, body.get("category"), name, first, body.get("keyword") or body.get("key") or name,
                           body.get("notice_months"), INTERVAL_MONTHS.get(interval, 1) or None))
        cid = cur.lastrowid
        con.execute("INSERT INTO contract_versions(contract_id, valid_from, amount, interval) VALUES (?, ?, ?, ?)",
                    (cid, first, amount, interval))
        for tx in body.get("tx_ids") or []:
            con.execute("INSERT OR REPLACE INTO contract_payments(tx_id, contract_id, auto) VALUES (?, ?, 0)", (int(tx), cid))
    return {"id": cid}


@router.post("/api/abo-suggestions/ignore")
def ignore_abo(body: dict = Body(...)):
    cfg = core.get_settings()
    keys = sorted(set(cfg.get("abo_ignore") or []) | {body["key"]})
    with core.db() as con:
        con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('abo_ignore', ?)", (core.json.dumps(keys),))
    return {"ok": True}
