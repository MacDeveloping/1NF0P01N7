#!/usr/bin/env python3
"""Infopoint HTTP-Server für iCal/WebDAV-Kalenderdaten.

Abhängigkeiten:
    python -m pip install icalendar recurring-ical-events

Start:
    python infopoint-server.py

Der Server lauscht auf 0.0.0.0, ist also über localhost und über die
lokale IP-Adresse des Rechners erreichbar. Zugriff aus dem Internet wird
nicht automatisch eingerichtet.
"""

from __future__ import annotations

import hmac
import json
import logging
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

try:
    from icalendar import Calendar
    import recurring_ical_events
except ImportError as exc:
    raise SystemExit(
        "Fehlende Abhängigkeit. Installiere sie mit:\n"
        "  python -m pip install icalendar recurring-ical-events"
    ) from exc


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "infopoint-server.toml"
BERLIN = ZoneInfo("Europe/Berlin")
LOG = logging.getLogger("infopoint-server")


def load_config() -> dict:
    """Lädt und prüft die TOML-Konfiguration."""
    try:
        with CONFIG_PATH.open("rb") as config_file:
            config = tomllib.load(config_file)
    except FileNotFoundError as exc:
        raise SystemExit(
            f"Konfigurationsdatei nicht gefunden: {CONFIG_PATH}"
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"Fehler in der TOML-Konfiguration: {exc}") from exc

    for key in ("port", "url_wochenuebersicht", "url_termine", "auth", "cluster_mode"):
        if key not in config:
            raise SystemExit(f"Pflichtparameter '{key}' fehlt in {CONFIG_PATH}")

    port = config["port"]
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise SystemExit("'port' muss eine Ganzzahl zwischen 1 und 65535 sein.")
    if not isinstance(config["auth"], str) or not config["auth"]:
        raise SystemExit("'auth' muss ein nicht leerer String sein.")

    if config["cluster_mode"] not in ("year", "event"):
        raise SystemExit("'cluster_mode' muss entweder 'year' oder 'event' sein.")

    return config


def normalize_calendar_url(url: str) -> str:
    """Übersetzt webcal:// in https:// für den HTTP-Abruf."""
    url = url.strip()
    if url.lower().startswith("webcal://"):
        return "https://" + url[len("webcal://"):]
    if url.lower().startswith("webcals://"):
        return "https://" + url[len("webcals://"):]
    return url


def fetch_calendar(url: str) -> Calendar:
    """Ruft eine iCalendar-Datei ab und parst sie."""
    if not url:
        raise ValueError("Für diesen Endpunkt ist keine Kalender-URL konfiguriert.")

    normalized_url = normalize_calendar_url(url)
    request = Request(
        normalized_url,
        headers={
            "User-Agent": "Infopoint-Server/1.0",
            "Accept": "text/calendar, text/plain, */*",
        },
    )
    with urlopen(request, timeout=20) as response:
        calendar_bytes = response.read()

    return Calendar.from_ical(calendar_bytes)


def as_berlin_datetime(value) -> datetime:
    """Konvertiert ein iCalendar-Datum/eine Uhrzeit nach Europe/Berlin."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # Bei einem DTSTART ohne TZID wird iCalendar-Zeit als UTC behandelt,
            # sofern die Quelle keine lokale TZID angegeben hat.
            value = value.replace(tzinfo=ZoneInfo("UTC"))
        return value.astimezone(BERLIN)

    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=BERLIN)

    raise ValueError(f"Nicht unterstützter DTSTART-Wert: {value!r}")


def parse_wochenuebersicht(calendar: Calendar) -> dict:
    """Erzeugt die JSON-Struktur für die kommenden drei Wochen.

    Wiederholungen werden mit recurring-ical-events expandiert. Dadurch
    werden EXDATE, RECURRENCE-ID und geänderte/gelöschte Einzeltermine
    berücksichtigt, soweit sie in den gelieferten iCalendar-Daten enthalten sind.

    Ganztägige Ereignisse erhalten time="". Termine werden pro Datum und
    Uhrzeit gruppiert. Bei identischer Uhrzeit gilt für die drei genannten
    Titel die Sonderreihenfolge Erststunde, Oberstunden, Unterstunde.
    """
    today = datetime.now(BERLIN).date()
    start = datetime.combine(today, time.min, tzinfo=BERLIN)
    end_date = today + timedelta(days=21)
    end = datetime.combine(end_date, time.min, tzinfo=BERLIN)

    # Die Bibliothek verarbeitet Serien und Ausnahmen, bevor wir die
    # ausgegebenen Instanzen in die gewünschte JSON-Struktur überführen.
    occurrences = recurring_ical_events.of(calendar).between(start, end)

    grouped = defaultdict(lambda: defaultdict(list))

    for component in occurrences:
        if component.name != "VEVENT":
            continue

        dtstart_prop = component.get("DTSTART")
        if dtstart_prop is None:
            continue

        raw_start = dtstart_prop.dt
        is_all_day = isinstance(raw_start, date) and not isinstance(raw_start, datetime)
        local_start = as_berlin_datetime(raw_start)

        event_date = local_start.date()
        if not (today <= event_date <= end_date):
            continue

        event_time = "" if is_all_day else local_start.strftime("%H:%M")
        title = str(component.get("SUMMARY", ""))
        notes = str(component.get("DESCRIPTION", ""))

        grouped[event_date][event_time].append({
            "title": title,
            "notes": notes,
        })

    special_order = {
        "Erststunde": 0,
        "Oberstunden": 1,
        "Unterstunde": 2,
    }

    days = []
    for event_date in sorted(grouped):
        times = []
        for event_time, events in grouped[event_date].items():
            # Die Sonderreihenfolge wird auf die drei exakt benannten Titel
            # angewendet; alle anderen Ereignisse bleiben danach stabil sortiert.
            events.sort(key=lambda event: (
                special_order.get(event["title"], 99),
                event["title"].casefold() if event["title"] in special_order else "",
            ))
            times.append({
                "time": event_time,
                "events": events,
            })

        # Uhrzeiten chronologisch sortieren; ganztägige Termine stehen zuerst.
        times.sort(key=lambda item: (item["time"] != "", item["time"]))
        days.append({
            "date": event_date.strftime("%d.%m.%Y"),
            "times": times,
        })

    return {"days": days}



def parse_termine(calendar: Calendar, cluster_mode: str) -> dict:
    """Parst zukünftige Einzeltermine in die JSON-Struktur für /termine.

    Wiederkehrende Serien werden nicht expandiert.
    Ganztägige iCalendar-Termine verwenden bei DTEND normalerweise
    ein exklusives Enddatum. Dieses wird um einen Tag korrigiert,
    damit ein eintägiger Termin Start- und Enddatum am selben Tag hat.
    """
    today = datetime.now(BERLIN).date()
    events = []

    def to_local_date(value) -> date:
        """Konvertiert iCalendar-Datumswerte in ein Berliner Kalenderdatum."""
        if isinstance(value, datetime):
            # Zeiten ohne TZID werden als lokale Berliner Zeit interpretiert.
            if value.tzinfo is None:
                value = value.replace(tzinfo=BERLIN)
            return value.astimezone(BERLIN).date()

        if isinstance(value, date):
            return value

        raise ValueError(f"Ungültiger iCalendar-Datumswert: {value!r}")

    for component in calendar.walk("VEVENT"):
        # DTSTART ist für einen auswertbaren Termin erforderlich.
        dtstart_prop = component.get("DTSTART")
        if dtstart_prop is None:
            continue

        start_value = dtstart_prop.dt
        start_date = to_local_date(start_value)

        # DTEND ist in iCalendar optional. Fehlt es, wird für diese
        # JSON-Struktur das Startdatum auch als Enddatum verwendet.
        dtend_prop = component.get("DTEND")

        if dtend_prop is not None:
            end_value = dtend_prop.dt
            end_date = to_local_date(end_value)

            # Bei ganztägigen Terminen ist DTEND laut iCalendar
            # exklusiv: DTSTART=05.10. und DTEND=06.10. bedeutet
            # einen eintägigen Termin am 05.10.
            is_all_day = (
                isinstance(start_value, date)
                and not isinstance(start_value, datetime)
            )
            end_is_all_day = (
                isinstance(end_value, date)
                and not isinstance(end_value, datetime)
            )

            if is_all_day and end_is_all_day and end_date > start_date:
                end_date -= timedelta(days=1)
        else:
            end_date = start_date

        # Nur Termine, die heute oder später beginnen, ausgeben.
        if start_date < today:
            continue

        events.append({
            "date_start": start_date.strftime("%d.%m.%Y"),
            "date_end": end_date.strftime("%d.%m.%Y"),
            "title": str(component.get("SUMMARY", "")),
            "notes": str(component.get("DESCRIPTION", "")),
        })

    # Chronologische Reihenfolge, bei gleichem Startdatum nach Titel.
    events.sort(key=lambda event: (
        datetime.strptime(event["date_start"], "%d.%m.%Y").date(),
        event["title"].casefold(),
    ))

    return {
        "cluster_mode": cluster_mode,
        "events": events,
    }



class InfopointRequestHandler(BaseHTTPRequestHandler):
    server_version = "InfopointServer/1.0"

    def send_json(self, status_code: int, payload: dict | str) -> None:
        if isinstance(payload, str):
            body = payload.encode("utf-8")
            content_type = "text/plain; charset=utf-8"
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"

        self.send_response(status_code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self) -> None:
        parsed_url = urlsplit(self.path)
        query = parse_qs(parsed_url.query, keep_blank_values=True)
        supplied_auth = query.get("auth", [""])[0]

        if not hmac.compare_digest(supplied_auth, self.server.config["auth"]):
            self.send_json(401, "autentifizierung falsch")
            return

        route = parsed_url.path.rstrip("/") or "/"

        try:
            if route == "/wochenuebersicht":
                calendar = fetch_calendar(self.server.config["url_wochenuebersicht"])
                result = parse_wochenuebersicht(calendar)
                self.send_json(200, result)
            elif route == "/termine":
                calendar = fetch_calendar(self.server.config["url_termine"])
                result = parse_termine(
                    calendar,
                    self.server.config["cluster_mode"],
                )
                self.send_json(200, result)
            else:
                self.send_json(404, {"error": "Endpunkt nicht gefunden"})
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            LOG.exception("Fehler beim Verarbeiten von %s", route)
            self.send_json(502, {
                "error": "Kalenderdaten konnten nicht abgerufen oder verarbeitet werden.",
                "detail": str(exc),
            })
        except Exception:
            LOG.exception("Unerwarteter Fehler beim Verarbeiten von %s", route)
            self.send_json(500, {"error": "Interner Serverfehler"})

    def log_message(self, format_string: str, *args) -> None:
        LOG.info("%s - %s", self.address_string(), format_string % args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()

    # 0.0.0.0 bindet alle IPv4-Netzwerkschnittstellen: localhost und LAN-IP.
    server = ThreadingHTTPServer(("0.0.0.0", config["port"]), InfopointRequestHandler)
    server.config = config

    LOG.info("Infopoint-Server läuft auf Port %s", config["port"])
    LOG.info("Lokal: http://127.0.0.1:%s", config["port"])
    LOG.info("Im Heimnetz: http://<lokale-IP-des-Rechners>:%s", config["port"])

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Server wird beendet …")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
