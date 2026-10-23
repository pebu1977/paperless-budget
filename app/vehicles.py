"""Fahrzeuge: Tankkarten-Rechnungen lesen und Kennzahlen berechnen.

Ohne Abhängigkeit zu FastAPI/Paperless/Traccar, damit es sich isoliert testen lässt.

Tankkarten-Rechnung (getestet mit Vögtlin-Meyer):
    Karten-Nr. 700000000012345600 Max Muster
    05.06.26 18:56:00 TS Musterhausen Benzin bleifrei 95 44.58 L15 175.0 8.1 % 78.02 0.00 1.78 76.24
    12.06.26 07:52:37 TS Beispielwil Benzin bleifrei 95 45.93 L15 00          <- Zeile kann umbrechen
    172.0 8.1 % 79.00 0.00 1.84 77.16
    Total Tankkarte 152.72 00 257.93
  Pro Tankvorgang: Menge + Einheit (L/kWh), danach MwSt-Satz und Beträge; der letzte Betrag ist
  der belastete Betrag nach Rabatt, der erste der Bruttobetrag.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

AMT = r"\d{1,3}(?:['’]\d{3})*\.\d{2}"
CARD_RE = re.compile(r"karten-?\s*nr\.?\s*:?\s*(\d[\d ]{5,}\d)", re.I)
START_RE = re.compile(r"^\s*(\d{2}\.\d{2}\.(?:\d{4}|\d{2}))\s+(\d{1,2}:\d{2}(?::\d{2})?)\s+(.*)$")
VALUES_RE = re.compile(
    rf"^(?P<desc>.*?)\s(?P<qty>\d+\.\d{{2,3}})\s+(?P<unit>L|l|kWh|KWH|kwh|Liter)\w*\b.*?"
    rf"(?P<pct>\d+(?:\.\d+)?)\s*%\s+(?P<vals>(?:-?{AMT}\s*)+)$"
)
TOTAL_RE = re.compile(r"^\s*total\b", re.I)


@dataclass
class FuelEntry:
    date: str          # ISO
    time: str
    card: str          # Kartennummer (nur Ziffern)
    description: str   # Tankstelle + Produkt
    quantity: float
    unit: str          # "L" | "kWh"
    amount: float      # belasteter Betrag (nach Rabatt)
    gross: float
    hash: str = ""


def _iso(d: str) -> Optional[str]:
    dd, mm, yy = d.split(".")
    try:
        return date(int(yy) + (2000 if len(yy) == 2 else 0), int(mm), int(dd)).isoformat()
    except ValueError:
        return None


def _num(s: str) -> float:
    return float(re.sub(r"['’]", "", s))


def parse_fuel_invoice(content: str) -> list[FuelEntry]:
    entries: list[FuelEntry] = []
    card = ""
    buf: Optional[list] = None  # [date, time, text]

    def flush():
        nonlocal buf
        if not buf:
            return
        iso, tm, text = buf
        buf = None
        m = VALUES_RE.match(re.sub(r"\s+", " ", text).strip())
        if not (iso and m):
            return
        vals = [_num(v) for v in re.findall(rf"-?{AMT}", m.group("vals"))]
        unit = "kWh" if m.group("unit").lower().startswith("k") else "L"
        entries.append(FuelEntry(
            date=iso, time=tm, card=card, description=m.group("desc").strip()[:120],
            quantity=float(m.group("qty")), unit=unit, amount=vals[-1], gross=vals[0],
        ))

    for line in (content or "").splitlines():
        if not line.strip():
            continue
        c = CARD_RE.search(line)
        if c:
            flush()
            card = re.sub(r"\D", "", c.group(1))
            continue
        if TOTAL_RE.match(line):
            flush()
            continue
        s = START_RE.match(line)
        if s:
            flush()
            buf = [_iso(s.group(1)), s.group(2), s.group(3)]
            continue
        if buf is not None:
            buf[2] += " " + line.strip()
    flush()

    for e in entries:
        base = f"{e.date}|{e.time}|{e.card}|{e.quantity:.2f}|{e.amount:.2f}"
        e.hash = hashlib.sha1(base.encode()).hexdigest()
    return entries


def card_matches(card: str, keys: str) -> bool:
    """Karten-Schlüssel eines Fahrzeugs: kommagetrennt, ganze Nummer oder Endziffern (z.B. '2345600, 1234')."""
    digits = re.sub(r"\D", "", card or "")
    if not digits:
        return False
    for k in re.split(r"[,;\s]+", keys or ""):
        k = re.sub(r"\D", "", k)
        if len(k) >= 4 and (digits == k or digits.endswith(k) or k in digits):
            return True
    return False


def consumption(quantity: float, km: float) -> Optional[float]:
    return round(quantity / km * 100, 2) if quantity and km and km > 0 else None


def per_km(cost: float, km: float) -> Optional[float]:
    return round(cost / km, 3) if km and km > 0 else None
