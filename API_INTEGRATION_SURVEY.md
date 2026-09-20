# JARVIS × Public-APIs — Integration Survey

**Source:** [`public-apis/public-apis`](https://github.com/public-apis/public-apis) (canonical curated list).
**Scanned:** 1,850 APIs across 51 categories. Each was reviewed against JARVIS's *existing* capability
inventory (LLM fleet, voice, vision, web/search, memory vaults, Google calendar+mail, tasks/scheduler,
smarthome, telegram, downloader, computer-use) so we only recommend what fills a **real gap**.

Every pick below maps to a concrete JARVIS extension point:

```
.env KEY  →  utils/<thing>.py  →  new tool in TOOL_SPECS in utils/agent_loop.py  →  UI button/command
```

---

## TIER 1 — Highest value, free / no-auth, low effort (build first)

These add capabilities JARVIS genuinely lacks and cost nothing to start.

### Live / real-time awareness
| API | Why (JARVIS value) | Mapped to | Auth |
|---|---|---|---|
| **Open-Meteo** | Global weather + forecast → proactive "rain/heat/cold" briefings | `utils/weather.py` → `get_weather` → `proactive.py` | None |
| **US NWS** | Official forecasts + **severe-weather alerts** (watch/warnings) | same weather util; alert polling | None |
| **Frankfurter** | ECB currency conversion + history ("250 USD in EUR?") | `utils/currency.py` → `convert_currency` | None |
| **Nominatim** | Forward/reverse geocoding (feeds weather, transit, "near me") | `utils/geo.py` → `geocode` | None |
| **OpenSky** | Real-time ADS-B flight tracking ("track my flight XY1234") | `utils/flights.py` → `track_flight` | None |
| **WhereParcel** | Package tracking across UPS/FedEx/USPS/DHL | `utils/tracking.py` → `track_package` | free key |
| **NewsAPI.org** | Curated live headlines → morning/evening briefings | `proactive.py` / briefing tool | free key |
| **Alpha Vantage / Finnhub** | Real-time stock quotes + market news | `utils/stocks.py` → `get_stock` | free key |
| **TheSportsDB / balldontlie** | Multi-league scores + NBA stats/standings | `utils/sports.py` | free / None |
| **openFDA** | Drug/device/food safety + label data (reliable med info) | `utils/health.py` | free key |
| **AQICN** | Air-quality index for 1000+ cities | supplement weather | free key |
| **transport.rest** | Unified public-transit across many city networks | `utils/transit.py` | None |

### Knowledge / reference (better than free-text LLM)
| API | Why | Mapped to | Auth |
|---|---|---|---|
| **Free Dictionary** | Word defs, **IPA pronunciation**, synonyms, POS | `define` tool | None |
| **Open Library + Gutendex** | Book metadata/covers + Gutenberg full texts | `book` tool | None |
| **NASA APOD / Launch Library 2 / Open Notify / Sunrise-Sunset** | Space facts, launches, ISS position, sun times | `utils/sci.py` | None/free |
| **Met Museum / Art Institute Chicago** | Open collection metadata + images | `utils/art.py` | None |
| **Pexels** | Image search for visual answers | `image_search` tool | free key |
| **TMDb / TVMaze** | Movie/TV metadata + posters, TV schedules | `utils/media.py` | free key / None |
| **MusicBrainz / Lyrics.ovh / Radio Browser** | Music metadata, lyrics, live radio | `utils/music.py` | None |

### Daily life
| API | Why | Mapped to | Auth |
|---|---|---|---|
| **Open Food Facts** | Barcode/nutrition lookup — ties into `fridge_vision` | `utils/nutrition.py` | None |
| **TheMealDB** | Recipe from ingredients ("cook with chicken+rice?") | `utils/recipes.py` | free key |
| **Nager.Date** | 90+ country public holidays → scheduler skips/flags | `utils/scheduler_holidays.py` | None |
| **TinyURL** | Shorten links before JARVIS sends via courier/telegram | `utils/url_shortener.py` | free key |
| **SeatGeek** | Concert/event discovery ("what's on this weekend") | `utils/events.py` | free key |
| **file.io / GoFile** | Ephemeral file hosting to send exports via courier | `utils/file_share.py` | None |
| **Imgbb** | Host screenshots/images → share link (ties to vision) | `utils/image_share.py` | free key |

### Development / ops
| API | Why | Mapped to | Auth |
|---|---|---|---|
| **Libraries.io** | Query 30+ package managers (npm/PyPI/Maven) for versions/deps | `package_lookup` tool | free key |
| **Judge0 CE + Wandbox** | **Run & verify code `coder.py` generates** | `run_code` tool | free / None |
| **Kroki** | ASCII → PlantUML/Mermaid/Graphviz diagrams | `diagram` util | None |
| **NetworkCalc / host-t** | Subnet/DNS/network calculators for diagnostics | `net_tools` | None |
| **Is This Site Down?** | Uptime + **TLS/SSL expiry** check | `site_health` monitor | None |
| **flaky** | Fake REST with chaos control → test JARVIS retries | dev/test harness | None |

### Security / privacy hardening
| API | Why | Mapped to | Auth |
|---|---|---|---|
| **VirusTotal** | Multi-engine hash/file/URL scan **before saving any download** | `utils/urlguard.py` (called in `downloader.py`) | free key |
| **Google Safe Browsing** | Fast phishing/malware URL pre-check | `utils/urlguard.py` | free |
| **HaveIBeenPwned** | Breach + compromised-password lookup (k-anonymity range API) | `security.py` | free |
| **URLhaus (Abuse.ch)** | Malicious-URL DB as a no-key blocklist layer | `utils/urlguard.py` | None |
| **EmailRep + Kickbox** | Email risk/verification (vet inbound, validate before send) | `utils/emailguard.py` | None |
| **AbuseIPDB / IPLogs** | IP reputation + VPN/proxy/Tor origin detection | `security.py` | free / None |

---

## TIER 2 — Worth building soon (free-ish, slightly more effort)
- **WolframAlpha** — structured factual+math answers that complement the LLM (free key).
- **Jina AI** — high-quality embeddings/rerank to upgrade `utils/rlm/memory.py` vector recall (free key).
- **OCR.Space** — OCR for scanned docs/photos (free tier); pairs with the new vision pipeline.
- **iLovePDF / pdflayer** — web→PDF + document merge/split for report generation.
- **DocStruct** — extract invoices/receipts/bank statements → text (No auth).
- **Deepcode / DeepAI** — code review to back `utils/coder.py`.
- **Notion (OAuth)** — sync JARVIS knowledge vault to a Notion workspace.
- **Amadeus (OAuth)** — flight/travel planning (not just tracking).
- **Etherscan** — crypto balance/tx watch, if the user holds crypto.
- **OpenAlex / Semantic Scholar** — research-paper lookup (academic use).

---

## TIER 3 — Worth watching (build only if a need appears)
- **Pixela** — habit/effort streaks → `goals.py` / `recurring.py`.
- **Open Charge Map** — EV charging stations if the user drives electric.
- **Infermedica** — symptom triage (medically sensitive → explicit opt-in).
- **OpenAQ / eBird / IUCN** — environmental & wildlife data.
- **CheapShark** — game price deals.
- **StackExchange API** — code Q&A fallback.
- **Mailsac** — disposable email for testing notification flows.

---

## Explicitly NOT recommended (with reasons)
- **Scraping/screenshot/anti-bot** (ProxyCrawl, ScraperApi, ZenRows…): redundant — JARVIS has `browser_use`, `computer_use`, `downloader`, `web_reader`.
- **Google Docs/Sheets/maps/SDKs**: JARVIS already keys `GOOGLE_API_KEY` (secretary + geocoding).
- **Calendar APIs**: Google Calendar is native; only holiday data (Nager.Date) is worth adding.
- **Email-send infra** (Sendgrid, Mailtrap…): secretary uses Gmail.
- **Auth/user-management platforms** (Auth0, Stytch…): JARVIS is personal, no login management.
- **Chat/LLM/NLP/translation/sentiment APIs**: JARVIS's own LLM fleet + vision covers these.
- **Blockchain/DeFi deep stack** (Covalent, Bitquery, The Graph…): no consumer need; Etherscan kept on watch.
- **Betting/odds APIs**: JARVIS shouldn't facilitate gambling.
- **Paid enterprise finance/OSINT** (Plaid, Intrinio, Shodan, Censys…): expensive, no personal payoff.
- **Trivial/joke/filler** (HTTP Cat, Chuck Norris, isEven, memes, quotes, placeholders): the LLM generates these better.
- **Single-franchise gimmicks** (SWAPI, Harry Potter, GOT…): no ongoing value.
- **Localized single-city transit** (BART, TfL, MBTA…): `transport.rest` covers the general case.
- **Dated COVID stacks (~20)**: no ongoing value.

---

## Suggested build order (quick wins that light up the assistant)
1. **Weather + currency** (`Open-Meteo` + `Frankfurter`, no keys) — instant proactive daily briefings.
2. **Geocoding** (`Nominatim`, no key) — underpins weather/transit/events.
3. **URL/file guard** (`VirusTotal` + `Google Safe Browsing` + `URLhaus`) — harden the existing download path.
4. **Run code** (`Judge0` + `Wandbox`) — make the coder agent verify what it writes.
5. **Flights + packages** (`OpenSky` + `WhereParcel`) — "track my X".
6. **Nutrition/recipes** (`Open Food Facts` + `TheMealDB`) — closes the loop with `fridge_vision`.
7. **Word/book/media** (`Free Dictionary`, `Open Library`, `TMDb`) — fast structured reference answers.

*Full cluster breakdowns were produced by parallel review and are available on request; the per-cluster JSON sources live under `/tmp/apiclusters/`.*

---

## ✅ Build status (Tier-1 picks wired into JARVIS)

All 9 new tools are live in `TOOL_SPECS` + `executor` (server boots clean,
vision endpoint unaffected). Keys are optional — every integration degrades
gracefully when unset.

| Tool | Source module | Auth | Verified |
|---|---|---|---|
| `get_weather` (upgraded, +city) | `utils/weather_api.py` (Open-Meteo + NWS) | None | London 20°C + alerts |
| `convert_currency` | `utils/currency_api.py` (Frankfurter) | None | 100 USD = 87.26 EUR |
| `geocode` (fwd+rev) | `utils/nominatim_api.py` | None | Tokyo → 35.6769,139.7639 |
| `check_url` (+ `file:` scan) | `utils/urlguard.py` (URLhaus + Safe Browsing + VT) | URLhaus None; SB/VT key | example.com clean |
| `run_code` | `utils/run_code.py` (Wandbox + Judge0) | None / JUDGE0 key | JS `hi` via nodejs |
| `track_flight` (+ `near`) | `utils/flight_api.py` (OpenSky) | None | 76 aircraft @ LHR |
| `track_package` (carrier infer) | `utils/track_api.py` (WhereParcel) | WHEREPARCEL key | USPS detected |
| `food_lookup` (barcode/name) | `utils/food_api.py` (Open Food Facts) | None | Prince 466 kcal |
| `recipe` | `utils/food_api.py` (TheMealDB) | free key | chicken → 3 meals |

**Security hardening:** `downloader.py` now pre-scans every download URL and
refuses flagged ones; optional VirusTotal file-hash after save.

**To add keys** (each enables more depth, all optional): `SAFE_BROWSING_API_KEY`,
`VIRUSTOTAL_API_KEY`, `JUDGE0_API_KEY`, `WHEREPARCEL_API_KEY`, `THEMEALDB_API_KEY`.
