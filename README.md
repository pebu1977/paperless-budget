# Paperless Budget

Budget-Web-App, die direkt mit der REST-API von **Paperless-ngx** arbeitet.

- **Ausgaben** = Paperless-Dokumente mit gesetztem Custom Field **„Total“** (Typ *Währung*)
- **Kategorien** = Paperless-Tags, die du in der App als Budget-Kategorie markierst
- **Monatsbudget** pro Kategorie mit Fortschrittsbalken und Warnung bei Überschreitung
- **Dashboard**: Einnahmen, Ausgaben, Saldo, Verteilung, 12-Monats-Verlauf (Klick auf einen Monat springt hin)
- **Zurückschreiben**: Betrag und Kategorie-Tag direkt in Paperless setzen (Ansichten „Ausgaben“ und „Erfassen“)
- **Einnahmen**: einmalig oder monatlich wiederkehrend (mit optionalem Enddatum), lokal gespeichert
- **Abgleich mit Kontoauszügen**: PDF-Auszüge aus Paperless einlesen, Belastungen mit Rechnungen abgleichen,
  bei Treffer Tag „bezahlt“ + Datumsfeld „Bezahlt am“ in Paperless setzen; Zahlungen ohne Beleg zählen als Ausgabe
- **Fahrzeuge**: Kosten pro Fahrzeug (Tankkarten-Rechnungen, Belege, Bankbuchungen), Kilometer aus **Traccar**,
  Kosten/km, Treibstoff/km, Verbrauch (L bzw. kWh/100 km) und Monatsverlauf

Der API-Token bleibt im Backend; der Browser spricht nur mit der App. Budgets, Einnahmen und
Einstellungen liegen in `./data/budget.db` (SQLite) – Paperless selbst wird nur um Betrag/Tags ergänzt.

## Installation

```bash
cp .env.example .env      # Token & URLs eintragen
docker compose up -d --build
```

Danach unter `http://<host>:8088` öffnen.

**API-Token:** In Paperless oben rechts auf dein Profil → *API-Auth-Token* → erzeugen/kopieren.
Am besten einen eigenen Paperless-Benutzer anlegen, der nur Dokumente sehen und ändern darf.

**Netzwerk:** Läuft Paperless auf demselben Docker-Host, im `docker-compose.yml` den `networks`-Block
einkommentieren (Netzwerkname mit `docker network ls` prüfen) und `PAPERLESS_URL` auf den
Container-Namen setzen, z.B. `http://paperless-webserver:8000`. Sonst die normale URL verwenden.

**Zugriffsschutz:** Hinter Nginx Proxy Manager mit Access List betreiben oder `BASIC_AUTH_USER` /
`BASIC_AUTH_PASSWORD` in `.env` setzen – die App kann Dokumente in Paperless ändern.

## Erste Schritte

1. **Übersicht**: Fehlt das Feld „Total“, legt die App es per Knopfdruck in Paperless an.
2. **Einstellungen**: Tags als Kategorien anhaken, Reihenfolge festlegen (bei mehreren Kategorie-Tags
   pro Dokument zählt der oberste), Monatsbudgets eintragen. Für „Erfassen“ z.B. nur Dokumenttyp
   *Rechnung* / *Quittung* wählen.
3. **Erfassen**: Dokumente des Monats ohne Betrag erscheinen hier mit OCR-Ausschnitt – Betrag und
   Kategorie eintragen, Enter, fertig.
4. **Einnahmen**: Lohn etc. als monatlich wiederkehrend erfassen.

Tipp: Mit einem Paperless-**Workflow** (Trigger „Dokument hinzugefügt“) kann das Feld „Total“ bei
neuen Rechnungen automatisch leer zugewiesen werden – oder Matching-Regeln setzen die Kategorie-Tags.

## Abgleich mit Kontoauszügen

1. **Einstellungen → Kontoauszüge & Abgleich**: Dokumenttyp (z.B. *Kontoauszug*) oder Tag wählen, an dem die App
   die Auszüge erkennt. Speichern.
2. **Abgleich → Neue einlesen**: Die App liest die Buchungen aus dem Paperless-Inhalt (OCR-Text).
   Jede Buchung wird über den Saldo geprüft; nicht prüfbare sind als „ungeprüft“ markiert (± tauscht das Vorzeichen).
   Getestet mit PostFinance, ZKB und Bank Zimmerberg (Layout wird automatisch erkannt); überlappende
   Auszüge werden nicht doppelt gezählt.
3. **Zuordnen**: Gleicher Betrag, Rechnung höchstens 60 Tage vor (bzw. 5 Tage nach) der Zahlung, Name im
   Buchungstext = Bonus. „Eindeutige zuordnen“ übernimmt alle Fälle, bei denen auch der Name eindeutig passt;
   Treffer nur über den Betrag bleiben Vorschläge zum Anklicken. Beim Zuordnen setzt die App
   an der Rechnung den Tag **bezahlt** und das Feld **Bezahlt am** (legt beide an, falls nötig).
4. **Ohne Beleg**: Kartenzahlungen etc. zählen als Ausgabe. Kategorie direkt wählen oder eine **Regel** anlegen
   (z.B. „MIGROL → Auto“, „KONTOÜBERTRAG AN → ignorieren“), die für alle künftigen Buchungen gilt.
5. **Ignorieren** für Umbuchungen zwischen eigenen Konten, Kreditkarten-Ladungen usw. – zählen dann nicht.

Doppelzählung: Eine zugeordnete Zahlung zählt nicht zusätzlich – nur die Rechnung (zum Rechnungsdatum).

## Fahrzeuge

1. **`.env`**: `TRACCAR_URL` und entweder `TRACCAR_TOKEN` (Traccar → Konto → Token) oder `TRACCAR_USER`/`TRACCAR_PASSWORD`.
   Läuft Traccar im selben Docker-Netz, den Container-Namen verwenden (z.B. `http://traccar:8082`).
2. **In Paperless** pro Fahrzeug einen Tag anlegen (z.B. „Fz Golf“) und an Service-, Versicherungs-, Steuer-
   und Reparaturbelege hängen.
3. **Einstellungen → Fahrzeuge**: Fahrzeug anlegen mit Antrieb, Traccar-Gerät, Paperless-Tag und Tankkarten-Nr.
   (Endziffern genügen, mehrere mit Komma). Darunter den Korrespondenten der Tankkarten-Rechnungen wählen
   (z.B. Vögtlin-Meyer) und speichern.
4. **Fahrzeuge → Neue einlesen**: Die Tankkarten-Rechnungen werden pro Tankvorgang gelesen (Datum, Tankstelle,
   Menge, Betrag nach Rabatt) und über die Karten-Nr. dem Fahrzeug zugeteilt.
5. **Bankbuchungen** (z.B. Tankomat mit Debitkarte) im Abgleich einem Fahrzeug zuweisen oder per Regel,
   z.B. `Tankomat + 2877` → Fahrzeug. Die Karten-Endziffern liest die App aus den Kontoauszügen.
   Tankstellen-Buchungen zählen als Treibstoff.

Kennzahlen: **Kosten/km** = alle Kosten ÷ km; **Treibstoff/km**; **Verbrauch** = Menge ÷ km × 100, nur aus Monaten,
in denen jeder Tankvorgang eine Mengenangabe hat (Tanken per Debitkarte hat keine Liter – dafür
„Tankvorgang manuell erfassen“). Traccar-km vergangener Monate werden zwischengespeichert.

Eine Tankkarten-Rechnung zählt fürs Fahrzeug nur über ihre Tankvorgänge, nicht zusätzlich als Beleg –
auch wenn sie den Fahrzeug-Tag trägt.

## Konfiguration (`.env`)

| Variable | Standard | Bedeutung |
|---|---|---|
| `PAPERLESS_URL` | `http://paperless:8000` | Adresse, unter der der Container Paperless erreicht |
| `PAPERLESS_TOKEN` | – | API-Token |
| `PAPERLESS_PUBLIC_URL` | = `PAPERLESS_URL` | Für „Dokument öffnen“-Links im Browser |
| `PAPERLESS_VERIFY_SSL` | `true` | `false` bei selbstsigniertem Zertifikat |
| `PAPERLESS_API_VERSION` | leer | API-Version fixieren, falls nötig |
| `AMOUNT_FIELD_NAME` | `Total` | Name des Währungs-Felds (Gross-/Kleinschreibung egal) |
| `CURRENCY` | `CHF` | Währung beim Schreiben und Anzeigen |
| `PAID_TAG_NAME` | `bezahlt` | Tag für zugeordnete Rechnungen |
| `PAID_FIELD_NAME` | `Bezahlt am` | Datumsfeld für das Zahlungsdatum |
| `MATCH_BEFORE_DAYS` | `5` | Rechnung darf so viele Tage nach der Zahlung datiert sein |
| `TRACCAR_URL` | leer | Traccar-Server für Kilometer |
| `TRACCAR_TOKEN` | leer | Traccar-Token (alternativ `TRACCAR_USER` / `TRACCAR_PASSWORD`) |
| `TRACCAR_VERIFY_SSL` | `true` | `false` bei selbstsigniertem Zertifikat |
| `BASIC_AUTH_USER` / `BASIC_AUTH_PASSWORD` | leer | Optionaler Login |

## Hinweise

- Rückerstattungen/Gutschriften als negativen Betrag erfassen.
- Datumsbasis ist das Paperless-Feld *Erstellt* (Belegdatum), nicht das Hinzufügedatum.
- Beim Zurückschreiben übernimmt die App alle anderen Custom Fields und Nicht-Kategorie-Tags des Dokuments.
- API-Endpunkte sind unter `/docs` (Swagger) einsehbar.
- Backup: `./data/budget.db` sichern (z.B. mit ins Synology-Backup aufnehmen).

## Struktur

```
app/main.py          FastAPI-Backend (Proxy zu Paperless, SQLite)
app/bank.py          Kontoauszug-Leser und Abgleich-Logik
app/vehicles.py      Tankkarten-Rechnungen lesen, Kennzahlen
app/static/          Frontend (HTML/CSS/JS, Chart.js lokal eingebunden)
Dockerfile, docker-compose.yml, .env.example
```
