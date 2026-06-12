#!/usr/bin/env python3
"""
Kaiser Vagtvarsling — natlig dataindsamling.
Henter events fra Ticketmaster API + venue-scrapere + manuel liste.

Kører via GitHub Actions (se .github/workflows/update-events.yml).
Én fejlende kilde vælter ALDRIG hele kørslen — status rapporteres i events.json.
"""

import json
import os
import re
import sys
import html as htmllib
from datetime import datetime, timedelta, date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT    = Path(__file__).resolve().parent.parent
UA      = {"User-Agent": "Mozilla/5.0 (compatible; KaiserVagtvarsling/1.0)"}
TIMEOUT = 25

# Ticketmaster API-nøgle — sat som GitHub Secret (TM_API_KEY)
# eller hardkodet herunder som fallback under udvikling
TM_KEY  = os.environ.get("TM_API_KEY", "cvNq50AfUsfOy6rFGl7ZAkbHuyN4xGVT")
TM_URL  = "https://app.ticketmaster.com/discovery/v2/events.json"

DANISH_MONTHS = {
    "januar":1,"februar":2,"marts":3,"april":4,"maj":5,"juni":6,
    "juli":7,"august":8,"september":9,"oktober":10,"november":11,"december":12,
    "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,"aug":8,
    "sep":9,"okt":10,"nov":11,"dec":12,
}

# ── Impact-scoring ──────────────────────────────────────────────────────
IMPACT_KEYWORDS = [
    (3, ["udsolgt","festival","sankthans","sankt hans","royal run","ironman",
         "davis cup","landskamp","vm ","em ","havnefront","byfest","jubilæum","sold out"]),
    (2, ["koncert","håndbold","musical","show","stævne","marked","comedy",
         "stand-up","teater","messe","loppemarked","tour","gala"]),
    (1, ["foredrag","talk","workshop","udstilling","rundvisning","børn","junior","møde"]),
]

def score_impact(title: str, default: int) -> int:
    t = title.lower()
    for level, words in IMPACT_KEYWORDS:
        if any(w in t for w in words):
            return level
    return default


# ── Dansk dato-parsing ──────────────────────────────────────────────────
DATE_RE = re.compile(
    r"(?:(?:man|tirs|ons|tors|fre|lør|søn)dag\s+)?(?:den\s+)?"
    r"(\d{1,2})\.?\s+([a-zæøå]+)\.?\s*(\d{4})?"
    r"(?:\s+kl\.?\s*(\d{1,2})[.:](\d{2}))?", re.IGNORECASE)
RANGE_RE = re.compile(
    r"(\d{1,2})\.?\s*(?:([a-zæøå]+)\.?\s*)?(?:\u2013|\u2014|-|til)\s*"
    r"(\d{1,2})\.?\s+([a-zæøå]+)\.?\s+(\d{4})", re.IGNORECASE)

def parse_danish_date(text: str, default_year: int):
    text = " ".join(text.split())
    m = RANGE_RE.search(text)
    if m:
        d1,mon1,d2,mon2,year = m.groups()
        mon2_n = DANISH_MONTHS.get(mon2.lower())
        mon1_n = DANISH_MONTHS.get(mon1.lower()) if mon1 else mon2_n
        if mon1_n and mon2_n:
            try:
                start = date(int(year), mon1_n, int(d1))
                end   = date(int(year), mon2_n, int(d2))
                if start <= end and (end - start).days <= 21:
                    out, cur = [], start
                    while cur <= end:
                        out.append((cur,""))
                        cur += timedelta(days=1)
                    return out
            except ValueError: pass
    m = DATE_RE.search(text)
    if m:
        day,mon,year,hh,mm = m.groups()
        mon_n = DANISH_MONTHS.get(mon.lower())
        if mon_n:
            try:
                d = date(int(year) if year else default_year, mon_n, int(day))
                t = f"{int(hh):02d}:{mm}" if hh else ""
                return [(d,t)]
            except ValueError: pass
    return []


# ── HTML-parsere ────────────────────────────────────────────────────────
def events_from_jsonld(soup):
    found = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try: data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError): continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for it in items:
            if not isinstance(it, dict): continue
            t = it.get("@type","")
            if not any("Event" in str(x) for x in (t if isinstance(t,list) else [t])): continue
            name  = htmllib.unescape(str(it.get("name",""))).strip()
            start = str(it.get("startDate",""))[:16]
            if not (name and start): continue
            try:
                dt = datetime.fromisoformat(start.replace("Z",""))
                found.append({"title":name,"date":dt.date(),"time":dt.strftime("%H:%M") if dt.hour else ""})
            except ValueError: continue
    return found

def events_from_headings(soup, default_year):
    found = []
    for h in soup.find_all(["h2","h3"]):
        title = h.get_text(" ", strip=True)
        if not title or len(title) < 3 or len(title) > 120: continue
        ctx, node = "", h
        for _ in range(6):
            node = node.find_previous(string=True)
            if node is None: break
            ctx = str(node).strip() + " " + ctx
            if re.search(r"\d{4}", ctx): break
        for d,t in parse_danish_date(ctx, default_year):
            found.append({"title":title,"date":d,"time":t})
    return found


# ── Ticketmaster Discovery API ──────────────────────────────────────────
# Venue-IDs på Ticketmaster DK
TM_VENUES = [
    {"venue_id": "rZ7HnEZ17a-fa",  "venue_name": "Royal Stage",  "cafe": "hillerod",  "default_impact": 2},
    {"venue_id": "rZ7HnEZ17aUfaa", "venue_name": "Farum Arena",  "cafe": "farum",     "default_impact": 2},
]

def fetch_ticketmaster(today):
    """Henter events fra Ticketmaster Discovery API for alle TM-venues."""
    events, status = [], []
    date_from = today.strftime("%Y-%m-%dT00:00:00Z")
    date_to   = (today + timedelta(days=365)).strftime("%Y-%m-%dT23:59:59Z")

    for v in TM_VENUES:
        try:
            params = {
                "apikey":      TM_KEY,
                "venueId":     v["venue_id"],
                "countryCode": "DK",
                "startDateTime": date_from,
                "endDateTime":   date_to,
                "size":        200,
                "sort":        "date,asc",
            }
            r = requests.get(TM_URL, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            data   = r.json()
            items  = (data.get("_embedded") or {}).get("events") or []
            count  = 0
            for e in items:
                name  = e.get("name","").strip()
                start = (e.get("dates") or {}).get("start") or {}
                d_str = start.get("localDate","")
                t_str = start.get("localTime","")[:5] if start.get("localTime") else ""
                if not (name and d_str): continue
                try: d_obj = date.fromisoformat(d_str)
                except ValueError: continue
                events.append({
                    "cafe":    v["cafe"],
                    "date":    d_str,
                    "time":    t_str,
                    "title":   name,
                    "venue":   v["venue_name"],
                    "impact":  score_impact(name, v["default_impact"]),
                    "source":  "Ticketmaster",
                    "url":     (e.get("url") or ""),
                })
                count += 1
            status.append({"name": f"TM:{v['venue_name']}", "ok": True, "count": count, "strategy": "api"})
            print(f"  OK   TM:{v['venue_name']:<18} {count:>3} events (api)")
        except Exception as ex:
            status.append({"name": f"TM:{v['venue_name']}", "ok": False, "count": 0, "error": str(ex)[:120]})
            print(f"  FEJL TM:{v['venue_name']:<18} {ex}", file=sys.stderr)
    return events, status


# ── Kultunaut RSS — bruges til Karens Minde / Sydhavnen ────────────────
def fetch_kultunaut_sydhavnen(today):
    """
    Kultunaut har åbent RSS-feed pr. postnummer.
    Sydhavnen: 2450 (SV) og 2500 (Valby) dækker Karens Minde-området.
    """
    events = []
    for zip_code, label in [("2450", "Sydhavnen"), ("2500", "Valby")]:
        url = f"https://www.kultunaut.dk/perl/arrlist/type-rss?area={zip_code}&lang=da"
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            soup = BeautifulSoup(r.content, "xml")
            for item in soup.find_all("item")[:40]:
                title = (item.find("title") or item).get_text(strip=True)
                desc  = (item.find("description") or item).get_text(" ", strip=True)
                pub   = (item.find("pubDate") or item).get_text(strip=True)
                # Dato fra pubDate (RFC 822: "Thu, 12 Jun 2026 00:00:00 +0200")
                try:
                    dt = datetime.strptime(pub[:16], "%a, %d %b %Y")
                    d_obj = dt.date()
                except ValueError:
                    pairs = parse_danish_date(desc, today.year)
                    if not pairs: continue
                    d_obj, _ = pairs[0]
                if d_obj < today or d_obj > today + timedelta(days=365): continue
                # Filtrer til Karens Minde / Sydhavn-relevante venues
                combined = (title + " " + desc).lower()
                if not any(kw in combined for kw in
                           ["karens minde","sydhavn","banegaard","baneGaard","halvandet","valby kultcenter"]):
                    continue
                events.append({
                    "cafe":   "sydhavnen",
                    "date":   d_obj.isoformat(),
                    "time":   "",
                    "title":  title,
                    "venue":  label,
                    "impact": score_impact(title, 1),
                    "source": "Kultunaut",
                })
        except Exception as ex:
            print(f"  FEJL Kultunaut {zip_code}: {ex}", file=sys.stderr)
    return events


# ── HTML-scraper-kilder (uændret) ───────────────────────────────────────
SCRAPE_SOURCES = [
    {"name":"Royal Stage",       "cafe":"hillerod",  "url":"https://royalstage.dk/kalender/",               "venue":"Royal Stage",         "default_impact":2},
    {"name":"Klaverfabrikken",   "cafe":"hillerod",  "url":"https://klaverfabrikken.dk/kalender/",          "venue":"Klaverfabrikken",     "default_impact":1},
    {"name":"Kulturværftet",     "cafe":"helsingor", "url":"https://kuto.dk/kalender/",                     "venue":"Kulturværftet",       "default_impact":1},
    {"name":"Toldkammeret",      "cafe":"helsingor", "url":"https://kuto.dk/toldkammeret/",                 "venue":"Toldkammeret",        "default_impact":1},
    {"name":"Kronborg",          "cafe":"helsingor", "url":"https://kronborg.dk/en/events/tours-and-events","venue":"Kronborg",            "default_impact":1},
    {"name":"Hørsholm/Rungsted", "cafe":"horsholm",  "url":"https://horsholm-rungsted.dk/eventkalender/",   "venue":"Hørsholm",            "default_impact":1},
]


def fetch_scrape_source(src, today):
    r = requests.get(src["url"], headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    raw = events_from_jsonld(soup)
    strategy = "jsonld"
    if len(raw) < 2:
        raw = events_from_headings(soup, today.year)
        strategy = "headings"
    events, seen = [], set()
    for e in raw:
        if e["date"] < today or e["date"] > today + timedelta(days=365): continue
        key = (e["date"].isoformat(), e["title"].lower())
        if key in seen: continue
        seen.add(key)
        events.append({
            "cafe":   src["cafe"],
            "date":   e["date"].isoformat(),
            "time":   e["time"],
            "title":  e["title"],
            "venue":  src["venue"],
            "impact": score_impact(e["title"], src["default_impact"]),
            "source": src["name"],
        })
    return events, strategy


# ── Manuel liste ────────────────────────────────────────────────────────
def load_manual(today):
    path = ROOT / "data" / "manual-events.json"
    if not path.exists(): return []
    items = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for e in items:
        try: d = date.fromisoformat(e["date"])
        except (KeyError, ValueError): continue
        if d < today: continue
        e.setdefault("time",""); e.setdefault("venue",""); e.setdefault("impact",2)
        e["source"] = e.get("source","Manuel liste")
        out.append(e)
    return out


# ── Hovedprogram ────────────────────────────────────────────────────────
def main():
    today = date.today()
    all_events, status = [], []

    # 1. Ticketmaster API (Royal Stage + Farum Arena)
    tm_events, tm_status = fetch_ticketmaster(today)
    all_events.extend(tm_events)
    status.extend(tm_status)

    # 2. HTML-scrapere
    for src in SCRAPE_SOURCES:
        try:
            evts, strategy = fetch_scrape_source(src, today)
            all_events.extend(evts)
            status.append({"name":src["name"],"ok":True,"count":len(evts),"strategy":strategy})
            print(f"  OK   {src['name']:<22} {len(evts):>3} events ({strategy})")
        except Exception as ex:
            status.append({"name":src["name"],"ok":False,"count":0,"error":str(ex)[:120]})
            print(f"  FEJL {src['name']:<22} {ex}", file=sys.stderr)

    # 3. Kultunaut RSS (Sydhavnen / Karens Minde)
    try:
        kult = fetch_kultunaut_sydhavnen(today)
        all_events.extend(kult)
        status.append({"name":"Kultunaut (Sydhavnen)","ok":True,"count":len(kult),"strategy":"rss"})
        print(f"  OK   {'Kultunaut (Sydhavnen)':<22} {len(kult):>3} events (rss)")
    except Exception as ex:
        status.append({"name":"Kultunaut (Sydhavnen)","ok":False,"count":0,"error":str(ex)[:120]})
        print(f"  FEJL Kultunaut (Sydhavnen): {ex}", file=sys.stderr)

    # 4. Manuel liste (vinder over dubletter)
    manual = load_manual(today)
    all_events.extend(manual)
    status.append({"name":"Manuel liste","ok":True,"count":len(manual),"strategy":"json"})
    print(f"  OK   {'Manuel liste':<22} {len(manual):>3} events")

    # Dedupe: manuel liste har højest prioritet
    seen, deduped = set(), []
    for e in sorted(all_events, key=lambda x: 0 if x["source"]=="Manuel liste" else 1):
        key = (e["cafe"], e["date"], e["title"].lower()[:40])
        if key in seen: continue
        seen.add(key)
        deduped.append(e)
    deduped.sort(key=lambda x: (x["date"], x.get("time","")))

    out = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sources":   status,
        "events":    deduped,
    }
    (ROOT / "events.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nSkrev {len(deduped)} events til events.json")

if __name__ == "__main__":
    main()

