# Kaiser Vagtvarsling

Eventoverblik og bemandingsvarsler for Café Kaisers 6 cafeer.
Vælg café + vagtplanperiode → se hvilke dage du skal skrue op for bemandingen.

## Aktivering (5 minutter ved PC'en)

1. **Opret repo** på github.com → "New repository" → navn fx `kaiser-vagtvarsling` → Public → Create
2. **Upload alle filer** fra denne mappe (træk-og-slip i GitHub eller `git push`).
   Vigtigt: mappestrukturen skal bevares, især `.github/workflows/`
3. **Tænd GitHub Pages:** Settings → Pages → Source: "Deploy from a branch" → Branch: `main` / `(root)` → Save.
   Siden ligger nu på `https://<dit-brugernavn>.github.io/kaiser-vagtvarsling/`
4. **Kør første dataindsamling:** Actions-fanen → "Opdater eventdata" → "Run workflow".
   Derefter kører den automatisk hver nat kl. 04.
5. **Logo:** Erstat indholdet af `<div class="logo-slot">` i `index.html` med
   `<img src="logo.png" style="width:100%;height:100%;object-fit:contain">` og upload `logo.png`

## Sådan hænger det sammen

```
GitHub Action (hver nat 04:00)
   └─ scripts/fetch_events.py
        ├─ scraper venue-kalendere (Royal Stage, Kulturværftet, Kronborg, ...)
        ├─ læser data/manual-events.json   ← HER tilføjer du selv events
        └─ skriver events.json
              └─ index.html læser events.json og viser overblikket
```

## Tilføj et event manuelt

Redigér `data/manual-events.json` direkte på GitHub (blyant-ikonet) og tilføj:

```json
{
  "cafe": "hillerod",
  "date": "2026-08-15",
  "time": "14:00",
  "title": "Byfest i gågaden",
  "venue": "Hillerød bymidte",
  "impact": 3,
  "note": "Hele byen på gaden"
}
```

`cafe`: `hillerod` / `helsingor` / `farum` / `vanlose` / `horsholm` / `sydhavnen`
`impact`: 3 = rød (høj), 2 = gul (forhøjet), 1 = grøn (mindre)

Manuel liste vinder altid over scrapede dubletter, så du kan også bruge den
til at rette/opgradere et scraped event (samme dato + titel).

## Datakilder

| Kilde | Café | Metode |
|---|---|---|
| royalstage.dk/kalender | Hillerød | scrape |
| kuto.dk/kalender | Helsingør | scrape |
| kronborg.dk | Helsingør | scrape |
| ticketmaster.dk (Farum Arena) | Farum | scrape |
| horsholm-rungsted.dk | Hørsholm | scrape |
| Karens Minde | Sydhavnen | scrape |
| data/manual-events.json | Alle | manuel (Ironman, VM halvmaraton, Sankthans m.fl.) |

Status for hver kilde vises øverst til højre på siden ("Datakilder: ● 6/7 OK").
Fejler en kilde, kører resten videre — fejlen vises blot i status.

## Kendte begrænsninger (v1.0)

- Scraperne er heuristiske: ændrer en venue-side layout, kan kilden fejle indtil
  selectoren justeres i `fetch_events.py`. Statusvisningen afslører det med det samme.
- Ticketmaster-siden kan blokere scraping. Plan B: opret gratis API-nøgle på
  developer.ticketmaster.com og brug Discovery API (Danmark er dækket).
- Impact-score er regelbaseret (keywords). Næste niveau: kalibrér mod jeres egne
  POS-tal pr. eventtype.
