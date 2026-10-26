"""Parkingpay-Auszüge (Digitalparking AG) lesen: Parkvorgänge mit Datum, Kennzeichen/Badge, Ort und Betrag.

1) Kontoauszug von parkingpay.ch (Monatsbericht): ein Block pro Buchung
       03.09.2026   Parkvorgang                     -2.50
                    Musterstrasse 12, Musterstadt
                    03.09.2026 - 15:06
                    03.09.2026 - 15:41
                    AG123456 (Anna)           <- Kennzeichen oder Badge-Nr., in Klammern die Person
                    Ref. 600000001
                    MWST-Code: B
   Je nach Textextraktion stehen Datum, Beschreibung und Betrag auch auf eigenen Zeilen.
   Einzahlungen, Saldo- und Totalzeilen werden übersprungen.

2) Unbekanntes Layout – tolerant:
  * eine Buchung = Zeile (oder bis zu drei Zeilen) mit Datum, Schweizer Kennzeichen und Betrag
  * Aufladungen, Gutschriften, Saldo- und Totalzeilen werden übersprungen
  * ohne erkennbares Kennzeichen wird der Parkvorgang trotzdem übernommen (später von Hand zuordnen)
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional

CANTONS = "AG|AI|AR|BE|BL|BS|FR|GE|GL|GR|JU|LU|NE|NW|OW|SG|SH|SO|SZ|TG|TI|UR|VD|VS|ZG|ZH|FL"
DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})(?!\d)")
TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?::\d\d)?(?!\d)")
PLATE_RE = re.compile(rf"(?<![A-Za-z])({CANTONS})[\s\-·.]?(\d{{1,6}})(?!\d)")
AMOUNT_RE = re.compile(r"(?<![\d.:'])(-?\s?\d{1,3}(?:['’]\d{3})*[.,]\d{2})(?![\d.])")
SKIP_RE = re.compile(r"aufladung|einzahlung|gutschrift|saldo|übertrag|uebertrag|total|summe|guthaben|zahlung erhalten|"
                     r"rückerstattung|rueckerstattung|vorauszahlung|kontostand|mwst|mehrwertsteuer|seite \d", re.IGNORECASE)
NOISE_RE = re.compile(r"\bCHF\b|\bFr\.?|\bSFr\.?|\b\d{6,}\b|[|•]")


@dataclass
class ParkingEntry:
    date: str                  # ISO (Buchungsdatum)
    time: Optional[str]        # Beginn
    plate: Optional[str]       # normalisiert, z.B. "ZH123456" – oder Badge-Nr.
    location: str
    amount: float
    hash: str
    end: Optional[str] = None  # Ende, z.B. "12.09. 07:12"
    driver: Optional[str] = None
    kind: str = "Parkvorgang"


PP_HEAD = re.compile(r"^(\d{2}\.\d{2}\.\d{4})\s+(\S.*?)\s+(-?\d{1,3}(?:['’]\d{3})*[.,]\d{2})$")
PP_DATE = re.compile(r"^(\d{2}\.\d{2}\.\d{4})$")
PP_AMT = re.compile(r"^(-?\d{1,3}(?:['’]\d{3})*[.,]\d{2})$")
PP_DESC_AMT = re.compile(r"^(\S.*?)\s+(-?\d{1,3}(?:['’]\d{3})*[.,]\d{2})$")
PP_STAMP = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})\s*-\s*(\d{1,2}):(\d{2})$")
PP_ID = re.compile(rf"^((?:{CANTONS})\s?\d{{1,6}}|\d{{6,16}})(?:\s*\(([^)]*)\))?$")
PP_SKIP_DESC = re.compile(r"saldo|einzahlung|gutschrift|rückerstattung|rueckerstattung|total|übertrag|aufladung", re.IGNORECASE)
PP_PAYMETHOD = re.compile(r"^(twint|postfinance( card| e-finance)?|e-banking|ebanking|kreditkarte|visa|mastercard|"
                          r"american express|amex|lsv|debit direct|banküberweisung|einzahlungsschein|qr-rechnung)$", re.IGNORECASE)
PP_STOP = re.compile(r"^(total bewegungen|neuer saldo|mwst-übersicht|digitalparking ag\b|datum$|beschreibung$|chf$|datum\s+beschreibung)",
                     re.IGNORECASE)


def _pp_header(lines: list[str], i: int) -> Optional[tuple[str, str, float, int]]:
    """(Datum, Beschreibung, Betrag, Anzahl Zeilen) wenn bei Zeile i eine Buchung beginnt."""
    m = PP_HEAD.match(lines[i])
    if m and not PP_STAMP.match(lines[i]):
        return m.group(1), m.group(2).strip(), _amount(m.group(3)), 1
    if PP_DATE.match(lines[i]) and i + 1 < len(lines):
        m2 = PP_DESC_AMT.match(lines[i + 1])
        if m2 and not PP_DATE.match(lines[i + 1]):
            return lines[i], m2.group(1).strip(), _amount(m2.group(2)), 2
        if i + 2 < len(lines) and not PP_AMT.match(lines[i + 1]) and not PP_STAMP.match(lines[i + 1]) and PP_AMT.match(lines[i + 2]):
            return lines[i], lines[i + 1].strip(), _amount(lines[i + 2]), 3
    return None


def parse_parkingpay(text: str) -> list[ParkingEntry]:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    out: list[ParkingEntry] = []
    seen: dict[str, int] = {}
    i = 0
    while i < len(lines):
        h = _pp_header(lines, i)
        if not h:
            i += 1
            continue
        d, desc, amount, n = h
        j = i + n
        loc, stamps, ident, driver, extra = None, [], None, None, []
        while j < len(lines) and not _pp_header(lines, j) and not PP_STOP.match(lines[j]):
            l = lines[j]
            sm, im = PP_STAMP.match(l), PP_ID.match(l)
            if sm:
                stamps.append(sm)
            elif im and not ident:
                ident, driver = re.sub(r"\s", "", im.group(1)), (im.group(2) or "").strip() or None
            elif l.lower().startswith(("ref.", "mwst-code", "mwst code")) or PP_PAYMETHOD.match(l):
                pass        # Referenzen, MWST-Code, Zahlungsart einer Einzahlung (kann bei der Textextraktion verrutschen)
            elif loc is None:
                loc = l
            else:
                extra.append(l)
            j += 1
        i = j
        if PP_SKIP_DESC.search(desc) or amount >= 0:
            continue
        dd, mm, yy = d.split(".")
        iso = f"{yy}-{mm}-{dd}"
        start = f"{int(stamps[0].group(4)):02d}:{stamps[0].group(5)}" if stamps else None
        end = None
        if len(stamps) > 1:
            e = stamps[-1]
            end = (f"{e.group(1)}.{e.group(2)}. " if (e.group(1), e.group(2)) != (stamps[0].group(1), stamps[0].group(2)) else "") + \
                  f"{int(e.group(4)):02d}:{e.group(5)}"
        location = loc or ""
        if desc.lower() != "parkvorgang":
            location = f"{desc}: {location}".strip(": ")
        key = f"{iso}|{start}|{ident}|{abs(amount):.2f}|{location.lower()}"
        k = seen.get(key, 0)
        seen[key] = k + 1
        out.append(ParkingEntry(iso, start, ident, location[:120], round(abs(amount), 2),
                                hashlib.sha1(f"{key}|{k}".encode()).hexdigest()[:16], end, driver, desc))
    return out


def norm_plate(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def plate_matches(plate: Optional[str], keys: str) -> bool:
    """Kennzeichen des Fahrzeugs (kommagetrennt) mit dem Kennzeichen des Parkvorgangs vergleichen."""
    p = norm_plate(plate or "")
    return bool(p) and any(p == norm_plate(k) for k in (keys or "").split(",") if k.strip())


def _iso(m: re.Match) -> str:
    d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
    y = int(y) + (2000 if len(y) == 2 else 0)
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _amount(s: str) -> float:
    return float(s.replace(" ", "").replace("'", "").replace("’", "").replace(",", "."))


def parse_parking_statement(text: str) -> list[ParkingEntry]:
    if re.search(r"parkvorgang", text or "", re.IGNORECASE):   # echter Parkingpay-Kontoauszug
        found = parse_parkingpay(text)
        if found:
            return found
    return parse_generic(text)


def parse_generic(text: str) -> list[ParkingEntry]:
    lines = [l.strip() for l in (text or "").splitlines()]
    out: list[ParkingEntry] = []
    seen: dict[str, int] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        dm = DATE_RE.search(line)
        if not dm or SKIP_RE.search(line):
            i += 1
            continue
        # Buchung kann über bis zu drei Zeilen gehen (Ort/Kennzeichen/Betrag umbrochen)
        block, j = line, i
        while j + 1 < len(lines) and j - i < 2 and not (PLATE_RE.search(block) and AMOUNT_RE.search(block)):
            nxt = lines[j + 1]
            if DATE_RE.search(nxt) and not TIME_RE.match(nxt) or SKIP_RE.search(nxt):
                break
            block += " " + nxt
            j += 1
        # Datum/Zeit aus dem Text entfernen, bevor Beträge gesucht werden (sonst wird "12.10" zum Betrag)
        rest = DATE_RE.sub(" ", block)
        times = TIME_RE.findall(rest)
        rest_nt = TIME_RE.sub(" ", rest)
        amounts = AMOUNT_RE.findall(rest_nt)
        if not amounts:
            i += 1
            continue
        amount = abs(_amount(amounts[-1]))
        if amount <= 0 or amount > 500:
            i = j + 1
            continue
        pm = PLATE_RE.search(rest_nt)
        plate = f"{pm.group(1)}{pm.group(2)}" if pm else None
        loc = rest_nt
        if pm:
            loc = loc.replace(pm.group(0), " ")
        for a in amounts:
            loc = loc.replace(a, " ")
        loc = NOISE_RE.sub(" ", loc)
        loc = re.sub(r"\s*[-–]\s*(?=\s|$)", " ", loc)
        loc = re.sub(r"\s{2,}", " ", loc).strip(" ,;:-–")
        time = f"{int(times[0][0]):02d}:{times[0][1]}" if times else None
        key = f"{_iso(dm)}|{time}|{plate}|{amount:.2f}|{loc.lower()}"
        n = seen.get(key, 0)
        seen[key] = n + 1
        h = hashlib.sha1(f"{key}|{n}".encode()).hexdigest()[:16]
        out.append(ParkingEntry(_iso(dm), time, plate, loc[:120], round(amount, 2), h))
        i = j + 1
    return out
