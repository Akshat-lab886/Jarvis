"""General purpose / reference lookups for JARVIS — a second batch of
public-API integrations (survey Tier-2 + overlooked gems), all with
no-auth or free-key paths.

  define(word)         Free Dictionary (no auth) — defs, IPA, synonyms
  book_lookup(query)   Open Library (no auth) — book metadata
  movie_lookup(title)  TMDb (THEMOVIEDB_API_KEY, free) — movies/TV
  space() / space_apod()  NASA APOD + Open Notify ISS (no auth)
  crypto_price(symbol) CoinGecko (no auth) — live crypto prices
  holidays(country)    Nager.Date (no auth) — public holidays
"""
import os
import requests

_TIMEOUT = 12
_UA = "JARVIS-assistant/1.0 (personal assistant)"

_DICT = "https://api.dictionaryapi.dev/api/v2/entries/en"
_OL = "https://openlibrary.org"
_TMDB = "https://api.themoviedb.org/3"
_TMDB_KEY = (os.getenv("THEMOVIEDB_API_KEY") or "").strip()
_APOD = "https://api.nasa.gov/planetary/apod"
_NASA_KEY = (os.getenv("NASA_API_KEY") or "DEMO_KEY").strip()
_NOTIFY = "https://api.open-notify.org/iss-now.json"
_COIN = "https://api.coingecko.com/api/v3"
_NAGER = "https://date.nager.at/api/v3"


# ---- Free Dictionary (no auth) ---- #

def define(word):
    """Word definitions, IPA pronunciation, synonyms, part of speech."""
    try:
        r = requests.get(f"{_DICT}/{word.strip().lower()}", timeout=25)
        if r.status_code != 200:
            return f"No dictionary entry found for '{word}', Sir."
        data = r.json()
        entry = data[0]
        phon = (entry.get("phonetic")) or (
            entry.get("phonetics") or [{}])[0].get("text") or ""
        meanings = entry.get("meanings") or []
        pos = meanings[0].get("partOfSpeech") if meanings else ""
        defn = (meanings[0].get("definitions") or [{}])[0].get(
            "definition") if meanings else ""
        syns = meanings[0].get("synonyms") or [] if meanings else []
        out = f"{word.capitalize()}"
        if phon:
            out += f" ({phon})"
        if pos:
            out += f" [{pos}]"
        if defn:
            out += f": {defn}"
        if syns:
            out += f" — Syn: {', '.join(syns[:3])}"
        return out
    except Exception as e:
        return f"Dictionary unavailable: {e}"


# ---- Open Library (no auth) ---- #

def book_lookup(query, limit=3):
    """Search books by title/author → title + author + year."""
    try:
        r = requests.get(f"{_OL}/search.json",
                         params={"q": query, "limit": limit},
                         headers={"User-Agent": _UA}, timeout=25)
        docs = (r.json().get("docs") or [])
    except Exception as e:
        return f"Book lookup unavailable: {e}"
    if not docs:
        return f"No books found for '{query}', Sir."
    lines = []
    for d in docs:
        title = (d.get("title") or "").strip() or "untitled"
        authors = (d.get("author_name") or [])[:2]
        year = d.get("first_publish_year")
        who = ", ".join(authors) if authors else "unknown author"
        lines.append(f"{title} by {who}" + (f" ({year})" if year else ""))
    return "; ".join(lines[:limit])


# ---- TMDb (free key) ---- #

def movie_lookup(title, limit=3):
    """Search movies/TV by title → name/director/year/rating."""
    if not _TMDB_KEY:
        return ("Movie lookup needs a free THEMOVIEDB_API_KEY — set it "
                "and I'll look up films, Sir.")
    try:
        r = requests.get(f"{_TMDB}/search/movie",
                         params={"api_key": _TMDB_KEY, "query": title},
                         timeout=_TIMEOUT)
        results = (r.json().get("results") or [])
    except Exception as e:
        return f"Movie lookup unavailable: {e}"
    if not results:
        return f"No films found for '{title}', Sir."
    out = []
    for m in results[:limit]:
        name = (m.get("title") or "untitled")
        year = (m.get("release_date") or "")[:4]
        rating = m.get("vote_average")
        out.append(f"{name}" + (f" ({year})" if year else "") +
                   (f" ★{rating:.1f}" if rating else ""))
    return "; ".join(out)


# ---- NASA / ISS (no auth) ---- #

def space_apod():
    """NASA Astronomy Picture of the Day: title + explanation."""
    try:
        r = requests.get(_APOD, params={"api_key": _NASA_KEY},
                         timeout=_TIMEOUT)
        d = r.json()
        return f"NASA APOD: {d.get('title')} — {d.get('explanation', '')[:220]}"
    except Exception as e:
        return f"NASA APOD unavailable: {e}"


def iss_location():
    """Current ISS position (Open Notify, no auth)."""
    try:
        r = requests.get(_NOTIFY, timeout=_TIMEOUT)
        pos = r.json().get("iss_position") or {}
        lat, lon = pos.get("latitude"), pos.get("longitude")
        if lat is None:
            return "ISS position unavailable right now, Sir."
        return (f"The ISS is currently over {lat}, {lon}. "
                f"(Realize: it orbits ~7.6 km/s.)")
    except Exception as e:
        return f"ISS tracking unavailable: {e}"


# ---- CoinGecko (no auth) ---- #

def crypto_price(symbol="bitcoin"):
    """Live price for a crypto by common name/symbol."""
    sym = (symbol or "").strip().lower()
    coin_map = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana",
                "bnb": "binancecoin", "xrp": "ripple", "ada": "cardano",
                "doge": "dogecoin", "dot": "polkadot", "avax": "avalanche-2",
                "matic": "matic-network", "link": "chainlink", "ltc": "litecoin"}
    coin = coin_map.get(sym, sym)
    try:
        r = requests.get(f"{_COIN}/simple/price",
                         params={"ids": coin,
                                 "vs_currencies": "usd",
                                 "include_24hr_change": "true"},
                         headers={"User-Agent": _UA}, timeout=_TIMEOUT)
        d = r.json()
        if coin not in d:
            return f"I couldn't find a price for '{symbol}' on CoinGecko, Sir."
        price = d[coin].get("usd")
        chg = d[coin].get("usd_24h_change")
        change = f" ({chg:+.2f}% 24h)" if chg is not None else ""
        return f"{coin.capitalize()} (BTC-style): ${price:,.2f}{change}"
    except Exception as e:
        return f"Crypto price unavailable: {e}"


# ---- Nager.Date (no auth) ---- #

def holidays(country="US", year=None):
    """Public holidays for a country (ISO-2, e.g. US/GB/IN)."""
    from datetime import datetime
    year = year or datetime.now().year
    try:
        r = requests.get(f"{_NAGER}/PublicHolidays/{year}/{country.upper()}",
                         timeout=_TIMEOUT)
        data = r.json()
    except Exception as e:
        return f"Public holidays unavailable: {e}"
    if isinstance(data, dict) and data.get("statusCode") == 404:
        return f"No holiday data for country '{country}', Sir."
    if not data:
        return f"No public holidays for {country}/{year}."
    names = [f"{d.get('date', '')}: {d.get('localName', '')}"
             for d in data[:5]]
    return (f"{country.upper()} {year} public holidays: " + "; ".join(names)
            + ("…" if len(data) > 5 else ""))


if __name__ == "__main__":
    import sys
    arg = sys.argv[1] if len(sys.argv) > 1 else "bitcoin"
    print("define:", define("serendipity"))
    print("book:", book_lookup("The Pragmatic Programmer"))
    print("crypto:", crypto_price(arg))
    print("holidays:", holidays("US"))
