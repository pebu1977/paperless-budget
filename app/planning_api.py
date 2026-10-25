"""API: Vormerkungen (Bestellungen, die noch nicht verrechnet sind) und "Rest vom Lohn".

Bestellungen kommen auf zwei Wegen:
  A) Paperless: Bestellbestätigungen (z.B. per Mail-Regel importiert) mit dem Tag ORDER_TAG_NAME ("Bestellung").
     Betrag aus dem Feld "Total" oder aus dem Text ("Total CHF 129.00").
  C) von Hand in der App.
Eine offene Vormerkung wird automatisch erledigt, sobald eine passende Rechnung (Paperless) oder
Belastung (Kontoauszug) vom gleichen Händler mit etwa gleichem Betrag auftaucht.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response

from . import bank
from . import contracts_api as ct
from . import main as core

router = APIRouter()
def _order_name() -> str:
    return core.get_settings()["order_tag_name"]


def _expect_days() -> int:
    return int(core.get_settings()["order_expect_days"] or 30)

with core.db() as _con:
    _con.executescript(
        """
        CREATE TABLE IF NOT EXISTS planned (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,                    -- Bestelldatum
            expected TEXT,                         -- Rechnung / Abbuchung erwartet am
            merchant TEXT NOT NULL, description TEXT,
            amount REAL,                           -- NULL = Betrag nicht erkannt
            source TEXT NOT NULL DEFAULT 'manuell', -- manuell | paperless
            doc_id INTEGER UNIQUE, correspondent_id INTEGER,
            status TEXT NOT NULL DEFAULT 'open',   -- open | done | cancelled
            done_ref TEXT, done_title TEXT, done_at TEXT, note TEXT
        );
        """
    )

# Rechnungs-/Bestell-Total: Stichwort, dann (optional) Währung, dann Betrag mit 2 Nachkommastellen
_TOTAL = re.compile(
    r"(?:gesamtbetrag|gesamtsumme|gesamttotal|rechnungsbetrag|bestellwert|bestellsumme|endbetrag|zu bezahlen|zahlbetrag|"
    r"grand total|order total|total|summe|betrag)\b[^\n\d]{0,40}?(?:chf|fr\.|sfr\.?|eur|€)?\s*([\d'’]{1,7}[.,]\d{2})(?!\d)",
    re.IGNORECASE)
_SKIP = re.compile(r"zwischen|sub|ersparnis|gespart|rabatt|mwst|mehrwertsteuer|versand|porto|gutschein", re.IGNORECASE)


_STRONG = re.compile(r"zu bezahlen|zahlbetrag|gesamtbetrag|endbetrag|rechnungsbetrag|grand total|order total|total\s+inkl|gesamttotal", re.IGNORECASE)


def amount_from_text(text: str) -> Optional[float]:
    """Bestell-Total aus dem Text: letzter "starker" Treffer (Zu bezahlen, Gesamtbetrag, Total inkl. MwSt …),
    sonst der grösste Betrag hinter Total/Summe. Zwischensummen, MWST, Versand, Rabatt werden ignoriert."""
    strong, weak = [], []
    for line in (text or "").splitlines():
        for m in _TOTAL.finditer(line):
            if _SKIP.search(line[:m.start()]) or _SKIP.match(m.group(0)):
                continue
            v = float(m.group(1).replace("'", "").replace("’", "").replace(",", "."))
            if v <= 0:
                continue
            (strong if _STRONG.search(m.group(0)) else weak).append(v)
    if strong:
        return strong[-1]
    return max(weak) if weak else None


async def order_tag_id() -> Optional[int]:
    meta = await core.get_meta()
    return next((t["id"] for t in meta["tags"] if t["name"].strip().lower() == _order_name().strip().lower()), None)


async def import_orders() -> dict:
    """Neue Bestellbestätigungen mit dem Tag aus Paperless übernehmen."""
    tid = await order_tag_id()
    if not tid:
        return {"tag": None, "added": 0}
    meta = await core.get_meta()
    fid = meta["amount_field"]["id"] if meta["amount_field"] else None
    corr = {c["id"]: c["name"] for c in meta["correspondents"]}
    since = (date.today() - timedelta(days=180)).isoformat()
    docs = await core.pl_all("/api/documents/", {"tags__id__all": tid, "created__date__gt": since,
                                                 "fields": core.DOC_FIELDS + ",content"})
    with core.db() as con:
        known = {r[0] for r in con.execute("SELECT doc_id FROM planned WHERE doc_id IS NOT NULL")}
        no_amount = {r[0] for r in con.execute(
            "SELECT doc_id FROM planned WHERE doc_id IS NOT NULL AND amount IS NULL AND status = 'open'")}
    added = 0
    for d in docs:
        n = core.normalize(d, fid, [])
        amount = n["amount"] if n["amount"] else amount_from_text(d.get("content") or "")
        if d["id"] in known:
            if d["id"] in no_amount and amount:     # Betrag inzwischen erfasst (Feld "Total" in Paperless gesetzt)
                with core.db() as con:
                    con.execute("UPDATE planned SET amount = ? WHERE doc_id = ?", (amount, d["id"]))
            continue
        od = _d(n["date"]) or date.today()
        with core.db() as con:
            con.execute("INSERT OR IGNORE INTO planned(date, expected, merchant, description, amount, source, doc_id, correspondent_id) "
                        "VALUES (?, ?, ?, ?, ?, 'paperless', ?, ?)",
                        (od.isoformat(), (od + timedelta(days=_expect_days())).isoformat(),
                         corr.get(n["correspondent"]) or n["title"][:60], n["title"], amount, d["id"], n["correspondent"]))
        added += 1
    return {"tag": tid, "added": added}


def _d(s: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(s[:10]) if s else None
    except ValueError:
        return None


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.5, 0.01 * a)


def _done(con, pid: int, ref: str, title: str):
    con.execute("UPDATE planned SET status = 'done', done_ref = ?, done_title = ?, done_at = ? WHERE id = ?",
                (ref, title[:120], datetime.now().isoformat(timespec="seconds"), pid))


async def auto_clear() -> int:
    """Offene Vormerkungen mit Rechnung oder Belastung abgleichen."""
    with core.db() as con:
        open_ = [dict(r) for r in con.execute("SELECT * FROM planned WHERE status = 'open' AND amount IS NOT NULL")]
    if not open_:
        return 0
    first = min(_d(p["date"]) for p in open_) - timedelta(days=3)
    n = 0
    used: set[str] = set()
    with core.db() as con:
        used |= {r[0] for r in con.execute("SELECT done_ref FROM planned WHERE done_ref IS NOT NULL")}
        txs = [dict(r) for r in con.execute(
            "SELECT id, date, amount, merchant, text FROM transactions WHERE amount < 0 AND status != 'ignored' AND date >= ?",
            (first.isoformat(),))]
    # Rechnungen aus Paperless (ohne Bestell-Tag)
    invoices = []
    try:
        tid = await order_tag_id()
        invoices = [d for d in await core.fetch_expenses(first, date.today() + timedelta(days=1), include_excluded=True)
                    if not tid or tid not in d["tags"]]
    except HTTPException:
        pass
    meta = await core.get_meta()
    corr = {c["id"]: c["name"] for c in meta["correspondents"]}
    with core.db() as con:
        for p in open_:
            od = _d(p["date"]) - timedelta(days=3)
            limit = _d(p["date"]) + timedelta(days=120)
            toks = bank._tokens(p["merchant"])
            hit = None
            for inv in invoices:
                ref = f"doc:{inv['id']}"
                if ref in used or inv["id"] == p["doc_id"] or inv["amount"] is None:
                    continue
                idate = _d(inv["date"])
                if not idate or not (od <= idate <= limit) or not _close(p["amount"], inv["amount"]):
                    continue
                same = (p["correspondent_id"] and inv["correspondent"] == p["correspondent_id"]) or \
                       (toks & bank._tokens(f"{corr.get(inv['correspondent'], '')} {inv['title']}"))
                if same:
                    hit = (ref, f"Rechnung: {inv['title']}")
                    break
            if not hit:
                for t in txs:
                    ref = f"tx:{t['id']}"
                    tdate = _d(t["date"])
                    if ref in used or not (od <= tdate <= limit) or not _close(p["amount"], -t["amount"]):
                        continue
                    if toks & bank._tokens(f"{t['merchant'] or ''} {t['text']}"):
                        hit = (ref, f"Bank: {t['merchant'] or t['text'][:60]}")
                        break
            if hit:
                _done(con, p["id"], *hit)
                used.add(hit[0])
                n += 1
    return n


# ---------------------------------------------------------------- Rest vom Lohn
def recurring_in_month(start: date, end: date, today: date) -> list[dict]:
    """Fällige Vertrags-/Kreditzahlungen im Monat, mit Status bezahlt/offen (volle Beträge, keine Verteilung)."""
    out = []
    with core.db() as con:
        for c in [dict(r) for r in con.execute("SELECT * FROM contracts WHERE active = 1")]:
            versions = ct._versions(con, c["id"])
            if not ct.is_due(c, versions, start):
                continue
            anchor = ct._anchor(c, versions)
            due = date(start.year, start.month, min(anchor.day, 28)) if anchor else start
            v = ct._version_at(versions, start)
            bal = ct.loan_balance(c, versions, start) if c["kind"] == "kredit" else None
            amount = ct._period_cost(c, v, bal)
            pays = con.execute(
                "SELECT t.date, t.amount FROM contract_payments cp JOIN transactions t ON t.id = cp.tx_id "
                "WHERE cp.contract_id = ? AND ((t.date >= ? AND t.date < ?) OR (t.date >= ? AND t.date <= ?))",
                (c["id"], start.isoformat(), end.isoformat(),
                 (due - timedelta(days=12)).isoformat(), (due + timedelta(days=12)).isoformat())).fetchall()
            paid = round(-sum(p["amount"] for p in pays), 2)
            out.append({"contract_id": c["id"], "name": c["name"], "kind": c["kind"], "category": c["category"],
                        "due": due.isoformat(), "amount": amount, "interval": v["interval"] if v else None,
                        "paid": paid, "paid_date": pays[0]["date"] if pays else None, "status": "bezahlt" if pays else "offen",
                        "overdue": not pays and due < today})
    return sorted(out, key=lambda x: x["due"])


@router.get("/api/plan")
async def plan(month: str):
    start, end = core.month_range(month)
    today = date.today()
    try:
        await import_orders()
        await auto_clear()
    except HTTPException:
        pass
    s = await core.summary(month)
    recurring = recurring_in_month(start, end, today)
    due_open = round(sum((r["amount"] or 0) for r in recurring if r["status"] == "offen"), 2)
    cur_m = start <= today < end
    with core.db() as con:
        orders = [dict(r) for r in con.execute("SELECT * FROM planned WHERE status = 'open' ORDER BY expected, date")]
        recent_done = [dict(r) for r in con.execute(
            "SELECT * FROM planned WHERE status = 'done' AND done_at >= ? ORDER BY done_at DESC LIMIT 5",
            ((today - timedelta(days=14)).isoformat(),))]
    pub = core.PAPERLESS_PUBLIC_URL
    for o in orders + recent_done:
        o["url"] = f"{pub}/documents/{o['doc_id']}/details" if o["doc_id"] else None
    # offene Bestellungen zählen im Monat, in dem sie erwartet werden; überfällige im laufenden Monat
    in_month = [o for o in orders if (start.isoformat() <= (o["expected"] or o["date"]) < end.isoformat())
                or (cur_m and (o["expected"] or o["date"]) < start.isoformat())]
    if start > today:      # Zukunft: nur was dann erwartet wird
        in_month = [o for o in orders if start.isoformat() <= (o["expected"] or o["date"]) < end.isoformat()]
    elif end <= today:     # Vergangenheit: keine offenen Vormerkungen mehr einrechnen
        in_month = []
    orders_sum = round(sum(o["amount"] or 0 for o in in_month), 2)
    income = s["total_income"]
    free = round(income - s["total_spent"] - due_open - orders_sum, 2)
    return {"month": month, "income": income, "spent": s["total_spent"], "due_open": due_open, "orders": orders_sum,
            "free": free, "recurring": recurring, "orders_in_month": [o["id"] for o in in_month], "open_orders": orders,
            "recent_done": recent_done, "order_tag": _order_name(), "order_tag_found": bool(await order_tag_id()),
            "past": end <= today}


# ---------------------------------------------------------------- Vormerkungen bearbeiten
FIELDS = ["date", "expected", "merchant", "description", "amount", "note", "status"]


@router.post("/api/orders", status_code=201)
def add_order(body: dict = Body(...)):
    if not (body.get("merchant") or "").strip():
        raise HTTPException(400, "Händler angeben")
    od = _d(body.get("date")) or date.today()
    exp = _d(body.get("expected")) or od + timedelta(days=_expect_days())
    with core.db() as con:
        cur = con.execute("INSERT INTO planned(date, expected, merchant, description, amount, note, source) VALUES (?, ?, ?, ?, ?, ?, 'manuell')",
                          (od.isoformat(), exp.isoformat(), body["merchant"].strip(), body.get("description"),
                           body.get("amount"), body.get("note")))
        return dict(con.execute("SELECT * FROM planned WHERE id = ?", (cur.lastrowid,)).fetchone())


@router.put("/api/orders/{oid}")
def update_order(oid: int, body: dict = Body(...)):
    data = {k: (None if body[k] == "" else body[k]) for k in FIELDS if k in body}
    if data.get("status") == "open":
        data.update(done_ref=None, done_title=None, done_at=None)
    elif data.get("status") in ("done", "cancelled"):
        data.update(done_at=datetime.now().isoformat(timespec="seconds"),
                    done_title=body.get("done_title") or ("von Hand erledigt" if data["status"] == "done" else "storniert"))
        data.setdefault("done_ref", f"manual:{oid}")
    if data:
        with core.db() as con:
            con.execute(f"UPDATE planned SET {', '.join(k + ' = ?' for k in data)} WHERE id = ?", [*data.values(), oid])
    return {"ok": True}


@router.delete("/api/orders/{oid}", status_code=204)
def delete_order(oid: int):
    """Manuelle löschen; Paperless-Vormerkungen nur stornieren (sonst würden sie neu importiert)."""
    with core.db() as con:
        r = con.execute("SELECT source FROM planned WHERE id = ?", (oid,)).fetchone()
        if r and r["source"] == "paperless":
            con.execute("UPDATE planned SET status = 'cancelled', done_title = 'gelöscht', done_ref = ? WHERE id = ?", (f"manual:{oid}", oid))
        else:
            con.execute("DELETE FROM planned WHERE id = ?", (oid,))
    return Response(status_code=204)
