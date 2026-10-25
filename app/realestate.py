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

    def key_info(key_id: Optional[int]) -> Optional[dict]:
        """Kennzahlen eines Schlüssels für die Abrechnung: Total Anteile, Anteil der Wohnung, Einheit."""
        k = key_by.get(key_id)
        if not k:
            return None
        if k.type == "area":
            total, mine, lbl = sum(u.area for u in units), unit.area, "m²"
        elif k.type == "equal":
            total, mine, lbl = float(len(units)), 1.0, "Whg."
        else:
            total, mine, lbl = sum(float(v or 0) for v in k.shares.values()), float(k.shares.get(unit_id, 0) or 0), "%"
        return {"id": k.id, "name": k.name, "type": k.type, "total": round(total, 3), "mine": round(mine, 3), "unit": lbl}

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
            "key_info": None if direct else key_info(cat.key_id),
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
            "room_key": key_info(room_key_id), "water_key": key_info(water_key_id),
        },
        "groups": group_list,
        "subtotal": raw, "rounding": r2(total - raw), "total": total,
        "akonto": r2(akonto), "saldo": r2(total - akonto),
        "property_total": property_total,
    }


# ---------------------------------------------------------------- Word-Dokument
GROUP_COLORS = [("warmwasser", "C8916B"), ("raumheizung", "B5652B"), ("heiz", "A0522D"), ("wasser", "6E5A9E"),
                ("unterhalt", "77933C"), ("entsorg", "77933C"), ("hauswart", "77933C"), ("garten", "77933C"),
                ("strom", "3F8FA8"), ("allgemein", "3F8FA8"), ("versicher", "4F6FA8"), ("lift", "8C6D3F")]
FALLBACK_COLORS = ["4F6FA8", "8064A2", "3F8FA8", "C0782E", "6E8B3D"]


def group_color(name: str, i: int = 0) -> str:
    n = name.lower()
    return next((c for k, c in GROUP_COLORS if k in n), FALLBACK_COLORS[i % len(FALLBACK_COLORS)])


def _n3(x: float) -> str:
    return f"{x:,.3f}".replace(",", "'")


def build_docx(path: str, st: dict, *, owner: dict, tenant: dict, prop: dict, invoice_date: date, due_days: int = 30,
               intro: Optional[str] = None, closing: Optional[str] = None) -> str:
    """Abrechnung als .docx im Aufbau der Vorlage: Brief (Seite 1), Kostenübersicht (Seite 2), Details (ab Seite 3)."""
    from docx import Document
    from docx.enum.section import WD_ORIENT  # noqa: F401
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    p0, p1 = (date.fromisoformat(x) for x in st["period"])
    s0, s1 = (date.fromisoformat(x) for x in st["stay"])
    interim = st.get("interim")
    due = invoice_date + timedelta(days=due_days)
    fd = lambda d: d.strftime("%d.%m.%Y")
    if interim:
        short = f"{p0:%d.%m.}–{p1:%d.%m.%y}"
        plabel = f"{fd(p0)} – {fd(p1)}"
    else:
        short = f"{str(p0.year)[2:]}/{str(p1.year)[2:]}" if p0.year != p1.year else str(p0.year)
        plabel = f"{p0.year}/{p1.year}" if p0.year != p1.year else str(p0.year)
    h = st["heating"]
    RIGHT, LEFT = WD_ALIGN_PARAGRAPH.RIGHT, WD_ALIGN_PARAGRAPH.LEFT
    FULL = 17.5

    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21), Cm(29.7)
    sec.left_margin, sec.right_margin = Cm(2.0), Cm(1.5)
    sec.top_margin, sec.bottom_margin = Cm(1.6), Cm(1.6)
    sec.header_distance = sec.footer_distance = Cm(0.9)
    base = doc.styles["Normal"]
    base.font.name = "Arial"
    base.font.size = Pt(9)
    base.element.rPr.rFonts.set(qn("w:eastAsia"), "Arial")
    base.paragraph_format.space_after = Pt(0)
    base.paragraph_format.space_before = Pt(0)

    # ---- Helfer
    def border(el_pr, sides: dict):
        b = el_pr.find(qn("w:pBdr")) if el_pr.tag == qn("w:pPr") else el_pr.find(qn("w:tcBorders"))
        if b is None:
            b = OxmlElement("w:pBdr" if el_pr.tag == qn("w:pPr") else "w:tcBorders")
            el_pr.append(b)
        for side, (val, sz) in sides.items():
            e = OxmlElement(f"w:{side}")
            e.set(qn("w:val"), val)
            e.set(qn("w:sz"), str(sz))
            e.set(qn("w:space"), "0")
            e.set(qn("w:color"), "000000")
            b.append(e)

    def cell_border(cell, **sides):
        border(cell._tc.get_or_add_tcPr(), {k: v for k, v in sides.items()})

    def shade(cell, hex_):
        tcPr = cell._tc.get_or_add_tcPr()
        sh = OxmlElement("w:shd")
        sh.set(qn("w:val"), "clear")
        sh.set(qn("w:color"), "auto")
        sh.set(qn("w:fill"), hex_)
        tcPr.append(sh)

    def margins(cell, top=30, bottom=30, left=60, right=60):
        tcPr = cell._tc.get_or_add_tcPr()
        m = OxmlElement("w:tcMar")
        for side, v in (("top", top), ("bottom", bottom), ("left", left), ("right", right)):
            e = OxmlElement(f"w:{side}")
            e.set(qn("w:w"), str(v))
            e.set(qn("w:type"), "dxa")
            m.append(e)
        tcPr.append(m)

    def write(p, text, bold=False, italic=False, size=None, color=None, underline=False):
        r = p.add_run("" if text is None else str(text))
        r.bold, r.italic, r.underline = bold, italic, underline
        if size:
            r.font.size = Pt(size)
        if color:
            r.font.color.rgb = RGBColor.from_string(color)
        return r

    def para(text="", bold=False, italic=False, size=None, align=None, after=0, before=0, color=None, container=None):
        p = (container or doc).add_paragraph()
        write(p, text, bold, italic, size, color)
        p.paragraph_format.space_after, p.paragraph_format.space_before = Pt(after), Pt(before)
        if align is not None:
            p.alignment = align
        return p

    def ctext(cell, text, bold=False, italic=False, size=None, align=None, color=None, underline=False, first=True):
        p = cell.paragraphs[0] if first and not cell.paragraphs[0].text else cell.add_paragraph()
        write(p, text, bold, italic, size, color, underline)
        if align is not None:
            p.alignment = align
        return p

    def table(cols, container=None, align=WD_TABLE_ALIGNMENT.LEFT):
        t = (container or doc).add_table(rows=0, cols=len(cols))
        t.alignment = align
        t.autofit = False
        for j, w in enumerate(cols):
            t.columns[j].width = Cm(w)
        return t

    def row(t, values, widths, bold=False, size=None, aligns=None, color=None, underline=False):
        cells = t.add_row().cells
        for j, v in enumerate(values):
            cells[j].width = Cm(widths[j])
            margins(cells[j])
            if v is not None and v != "":
                ctext(cells[j], v, bold=bold, size=size, color=color, underline=underline,
                      align=(aligns[j] if aligns else (RIGHT if j > 0 else LEFT)))
        return cells

    def bar(text, color, width=FULL, container=None, text_color="FFFFFF"):
        t = table([width], container)
        c = t.add_row().cells[0]
        c.width = Cm(width)
        shade(c, color)
        margins(c, 20, 20, 80, 80)
        ctext(c, text, size=10, color=text_color)
        return t

    def gap(pt=6, container=None):
        para("", after=0, before=0, container=container).paragraph_format.line_spacing = Pt(pt)

    def page_break():
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    def field(p, instr):
        for kind, text in (("begin", None), ("instr", instr), ("separate", None), ("text", "1"), ("end", None)):
            r = p.add_run()
            r.bold = True
            if kind == "instr":
                el = OxmlElement("w:instrText")
                el.set(qn("xml:space"), "preserve")
                el.text = f" {text} "
            elif kind == "text":
                el = OxmlElement("w:t")
                el.text = text
            else:
                el = OxmlElement("w:fldChar")
                el.set(qn("w:fldCharType"), kind)
            r._r.append(el)

    # ---- Kopf-/Fusszeile ab Seite 2 (Linie oben, "Seite x/y" unten rechts)
    sec.different_first_page_header_footer = True
    hp = sec.header.paragraphs[0]
    border(hp._p.get_or_add_pPr(), {"bottom": ("single", 6)})
    fp = sec.footer.paragraphs[0]
    border(fp._p.get_or_add_pPr(), {"top": ("single", 6)})
    fp.alignment = RIGHT
    write(fp, "Seite ", bold=True, size=8)
    field(fp, "PAGE")
    write(fp, "/", bold=True, size=8)
    field(fp, "NUMPAGES")
    for r in fp.runs:
        r.font.size = Pt(8)

    # ================================================================ Seite 1: Brief
    head = table([9.5, 8.0])
    c = head.add_row().cells
    c[0].width, c[1].width = Cm(9.5), Cm(8.0)
    for i, line in enumerate([owner.get("name", "")] + (owner.get("address") or "").splitlines()):
        ctext(c[1], line, bold=True, size=8.5, first=(i == 0))
    c = head.add_row().cells
    c[0].width, c[1].width = Cm(9.5), Cm(8.0)
    gp = c[0].paragraphs[0]
    gp.paragraph_format.space_before = Pt(40)
    for i, line in enumerate([tenant.get("name", "")] + (tenant.get("address") or "").replace(", ", "\n").splitlines()):
        pp = ctext(c[0], line, bold=True, size=8.5, first=(i == 0))
        if i == 0:
            pp.paragraph_format.space_before = Pt(40)
            pp.paragraph_format.left_indent = Cm(0.6)
        else:
            pp.paragraph_format.left_indent = Cm(0.6)
    info = c[1]
    pp = ctext(info, "Abrechnungsperiode:", bold=True, italic=True, size=8.5)
    pp.paragraph_format.space_before = Pt(14)
    ctext(info, f"{fd(p0)} – {fd(p1)}", size=8.5, first=False)
    if interim and (s0 != p0 or s1 != p1):
        ctext(info, f"Ihre Nutzung: {fd(s0)} – {fd(s1)}", size=8.5, first=False)
    ctext(info, "", first=False)
    pp = ctext(info, "Rechnungsdatum: ", bold=True, italic=True, size=8.5, first=False)
    write(pp, fd(invoice_date), size=8.5)
    pp = ctext(info, "Zahlbar bis: ", bold=True, italic=True, size=8.5, first=False)
    write(pp, fd(due), bold=True, size=8.5)
    ctext(info, "", first=False)
    ctext(info, "Liegenschaft:", bold=True, italic=True, size=8.5, first=False)
    for line in (prop.get("address") or prop.get("name", "")).replace(", ", "\n").splitlines():
        ctext(info, line, size=8.5, first=False)

    title = (f"Zwischenabrechnung Heiz- und Nebenkosten {plabel} für '{st['unit']['name']}'" if interim
             else f"Heiz- und Nebenkostenabrechnung {plabel} für '{st['unit']['name']}'")
    para(title, bold=True, size=10.5, before=48, after=14)
    para(tenant.get("salutation") or "Guten Tag", bold=True, size=10, after=14)
    if not intro:
        intro = ("Mit diesem Brief erhalten Sie die Zwischenabrechnung der Heiz- und Nebenkosten mit detaillierter Energiekostenübersicht."
                 if interim else "Mit diesem Brief erhalten Sie die Nebenkostenabrechnung mit detaillierter Energiekostenübersicht.")
    para(intro, bold=True, size=9.5, after=16)

    saldo = st["saldo"]
    W = [6.0, 5.0, 5.0]
    t = table(W)
    row(t, ["CHF", "Ihre Kosten", "Liegenschaft"], W, size=9)
    row(t, ["", short, short], W, size=9)
    gap_cells = row(t, ["", "", ""], W)
    gap_cells[0].paragraphs[0].paragraph_format.line_spacing = Pt(6)
    row(t, ["Total", chf(st["total"]), chf(st["property_total"])], W, size=9)
    row(t, ["Akonto", chf(st["akonto"]), ""], W, size=9)
    cells = row(t, ["Saldo zu unseren Gunsten" if saldo >= 0 else "Guthaben zu Ihren Gunsten", chf(abs(saldo)), ""], W, size=9)
    cell_border(cells[1], top=("single", 6), bottom=("double", 6))
    if saldo > 0:
        txt = f"Bitte überweisen Sie uns den Saldo bis zum {fd(due)} auf das Mietzinskonto" + (f" {owner['iban']}." if owner.get("iban") else ".")
    elif saldo < 0:
        txt = "Das Guthaben überweisen wir Ihnen in den nächsten Tagen bzw. verrechnen es mit der nächsten Miete."
    else:
        txt = ""
    if txt:
        para(txt, bold=True, size=9.5, before=22)
    page_break()

    # ================================================================ Seite 2: Kostenübersicht
    bar("Kostenübersicht", "000000")
    gap(10)
    W = [7.5, 3.0, 4.5]
    ov = table(W)

    def box(cells, top=False, bottom=False):
        sides = {"left": ("single", 6), "right": ("single", 6)}
        if top:
            sides["top"] = ("single", 6)
        if bottom:
            sides["bottom"] = ("single", 6)
        cell_border(cells[2], **sides)

    cells = row(ov, ["", "", ""], W)
    ctext(cells[2], "Ihr Objekt", size=9, align=RIGHT)
    ctext(cells[2], short, size=9, align=LEFT, first=False)
    box(cells, top=True)

    def ov_bar(text, color):
        cells = ov.add_row().cells
        m = cells[0].merge(cells[1]).merge(cells[2])
        shade(m, color)
        margins(m, 15, 15, 60, 60)
        ctext(m, text, size=9.5, color="FFFFFF")

    def ov_cost(value):
        cells = row(ov, ["", "Kosten   CHF", chf(value)], W, size=9, aligns=[LEFT, RIGHT, LEFT])
        cells[2].paragraphs[0].paragraph_format.left_indent = Cm(0.6)
        box(cells)
        cells = row(ov, ["", "", ""], W)
        box(cells)

    if h["total"]:
        cells = row(ov, ["ENERGIE", "", ""], W, size=9)
        box(cells)
        ov_bar("Warmwasser", group_color("Warmwasser"))
        ov_cost(h["water_unit"])
        ov_bar("Raumheizung", group_color("Raumheizung"))
        ov_cost(h["room_unit"])
        cells = row(ov, ["Total: Energiekosten", "", ""], W, size=9)
        cell_border(cells[0], top=("single", 6))
        cell_border(cells[1], top=("single", 6))
        box(cells)
        cells = row(ov, ["", "CHF", chf(h["energy_unit"])], W, size=9, aligns=[LEFT, RIGHT, LEFT])
        cells[2].paragraphs[0].paragraph_format.left_indent = Cm(0.6)
        for c in cells:
            shade(c, "D0D0D0")
        box(cells)
        cells = row(ov, ["", "", ""], W)
        box(cells)
    for i, g in enumerate(st["groups"]):
        ov_bar(g["name"], group_color(g["name"], i))
        ov_cost(g["unit_total"])
    cells = row(ov, ["Rundungsdifferenz", "CHF", chf(st["rounding"])], W, size=9, aligns=[LEFT, RIGHT, LEFT])
    cells[2].paragraphs[0].paragraph_format.left_indent = Cm(0.6)
    for c in cells[:2]:
        cell_border(c, top=("single", 6))
    box(cells)
    cells = row(ov, ["TOTAL", "CHF", chf(st["total"])], W, size=9.5, color="FFFFFF", aligns=[LEFT, RIGHT, LEFT])
    cells[2].paragraphs[0].paragraph_format.left_indent = Cm(0.6)
    for c in cells:
        shade(c, "000000")
    cells = row(ov, ["", "", ""], W)
    box(cells, bottom=True)

    gap(14)
    W = [4.0, 1.5, 2.0, 2.0]
    t = table(W)
    row(t, ["Bezugsgrössen der Kennzahlen", "", "", ""], W, size=7)
    row(t, ["Wohnfläche", "m²", f"{st['unit']['area']:.2f}", f"{st['area_total']:.2f}"], W, size=7,
        aligns=[LEFT, LEFT, RIGHT, RIGHT])
    row(t, ["Nutzungsdauer", "Tage", str(st["days"]), str(st["days_total"])], W, size=7, aligns=[LEFT, LEFT, RIGHT, RIGHT])
    for r_ in t.rows[1:]:
        for run in r_.cells[3].paragraphs[0].runs:
            run.font.color.rgb = RGBColor.from_string("A0A0A0")
    page_break()

    # ================================================================ Detailseiten
    LW, RW = 10.3, 7.2

    def outer(two=True):
        t = table([LW, RW] if two else [FULL])
        return t

    def framed(cells_list):
        for c in cells_list:
            cell_border(c, top=("single", 6), bottom=("single", 6), left=("single", 6), right=("single", 6))

    def alloc_rows(left, right, items, saldo_label, saldo_value):
        """Zuweisungsblock: links Bezeichnung/Betrag/Anteile/CHF pro Einheit, rechts (grau) Ihr Anteil/Tage/%/Gesamt."""
        ctext(left, "Zuweisung", size=8)
        Wl = [3.0, 2.4, 2.6, 1.8]
        tl = table(Wl, left)
        row(tl, ["", "Betrag Total", "Total", "CHF/"], Wl, size=7.5)
        row(tl, ["Bezeichnung", "(CHF)", "Anteile", "Einh."], Wl, size=7.5, underline=False)
        tl.rows[-1].cells[0].paragraphs[0].runs[0].underline = True
        shade(right, "F2F2F2")
        ctext(right, f"Ihre Abrechnung von {fd(s0)} bis {fd(s1)}", size=7.5)
        Wr = [1.7, 1.2, 1.3, 2.4]
        tr = table(Wr, right)
        row(tr, ["", "", "", "Gesamt"], Wr, size=7.5)
        row(tr, ["Ihr Anteil", "Tage", "%", "(CHF)"], Wr, size=7.5)
        pct_time = st["days"] / st["days_total"] * 100 if st["days_total"] else 0
        for i, (name, total, ki, amount) in enumerate(items):
            per = total / ki["total"] if ki and ki["total"] else 0
            row(tl, [name, chf(total), f"{_n3(ki['total'])} {ki['unit']}" if ki else "–", f"{per:,.3f}".replace(",", "'")], Wl, size=7.5)
            cells = row(tr, [_n3(ki["mine"]) if ki else "–", str(st["days"]), f"{pct_time:.1f}", chf(amount)], Wr, size=7.5)
            if i == len(items) - 1:
                cells[3].paragraphs[0].runs[0].underline = True
        tt = table([4.6, 2.0], right)
        row(tt, [saldo_label, chf(saldo_value)], [4.6, 2.0], size=8)

    # ---- Heizkosten
    if h["total"]:
        bar("Heizkosten", group_color("Heizkosten"))
        t = outer()
        c = t.add_row().cells
        c[0].width, c[1].width = Cm(LW), Cm(RW)
        framed(c)
        Wl = [4.4, 2.0, 1.3, 2.2]
        letters = "ABCDEFGH"
        grps = list(h["groups"].items())
        for gi, (grp, gsum) in enumerate(grps):
            ctext(c[0], f"{letters[gi]}) {grp}", size=8, first=(gi == 0))
            tl = table(Wl, c[0])
            hdr = row(tl, ["Bezeichnung", "Menge", "Einh.", "Betrag CHF"], Wl, size=7.5, aligns=[LEFT, RIGHT, LEFT, RIGHT])
            for hc in hdr:
                for run in hc.paragraphs[0].runs:
                    run.underline = True
            for x in h["rows"]:
                if x["grp"] == grp:
                    row(tl, [x["name"], _n3(x["quantity"]) if x["quantity"] else "", x["unit_label"] if x["quantity"] else "",
                             chf(x["amount"])], Wl, size=7.5, aligns=[LEFT, RIGHT, LEFT, RIGHT])
            row(tl, ["", "", "", ""], Wl, size=4)
            row(tl, ["Total" if gi == 0 else f"Total {grp}", "", "", chf(gsum)], Wl, size=7.5)
            ctext(c[0], "", size=4, first=False)
        ctext(c[1], "Gesamtkosten (" + "+".join(letters[:len(grps)]) + ")", size=8, underline=True)
        Wr = [4.0, 1.2, 1.8]
        tr = table(Wr, c[1])
        for grp, gsum in grps:
            row(tr, [grp, "", chf(gsum)], Wr, size=7.5)
        row(tr, ["", "", ""], Wr, size=4)
        row(tr, ["Total: Heizkosten", "", chf(h["total"])], Wr, size=7.5)
        ctext(c[1], "", size=6, first=False)
        ctext(c[1], "Zuweisung", size=8, first=False)
        tz = table(Wr, c[1])
        row(tz, ["Raumheizung", f"{h['room_pct']:.2f}%", chf(h["room_total"])], Wr, size=7.5)
        row(tz, ["Warmwasser", f"{100 - h['room_pct']:.2f}%", chf(h["water_total"])], Wr, size=7.5)
        c = t.add_row().cells
        c[0].width, c[1].width = Cm(LW), Cm(RW)
        framed(c)
        alloc_rows(c[0], c[1], [("Raumheizung", h["room_total"], h.get("room_key"), h["room_unit"]),
                                ("Warmwasser", h["water_total"], h.get("water_key"), h["water_unit"])],
                   "Saldo Heizkosten", h["energy_unit"])
        oil = st.get("oil")
        if oil and oil.get("complete"):
            gap(6)
            Wo = [5.5, 2.6, 2.6, 2.6, 2.6]
            to = table(Wo)
            row(to, ["Verbrauch Heizöl", "Datum", "Menge", "Preis", "Betrag"], Wo, size=7.5)
            row(to, ["", "", "in Liter", "CHF / 100 l", "in CHF"], Wo, size=7)
            st_, en_ = oil["start"], oil["end"]
            row(to, ["Anfangsbestand", fd(date.fromisoformat(st_["date"])), _n3(st_["liters"]), f"{st_['price']:.2f}", chf(oil["start_value"])],
                Wo, size=7.5)
            if oil["purchases"]["count"]:
                row(to, ["Einkäufe", "", _n3(oil["purchases"]["liters"]), "", chf(oil["purchases"]["amount"])], Wo, size=7.5)
            row(to, ["Endbestand", fd(date.fromisoformat(en_["date"])), _n3(-en_["liters"]), f"{en_['price']:.2f}", chf(-oil["end_value"])],
                Wo, size=7.5)
            cells = row(to, ["Verbrauch", "", _n3(oil["liters"]), f"{oil['price']:.2f}" if oil.get("price") else "", chf(oil["amount"])],
                        Wo, size=8)
            cell_border(cells[4], bottom=("double", 6))
        gap(8)

    # ---- übrige Gruppen
    for gi, g in enumerate(st["groups"]):
        color = group_color(g["name"], gi)
        keys = {r["key_info"]["id"] if r.get("key_info") else None for r in g["rows"]}
        boxed = len(keys) == 1 and None not in keys
        bar(g["name"], color)
        if boxed:
            ki = g["rows"][0]["key_info"]
            t = outer()
            c = t.add_row().cells
            m = c[0].merge(c[1])
            framed([m])
            Wl = [4.4, 2.0, 1.3, 2.2]
            tl = table(Wl, m)
            hdr = row(tl, ["Bezeichnung", "Menge", "Einh.", "Betrag CHF"], Wl, size=7.5, aligns=[LEFT, RIGHT, LEFT, RIGHT])
            for hc in hdr:
                for run in hc.paragraphs[0].runs:
                    run.underline = True
            for r_ in g["rows"]:
                row(tl, [r_["name"], _n3(r_["quantity"]) if r_.get("quantity") else "", r_["unit_label"] if r_.get("quantity") else "",
                         chf(r_["total"])], Wl, size=7.5, aligns=[LEFT, RIGHT, LEFT, RIGHT])
            row(tl, ["", "", "", ""], Wl, size=4)
            row(tl, ["Total", "", "", chf(g["total"])], Wl, size=7.5)
            c = t.add_row().cells
            c[0].width, c[1].width = Cm(LW), Cm(RW)
            framed(c)
            alloc_rows(c[0], c[1], [(g["name"], g["total"], ki, g["unit_total"])], f"Saldo {g['name']}", g["unit_total"])
        else:
            t = outer(two=False)
            c = t.add_row().cells[0]
            c.width = Cm(FULL)
            framed([c])
            Wl = [6.4, 2.6, 2.2, 1.6, 2.8]
            tl = table(Wl, c)
            hdr = row(tl, ["Bezeichnung", "Total (CHF)", "Ihr Anteil", "Tage", "Betrag (CHF)"], Wl, size=7.5)
            for hc in hdr:
                for run in hc.paragraphs[0].runs:
                    run.underline = True
            row(tl, ["", "", "", "", ""], Wl, size=4)
            for r_ in g["rows"]:
                row(tl, [r_["name"], chf(r_["total"]), "direkt" if r_.get("direct") else f"{r_['share_pct']:.1f}%",
                         "–" if r_.get("direct") else str(st["days"]), chf(r_["unit_amount"])], Wl, size=7.5)
            cells = row(tl, ["Total", "", "", "", chf(g["unit_total"])], Wl, size=7.5)
            cell_border(cells[4], top=("single", 6), bottom=("double", 6))
        gap(8)

    cp = para(closing or "Die Belege der Nebenkostenabrechnung können Sie nach Absprache innert 30 Tagen bei uns einsehen.",
              bold=True, size=7.5, before=4, after=12)
    gp_ = para("Freundliche Grüsse", bold=True, size=9, after=14)
    cp.paragraph_format.keep_with_next = gp_.paragraph_format.keep_with_next = True
    para(owner.get("name", ""), bold=True, size=9)

    # leere Absätze in Tabellenzellen (Platzhalter vor/nach verschachtelten Tabellen) flach machen
    from docx.enum.text import WD_LINE_SPACING

    def flatten(tables):
        for tb in tables:
            for r_ in tb.rows:
                for cell in r_.cells:
                    for pg in cell.paragraphs:
                        if not pg.text.strip():
                            pg.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
                            pg.paragraph_format.line_spacing = Pt(3)
                    flatten(cell.tables)
    flatten(doc.tables)
    doc.save(path)
    return path
