"""Liegenschaften: Nebenkostenabrechnung berechnen und als Word-Dokument erzeugen.

Ohne Abhängigkeit zu FastAPI/Paperless, damit es sich isoliert testen lässt.

Modell
  Haus (property)        – Adresse, Eigentümer, Abrechnungsperiode, Heizpool-Aufteilung (z.B. 70 % Raumheizung / 30 % Warmwasser)
  Wohnung (unit)         – Name, Fläche, selbst bewohnt
  Verteilschlüssel (key) – feste Prozente pro Wohnung (oder Fläche / gleichmässig)
  Kostenart (category)   – Gruppe (z.B. "Heizkosten", "Wasser"), Schlüssel, gehört zum Heizpool ja/nein
  Kostenposition         – aus Beleg, Fixkosten oder manuell; entweder per Schlüssel verteilt
                           oder direkt einer Wohnung belastet (unit_id)

Berechnung für eine Wohnung und ein Mietverhältnis
  Anteil = Betrag × Schlüssel-Anteil der Wohnung × Nutzungstage / Periodentage
  Heizpool (alle Kostenarten mit heating=1): Summe × Raumheizung-% → Schlüssel "Raumheizung",
                                             Summe × (1 − Raumheizung-%) → Schlüssel "Warmwasser"
  Direkt belastete Positionen zählen zu 100 % (ebenfalls nach Nutzungstagen).
  Das Total wird auf 5 Rappen gerundet (Rundungsdifferenz wird ausgewiesen).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional


def r2(x: float) -> float:
    """Kaufmännisch auf Rappen runden (0.005 → aufrunden)."""
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def r05(x: float) -> float:
    """Auf 5 Rappen runden."""
    return float((Decimal(str(x)) * 20).quantize(Decimal("1"), rounding=ROUND_HALF_UP) / 20)


def chf(x: float, sign: bool = False) -> str:
    s = f"{abs(x):,.2f}".replace(",", "'")
    return ("-" if x < 0 else "") + s


def overlap_days(a0: date, a1: date, b0: date, b1: date) -> int:
    """Anzahl Tage der Überschneidung zweier Zeiträume (je inklusive)."""
    s, e = max(a0, b0), min(a1, b1)
    return max(0, (e - s).days + 1)


def months_in(a0: date, a1: date) -> float:
    """Monate (anteilig nach Tagen) – für monatliche Fixkosten und Akonto."""
    return overlap_days(a0, a1, a0, a1) / 365.0 * 12


def period_for(start_month: int, year: int) -> tuple[date, date]:
    """Abrechnungsperiode, die im Jahr `year` beginnt (z.B. 1.3.2025–28.2.2026)."""
    s = date(year, start_month, 1)
    e_year, e_month = (year + 1, start_month) if start_month > 1 else (year + 1, 1)
    e = date(e_year, e_month, 1) - timedelta(days=1)
    return s, e


@dataclass
class Unit:
    id: int
    name: str
    area: float = 0.0
    owner_occupied: bool = False


@dataclass
class Key:
    id: int
    name: str
    type: str = "percent"            # percent | area | equal
    shares: dict = field(default_factory=dict)   # unit_id -> Prozent

    def share(self, unit_id: int, units: list[Unit]) -> float:
        if self.type == "area":
            tot = sum(u.area for u in units)
            u = next((x for x in units if x.id == unit_id), None)
            return (u.area / tot) if (u and tot) else 0.0
        if self.type == "equal":
            return 1.0 / len(units) if units else 0.0
        return float(self.shares.get(unit_id, 0) or 0) / 100.0


@dataclass
class Category:
    id: int
    grp: str
    name: str
    key_id: Optional[int] = None
    heating: bool = False
    unit_label: str = ""
    sort: int = 0


@dataclass
class Cost:
    category_id: int
    amount: float
    label: str = ""
    quantity: Optional[float] = None
    unit_id: Optional[int] = None    # direkt dieser Wohnung belastet
    source: str = ""                 # Beleg | Fix | Manuell | Bank
    date: Optional[date] = None


def compute_statement(*, units: list[Unit], keys: list[Key], categories: list[Category], costs: list[Cost],
                      unit_id: int, period: tuple[date, date], stay: tuple[date, date],
                      room_pct: float = 70.0, room_key_id: Optional[int] = None, water_key_id: Optional[int] = None,
                      akonto: float = 0.0) -> dict:
    p0, p1 = period
    days_total = (p1 - p0).days + 1
    days = overlap_days(p0, p1, stay[0], stay[1])
    factor = days / days_total if days_total else 0
    key_by = {k.id: k for k in keys}
    cat_by = {c.id: c for c in categories}
    unit = next(u for u in units if u.id == unit_id)

    def share_of(key_id: Optional[int]) -> float:
        k = key_by.get(key_id)
        return k.share(unit_id, units) if k else 0.0

    # --- Heizpool
    heat_items = [c for c in costs if cat_by.get(c.category_id) and cat_by[c.category_id].heating and c.unit_id is None]
    heat_by_cat: dict[int, dict] = {}
    for c in heat_items:
        e = heat_by_cat.setdefault(c.category_id, {"amount": 0.0, "quantity": 0.0, "has_qty": False})
        e["amount"] += c.amount
        if c.quantity:
            e["quantity"] += c.quantity
            e["has_qty"] = True
    heat_rows = []
    for cid, e in heat_by_cat.items():
        cat = cat_by[cid]
        heat_rows.append({"grp": cat.grp, "name": cat.name, "amount": r2(e["amount"]),
                          "quantity": r2(e["quantity"]) if e["has_qty"] else None, "unit_label": cat.unit_label, "sort": cat.sort})
    heat_rows.sort(key=lambda x: (x["grp"], x["sort"], x["name"]))
    heat_total = r2(sum(x["amount"] for x in heat_rows))
    heat_groups: dict[str, float] = {}
    for x in heat_rows:
        heat_groups[x["grp"]] = r2(heat_groups.get(x["grp"], 0) + x["amount"])
    room_total = r2(heat_total * room_pct / 100)
    water_total = r2(heat_total - room_total)
    room_share, water_share = share_of(room_key_id), share_of(water_key_id)
    room_unit = r2(room_total * room_share * factor)
    water_unit = r2(water_total * water_share * factor)

    # --- übrige Gruppen (und direkt belastete Positionen, auch Heizung)
    groups: dict[str, dict] = {}
    for c in costs:
        cat = cat_by.get(c.category_id)
        if not cat or (cat.heating and c.unit_id is None):
            continue
        if c.unit_id is not None and c.unit_id != unit_id:
            continue
        if c.unit_id is not None and c.date and not (stay[0] <= c.date <= stay[1]):
            continue   # direkt belastet, aber ausserhalb dieses Mietverhältnisses
        g = groups.setdefault(cat.grp, {"name": cat.grp, "rows": {}, "sort": cat.sort})
        direct = c.unit_id is not None
        k = (cat.id, direct, c.label if direct else "")
        row = g["rows"].setdefault(k, {
            "name": (c.label or cat.name) if direct else cat.name,
            "total": 0.0, "quantity": None, "unit_label": cat.unit_label,
            "key": "direkt" if direct else (key_by[cat.key_id].name if cat.key_id in key_by else "–"),
            "share": 1.0 if direct else share_of(cat.key_id), "sort": cat.sort, "direct": direct,
        })
        row["total"] += c.amount
        if c.quantity:
            row["quantity"] = (row["quantity"] or 0) + c.quantity
    group_list = []
    for g in sorted(groups.values(), key=lambda x: (x["sort"], x["name"])):
        rows = []
        for row in sorted(g["rows"].values(), key=lambda x: (x["sort"], x["name"])):
            row["total"] = r2(row["total"])
            # direkt belastete Positionen zählen voll (sie fallen in die Mietdauer), verteilte nach Nutzungstagen
            row["unit_amount"] = r2(row["total"] * row["share"] * (1 if row["direct"] else factor))
            row["share_pct"] = round(row["share"] * 100, 2)
            rows.append(row)
        group_list.append({"name": g["name"], "rows": rows, "unit_total": r2(sum(r["unit_amount"] for r in rows)),
                           "total": r2(sum(r["total"] for r in rows))})

    energy_unit = r2(room_unit + water_unit)
    raw = r2(energy_unit + sum(g["unit_total"] for g in group_list))
    total = r05(raw)
    property_total = r2(sum(c.amount for c in costs))
    return {
        "unit": {"id": unit.id, "name": unit.name, "area": unit.area},
        "area_total": r2(sum(u.area for u in units)),
        "period": [p0.isoformat(), p1.isoformat()], "stay": [max(p0, stay[0]).isoformat(), min(p1, stay[1]).isoformat()],
        "days_total": days_total, "days": days, "factor": round(factor, 4),
        "heating": {
            "rows": heat_rows, "groups": heat_groups, "total": heat_total, "room_pct": room_pct,
            "room_total": room_total, "water_total": water_total,
            "room_share_pct": round(room_share * 100, 2), "water_share_pct": round(water_share * 100, 2),
            "room_unit": room_unit, "water_unit": water_unit, "energy_unit": energy_unit,
        },
        "groups": group_list,
        "subtotal": raw, "rounding": r2(total - raw), "total": total,
        "akonto": r2(akonto), "saldo": r2(total - akonto),
        "property_total": property_total,
    }


# ---------------------------------------------------------------- Word-Dokument
def build_docx(path: str, st: dict, *, owner: dict, tenant: dict, prop: dict, invoice_date: date, due_days: int = 30,
               intro: Optional[str] = None, closing: Optional[str] = None) -> str:
    """Abrechnung als .docx im Aufbau der bisherigen Vorlage schreiben."""
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    p0, p1 = (date.fromisoformat(x) for x in st["period"])
    due = invoice_date + timedelta(days=due_days)
    fmt_d = lambda d: d.strftime("%d.%m.%Y")
    plabel = f"{p0.year}/{p1.year}" if p0.year != p1.year else str(p0.year)
    short = f"{str(p0.year)[2:]}/{str(p1.year)[2:]}" if p0.year != p1.year else str(p0.year)

    doc = Document()
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Cm(2.2)
    sec.top_margin, sec.bottom_margin = Cm(1.8), Cm(1.8)
    base = doc.styles["Normal"]
    base.font.name = "Arial"
    base.font.size = Pt(10)
    base.element.rPr.rFonts.set(qn("w:eastAsia"), "Arial")
    base.paragraph_format.space_after = Pt(2)

    def para(text="", bold=False, size=None, italic=False, align=None, space_after=None, color=None):
        p = doc.add_paragraph()
        r = p.add_run(text)
        r.bold, r.italic = bold, italic
        if size:
            r.font.size = Pt(size)
        if color:
            r.font.color.rgb = RGBColor.from_string(color)
        if align:
            p.alignment = align
        if space_after is not None:
            p.paragraph_format.space_after = Pt(space_after)
        if bold and size and size >= 11:
            p.paragraph_format.keep_with_next = True
        return p

    def shade(cell, hex_):
        tcPr = cell._tc.get_or_add_tcPr()
        sh = OxmlElement("w:shd")
        sh.set(qn("w:val"), "clear")
        sh.set(qn("w:color"), "auto")
        sh.set(qn("w:fill"), hex_)
        tcPr.append(sh)

    def table(rows, widths, header=False, bold_last=False, right_from=1, shade_header="E8EDE9"):
        t = doc.add_table(rows=0, cols=len(widths))
        t.alignment = WD_TABLE_ALIGNMENT.LEFT
        t.autofit = False
        for j, w in enumerate(widths):
            t.columns[j].width = Cm(w)
        for i, row in enumerate(rows):
            cells = t.add_row().cells
            for j, val in enumerate(row):
                c = cells[j]
                c.width = Cm(widths[j])
                p = c.paragraphs[0]
                p.paragraph_format.space_after = Pt(1)
                run = p.add_run("" if val is None else str(val))
                if (header and i == 0) or (bold_last and i == len(rows) - 1):
                    run.bold = True
                if j >= right_from:
                    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                if i < len(rows) - 1:
                    p.paragraph_format.keep_with_next = True
                if header and i == 0:
                    shade(c, shade_header)
        doc.add_paragraph().paragraph_format.space_after = Pt(4)
        return t

    # Absender / Empfänger
    for line in [owner.get("name", "")] + (owner.get("address") or "").splitlines():
        para(line, bold=True, size=9, color="555555", space_after=0)
    para(space_after=10)
    for line in [tenant.get("name", "")] + (tenant.get("address") or "").splitlines():
        para(line, bold=True, space_after=0)
    para(space_after=10)

    info = doc.add_table(rows=0, cols=2)
    for k, v in [("Abrechnungsperiode:", f"{fmt_d(p0)} – {fmt_d(p1)}"), ("Rechnungsdatum:", fmt_d(invoice_date)),
                 ("Zahlbar bis:", fmt_d(due)), ("Liegenschaft:", prop.get("address") or prop.get("name", ""))]:
        c = info.add_row().cells
        c[0].width, c[1].width = Cm(4.5), Cm(11)
        c[0].paragraphs[0].add_run(k).italic = True
        c[1].paragraphs[0].add_run(v.replace("\n", ", ")).bold = k == "Zahlbar bis:"
    para(space_after=6)

    para(f"Heiz- und Nebenkostenabrechnung {plabel} für '{st['unit']['name']}'", bold=True, size=12, space_after=8)
    para(tenant.get("salutation") or "Guten Tag", space_after=6)
    para(intro or "Mit diesem Brief erhalten Sie die Nebenkostenabrechnung mit detaillierter Energiekostenübersicht.", space_after=8)

    saldo = st["saldo"]
    table([
        ["CHF", "Ihre Kosten", "Liegenschaft"],
        ["", short, short],
        ["Total Kosten", chf(st["total"]), chf(st["property_total"])],
        ["Total Akonto", chf(st["akonto"]), ""],
        ["Saldo zu unseren Gunsten" if saldo >= 0 else "Guthaben zu Ihren Gunsten", chf(abs(saldo)), ""],
    ], [7, 4.5, 4.5], header=True, bold_last=True)
    if saldo > 0:
        para(f"Bitte überweisen Sie uns den Saldo von CHF {chf(saldo)} bis zum {fmt_d(due)} auf das Mietzinskonto"
             + (f" ({owner['iban']})." if owner.get("iban") else "."), space_after=8)
    elif saldo < 0:
        para(f"Das Guthaben von CHF {chf(-saldo)} überweisen wir Ihnen in den nächsten Tagen bzw. verrechnen es mit der nächsten Miete.",
             space_after=8)

    # Kostenübersicht
    para("Kostenübersicht", bold=True, size=11, space_after=4)
    h = st["heating"]
    rows = [["", short]]
    if h["total"]:
        rows += [["ENERGIE", ""], ["   Raumheizung", chf(h["room_unit"])], ["   Warmwasser", chf(h["water_unit"])],
                 ["Total Energiekosten", chf(h["energy_unit"])]]
    for g in st["groups"]:
        rows.append([g["name"], chf(g["unit_total"])])
    if st["rounding"]:
        rows.append(["Rundungsdifferenz", chf(st["rounding"])])
    rows.append(["TOTAL CHF", chf(st["total"])])
    table(rows, [11.5, 4.5], header=True, bold_last=True)

    # Bezugsgrössen
    para("Bezugsgrössen", bold=True, size=11, space_after=4)
    rows = [["", "", "Ihr Objekt", "Liegenschaft"],
            ["Wohnfläche", "m²", f"{st['unit']['area']:.2f}", f"{st['area_total']:.2f}"],
            ["Nutzungsdauer", "Tage", str(st["days"]), str(st["days_total"])]]
    if h["total"]:
        rows += [["Anteil Raumheizung", "%", f"{h['room_share_pct']:.2f}", "100.00"],
                 ["Anteil Warmwasser", "%", f"{h['water_share_pct']:.2f}", "100.00"]]
    table(rows, [6, 2, 4, 4], header=True, right_from=2)

    # Heizkosten-Detail
    if h["total"]:
        para("Heizkosten", bold=True, size=11, space_after=4)
        rows = [["Bezeichnung", "Menge", "Betrag CHF"]]
        for grp in h["groups"]:
            rows.append([grp, "", ""])
            for x in h["rows"]:
                if x["grp"] == grp:
                    q = f"{x['quantity']:,.2f} {x['unit_label']}".replace(",", "'") if x["quantity"] else ""
                    rows.append(["   " + x["name"], q, chf(x["amount"])])
            rows.append(["   Total " + grp, "", chf(h["groups"][grp])])
        rows.append(["Total Heizkosten", "", chf(h["total"])])
        table(rows, [9, 3.5, 3.5], header=True, bold_last=True)
        rows = [["Zuweisung", "%", "Liegenschaft CHF", "Ihr Anteil %", "Tage", "Ihre Kosten CHF"],
                ["Raumheizung", f"{h['room_pct']:.2f}", chf(h["room_total"]), f"{h['room_share_pct']:.2f}", str(st["days"]), chf(h["room_unit"])],
                ["Warmwasser", f"{100 - h['room_pct']:.2f}", chf(h["water_total"]), f"{h['water_share_pct']:.2f}", str(st["days"]), chf(h["water_unit"])],
                ["Total Energiekosten", "", chf(h["total"]), "", "", chf(h["energy_unit"])]]
        table(rows, [4.2, 1.5, 3, 2.3, 1.4, 3.6], header=True, bold_last=True)

    # Übrige Gruppen
    for g in st["groups"]:
        para(g["name"], bold=True, size=11, space_after=4)
        rows = [["Bezeichnung", "Total CHF", "Schlüssel", "Ihr Anteil %", "Tage", "Ihre Kosten CHF"]]
        for r in g["rows"]:
            rows.append([r["name"], chf(r["total"]), r["key"], f"{r['share_pct']:.2f}", "–" if r.get("direct") else str(st["days"]),
                         chf(r["unit_amount"])])
        rows.append(["Total " + g["name"], "", "", "", "", chf(g["unit_total"])])
        table(rows, [5.0, 2.4, 2.2, 2.2, 1.4, 2.8], header=True, bold_last=True)

    para(closing or "Die Belege der Nebenkostenabrechnung können Sie nach Absprache innert 30 Tagen bei uns einsehen.", space_after=10)
    para("Freundliche Grüsse", space_after=18)
    para(owner.get("name", ""))
    doc.save(path)
    return path
