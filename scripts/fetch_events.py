#!/usr/bin/env python3
"""
Kaiser Vagtvarsling v2 — natlig dataindsamling.
Henter: venue-events (scrape + Ticketmaster API) + danske helligdage (beregnet)
        + 16-dages vejrprognose pr. café (Open-Meteo, gratis, ingen nøgle)

Designprincip: scriptet fejler ALDRIG med exit code 1.
Alt er pakket ind i fejlhåndtering, og events.json skrives altid —
også selvom enkelte kilder fejler. Fejl rapporteres i 'sources'.
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
UA      = {"User-Agent": "Mozilla/5.0 (compatible; KaiserVagtvarsling/2.0)"}
TIMEOUT = 25

TM_KEY = os.environ.get("TM_API_KEY", "")
TM_URL = "https://app.ticketmaster.com/discovery/v2/events.json"

# Café-koordinater til vejrprognose
CAFES = {
    "hillerod":  (55.927, 12.300),
    "helsingor": (56.036, 12.612),
    "farum":     (55.809, 12.371),
    "vanlose":   (55.687, 12.491),
    "horsholm":  (55.881, 12.501),
    "sydhavnen": (55.651, 12.546),
}

DANISH_MONTHS = {
    "januar":1,"februar":2,"marts":3,"april":4,"maj":5,"juni":6,
    "juli":7,"august":8,"september":9,"oktober":10,"november":11,"december":12,
    "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,"aug":8,
    "sep":9,"okt":10,"nov":11,"dec":12,
}

IMPACT_KEYWORDS = [
    (3, ["udsolgt","festival","sankthans","sankt hans","royal run","ironman",
         "davis cup","landskamp","vm ","em ","havnefront","byfest","jubilæum","sold out"]),
    (2, ["koncert","håndbold","musical","show","stævne","marked","comedy",
         "stand-up","teater","messe","loppemarked","tour","gala"]),
    (1, ["foredrag","talk","workshop","udstilling","rundvisning","børn","junior","møde",
         "fælleslæsning","jam","quiz","open mic","yoga","croquis","hyg"]),
]

def score_impact(title, default):
    t = title.lower()
    for level, words in IMPACT_KEYWORDS:
        if any(w in t for w in words):
            return level
    return default


# ══════════════════════════════════════════════════════════════════════
# DANSKE HELLIGDAGE — beregnes matematisk, ingen ekstern kilde nødvendig
# ══════════════════════════════════════════════════════════════════════
def easter_sunday(year):
    """Gauss' påskeformel."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19*a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2*e + 2*i - h - k) % 7
    m = (a + 11*h + 22*l) // 451
    month = (h + l - 7*m + 114) // 31
    day = ((h + l - 7*m + 114) % 31) + 1
    return date(year, month, day)

def danish_holidays(year):
    """Returnerer [(dato, navn, impact, note)] for et år."""
    E = easter_sunday(year)
    hols = [
        (date(year,1,1),  "Nytårsdag",            2, "Helligdag — mange har fri"),
        (E - timedelta(days=3), "Skærtorsdag",     2, "Helligdag — påskeferie, mange har fri hele ugen"),
        (E - timedelta(days=2), "Langfredag",      2, "Helligdag — påskeferie"),
        (E,                "Påskedag",             2, "Helligdag"),
        (E + timedelta(days=1), "2. påskedag",     2, "Helligdag — forlænget weekend slutter"),
        (E + timedelta(days=39), "Kr. himmelfartsdag", 3, "Torsdags-helligdag — klassisk forlænget weekend, stor cafétrafik"),
        (E + timedelta(days=40), "Klemmedag (fre. efter Kr. himmelfart)", 2, "Mange holder fri — forlænget weekend"),
        (E + timedelta(days=49), "Pinsedag",       2, "Helligdag"),
        (E + timedelta(days=50), "2. pinsedag",    2, "Helligdag — forlænget weekend"),
        (date(year,6,5),  "Grundlovsdag",          2, "Mange butikker lukker tidligt — caféer får ofte ekstra trafik"),
        (date(year,12,24),"Juleaftensdag",         1, "De fleste lukker — tjek åbningstider"),
        (date(year,12,25),"1. juledag",            1, "Helligdag"),
        (date(year,12,26),"2. juledag",            2, "Helligdag — mange i byen for at bytte gaver / café-besøg"),
        (date(year,12,31),"Nytårsaftensdag",       1, "Tidlig lukning de fleste steder"),
    ]
    return hols

def holiday_events(today):
    """Helligdage som events for alle cafeer, 12 mdr frem."""
    out = []
    for year in (today.year, today.year + 1):
        for d, name, impact, note in danish_holidays(year):
            if d < today or d > today + timedelta(days=365):
                continue
            for cafe in CAFES:
                out.append({
                    "cafe": cafe, "date": d.isoformat(), "time": "",
                    "title": name, "venue": "Helligdag", "impact": impact,
                    "note": note, "source": "Helligdage",
                })
    return out


# ══════════════════════════════════════════════════════════════════════
# VEJR — Open-Meteo (gratis, ingen API-nøgle, 16 dages prognose)
# ══════════════════════════════════════════════════════════════════════
def fetch_weather():
    """Returnerer {cafe: {dato: {t: maxtemp, p: regn-sandsynlighed %, c: vejrkode}}}"""
    weather = {}
    for cafe, (lat, lon) in CAFES.items():
        try:
            r = requests.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": lat, "longitude": lon,
                "daily": "weather_code,temperature_2m_max,precipitation_probability_max",
                "forecast_days": 16, "timezone": "Europe/Copenhagen",
            }, timeout=TIMEOUT)
            r.raise_for_status()
            d = r.json().get("daily", {})
            days  = d.get("time", [])
            codes = d.get("weather_code", [])
            temps = d.get("temperature_2m_max", [])
            precs = d.get("precipitation_probability_max", [])
            weather[cafe] = {}
            for i, day in enumerate(days):
                weather[cafe][day] = {
                    "t": round(temps[i]) if i < len(temps) and temps[i] is not None else None,
                    "p": precs[i] if i < len(precs) and precs[i] is not None else None,
                    "c": codes[i] if i < len(codes) else None,
                }
        except Exception as ex:
            print(f"  FEJL Vejr {cafe}: {ex}", file=sys.stderr)
    return weather


# ══════════════════════════════════════════════════════════════════════
# Dato-parsing (dansk) — uændret fra v1
# ══════════════════════════════════════════════════════════════════════
DATE_RE = re.compile(
    r"(?:(?:man|tirs|ons|tors|fre|lør|søn)dag\s+)?(?:den\s+)?"
    r"(\d{1,2})\.?\s+([a-zæøå]+)\.?\s*(\d{4})?"
    r"(?:\s+kl\.?\s*(\d{1,2})[.:](\d{2}))?", re.IGNORECASE)
RANGE_RE = re.compile(
    r"(\d{1,2})\.?\s*(?:([a-zæøå]+)\.?\s*)?(?:\u2013|\u2014|-|til)\s*"
    r"(\d{1,2})\.?\s+([a-zæøå]+)\.?\s+(\d{4})", re.IGNORECASE)

def parse_danish_date(text, default_year):
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
                        out.append((cur,"")); cur += timedelta(days=1)
                    return out
            except ValueError: pass
    m = DATE_RE.search(text)
    if m:
        day,mon,year,hh,mm = m.groups()
        mon_n = DANISH_MONTHS.get(mon.lower())
        if mon_n:
            try:
                d = date(int(year) if year else default_year, mon_n, int(day))
                return [(d, f"{int(hh):02d}:{mm}" if hh else "")]
            except ValueError: pass
    return []


# ══════════════════════════════════════════════════════════════════════
# HTML-parsere — uændret fra v1
# ══════════════════════════════════════════════════════════════════════
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
        # kuto.dk-mønster: dato står EFTER overskriften
        if not re.search(r"\d{4}", ctx):
            nxt = h.find_next(string=re.compile(r"\d{4}"))
            if nxt: ctx = str(nxt)
        for d,t in parse_danish_date(ctx, default_year):
            found.append({"title":title,"date":d,"time":t})
    return found


# ══════════════════════════════════════════════════════════════════════
# Kilder
# ══════════════════════════════════════════════════════════════════════
SCRAPE_SOURCES = [
    {"name":"Royal Stage",       "cafe":"hillerod",  "url":"https://royalstage.dk/kalender/",               "venue":"Royal Stage",     "default_impact":2},
    {"name":"Klaverfabrikken",   "cafe":"hillerod",  "url":"https://klaverfabrikken.dk/kalender/",          "venue":"Klaverfabrikken", "default_impact":1},
    {"name":"Kulturværftet",     "cafe":"helsingor", "url":"https://kuto.dk/kalender/",                     "venue":"Kulturværftet",   "default_impact":1},
    {"name":"Toldkammeret",      "cafe":"helsingor", "url":"https://kuto.dk/toldkammeret/",                 "venue":"Toldkammeret",    "default_impact":1},
    {"name":"Kronborg",          "cafe":"helsingor", "url":"https://kronborg.dk/en/events/tours-and-events","venue":"Kronborg",        "default_impact":1},
    {"name":"Hørsholm/Rungsted", "cafe":"horsholm",  "url":"https://horsholm-rungsted.dk/eventkalender/",   "venue":"Hørsholm",        "default_impact":1},
]

TM_VENUES = [
    {"keyword": "Royal Stage", "city": "Hillerød", "venue_name": "Royal Stage", "cafe": "hillerod", "default_impact": 2},
    {"keyword": "Farum Arena", "city": "Farum",    "venue_name": "Farum Arena", "cafe": "farum",    "default_impact": 2},
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
            "cafe": src["cafe"], "date": e["date"].isoformat(), "time": e["time"],
            "title": e["title"], "venue": src["venue"],
            "impact": score_impact(e["title"], src["default_impact"]),
            "source": src["name"],
        })
    return events, strategy

def fetch_ticketmaster(today):
    events, status = [], []
    if not TM_KEY:
        status.append({"name":"Ticketmaster","ok":False,"count":0,"error":"TM_API_KEY mangler (GitHub Secret)"})
        return events, status
    for v in TM_VENUES:
        try:
            r = requests.get(TM_URL, params={
                "apikey": TM_KEY, "keyword": v["keyword"], "city": v["city"],
                "countryCode": "DK", "size": 200, "sort": "date,asc",
                "startDateTime": today.strftime("%Y-%m-%dT00:00:00Z"),
                "endDateTime":   (today+timedelta(days=365)).strftime("%Y-%m-%dT23:59:59Z"),
            }, timeout=TIMEOUT)
            r.raise_for_status()
            items = (r.json().get("_embedded") or {}).get("events") or []
            count = 0
            for e in items:
                name  = e.get("name","").strip()
                start = (e.get("dates") or {}).get("start") or {}
                d_str = start.get("localDate","")
                t_str = (start.get("localTime") or "")[:5]
                if not (name and d_str): continue
                events.append({
                    "cafe": v["cafe"], "date": d_str, "time": t_str,
                    "title": name, "venue": v["venue_name"],
                    "impact": score_impact(name, v["default_impact"]),
                    "source": "Ticketmaster",
                })
                count += 1
            status.append({"name":f"TM:{v['venue_name']}","ok":True,"count":count,"strategy":"api"})
            print(f"  OK   TM:{v['venue_name']:<18} {count:>3} events (api)")
        except Exception as ex:
            status.append({"name":f"TM:{v['venue_name']}","ok":False,"count":0,"error":str(ex)[:120]})
            print(f"  FEJL TM:{v['venue_name']}: {ex}", file=sys.stderr)
    return events, status

def fetch_kultunaut_sydhavnen(today):
    events = []
    for zip_code, label in [("2450","Sydhavnen"),("2500","Valby")]:
        try:
            url = f"https://www.kultunaut.dk/perl/arrlist/type-rss?area={zip_code}&lang=da"
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            soup = BeautifulSoup(r.content, "html.parser")
            for item in soup.find_all("item")[:40]:
                title = (item.find("title") or item).get_text(strip=True)
                desc  = (item.find("description") or item).get_text(" ", strip=True)
                pub   = (item.find("pubdate") or item).get_text(strip=True)
                try:
                    d_obj = datetime.strptime(pub[:16], "%a, %d %b %Y").date()
                except ValueError:
                    pairs = parse_danish_date(desc, today.year)
                    if not pairs: continue
                    d_obj, _ = pairs[0]
                if d_obj < today or d_obj > today + timedelta(days=365): continue
                combined = (title + " " + desc).lower()
                if not any(kw in combined for kw in
                           ["karens minde","sydhavn","banegaard","halvandet","valby kulturhus"]):
                    continue
                events.append({
                    "cafe":"sydhavnen","date":d_obj.isoformat(),"time":"",
                    "title":title,"venue":label,
                    "impact":score_impact(title,1),"source":"Kultunaut",
                })
        except Exception as ex:
            print(f"  FEJL Kultunaut {zip_code}: {ex}", file=sys.stderr)
    return events

def load_manual(today):
    try:
        path = ROOT / "data" / "manual-events.json"
        if not path.exists(): return [], None
        items = json.loads(path.read_text(encoding="utf-8"))
        out = []
        for e in items:
            try: d = date.fromisoformat(e["date"])
            except (KeyError, ValueError, TypeError): continue
            if d < today: continue
            e.setdefault("time",""); e.setdefault("venue",""); e.setdefault("impact",2)
            e["source"] = e.get("source","Manuel liste")
            out.append(e)
        return out, None
    except Exception as ex:
        # Typisk: JSON-syntaksfejl efter manuel redigering. Må ikke vælte kørslen.
        return [], str(ex)[:150]


# ══════════════════════════════════════════════════════════════════════
def main():
    today = date.today()
    all_events, status = [], []

    # 1. Ticketmaster API
    tm_events, tm_status = fetch_ticketmaster(today)
    all_events.extend(tm_events); status.extend(tm_status)

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

    # 3. Kultunaut (Sydhavnen)
    try:
        kult = fetch_kultunaut_sydhavnen(today)
        all_events.extend(kult)
        status.append({"name":"Kultunaut (Sydhavnen)","ok":True,"count":len(kult),"strategy":"rss"})
        print(f"  OK   {'Kultunaut (Sydhavnen)':<22} {len(kult):>3} events (rss)")
    except Exception as ex:
        status.append({"name":"Kultunaut (Sydhavnen)","ok":False,"count":0,"error":str(ex)[:120]})

    # 4. Helligdage (beregnet — kan ikke fejle eksternt)
    hols = holiday_events(today)
    all_events.extend(hols)
    status.append({"name":"Helligdage","ok":True,"count":len(hols)//len(CAFES),"strategy":"beregnet"})
    print(f"  OK   {'Helligdage':<22} {len(hols)//len(CAFES):>3} dage (beregnet)")

    # 5. Manuel liste
    manual, err = load_manual(today)
    all_events.extend(manual)
    status.append({"name":"Manuel liste","ok":err is None,"count":len(manual),
                   **({"error":f"JSON-fejl i manual-events.json: {err}"} if err else {"strategy":"json"})})
    print(f"  {'OK  ' if not err else 'FEJL'} {'Manuel liste':<22} {len(manual):>3} events")

    # 6. Vejr (16 dage frem pr. café)
    weather = {}
    try:
        weather = fetch_weather()
        ok_count = len(weather)
        status.append({"name":"Vejr (Open-Meteo)","ok":ok_count>0,"count":ok_count,"strategy":"api"})
        print(f"  OK   {'Vejr (Open-Meteo)':<22} {ok_count:>3} cafeer (16 dage)")
    except Exception as ex:
        status.append({"name":"Vejr (Open-Meteo)","ok":False,"count":0,"error":str(ex)[:120]})

    # Dedupe — manuel liste vinder
    seen, deduped = set(), []
    for e in sorted(all_events, key=lambda x: 0 if x["source"]=="Manuel liste" else 1):
        key = (e["cafe"], e["date"], e["title"].lower()[:40])
        if key in seen: continue
        seen.add(key)
        deduped.append(e)
    deduped.sort(key=lambda x: (x["date"], x.get("time","")))

    out = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sources": status,
        "events": deduped,
        "weather": weather,
    }
    (ROOT / "events.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nSkrev {len(deduped)} events + vejr for {len(weather)} cafeer til events.json")


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        # Absolut sidste sikkerhedsnet: skriv fejlen i events.json, exit 0
        print(f"KRITISK FEJL: {ex}", file=sys.stderr)
        try:
            (ROOT / "events.json").write_text(json.dumps({
                "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "sources": [{"name":"Script","ok":False,"count":0,"error":str(ex)[:200]}],
                "events": [], "weather": {},
            }, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    sys.exit(0)
