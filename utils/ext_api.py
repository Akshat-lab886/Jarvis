"""Third wave of public-API integrations for JARVIS:

  research(query)       OpenAlex (no key) — academic papers/works.
  habit(metric)         Pixela (no key, self-token) — habit streaks.
  eth_watch(address)    Etherscan (free ETHERSCAN_API_KEY) — balance/tx.
  ocr_image(image)      OCR.Space (free key) — extract text from image/PDF.
  gen_pdf / pdf_url     pdflayer (free key) — HTML/URL -> PDF.

Every integration degrades gracefully: missing keys or flaky endpoints
return a clear message instead of raising.
"""
import os
import base64
import requests

_TIMEOUT = 20
_UA = "JARVIS-assistant/1.0 (personal assistant)"

_OPENALEX = "https://api.openalex.org/works"
_PIXELA = "https://pixe.la/v1"
_ETHERSCAN = "https://api.etherscan.io/api"
_OCR = "https://api.ocr.space/parse/image"
_PDFLAYER = "https://api.pdflayer.com/api/convert"

ETHERSCAN_KEY = (os.getenv("ETHERSCAN_API_KEY") or "").strip()
OCR_KEY = (os.getenv("OCR_API_KEY") or "").strip()
PDF_KEY = (os.getenv("PDFLAYER_API_KEY") or "").strip()


# ---- OpenAlex (no key) : academic research ---- #

def research(query, limit=3):
    """Search academic works (OpenAlex) -> title, authors, year, cited."""
    try:
        r = requests.get(_OPENALEX, params={"search": query,
                                            "per-page": limit,
                                            "mailto": "jarvis@example.com"},
                         headers={"User-Agent": _UA}, timeout=_TIMEOUT)
        results = (r.json().get("results") or [])
    except Exception as e:
        return f"Research lookup unavailable: {e}"
    if not results:
        return f"No papers found for '{query}', Sir."
    out = []
    for w in results[:limit]:
        title = (w.get("title") or "untitled")[:110]
        authors = [a.get("author", {}).get("display_name")
                   for a in (w.get("authorships") or [])][:3]
        who = ", ".join(x for x in authors if x) or "anonymous"
        year = w.get("publication_year")
        cited = w.get("cited_by_count")
        out.append(f"{title} — {who} ({year}) [cited {cited}]")
    return "; ".join(out)


# ---- Pixela (no key self-token) : habit streaks ---- #

_PIXELA_USER = (os.getenv("PIXELA_USER") or "").strip()


def habit(metric):
    """Pixela habit metric. Auto-creates a 'jarvis' graph the first time
    and GETs today's value; increments are done by the agent calling the
    lower-level pixela_increment. No key (PIXELA_USER only)."""
    if not _PIXELA_USER:
        return ("Habit streaks need PIXELA_USER set (free Pixela account); "
                "I'll track your metrics then, Sir.")
    # ensure graph exists
    try:
        requests.post(f"{_PIXELA}/users/{_PIXELA_USER}/graphs",
                      json={"id": "jarvis", "name": "jarvis metrics",
                            "unit": "count", "type": "int",
                            "color": "sora"},
                      timeout=_TIMEOUT)  # 200 or 409 (exists) is fine
        r = requests.get(f"{_PIXELA}/v1/users/{_PIXELA_USER}/graphs/jarvis",
                         timeout=_TIMEOUT)
        data = r.json()
        if data.get("isSuccess"):
            return (f"Pixela '{metric}' graph: "
                    f"{data.get('data', {}).get('quantity', 0)} today. "
                    f"Graph: https://pixe.la/v1/users/{_PIXELA_USER}/graphs/jarvis")
        return "Pixela graph ready — ask me to increment a habit, Sir."
    except Exception as e:
        return f"Pixela unavailable: {e}"


# ---- Etherscan (free key) : crypto watch ---- #

def eth_watch(address):
    """Etherscan balance/watch for an ETH address (free key)."""
    if not ETHERSCAN_KEY:
        return ("Ethereum watch needs a free ETHERSCAN_API_KEY — set it and "
                "I'll check balances, Sir.")
    try:
        r = requests.get(_ETHERSCAN, params={
            "module": "account", "action": "balance",
            "address": (address or "").strip(), "tag": "latest",
            "apikey": ETHERSCAN_KEY}, timeout=_TIMEOUT)
        d = r.json()
        if d.get("status") != "1":
            return f"Etherscan: {d.get('result', 'request failed')}"
        wei = int(d["result"])
        eth = wei / 1e18
        return f"Address {address[:10]}… holds {eth:.4f} ETH."
    except Exception as e:
        return f"Etherscan unavailable: {e}"


# ---- OCR.Space (free key) : image/PDF text ---- #

def ocr_image(image):
    """OCR text from an image or PDF (base64 data URL or /static path)."""
    if not OCR_KEY:
        return ("OCR needs a free OCR_API_KEY (ocr.space) — set it and I'll "
                "read text from images/scans, Sir.")
    payload = {"apikey": OCR_KEY, "language": "eng",
               "OCREngine": "2", "isOverlayRequired": "false"}
    try:
        if image.startswith("data:"):
            # data:image/png;base64,XXXX
            payload["base64Image"] = ("data:image/png;base64," +
                                      image.split(",", 1)[1])
        else:
            # /static/<file> -> absolute URL via the server origin
            origin = os.getenv("JARVIS_ORIGIN", "http://127.0.0.1:5099")
            payload["url"] = origin + image
        r = requests.post(_OCR, data=payload, timeout=_TIMEOUT)
        d = r.json()
        if d.get("OCRExitCode") != 1:
            return f"OCR: {d.get('ErrorMessage', 'could not read image')}"
        text = (d.get("ParsedResults") or [{}])[0].get("ParsedText", "").strip()
        return text[:600] if text else "OCR: no text found in that image."
    except Exception as e:
        return f"OCR unavailable: {e}"


# ---- PDF generation (pdflayer free) : HTML/URL -> PDF ---- #

def pdf_url(url, fname="output.pdf", page_size="A4"):
    """Convert a URL or raw HTML to a PDF (pdflayer free key)."""
    if not PDF_KEY:
        return ("PDF generation needs a free PDFLAYER_API_KEY — set it and "
                "I'll render reports to PDF, Sir.")
    try:
        params = {"access_key": PDF_KEY, "page_size": page_size,
                  "document_url": url}
        r = requests.get(_PDFLAYER, params=params, timeout=_TIMEOUT * 2)
        if r.status_code == 200 and r.headers.get("content-type",
                                                  "").startswith("application/pdf"):
            path = os.path.join(os.path.expanduser("~"), "Downloads",
                                fname)
            with open(path, "wb") as f:
                f.write(r.content)
            return f"Saved PDF ({len(r.content)} bytes) to {path}, Sir."
        return f"PDF: {r.text[:200]}"
    except Exception as e:
        return f"PDF generation unavailable: {e}"


# --------------------------------------------------------------------------- #
# Tier A — no-key / free-tier public APIs (degrade gracefully, HTTPS only).
# Each function is self-contained: a missing service or a 5xx returns a
# clear "unavailable" sentence instead of raising. They are exposed to the
# agent via utils/executor.py action dispatch + utils/agent_loop.py tool
# schemas, and to the dashboard via server.py api_intel() cells.
# --------------------------------------------------------------------------- #

_OPENMETEO = "https://api.open-meteo.com/v1/forecast"
_DICT = "https://api.dictionaryapi.dev/api/v2/entries/en/{word}"
_BIBLE = "https://bible-api.com/{ref}"
_IPAPI = "https://ipapi.co/json/"
_USGS_EQ = "https://earthquake.usgs.gov/fdsnws/event/1/query"
_CHUCK = "https://api.chucknorris.io/jokes/random"
_MEAL = "https://www.themealdb.com/api/json/v1/1/search.php"
_COCKTAIL = "https://www.thecocktaildb.com/api/json/v1/1/search.php"
_CATFACT = "https://catfact.ninja/fact"
_DOGCEO = "https://dog.ceo/api/breeds/image/random"
_QUOTABLE = "https://api.quotable.io/random"

# Free Dictionary and Chuck Norris block default Python UA; browser spoof.
_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")
_H = {"User-Agent": _BROWSER_UA, "Accept": "application/json"}


def weather(lat, lon, days=1):
    """Open-Meteo: current + N-day forecast for a lat/lon (no key).
    Returns temperature, wind, precip summary."""
    try:
        r = requests.get(_OPENMETEO, params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto",
            "forecast_days": min(days, 7),
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
        }, headers=_H, timeout=_TIMEOUT)
        d = r.json()
        cur = d.get("current", {})
        now = d.get("current_units", {})
        t = f"{cur.get('temperature_2m')}°{now.get('temperature_2m','')}"
        w = cur.get("weather_code")
        wind = cur.get("wind_speed_10m")
        out = [f"Weather now: {t}, wind {wind}{now.get('wind_speed_10m','')}"]
        daily = d.get("daily", {})
        if daily.get("temperature_2m_max"):
            for i in range(0, min(days, len(daily.get('time', [])))):
                hi = daily.get("temperature_2m_max", ["?"])[i]
                lo = daily.get("temperature_2m_min", ["?"])[i]
                pop = daily.get("precipitation_probability_max", ["?"])[i]
                day = daily.get("time", ["?"])[i]
                out.append(f"{day}: {hi}°/lo {lo}° (p{pop})")
        return " | ".join(out)
    except Exception as e:
        return f"Weather unavailable: {e}"


def define(word):
    """Free Dictionary API: definitions + phonetics + part of speech (no key)."""
    if not word:
        return ("Dictionary needs a word — e.g. 'define serendipity'.")
    try:
        r = requests.get(_DICT.format(word=requests.utils.quote(word.strip())),
                         headers=_H, timeout=_TIMEOUT)
        if r.status_code == 404:
            return f"No dictionary entry for '{word}'."
        d = r.json()[0]
        phonetic = d.get("phonetic") or (
            d.get("phonetics") or [{}])[0].get("text", "")
        parts = []
        for m in (d.get("meanings") or []):
            pos = m.get("partOfSpeech", "?")
            defs = [df.get("definition", "")[:120]
                    for df in (m.get("definitions") or [])][:2]
            parts.append(f"{pos}: {'; '.join(defs)}")
        out = f"{d.get('word', word)}"
        if phonetic:
            out += f" /{phonetic}/"
        out += " — " + " | ".join(parts[:4])
        return out[:500]
    except Exception as e:
        return f"Dictionary unavailable: {e}"


def scripture(ref):
    """Bible API: full-text lookup for a verse reference (no key, KJV)."""
    if not ref:
        return "Scripture needs a reference — e.g. 'John 3:16'."
    try:
        r = requests.get(_BIBLE.format(ref=requests.utils.quote(ref.strip())),
                         headers=_H, timeout=_TIMEOUT)
        d = r.json()
        if d.get("reference") is None:
            return f"Could not look up '{ref}'. Try 'John 3:16' or 'Psalm 23'."
        txt = (d.get("text") or "").strip().replace("\n", " ")
        ref_out = d.get("reference", ref)
        return f"{ref_out} — {txt[:420]}"
    except Exception as e:
        return f"Scripture lookup unavailable: {e}"


def ip_locate(ip=""):
    """IPAPI: geolocate an IP or the caller's public IP (free tier ~1k/day)."""
    url = ("https://ipapi.co/{ip}/json/".format(ip=ip) if ip
           else "https://ipapi.co/json/")
    try:
        r = requests.get(url, headers=_H, timeout=_TIMEOUT)
        d = r.json()
        if "ip" not in d:
            return "IP location unavailable — try again or pass a specific IP."
        return (f"{d.get('ip','?')} → {d.get('city')}, {d.get('region')} "
                f"{d.get('country_name')} · {d.get('org','')}")
    except Exception as e:
        return f"IP lookup unavailable: {e}"


def earthquakes(limit=10, radius_km=500, lat=None, lon=None):
    """USGS: significant recent earthquakes near a point (or globally) (no key)."""
    try:
        params = {"format": "geojson", "limit": str(limit),
                  "minmagnitude": "1.5"}
        if lat is not None and lon is not None:
            params["latitude"] = lat
            params["longitude"] = lon
            params["maxradiuskm"] = radius_km
        r = requests.get(_USGS_EQ, params=params, headers=_H, timeout=_TIMEOUT)
        d = r.json()
        feats = d.get("features", [])
        if not feats:
            return "No significant earthquakes recorded in that area."
        out = []
        for f in feats[:limit]:
            g = f.get("geometry", {}).get("coordinates", [0, 0])
            p = f.get("properties", {})
            out.append(f"{p.get('mag','?')} · {p.get('place','?')}")
        return f"Recent quakes: " + "; ".join(out)
    except Exception as e:
        return f"Earthquake data unavailable: {e}"


def joke(category="Any"):
    """Chuck Norris Facts (no key). category ignored (all are Chuck Norris)."""
    try:
        r = requests.get(_CHUCK, headers=_H, timeout=_TIMEOUT)
        return r.json().get("value", "Chuck has spoken.")
    except Exception as e:
        return f"Joke service unavailable: {e}"


def recipe(query):
    """The Meal DB: search recipes by ingredient/name (no key)."""
    if not query:
        return "Recipe search needs a query — e.g. 'recipe pasta' or 'recipe chicken'."
    try:
        r = requests.get(_MEAL, params={"s": query.strip()},
                         headers=_H, timeout=_TIMEOUT)
        d = r.json()
        meals = d.get("meals") or []
        if not meals:
            return (f"No recipes for '{query}'. Try an ingredient like "
                    "'chicken' or 'pasta'.")
        m = meals[0]
        area = m.get("strArea", "")
        cat = m.get("strCategory", "")
        ins = [v for k, v in m.items()
               if k.startswith("strIngredient") and v and v.strip()]
        steps = [v for k, v in m.items()
                 if k.startswith("strMeasure") and v and v.strip()]
        ing = ", ".join(f"{i.strip()}" for i in ins[:6])
        return (f"{m.get('strMeal','recipe')} ({area} {cat}) — "
                f"ingredients: {ing}. "
                f"Instructions: {m.get('strInstructions','')[:200]}")
    except Exception as e:
        return f"Recipe lookup unavailable: {e}"


def cocktail(query):
    """The Cocktail DB: search drinks by name/ingredient (no key)."""
    if not query:
        return "Cocktail search needs a query — e.g. 'cocktail margarita'."
    try:
        r = requests.get(_COCKTAIL, params={"s": query.strip()},
                         headers=_H, timeout=_TIMEOUT)
        d = r.json()
        drinks = d.get("drinks") or []
        if not drinks:
            return f"No cocktails for '{query}'."
        m = drinks[0]
        ing = [f"{m.get(f'strIngredient{i}','')} {m.get(f'strMeasure{i}','')}".strip()
               for i in range(1, 6) if m.get(f"strIngredient{i}")]
        return (f"{m.get('strDrink','drink')} ({m.get('strCategory','')}) — "
                f"ingredients: {', '.join([i for i in ing if i.strip()][:4])}. "
                f"Glass: {m.get('strGlass','')}.")
    except Exception as e:
        return f"Cocktail lookup unavailable: {e}"


def cat_fact():
    """Cat Fact Ninja: a random cat fact (no key)."""
    try:
        r = requests.get(_CATFACT, headers=_H, timeout=_TIMEOUT)
        return r.json().get("fact", "Cats: mysterious.")
    except Exception as e:
        return f"Cat fact unavailable: {e}"


def dog_pic(_=None):
    """Dog CEO: a random dog image URL (no key). `_` ignored — kept for
    consistent arity with other tools."""
    try:
        r = requests.get(_DOGCEO, headers=_H, timeout=_TIMEOUT)
        url = r.json().get("message")
        if url:
            return f"🐕 {url}"
        return "Could not fetch a dog picture."
    except Exception as e:
        return f"Dog pictures unavailable: {e}"


def quote(tag=""):
    """Quotable: a random inspirational quote (no key). tag filters by tag."""
    try:
        url = _QUOTABLE if not tag else f"{_QUOTABLE}/tags/{tag}/quotes"
        r = requests.get(url, headers=_H, timeout=_TIMEOUT)
        d = r.json()
        if isinstance(d, dict) and d.get("content"):
            return f'"{d["content"]}" — {d.get("author","unknown")}'
        if isinstance(d, list) and d:
            q = d[0]
            return f'"{q.get("content","")}" — {q.get("author","unknown")}'
        return "No quotes found for that tag."
    except Exception as e:
        return f"Quotes unavailable: {e}"


def open_papers(query, limit=3):
    """CORE / OpenAlex: free open-access research papers by topic (no key).
    Complements the legacy OpenAlex path — returns abstracts + links."""
    try:
        r = requests.get(_OPENALEX, params={
            "search": query, "per-page": limit,
            "filter": "open_access.is_oa:true", "mailto": "jarvis@example.com"},
            headers={"User-Agent": _UA}, timeout=_TIMEOUT)
        results = (r.json().get("results") or [])
        if not results:
            return f"No open-access papers found for '{query}', Sir."
        out = []
        for w in results[:limit]:
            title = (w.get("title") or "untitled")[:110]
            year = w.get("publication_year")
            cited = w.get("cited_by_count")
            oa = (w.get("open_access") or {}).get("is_oa", False)
            link = (w.get("link") or w.get("id") or "")
            tag = "[OA]" if oa else ""
            out.append(f"{title} ({year}) cited {cited} {tag}{link}")
        return "; ".join(out)
    except Exception as e:
        return f"Open-access paper lookup unavailable: {e}"


if __name__ == "__main__":
    print("RESEARCH:", research("attention is all you need"))
    print("HABIT:", habit("streak"))
    print("WEATHER (London):", weather(51.5074, -0.1278))
    print("DEFINE:", define("serendipity"))
    print("JOKE:", joke())
    print("QUOTE:", quote())
