# Paperless Budget

**Haushaltsbudget, Kontoauszug-Abgleich und Fahrzeugkosten – direkt aus deinen Belegen in [Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx).**

> *English summary:* A self-hosted budgeting web app that uses the Paperless-ngx REST API as its data source.
> Invoices and receipts with a monetary custom field become expenses, tags become budget categories.
> It parses (Swiss) bank statement PDFs from Paperless, reconciles payments with invoices and marks them as paid,
> and tracks vehicle costs with mileage from [Traccar](https://www.traccar.org/) (cost per km, fuel consumption).
> UI and docs are in German.

![Übersicht](docs/screenshots/uebersicht.png)

## Warum?

Wer seine Rechnungen und Quittungen ohnehin in Paperless-ngx archiviert, hat die Daten für ein Haushaltsbudget
schon beisammen. Paperless Budget macht daraus Auswertungen, ohne die Belege ein zweites Mal zu erfassen:
Paperless bleibt die einzige Quelle für Belege, die App ergänzt nur Budgets, Einnahmen, Regeln und
Kontobewegungen.

## Funktionen

### Budget
- **Ausgaben aus Paperless**: Dokumente mit einem Custom Field vom Typ *Währung* (Standard: „Total“)
- **Kategorien über Paperless-Tags**: frei wählbar, mit Monatsbudget und Warnung bei Überschreitung
- **Dashboard**: Einnahmen, Ausgaben, Saldo, Verteilung nach Kategorie, 12-Monats-Verlauf
- **Erfassen**: Belege ohne Betrag mit OCR-Ausschnitt anzeigen, Betrag und Kategorie eintippen –
  wird direkt in Paperless zurückgeschrieben
- **Einnahmen**: einmalig oder monatlich wiederkehrend

### Abgleich mit Kontoauszügen
- **PDF-Kontoauszüge aus Paperless einlesen** (OCR-Text) – das Layout wird automatisch erkannt
- **Jede Buchung wird über den Saldo geprüft**, auch über Seitenumbrüche hinweg
- **Zahlungen mit Rechnungen abgleichen** (Betrag, Datum, Name im Buchungstext); bei Treffer setzt die App
  in Paperless den Tag „bezahlt“ und das Datumsfeld „Bezahlt am“
- **Zahlungen ohne Beleg** (z.B. Kartenzahlungen) zählen ins Budget; **Regeln** ordnen sie automatisch zu
  oder ignorieren Umbuchungen
- **Offene Rechnungen**: Belege, zu denen noch keine Zahlung gefunden wurde

![Abgleich](docs/screenshots/abgleich.png)

### Fahrzeuge
- **Kosten pro Fahrzeug** aus Tankkarten-Rechnungen, Paperless-Belegen (Fahrzeug-Tag) und Bankbuchungen
- **Kilometer aus Traccar** pro Monat
- **Kennzahlen**: Kosten/km, Treibstoff/km, Verbrauch (L oder kWh/100 km), Preis pro Liter/kWh
- **Tankkarten-Rechnungen** werden pro Tankvorgang gelesen und über die Kartennummer dem Fahrzeug zugeteilt

![Fahrzeuge](docs/screenshots/fahrzeuge.png)

<details>
<summary>Weitere Ansicht: Belege nacherfassen</summary>

![Erfassen](docs/screenshots/erfassen.png)
</details>

*Alle Screenshots zeigen erfundene Demo-Daten.*

## Voraussetzungen

- Docker und Docker Compose
- Paperless-ngx 2.x (mit Custom Fields) und ein API-Token
- optional: Traccar (für Fahrzeug-Kilometer)

## Installation

```bash
git clone https://github.com/pebu1977/paperless-budget.git
cd paperless-budget
cp .env.example .env
nano .env            # PAPERLESS_URL, PAPERLESS_TOKEN, PAPERLESS_PUBLIC_URL eintragen
docker compose up -d --build
```

Danach `http://<host>:8088` öffnen.

**API-Token**: In Paperless oben rechts auf den Benutzer → *Mein Profil* → *API-Auth-Token*.
Am besten einen eigenen Paperless-Benutzer anlegen, der nur Dokumente sehen und bearbeiten darf.

**Paperless auf demselben Docker-Host**: Im `docker-compose.yml` den `networks`-Block einkommentieren,
den Netzwerknamen mit `docker network ls` prüfen und `PAPERLESS_URL` auf den Container setzen,
z.B. `http://paperless-webserver-1:8000`.

> [!IMPORTANT]
> Die App kann Dokumente in Paperless ändern und zeigt Finanzdaten. Nicht ungeschützt ins Internet stellen –
> hinter einen Reverse Proxy mit Zugriffsschutz (z.B. Nginx Proxy Manager mit Access List) oder
> `BASIC_AUTH_USER` / `BASIC_AUTH_PASSWORD` in der `.env` setzen.

## Einrichtung

1. **Übersicht**: Fehlt das Betrags-Feld in Paperless, legt die App es per Knopfdruck an.
2. **Einstellungen → Budget-Kategorien**: Tags als Kategorien anhaken, Reihenfolge festlegen
   (hat ein Dokument mehrere, zählt der oberste) und Monatsbudgets eintragen.
3. **Einstellungen → „Erfassen“ eingrenzen**: z.B. nur Dokumenttyp *Rechnung* und *Quittung*.
4. **Einnahmen**: Lohn als monatlich wiederkehrend erfassen.

Tipp: Ein Paperless-**Workflow** kann neuen Rechnungen das Betrags-Feld automatisch (leer) zuweisen und
Matching-Regeln die Kategorie-Tags setzen – dann muss man in der App nur noch den Betrag eintippen.

## Kontoauszüge abgleichen

1. Kontoauszüge als PDF in Paperless ablegen, z.B. mit Dokumenttyp *Kontoauszug*.
2. **Einstellungen → Kontoauszüge & Abgleich**: diesen Dokumenttyp (oder einen Tag) wählen.
3. **Abgleich → Neue einlesen**.
4. **Eindeutige zuordnen** übernimmt alle Treffer, bei denen Betrag *und* Name passen.
   Treffer nur über den Betrag bleiben Vorschläge zum Bestätigen.
5. Für Zahlungen ohne Beleg eine Kategorie wählen oder eine **Regel** anlegen. Mehrere Begriffe lassen sich
   mit `+` verbinden, auch mit Karten-Endziffern, z.B. `Tankomat + 1234` → Fahrzeug.
6. Umbuchungen zwischen eigenen Konten **ignorieren**, damit sie nicht als Ausgabe zählen.

Eine zugeordnete Zahlung zählt nicht doppelt – nur die Rechnung (zum Rechnungsdatum).

### Unterstützte Banken

Der Leser erkennt zwei verbreitete Layouts automatisch:

| Layout | Spalten | Getestet mit |
|---|---|---|
| A | `[Datum] Text Betrag Valuta [Saldo]` | PostFinance, Zürcher Kantonalbank |
| B | `Datum Text Valuta Betrag [Saldo]` | Bank Zimmerberg |

Andere Banken mit ähnlichem Aufbau funktionieren oft ebenfalls. Ob alles stimmt, sieht man sofort: Buchungen,
deren Vorzeichen nicht über den Saldo bestätigt werden konnte, sind als „ungeprüft“ markiert.

**Deine Bank funktioniert nicht?** Gerne ein Issue eröffnen mit dem OCR-Text eines Auszugs
(Paperless → Dokument → Reiter *Inhalt*) – **vorher IBAN, Kontonummer, Namen und Adresse unkenntlich machen.**

## Fahrzeuge

1. In der `.env` `TRACCAR_URL` und `TRACCAR_TOKEN` (oder `TRACCAR_USER` / `TRACCAR_PASSWORD`) setzen.
2. In Paperless pro Fahrzeug einen Tag anlegen (z.B. „Fz Kombi“) und an Service-, Versicherungs-,
   Steuer- und Reparaturbelege hängen.
3. **Einstellungen → Fahrzeuge**: Fahrzeug mit Antrieb, Traccar-Gerät, Paperless-Tag und Tankkarten-Nr.
   anlegen (Endziffern genügen, mehrere mit Komma). Darunter den Korrespondenten der Tankkarten-Rechnungen
   wählen.
4. **Fahrzeuge → Neue einlesen** liest die Tankkarten-Rechnungen (getestet mit Vögtlin-Meyer).
5. Tankvorgänge mit Debitkarte im Abgleich oder per Regel dem Fahrzeug zuordnen. Für einen korrekten
   Verbrauch die Liter über „Tankvorgang manuell erfassen“ ergänzen – oder bei Elektroautos die geladenen kWh.

Der Verbrauch wird nur aus Monaten berechnet, in denen jeder Tankvorgang eine Mengenangabe hat.

## Konfiguration

Alle Einstellungen in der `.env`:

| Variable | Standard | Bedeutung |
|---|---|---|
| `PAPERLESS_URL` | `http://paperless:8000` | Adresse, unter der der Container Paperless erreicht |
| `PAPERLESS_TOKEN` | – | API-Token |
| `PAPERLESS_PUBLIC_URL` | = `PAPERLESS_URL` | Für die Links „Dokument öffnen“ im Browser |
| `PAPERLESS_VERIFY_SSL` | `true` | `false` bei selbstsigniertem Zertifikat |
| `PAPERLESS_API_VERSION` | leer | API-Version fixieren, falls nötig |
| `AMOUNT_FIELD_NAME` | `Total` | Name des Währungs-Felds (Gross-/Kleinschreibung egal) |
| `CURRENCY` | `CHF` | Währung |
| `PAID_TAG_NAME` | `bezahlt` | Tag für bezahlte Rechnungen |
| `PAID_FIELD_NAME` | `Bezahlt am` | Datumsfeld für das Zahlungsdatum |
| `MATCH_BEFORE_DAYS` | `5` | Rechnung darf so viele Tage nach der Zahlung datiert sein |
| `TRACCAR_URL` | leer | Traccar-Server, z.B. `http://traccar:8082` |
| `TRACCAR_TOKEN` | leer | Traccar-Token (alternativ `TRACCAR_USER` / `TRACCAR_PASSWORD`) |
| `TRACCAR_VERIFY_SSL` | `true` | `false` bei selbstsigniertem Zertifikat |
| `BASIC_AUTH_USER` / `BASIC_AUTH_PASSWORD` | leer | Optionaler Login |

Budgets, Einnahmen, Regeln, eingelesene Buchungen und Fahrzeuge liegen in `./data/budget.db` (SQLite) –
diesen Ordner ins Backup aufnehmen.

## Was in Paperless verändert wird

Die App schreibt nur, wenn du es auslöst:

- Betrag (Währungs-Feld) und Kategorie-Tag beim Erfassen bzw. Ändern einer Ausgabe
- Tag „bezahlt“ und Feld „Bezahlt am“ beim Zuordnen einer Zahlung (Lösen entfernt beides wieder)
- einmalig das Betrags-Feld, den Tag „bezahlt“ und das Feld „Bezahlt am“, falls sie fehlen

Alle anderen Custom Fields und Tags eines Dokuments bleiben unverändert.

## Aufbau

```
app/main.py          FastAPI-Backend: Proxy zur Paperless-/Traccar-API, SQLite, Auswertungen
app/bank.py          Kontoauszug-Leser und Abgleich-Logik
app/vehicles.py      Tankkarten-Rechnungen lesen, Fahrzeug-Kennzahlen
app/static/          Frontend (HTML, CSS, Vanilla JS, Chart.js lokal eingebunden)
```

Der Paperless-Token bleibt im Backend, der Browser spricht nur mit der App.
Die API ist unter `/docs` (Swagger UI) dokumentiert.

### Lokal entwickeln

```bash
pip install -r requirements.txt
PAPERLESS_URL=http://localhost:8000 PAPERLESS_TOKEN=... DB_PATH=./data/budget.db \
  uvicorn app.main:app --reload --port 8088
```

## Grenzen

- Kontoauszüge werden aus dem OCR-Text gelesen. Schlechte Scans oder ungewöhnliche Layouts können
  fehlschlagen – digital erzeugte PDFs aus dem E-Banking funktionieren am besten.
- Nur eine Währung pro Installation.
- Oberfläche nur auf Deutsch.
- Kein Mehrbenutzer-System: Alle, die Zugriff auf die App haben, sehen alle Daten.

## Mitwirken

Issues und Pull Requests sind willkommen – besonders Beispiele weiterer Bank- und Tankkarten-Layouts
(immer anonymisiert).

## Lizenz

[MIT](LICENSE)

Nicht offiziell mit Paperless-ngx oder Traccar verbunden.
Chart.js ist unter der MIT-Lizenz enthalten.
