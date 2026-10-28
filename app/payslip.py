"""Lohnabrechnungen lesen: Monat, Person, Bruttolohn, Abzüge, Nettolohn, ausbezahlter Betrag.

Schweizer Lohnabrechnungen sind sehr unterschiedlich aufgebaut (Lohnprogramm des Arbeitgebers, oft gescannt).
Der Leser arbeitet deshalb zeilenweise über Stichworte:
  Bruttolohn …  7'400.00      AHV-Beitrag … -371.00      Nettolohn … 6'800.00      Ausbezahlter Lohn … 6'800.00
Abzüge = negative Beträge zwischen Brutto und Auszahlung (Totalzeilen ausgenommen).
Sonderzahlungen (13. Monatslohn, Bonus, Gratifikation, Prämie …) = Lohnarten vor dem Bruttolohn, einzeln gemerkt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

AMT_RE = re.compile(r"(?<![\d.])[-–~]?\s?\d{1,3}(?:['’`]\d{3})*[.,]\d{2}(?![\d%])")
MONTHS = {"januar": 1, "jan": 1, "februar": 2, "feb": 2, "märz": 3, "maerz": 3, "marz": 3, "mrz": 3, "april": 4, "apr": 4,
          "mai": 5, "juni": 6, "jun": 6, "juli": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
          "oktober": 10, "okt": 10, "november": 11, "nov": 11, "dezember": 12, "dez": 12}
MONTH_RE = re.compile(r"(?:lohn|gehalt|sal[äa]r)\w*\s+(?:per\s+|für\s+|fur\s+)?(?:\d{1,2}\.\s*)?(" + "|".join(sorted(MONTHS, key=len, reverse=True))
                      + r")\.?\s+(\d{4})", re.I)
MONTH_NUM_RE = re.compile(r"(?:lohn|gehalt|sal[äa]r|monat|periode)\w*\D{0,20}?\b(\d{1,2})[./-](\d{4})\b", re.I)
RANGE_RE = re.compile(r"vom\s+\d{1,2}\.\d{1,2}\.\d{4}\s+bis\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", re.I)
SALUTE_RE = re.compile(r"^\W*(herrn?|frau)\b\s*(.*)$", re.I)

GROSS_RE = re.compile(r"^\W*(total\s+)?brutto(lohn|gehalt|salär)?\b|^\W*bruttolohn", re.I)
NET_RE = re.compile(r"^\W*(total\s+)?netto(lohn|gehalt|salär)?\b|^\W*nettolohn", re.I)
PAYOUT_RE = re.compile(r"ausbezahlt|auszahlung|zahlbetrag|überweisung|ueberweisung|zu ihren gunsten|gutschrift bank", re.I)
CHILD_RE = re.compile(r"kinder|ausbildungszulage|familienzulage", re.I)
TOTAL_RE = re.compile(r"^\W*total\b", re.I)
THIRTEEN_RE = re.compile(r"\b13\.?\s*(?:monats)?(?:lohn|gehalt|sal[äa]r|ml)\b|13\.\s*monat|dreizehnte", re.I)
BONUS_RE = re.compile(r"bonus|gratifikation|erfolgs(?:beteiligung|prämie)|sonderzahlung|leistungsprämie|treueprämie|"
                      r"dienstalter|jubiläum|jubilaeum|provision|einmalzahlung|weihnachtsgeld|(?<!kranken)(?<!kassen)prämie", re.I)
KINDS = [("ahv", re.compile(r"\bahv|\biv\b|\beo\b", re.I)), ("alv", re.compile(r"\balv", re.I)),
         ("bvg", re.compile(r"\bpk\b|bvg|pensionskasse|vorsorge", re.I)),
         ("tax", re.compile(r"quellensteuer", re.I)),
         ("insurance", re.compile(r"nbu|uvg|ktv|ktg|krankentag|unfall|versicherung", re.I))]


@dataclass
class Payslip:
    month: Optional[str]                 # "JJJJ-MM"
    person: Optional[str]
    gross: Optional[float]
    net: Optional[float]
    payout: Optional[float]
    child_allowance: float = 0.0
    deductions: list = field(default_factory=list)   # [{"label", "amount", "kind"}] – Beträge positiv
    extras: list = field(default_factory=list)       # Sonderzahlungen [{"label", "amount", "kind": "13"|"bonus"}] (brutto)
    employer: Optional[str] = None

    @property
    def ok(self) -> bool:
        return bool(self.gross and (self.net or self.payout))


def _num(s: str) -> float:
    s = s.replace("–", "-").replace("~", "").replace(" ", "")
    return float(re.sub(r"['’`]", "", s).replace(",", "."))


def _amounts(line: str) -> list[float]:
    return [_num(a) for a in AMT_RE.findall(line)]


def _label(line: str) -> str:
    m = AMT_RE.search(line)                       # Bezeichnung = Text vor dem ersten Betrag („13. Monatslohn 7'400.00“)
    lbl = line[:m.start()] if m else line
    lbl = re.sub(r"\s+[-–~]?\d+(?:[.,]\d+)?\s*%?\s*$", "", lbl)   # Satz am Ende („AHV 5.3%“) weg
    return re.sub(r"\s{2,}", " ", lbl).strip(" :|-–~")


def parse_payslip(text: str) -> Payslip:
    lines = [l.rstrip() for l in (text or "").splitlines()]
    nonempty = [l.strip() for l in lines if l.strip()]
    month = None
    m = MONTH_RE.search(text or "")
    if m:
        month = f"{int(m.group(2)):04d}-{MONTHS[m.group(1).lower()]:02d}"
    else:
        r = RANGE_RE.search(text or "")
        if r:
            month = f"{int(r.group(3)):04d}-{int(r.group(2)):02d}"
        else:
            n = MONTH_NUM_RE.search(text or "")
            if n and 1 <= int(n.group(1)) <= 12:
                month = f"{int(n.group(2)):04d}-{int(n.group(1)):02d}"
    person = None
    for i, l in enumerate(nonempty):
        s = SALUTE_RE.match(l)
        if s and len(l) < 60:
            rest = s.group(2).strip(" |")
            person = rest or (nonempty[i + 1].strip(" |") if i + 1 < len(nonempty) else None)
            break
    gross = net = payout = None
    child = 0.0
    deductions: list = []
    extras: list = []
    phase = 0          # 0 vor Brutto, 1 Abzüge, 2 nach Netto
    for idx, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        am = _amounts(line)
        lbl = _label(line)
        if GROSS_RE.search(line) and am and gross is None:
            gross, phase = abs(am[-1]), 1
            continue
        if NET_RE.search(line) and am and net is None:
            net, phase = abs(am[-1]), 2
            continue
        if PAYOUT_RE.search(line) and payout is None and phase >= 1:
            vals = am
            if not vals:   # Betrag auf der nächsten Zeile ("Auszahlung:\nCHF 6'800.00 auf Bankkonto …")
                nxt = next((l.strip() for l in lines[idx + 1: idx + 3] if l.strip()), "")
                vals = _amounts(nxt)
            if vals:
                payout = abs(vals[0] if not am else am[-1])
            continue
        if phase == 0 and am and am[-1] > 0 and (THIRTEEN_RE.search(lbl) or BONUS_RE.search(lbl)):
            extras.append({"label": lbl[:60], "amount": round(am[-1], 2), "kind": "13" if THIRTEEN_RE.search(lbl) else "bonus"})
            continue
        if CHILD_RE.search(lbl) and am and not child and phase == 0:
            child = abs(am[-1])
            continue
        if phase >= 1 and am and am[-1] < 0 and not TOTAL_RE.search(line):
            kind = next((k for k, rx in KINDS if rx.search(lbl)), "other")
            deductions.append({"label": lbl[:60] or "Abzug", "amount": round(abs(am[-1]), 2), "kind": kind})
    if payout is None:
        payout = net
    return Payslip(month, person, gross, net, payout, child, deductions, extras)
