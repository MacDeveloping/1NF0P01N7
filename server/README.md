# Infopoint-Server

`infopoint-server.py` ist ein kleiner HTTP-Server, der iCalendar-Daten von konfigurierten Kalendern abruft und als JSON bereitstellt. Die Konfiguration liegt in `config.toml` im selben Verzeichnis.

## Voraussetzungen

- Python 3.10 oder neuer
- Zugriff auf die konfigurierten iCalendar-/Webcal-URLs

Benötigte Python-Pakete installieren:

```powershell
python -m pip install icalendar recurring-ical-events
```

Bei Python-Versionen ohne eingebautes `tomllib` wird zusätzlich das Paket `tomli` benötigt:

```powershell
python -m pip install tomli
```

## Installation und Start

1. In das Serververzeichnis wechseln:

   ```powershell
   cd server
   ```

2. `config.toml` anpassen.

3. Server starten:

   ```powershell
   python infopoint-server.py
   ```

Der Server lauscht standardmäßig auf allen IPv4-Netzwerkschnittstellen (`0.0.0.0`) am konfigurierten Port. Bei der Standardkonfiguration ist er unter `http://localhost:8008` erreichbar.

Beenden: `Strg+C`.

## Konfiguration: `config.toml`

| Wert | Beschreibung |
|---|---|
| `port` | TCP-Port des HTTP-Servers. Erlaubt sind Werte von `1` bis `65535`, zum Beispiel `8008`. |
| `url_wochenuebersicht` | iCalendar-/Webcal-URL für die Wochenübersicht. `webcal://` wird automatisch für den HTTP-Abruf umgesetzt. |
| `url_termine` | iCalendar-/Webcal-URL für die Terminliste. |
| `auth` | Gemeinsamer Authentifizierungswert für alle GET-Anfragen. Der Wert wird als URL-Parameter `auth` übergeben. |
| `cluster_mode` | Darstellungsmodus der normalen `/termine`-Antwort. Erlaubt sind `year` und `event`. Archivierte bzw. neu geparste Terminzeiträume verwenden immer `year`. |

Beispiel:

```toml
port = 8008
url_wochenuebersicht = "webcal://example.invalid/wochenuebersicht.ics"
url_termine = "webcal://example.invalid/termine.ics"
auth = "mein-geheimer-token"
cluster_mode = "event"
```

## HTTP-Endpunkte

Alle GET-Endpunkte benötigen den passenden Authentifizierungswert:

```text
?auth=DEIN_TOKEN
```

### Aktuelle Wochenübersicht

```text
GET /wochenuebersicht?auth=DEIN_TOKEN
```

Der Kalender wird live abgerufen. Zurückgegeben werden Termine ab dem aktuellen Tag für die nächsten drei Wochen, gruppiert nach Datum und Uhrzeit.

### Aktuelle Termine

```text
GET /termine?auth=DEIN_TOKEN
```

Der Kalender wird live abgerufen. Zurückgegeben werden zukünftige Einzeltermine. Das Feld `cluster_mode` entspricht dem Wert aus `config.toml`.

### Wochenübersicht für einen Zeitraum

```text
GET /archiv/wochenuebersicht/JJJJ?auth=DEIN_TOKEN
GET /archiv/wochenuebersicht/JJJJ-JJJJ?auth=DEIN_TOKEN
```

Beispiele:

```text
/archiv/wochenuebersicht/2026?auth=DEIN_TOKEN
/archiv/wochenuebersicht/2026-2031?auth=DEIN_TOKEN
```

Der konfigurierte Wochenübersichtskalender wird live neu abgerufen und für das vollständige angeforderte Jahr beziehungsweise den Jahresbereich geparst. Es werden keine vorhandenen Archivdateien gelesen.

### Termine für einen Zeitraum

```text
GET /archiv/termine/JJJJ?auth=DEIN_TOKEN
GET /archiv/termine/JJJJ-JJJJ?auth=DEIN_TOKEN
```

Beispiele:

```text
/archiv/termine/2026?auth=DEIN_TOKEN
/archiv/termine/2026-2031?auth=DEIN_TOKEN
```

Der konfigurierte Terminkalender wird live neu abgerufen und für den vollständigen Zeitraum geparst. Diese Antwort verwendet immer `cluster_mode: "year"`.

## Tagesarchivierung

Bei jeder erfolgreichen Anfrage an `/wochenuebersicht` oder `/termine` wird die erzeugte JSON-Antwort zusätzlich gespeichert:

```text
server/archiv/wochenuebersicht/
server/archiv/termine/
```

Die Verzeichnisse werden automatisch angelegt. Pro Endpunkt und Kalendertag bleibt eine Datei erhalten; eine weitere Anfrage am selben Tag ersetzt die vorhandene Datei und aktualisiert den Zeitstempel.

Unter Windows werden Doppelpunkte im Zeitanteil durch Bindestriche ersetzt, da Doppelpunkte in Windows-Dateinamen nicht erlaubt sind. Das Format lautet beispielsweise:

```text
2026-11-03_KW45_17-14-53_termine.json
```

Die `/archiv/...`-Endpunkte verwenden diese gespeicherten Dateien nicht, sondern parsen die iCalendar-Quelle bei jeder Anfrage neu.

## Fehlerverhalten

- `401`: Authentifizierung fehlt oder ist falsch.
- `404`: Endpunkt oder Zeitraumformat ist ungültig.
- `502`: Kalender konnte nicht abgerufen oder verarbeitet werden.
- `500`: Unerwarteter interner Serverfehler.
