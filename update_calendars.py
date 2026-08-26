#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import hashlib
import html as html_std
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from lxml import html as lxml_html


OUT = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/Berlin")
NOW = datetime.now(TZ)
def get(url: str) -> str:
    request = Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; ColumbiaCalendarExport/1.0)"}
    )
    with urlopen(request, timeout=12) as response:
        return response.read().decode("utf-8", errors="replace")


def unfold_ics(text: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", text)


def extract_vevent(text: str) -> str:
    match = re.search(r"BEGIN:VEVENT\r?\n(.*?)\r?\nEND:VEVENT", text, re.S)
    if not match:
        raise ValueError("No VEVENT found")
    body = html_std.unescape(
        match.group(1).replace("\r\n", "\n").replace("\r", "\n")
    )
    body = body.replace("[nbsp]", "")
    return "BEGIN:VEVENT\r\n" + body.replace("\n", "\r\n") + "\r\nEND:VEVENT"


def prop(event: str, name: str) -> str | None:
    flat = unfold_ics(event)
    match = re.search(rf"^{re.escape(name)}(?:;[^:]*)?:(.*)$", flat, re.M)
    return match.group(1).strip() if match else None


def event_start(event: str) -> datetime:
    value = prop(event, "DTSTART")
    if not value:
        return datetime.max.replace(tzinfo=TZ)
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=ZoneInfo("UTC")
        ).astimezone(TZ)
    return datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=TZ)


def clean_text(value: str) -> str:
    return " ".join(value.split())


def ics_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def make_theater_event(url: str) -> str | None:
    html = get(url)
    doc = lxml_html.fromstring(html)
    title_nodes = doc.xpath("//h1[contains(concat(' ',normalize-space(@class),' '),' header-title ')]")
    date_nodes = doc.xpath(
        "//*[contains(concat(' ',normalize-space(@class),' '),' header-date ')"
        " and not(contains(concat(' ',normalize-space(@class),' '),' header-date-prev '))]"
    )
    canonical_nodes = doc.xpath("//link[@rel='canonical']/@href")
    if not title_nodes or not date_nodes or not canonical_nodes:
        return None

    canonical_url = canonical_nodes[0]
    slug_date = re.search(r"/event/(\d{8})-", canonical_url)
    time_match = re.search(
        r"(\d{2})\.(\d{2})\.\s+um\s+(\d{2}):(\d{2})"
        r"(?:\s*/\s*Einlass\s+(\d{2}):(\d{2}))?",
        clean_text(date_nodes[0].text_content()),
    )
    if not slug_date or not time_match:
        return None

    year = int(slug_date.group(1)[:4])
    day, month, hour, minute = map(int, time_match.group(1, 2, 3, 4))
    start = datetime(year, month, day, hour, minute, tzinfo=TZ)
    end = start + timedelta(hours=4)
    doors = None
    if time_match.group(5):
        doors = f"{time_match.group(5)}:{time_match.group(6)}"

    title = clean_text(title_nodes[0].text_content())
    support = [
        clean_text(x.text_content())
        for x in doc.xpath(
            "//*[contains(concat(' ',normalize-space(@class),' '),' header-support-row ')"
            " or contains(concat(' ',normalize-space(@class),' '),' item-support-row ')]"
        )
    ]
    tour_nodes = doc.xpath(
        "//*[contains(concat(' ',normalize-space(@class),' '),' header-tour ')]"
    )
    description = []
    if tour_nodes:
        description.append(clean_text(tour_nodes[0].text_content()))
    if support:
        description.extend(support)
    if doors:
        description.append(f"Einlass: {doors}")
    description.append(f"Event: {canonical_url}")

    uid_seed = f"{canonical_url}|{start.isoformat()}"
    uid = hashlib.sha256(uid_seed.encode()).hexdigest()[:20] + "@columbia-theater.de"
    stamp = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    return "\r\n".join(
        [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;TZID=Europe/Berlin:{start.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND;TZID=Europe/Berlin:{end.strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{ics_escape(title)}",
            f"DESCRIPTION:{ics_escape(chr(10).join(description))}",
            "LOCATION:Columbia Theater\\, Columbiadamm 9-11\\, 10965 Berlin",
            f"URL:{canonical_url}",
            "END:VEVENT",
        ]
    )


def theater_events() -> list[str]:
    base = "https://columbia-theater.de/"
    doc = lxml_html.fromstring(get(base))
    urls = []
    for card in doc.xpath(
        "//a[contains(concat(' ',normalize-space(@class),' '),' item ')"
        " and contains(@href,'/event/')]"
    ):
        statuses = card.xpath(
            ".//*[contains(concat(' ',normalize-space(@class),' '),' item-status ')]"
        )
        status = clean_text(statuses[0].text_content() if statuses else "").lower()
        if "canceled" in status or "abgesagt" in status:
            continue
        if "relocated" in status or "verlegt" in status:
            continue
        urls.append(urljoin(base, card.get("href")))
    urls = list(dict.fromkeys(urls))
    def load(url: str) -> str | None:
        try:
            return make_theater_event(url)
        except Exception:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
        results = list(executor.map(load, urls))
    return sorted((x for x in results if x), key=event_start)


def halle_events() -> list[str]:
    base = "https://www.columbiahalle.berlin/events.html"
    doc = lxml_html.fromstring(get(base))
    urls = []
    for link in doc.xpath("//a[contains(normalize-space(.),'Calendar Entry')]"):
        cards = link.xpath(
            "ancestor::div[contains(concat(' ',normalize-space(@class),' '),"
            "' eventlist_event ')][1]"
        )
        card_text = clean_text(cards[0].text_content()).lower() if cards else ""
        if "cancelled" in card_text or "canceled" in card_text:
            continue
        urls.append(urljoin(base, link.get("href")))
    urls = list(dict.fromkeys(urls))

    def load(url: str) -> str | None:
        try:
            return extract_vevent(get(url))
        except Exception:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
        results = list(executor.map(load, urls))
    events = [x for x in results if x and event_start(x) >= NOW - timedelta(days=1)]
    return sorted(events, key=event_start)


def calendar(name: str, events: list[str]) -> str:
    header = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//OpenAI//Columbia Venues Calendar Export//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(name)}",
        "X-WR-TIMEZONE:Europe/Berlin",
    ]
    return "\r\n".join(header + events + ["END:VCALENDAR", ""])


def main() -> None:
    theater = theater_events()
    halle = halle_events()
    outputs = {
        "theater.ics": calendar("Columbia Theater – Upcoming Events", theater),
        "halle.ics": calendar("Columbiahalle – Upcoming Events", halle),
    }
    for filename, contents in outputs.items():
        (OUT / filename).write_text(contents, encoding="utf-8", newline="")
    print(f"Theater: {len(theater)}")
    print(f"Halle: {len(halle)}")


if __name__ == "__main__":
    main()
