"""Kontoauszüge lesen (OCR-Text aus Paperless) und Buchungen mit Rechnungen abgleichen.

Bewusst ohne Abhängigkeit zu FastAPI/Paperless, damit es sich isoliert testen lässt.

Getestet mit PostFinance, ZKB und Bank Zimmerberg. Zwei Layouts werden automatisch erkannt:
  A  "[Datum] Text Betrag Valuta [Saldo]"   (PostFinance, ZKB)
  B  "Datum Text Valuta Betrag [Saldo]"     (Bank Zimmerberg u.ä.)
  * Das Buchungsdatum gilt für Folgebuchungen ohne eigenes Datum weiter.
  * Folgezeilen gehören zum Buchungstext, auch über Seitenumbrüche (Seitenkopf wird übersprungen).
  * Der Saldo steht nur bei einzelnen Buchungen (in B teils in der Folgezeile). Zwischen zwei Saldi
    werden die Vorzeichen so bestimmt, dass die Summe exakt aufgeht – ausgehend von Stichworten.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from itertools import combinations
from datetime import date
from typing import Optional

AMT = r"\d{1,3}(?:[ '’  ]\d{3})*\.\d{2}"
DATE = r"\d{2}\.\d{2}\.(?:\d{4}|\d{2})"
DATE_ANY_RE = re.compile(DATE)
# Layout A – "[Datum] Text Betrag Valuta [Saldo]"   (PostFinance, ZKB)
BOOKING_A_RE = re.compile(rf"^\s*(?:(?P<date>{DATE})\s+)?(?P<text>.*?)\s+(?P<amt>-?{AMT})\s+(?P<valuta>{DATE})(?:\s+(?P<saldo>-?{AMT}))?\s*$")
# Layout B – "Datum Text Valuta Betrag [Saldo]"     (z.B. Bank Zimmerberg, Regionalbanken)
BOOKING_B_RE = re.compile(rf"^\s*(?P<date>{DATE})\s+(?P<text>.*?)\s+(?P<valuta>{DATE})\s+(?P<amt>-?{AMT})(?:\s+(?P<saldo>-?{AMT}))?\s*$")
TRAIL_AMT_RE = re.compile(rf"^(?P<text>.*?)\s*(?<![\d.'’])(?P<amt>-?{AMT})\s*$")
LEAD_DATE_RE = re.compile(rf"^\s*({DATE})\b")

BALANCE_RE = re.compile(r"kontostand|saldovortrag|anfangssaldo|alter saldo|saldo per|schlusssaldo|neuer saldo", re.I)
END_RE = re.compile(r"^\s*(total|umsatztotal|summe|additionen)\b|bitte überprüfen sie|wir bitten sie|freundliche grüsse", re.I)
PAGE_RE = re.compile(r"\bseite\s+\d+\s*(/|von)\s*\d+|\b[A-Z]{2}\s*-\s*\d+/\d+\s*$", re.I)
COLHEAD_RE = re.compile(r"^\s*datum\s+(text|buchungstext|beschreibung|geschäftsvorgang)", re.I)
# Füllzeilen: überspringen, die Buchung läuft aber weiter (Text kann über Seiten gehen)
NOISE_RE = re.compile(
    r"^\s*(datum\s+\d{2}\.\d{2}\.\d{4}\s*$|iban\b|kontonummer|konto-nr|bic\b|"
    r"\d{5}\s+[a-z]{2}\s+\d{6}\.\d{2}\s*$|karten nr\.|kauf/dienstleistung vom\s*$|vom\s+\d{2}\.\d{2}\.\d{4}\s*$|"
    rf"{DATE}\s*$|referenzen:|notprovided|sender referenz:|\d{{6,}}\s*$|[a-z0-9]{{14,}}\s*$|[\d*\-]{{10,}}\s*$)",
    re.I,
)
CARD_RE = re.compile(r"\s*(kartennummer|kartennr)\s*:.*$", re.I)
# Kartennummer (letzte 4 Ziffern) für Regeln, z.B. "Tankomat + 1234"
CARDNUM_RE = re.compile(r"(kartennummer|kartennr|karten nr\.)\s*:?\s*(?:[\dX*]*(\d{4})(?:-\d)?\b)?", re.I)
CARD_ONLY_RE = re.compile(r"^\s*[\dX*]*(\d{4})(?:-\d)?\s*$")
CREDIT_RE = re.compile(
    r"gutschrift|übertrag|überweisung von|eingang|einzahlung|lohn|gehalt|salär|rückerstattung|vergütung|retoure|storno",
    re.I,
)


@dataclass
class Booking:
    date: str            # ISO JJJJ-MM-TT
    text: str
    amount: float        # negativ = Belastung, positiv = Gutschrift
    sign_source: str     # "saldo" (geprüft) | "korrigiert" (über Saldo bestimmt) | "stichwort" (ungeprüft)
    merchant: str = ""   # Händler / Empfänger / Auftraggeber (für Kategorie-Regeln)
    card: str = ""       # letzte 4 Ziffern der Karte, falls angegeben
    hash: str = ""


@dataclass
class _Raw:
    date: str
    value: float
    head: str
    saldo: Optional[float]
    lines: list[str] = field(default_factory=list)
    sign: int = -1
    source: str = "stichwort"
    card: str = ""


def _num(s: str) -> float:
    return float(re.sub(r"[ '’  ]", "", s))


def _iso(d: str) -> Optional[str]:
    dd, mm, yy = d.split(".")
    year = int(yy) + (2000 if len(yy) == 2 else 0)
    try:
        return date(year, int(mm), int(dd)).isoformat()
    except ValueError:
        return None


def _solve(group: list[_Raw], delta: float) -> Optional[list[int]]:
    """Vorzeichen so wählen, dass die Summe der Saldo-Differenz entspricht (möglichst wenige Stichwort-Vorzeichen umdrehen)."""
    base = [g.sign for g in group]
    n = len(group)
    max_k = n if n <= 14 else 3
    for k in range(0, max_k + 1):
        for flip in combinations(range(n), k):
            signs = [(-s if i in flip else s) for i, s in enumerate(base)]
            if abs(sum(s * g.value for s, g in zip(signs, group)) - delta) < 0.005:
                return signs
    return None


def _apply(group: list[_Raw], signs: list[int]) -> None:
    for g, s in zip(group, signs):
        g.source = "saldo" if s == g.sign else "korrigiert"
        g.sign = s


_DETAIL_RE = re.compile(
    r"^(shopping vom|betrag in kontowährung|\d+(\.\d+)?% bearbeitungszuschlag|chf\s|eur\s|usd\s|"
    r"im inland vom|auftraggeber:|absender:|mitteilungen:|waren\b|vom\s|[\d' .]+$|auftrags-nr)",
    re.I,
)
_POSTCODE_RE = re.compile(r"^\d{4}\s+\S")
_TYPE_PREFIX_RE = re.compile(
    r"^(auftrag mobile banking|auftrag e-banking|auftrag|übertrag( von)?|vergütung|gehaltszahlung|gutschrift,?|"
    r"belastung( \(\d\))?|lastschrift|zahlung|lsv|ebill)\s*",
    re.I,
)


def guess_merchant(head: str, lines: list[str]) -> str:
    """Gegenpartei erraten: Kartenzahlung -> Händler; Lastschrift/Gutschrift -> Empfänger bzw. Auftraggeber."""
    # Kartenzahlung mit Uhrzeit im Kopf (z.B. "Debitkarten-Einkauf 01.05.2026 12:58 Drogerie" + "Moll AG")
    t = re.search(r"\b\d{2}:\d{2}\b\s*(.*)$", head)
    if t:
        first = t.group(1).strip()
        nxt = lines[0] if lines and not _POSTCODE_RE.match(lines[0]) else ""
        name = f"{first} {nxt}".strip()
        if name:
            return name
    h = head.upper()
    joined = " ".join(lines).upper()
    if "AUFTRAGGEBER" in joined or "ABSENDER" in joined:
        for i, l in enumerate(lines):
            if re.match(r"(auftraggeber|absender):", l, re.I) and i + 1 < len(lines):
                return lines[i + 1]
    if h.startswith("LASTSCHRIFT") or "E-RECHNUNG" in h or "EBILL" in h:
        # PostFinance: 1. Block = Bank des Empfängers (bis PLZ-Zeile), danach der Empfänger
        for i, l in enumerate(lines):
            if _POSTCODE_RE.match(l) and i + 1 < len(lines):
                return lines[i + 1]
    if h.startswith(("APPLE PAY", "TWINT", "KAUF/", "PREIS FÜR", "LADUNG")):
        for l in lines:
            if not _DETAIL_RE.match(l) and not _POSTCODE_RE.match(l):
                return l
    rest = DATE_ANY_RE.sub("", _TYPE_PREFIX_RE.sub("", head)).strip(" ,")
    rest = re.sub(r"auftrags-nr\.?\s*\S+", "", rest, flags=re.I).strip(" ,")
    if rest:
        return rest
    for l in lines:
        if not _DETAIL_RE.match(l) and not _POSTCODE_RE.match(l):
            return l
    return head


def detect_layout(lines: list[str]) -> str:
    a = sum(1 for l in lines if BOOKING_A_RE.match(l) and not BOOKING_B_RE.match(l))
    b = sum(1 for l in lines if BOOKING_B_RE.match(l) and not BOOKING_A_RE.match(l))
    return "B" if b > a else "A"


def parse_statement(content: str, doc_id: int = 0) -> list[Booking]:
    lines = [l for l in (content or "").splitlines() if l.strip()]
    layout = detect_layout(lines)
    booking_re = BOOKING_B_RE if layout == "B" else BOOKING_A_RE

    raws: list[_Raw] = []
    cur: Optional[_Raw] = None
    cur_date: Optional[str] = None
    saldo: Optional[float] = None
    group: list[_Raw] = []
    in_header = False
    expect_card = False

    def checkpoint(new_saldo: float):
        nonlocal saldo, group
        if saldo is not None and group:
            signs = _solve(group, round(new_saldo - saldo, 2))
            if signs:
                _apply(group, signs)
        saldo, group = new_saldo, []

    for line in lines:
        if END_RE.search(line):
            cur = None
            continue
        if PAGE_RE.search(line):
            in_header = True
            continue
        m = booking_re.match(line)
        if in_header:
            if COLHEAD_RE.match(line):
                in_header = False
                continue
            if not m:
                continue
            in_header = False
        if not m and BALANCE_RE.search(line):
            amts = re.findall(rf"-?{AMT}", DATE_ANY_RE.sub(" ", line))
            if amts:
                checkpoint(_num(amts[-1]))
            lead = LEAD_DATE_RE.match(line)
            if lead:
                cur_date = _iso(lead.group(1)) or cur_date
            cur = None
            continue
        if cur is not None and not m:
            cm = CARDNUM_RE.search(line)
            if cm:
                if cm.group(2):
                    cur.card = cm.group(2)
                expect_card = not cm.group(2)
            elif expect_card:
                co = CARD_ONLY_RE.match(line)
                expect_card = False
                if co and len(line.strip()) >= 8:
                    cur.card = co.group(1)
                    continue
        if COLHEAD_RE.match(line) or NOISE_RE.search(line):
            continue

        if m:
            if m.group("date"):
                cur_date = _iso(m.group("date")) or cur_date
            if not cur_date:
                continue
            amt = m.group("amt")
            head = CARD_RE.sub("", m.group("text")).strip()
            cur = _Raw(date=cur_date, value=abs(_num(amt)), head=head,
                       saldo=_num(m.group("saldo")) if m.group("saldo") else None)
            if amt.startswith("-"):
                cur.sign, cur.source = -1, "saldo"
            else:
                cur.sign = 1 if CREDIT_RE.search(head) else -1
            expect_card = False
            raws.append(cur)
            group.append(cur)
            if cur.saldo is not None:
                checkpoint(cur.saldo)
            continue

        if cur is None:
            continue
        text = CARD_RE.sub("", line).strip()
        # Layout B: Saldo steht teils am Ende einer Folgezeile ("5000 Aarau 487.07")
        if layout == "B" and cur.saldo is None and saldo is not None and group:
            t = TRAIL_AMT_RE.match(text)
            if t:
                cand = _num(t.group("amt"))
                signs = _solve(group, round(cand - saldo, 2))
                if signs:
                    cur.saldo = cand
                    _apply(group, signs)
                    saldo, group = cand, []
                    text = t.group("text").strip()
        if text:
            cur.lines.append(text)

    bookings = []
    for r in raws:
        text = " · ".join([r.head] + [l for l in r.lines if not re.fullmatch(rf"(waren|betrag in kontowährung)?\s*{AMT}", l, re.I)])
        bookings.append(Booking(date=r.date, text=text[:300], amount=round(r.sign * r.value, 2), sign_source=r.source,
                                merchant=guess_merchant(r.head, r.lines)[:80], card=r.card))

    # stabile Kennung je Buchung (gleiche Buchung beim erneuten Einlesen / in überlappenden Auszügen wiedererkennen)
    seen: dict[str, int] = {}
    for b in bookings:
        base = f"{b.date}|{abs(b.amount):.2f}|{b.text.lower()}"
        n = seen.get(base, 0)
        seen[base] = n + 1
        b.hash = hashlib.sha1(f"{base}|{n}".encode()).hexdigest()
    return bookings


# ---------------------------------------------------------------- Abgleich
_word = re.compile(r"[a-z0-9]{4,}")
STOPWORDS = {"gmbh", "sarl", "und", "the", "schweiz", "suisse", "zahlung", "rechnung", "versicherung", "postfach", "kartennummer"}


_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "é": "e", "è": "e", "à": "a"})


def _tokens(s: str) -> set[str]:
    s = (s or "").lower().translate(_UMLAUT)
    return {w for w in _word.findall(s) if w not in STOPWORDS}


def candidates(tx: dict, invoices: list[dict], corr_names: dict, window_days: int = 60, before_days: int = 5) -> list[dict]:
    """Passende Rechnungen für eine Belastung, bestes Ergebnis zuerst.

    tx: {"date", "text", "amount"(negativ)}; invoices: {"id","date","amount","correspondent","title"}.
    Bedingung: gleicher Betrag; Rechnungsdatum höchstens `window_days` vor bzw. `before_days` nach der Zahlung.
    """
    if tx["amount"] >= 0:
        return []
    value = -tx["amount"]
    tdate = date.fromisoformat(tx["date"])
    tx_tokens = _tokens(tx["text"])
    out = []
    for inv in invoices:
        if inv.get("amount") is None or abs(inv["amount"] - value) > 0.005:
            continue
        try:
            days = (tdate - date.fromisoformat(inv["date"])).days
        except ValueError:
            continue
        if days < -before_days or days > window_days:
            continue
        name = corr_names.get(inv.get("correspondent")) or ""
        name_hit = bool(_tokens(name) & tx_tokens) or bool(_tokens(inv.get("title", "")) & tx_tokens)
        score = 60 - min(abs(days), window_days) / window_days * 30 + (40 if name_hit else 0)
        out.append({**inv, "score": round(score), "days": days, "name_hit": name_hit})
    out.sort(key=lambda c: -c["score"])
    return out


def is_unique(cands: list[dict]) -> bool:
    """Für die automatische Zuordnung: genau ein Kandidat passt auch beim Namen, und er ist der beste.
    Kandidaten nur mit gleichem Betrag bleiben Vorschläge zum manuellen Bestätigen."""
    hits = [c for c in cands if c["name_hit"]]
    return len(hits) == 1 and cands[0]["name_hit"]
