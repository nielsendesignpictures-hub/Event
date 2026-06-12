#!/usr/bin/env python3
"""
Kaiser Vagtvarsling — natlig dataindsamling.
Henter events fra venue-kalendere + manuel liste og skriver events.json.

Kører via GitHub Actions (se .github/workflows/update-events.yml).
Én fejlende kilde vælter ALDRIG hele kørslen — status rapporteres i events.json.
"""

import json
import re
import sys
import html as htmllib
from datetime import datetime, timedelta, date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
UA = {"User-Agent": "Mozilla/5.0 (compatible; KaiserVagtvarsling/1.0; cafe-bemandingsplanlaegning)"}
TIMEOUT = 25

DANISH_MONTHS = {
    "januar": 1, "februar": 2, "marts": 3, "april": 4, "maj": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}

# ----------------------------------------------------------------------
# Impact-regler: 3 = rød, 2 = gul, 1 = grøn
# Først matchende keyword vinder; ellers kildens default.
# ----------------------------------------------------------------------
IMPACT_KEYWORDS = [
    (3, ["udsolgt", "festival", "sankthans", "sankt hans", "royal run", "ironman",
         "davis cup", "landskamp", "vm ", "em ", "havnefront", "byfest", "jubilæum"]),
    (2, ["koncert", "håndbold", "musical", "show", "stævne", "marked", "comedy",
         "stand-up", "teater", "messe", "loppemarked", "tour"]),
    (1, ["foredrag", "talk", "workshop", "udstilling", "rundvisning", "bio",
         "børn", "junior", "møde"]),
]

def score_impact(title: str, default: int) -> int:
    t = title.lower()
    for level, words in IMPACT_KEYWORDS:
        if any(w in t for w in words):
            return level
    return default


# ----------------------------------------------------------------------
# Dato-parsing (dansk)
# Matcher fx:
#   "Lørdag 20. juni 2026 kl. 20.00"
#   "Søndag den 11. januar 2026 kl. 15.00"
#   "28. august – 6. september 2026"   (interval)
# ----------------------------------------------------------------------
DATE_RE = re.compile(
    r"(?:(?:man|tirs|ons|tors|fre|lør|søn)dag\s+)?(?:den\s+)?"
    r"(\d{1,2})\.?\s+([a-zæøå]+)\.?\s*(\d{4})?"
    r"(?:\s+kl\.?\s*(\d{1,2})[.:](\d{2}))?",
    re.IGNORECASE,
)
RANGE_RE = re.compile(
    r"(\d{1,2})\.?\s*(?:([a-zæøå]+)\.?\s*)?(?:\u2013|\u2014|-|til)\s*"
    r"(\d{1,2})\.?\s+([a-zæøå]+)\.?\s+(\d{4})",
    re.IGNORECASE,
)

def parse_danish_date(text: str, default_year: int):
    """Returnerer liste af (date, time_str). Interval udvides dag for dag (max 21)."""
    text = " ".join(text.split())

    m = RANGE_RE.search(text)
    if m:
        d1, mon1, d2, mon2, year = m.groups()
        mon2_n = DANISH_MONTHS.get(mon2.lower())
        mon1_n = DANISH_MONTHS.get(mon1.lower()) if mon1 else mon2_n
        if mon1_n and mon2_n:
            try:
                start = date(int(year), mon1_n, int(d1))
                end = date(int(year), mon2_n, int(d2))
                if start <= end and (end - start).days <= 21:
                    out, cur = [], start
                    while cur <= end:
                        out.append((cur, ""))
                        cur += timedelta(days=1)
                    return out
            except ValueError:
                pass

    m = DATE_RE.search(text)
    if m:
        day, mon, year, hh, mm = m.groups()
        mon_n = DANISH_MONTHS.get(mon.lower())
        if mon_n:
            try:
                d = date(int(year) if year else default_year, mon_n, int(day))
                t = f"{int(hh):02d}:{mm}" if hh else ""
                return [(d, t)]
            except ValueError:
                pass
    return []


# ----------------------------------------------------------------------
# Parser-strategi 1: JSON-LD (schema.org Event) — mest robust hvor det findes
# ----------------------------------------------------------------------
def events_from_jsonld(soup):
    found = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for it in items:
            if not isinstance(it, dict):
                continue
            t = it.get("@type", "")
            types = t if isinstance(t, list) else [t]
            if not any("Event" in str(x) for x in types):
                continue
            name = htmllib.unescape(str(it.get("name", ""))).strip()
            start = str(it.get("startDate", ""))[:16]
            if not (name and start):
                continue
            try:
                dt = datetime.fromisoformat(start.replace("Z", ""))
                found.append({"title": name, "date": dt.date(), "time": dt.strftime("%H:%M") if dt.hour else ""})
            except ValueError:
                continue
    return found


# ----------------------------------------------------------------------
# Parser-strategi 2: heuristik — datolinje efterfulgt af overskrift
# (passer til Royal Stage, kuto.dk og de fleste WordPress-venuekalendere)
# ----------------------------------------------------------------------
def events_from_headings(soup, default_year):
    found = []
    for h in soup.find_all(["h2", "h3"]):
        title = h.get_text(" ", strip=True)
        if not title or len(title) < 3 or len(title) > 120:
            continue
        # Find nærmeste forudgående tekstblok med en dato
        ctx = ""
        node = h
        for _ in range(6):
            node = node.find_previous(string=True)
            if node is None:
                break
            ctx = str(node).strip() + " " + ctx
            if re.search(r"\d{4}", ctx):
                break
        for d, t in parse_danish_date(ctx, default_year):
            found.append({"title": title, "date": d, "time": t})
    return found


# ----------------------------------------------------------------------
# Kilder
# ----------------------------------------------------------------------
SOURCES = [
    {"name": "Royal Stage",       "cafe": "hillerod",  "url": "https://royalstage.dk/kalender/",            "venue": "Royal Stage",            "default_impact": 2},
    {"name": "Kulturværftet",     "cafe": "helsingor", "url": "https://kuto.dk/kalender/",                  "venue": "Kulturværftet",          "default_impact": 1},
    {"name": "Kronborg",          "cafe": "helsingor", "url": "https://kronborg.dk/en/events/tours-and-events", "venue": "Kronborg",           "default_impact": 1},
    {"name": "Farum Arena (TM)",  "cafe": "farum",     "url": "https://www.ticketmaster.dk/venue/farum-arena-farum-billetter/faa/203", "venue": "Farum Arena", "default_impact": 2},
    {"name": "Hørsholm/Rungsted", "cafe": "horsholm",  "url": "https://horsholm-rungsted.dk/eventkalender/", "venue": "Hørsholm",              "default_impact": 1},
    {"name": "Karens Minde",      "cafe": "sydhavnen", "url": "https://www.kulturhusetkarensminde.kk.dk/",  "venue": "Karens Minde Kulturhus", "default_impact": 1},
]


def fetch_source(src, today):
    r = requests.get(src["url"], headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    raw = events_from_jsonld(soup)
    strategy = "jsonld"
    if len(raw) < 2:  # JSON-LD tom eller næsten tom → heuristik
        raw = events_from_headings(soup, today.year)
        strategy = "headings"

    events, seen = [], set()
    for e in raw:
        if e["date"] < today or e["date"] > today + timedelta(days=365):
            continue
        key = (e["date"].isoformat(), e["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        events.append({
            "cafe": src["cafe"],
            "date": e["date"].isoformat(),
            "time": e["time"],
            "title": e["title"],
            "venue": src["venue"],
            "impact": score_impact(e["title"], src["default_impact"]),
            "source": src["name"],
        })
    return events, strategy


def load_manual(today):
    """data/manual-events.json — faste årlige + manuelt tilføjede events."""
    path = ROOT / "data" / "manual-events.json"
    if not path.exists():
        return []
    items = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for e in items:
        try:
            d = date.fromisoformat(e["date"])
        except (KeyError, ValueError):
            continue
        if d < today:
            continue
        e.setdefault("time", "")
        e.setdefault("venue", "")
        e.setdefault("impact", 2)
        e["source"] = e.get("source", "Manuel liste")
        out.append(e)
    return out


def main():
    today = date.today()
    all_events, status = [], []

    for src in SOURCES:
        try:
            evts, strategy = fetch_source(src, today)
            all_events.extend(evts)
            status.append({"name": src["name"], "ok": True, "count": len(evts), "strategy": strategy})
            print(f"  OK   {src['name']:<22} {len(evts):>3} events ({strategy})")
        except Exception as ex:  # noqa: BLE001 — én kilde må ikke vælte natkørslen
            status.append({"name": src["name"], "ok": False, "count": 0, "error": str(ex)[:120]})
            print(f"  FEJL {src['name']:<22} {ex}", file=sys.stderr)

    manual = load_manual(today)
    all_events.extend(manual)
    status.append({"name": "Manuel liste", "ok": True, "count": len(manual), "strategy": "json"})
    print(f"  OK   {'Manuel liste':<22} {len(manual):>3} events")

    # Global dedupe: manuel liste vinder over scrapede dubletter
    seen, deduped = set(), []
    for e in sorted(all_events, key=lambda x: 0 if x["source"] == "Manuel liste" else 1):
        key = (e["cafe"], e["date"], e["title"].lower()[:40])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    deduped.sort(key=lambda x: (x["date"], x["time"]))

    out = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sources": status,
        "events": deduped,
    }
    (ROOT / "events.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nSkrev {len(deduped)} events til events.json")


if __name__ == "__main__":
    main()
