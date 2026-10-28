"""Benutzer & Rechte: Passwort-Hashing und Zuordnung jeder API-Route zu einem Bereich.

Bereiche (je Benutzer: none | view | edit):
  budget     Übersicht, Ausgaben, Erfassen, Rest vom Lohn, Vormerkungen
  bank       Abgleich (Kontoauszüge, Buchungen, Regeln)
  vehicles   Fahrzeuge, Tankkarten, Parkingpay, Traccar
  realestate Immobilien, Nebenkosten, Mieten
  projects   Projekte
  contracts  Verträge, Kredite, Abos
  income     Einnahmen, Lohnregeln, Lohnabrechnungen
  meters     Zähler, Ablesungen, Prognose
Administratoren dürfen alles, inkl. Einstellungen, Benutzer, Backup.
GET = ansehen, alle anderen Methoden = bearbeiten. Unbekannte Routen: nur Administratoren.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Optional

AREAS = [("budget", "Budget (Übersicht, Ausgaben, Erfassen)"), ("bank", "Abgleich / Kontoauszüge"), ("vehicles", "Fahrzeuge"),
         ("realestate", "Immobilien"), ("projects", "Projekte"), ("contracts", "Verträge & Abos"), ("income", "Einnahmen & Lohn"),
         ("meters", "Zähler & Energie")]
AREA_KEYS = [a for a, _ in AREAS]
LEVEL = {"none": 0, "view": 1, "edit": 2}

PUBLIC_PATHS = {"/login.html", "/style.css", "/favicon.svg", "/favicon-32.png", "/icon-192.png", "/icon-512.png",
                "/apple-touch-icon.png", "/manifest.webmanifest", "/api/login", "/api/health", "/login.js"}


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    it = 240_000
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, it)
    return f"pbkdf2${it}${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, h = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# (Muster, Bereich) – Reihenfolge zählt, erster Treffer gilt. Bereich "common" = jeder angemeldete Benutzer,
# "admin" = nur Administratoren, ("any", [Bereiche]) = Recht in einem der Bereiche genügt.
_RE_ENT = {"contracts": "contracts", "contract-versions": "contracts", "projects": "projects"}
RULES: list[tuple[str, object]] = [
    (r"^/api/(me|logout)(/|$)", "common"),
    (r"^/api/(meta|health)$", "common"),
    (r"^/api/settings$", "common-get"),            # lesen alle, ändern nur Admin
    (r"^/api/budgets$", ("budget", "common-get")),
    (r"^/api/vehicles$", ("vehicles", "common-get")),   # Fahrzeugliste braucht auch der Abgleich
    (r"^/api/exclude-reasons$", "budget"),
    (r"^/api/(users|status|backup|restore|setup|maintenance|export)(/|$)", "admin"),
    (r"^/api/(summary|trend|month-items|expenses|excluded|review|plan|orders)(/|$)", "budget"),
    (r"^/api/documents/", "budget"),
    (r"^/api/doc-search$", ("any", ["budget", "realestate", "projects"])),
    (r"^/api/allocations(/|$)", ("any", ["realestate", "projects", "budget"])),
    (r"^/api/(transactions|statements|rules|open-invoices)(/|$)", "bank"),
    (r"^/api/(vehicles|fuel-docs|fuel-entries|parking-docs|parking-entries|traccar)(/|$)", "vehicles"),
    (r"^/api/(projects-overview|projects)(/|$)", "projects"),
    (r"^/api/(contracts-overview|contracts|contract-payments|abo-suggestions)(/|$)", "contracts"),
    (r"^/api/(incomes|income-rules|income-credits|payslip-docs|payslips)(/|$)", "income"),
    (r"^/api/meters(/|$)", "meters"),
    (r"^/api/(properties-full|properties|nk-statements|files|rent-payments)(/|$)", "realestate"),
    (r"^/api/re/keys/", "realestate"),
    (r"^/api/re/", "re-ent"),
]


def required(path: str, method: str) -> tuple[str, Optional[list[str]], int]:
    """(Art, Bereiche, Stufe) – Art: public | common | admin | area"""
    if path in PUBLIC_PATHS:
        return "public", None, 0
    if not path.startswith("/api/"):
        return "common", None, 0          # Seiten und Skripte nur angemeldet (Daten kommen ohnehin nur über die API)
    lvl = LEVEL["view"] if method in ("GET", "HEAD", "OPTIONS") else LEVEL["edit"]
    for pat, area in RULES:
        if not re.search(pat, path):
            continue
        if area == "common":
            return "common", None, 0
        if area == "admin":
            return "admin", None, 0
        if area == "common-get":
            return ("common", None, 0) if lvl == 1 else ("admin", None, 0)
        if isinstance(area, tuple) and len(area) == 2 and area[1] == "common-get":
            return ("common", None, 0) if lvl == 1 else ("area", [area[0]], lvl)
        if isinstance(area, tuple) and area[0] == "any":
            return "area", list(area[1]), lvl
        if area == "re-ent":
            ent = path.split("/")[3] if len(path.split("/")) > 3 else ""
            return "area", [_RE_ENT.get(ent, "realestate")], lvl
        return "area", [area], lvl
    return "admin", None, 0


def allowed(user: Optional[dict], path: str, method: str) -> bool:
    kind, areas, lvl = required(path, method)
    if kind == "public":
        return True
    if not user:
        return False
    if user.get("is_admin"):
        return True
    if kind == "common":
        return True
    if kind == "admin":
        return False
    perms = user.get("perms") or {}
    return any(LEVEL.get(perms.get(a, "none"), 0) >= lvl for a in areas)


def can(user: Optional[dict], area: str, level: str = "view") -> bool:
    """Für Endpunkte, die Teile ausblenden (z.B. Einnahmen in der Übersicht). Ohne Benutzerverwaltung: alles erlaubt."""
    if user is None or user.get("is_admin"):
        return True
    return LEVEL.get((user.get("perms") or {}).get(area, "none"), 0) >= LEVEL[level]
