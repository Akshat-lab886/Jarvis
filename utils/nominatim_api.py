"""Geocoding via Nominatim (OpenStreetMap) — no API key.

Nominatim powering the survey's Tier-1 geocoding pick: forward (place →
lat/lon) and reverse (lat/lon → address) geocoding with no key. Per
Nominatim's usage policy for light personal use, we set a real
User-Agent and keep queries to a single one (no bulk scraping).
"""
import requests

_TIMEOUT = 8
_BASE = "https://nominatim.openstreetmap.org"
_UA = "JARVIS-assistant/1.0 (personal assistant)"


def _get(path, params):
    resp = requests.get(_BASE + path, params=params,
                        headers={"User-Agent": _UA}, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def geocode(query, limit=1):
    """Forward geocode a place → list of {lat, lon, display_name}."""
    try:
        data = _get("/search", {"q": query, "format": "json", "limit": limit,
                                "addressdetails": 1})
    except Exception as e:
        return {"error": f"geocoding unavailable: {e}"}
    return [{"lat": float(d.get("lat")), "lon": float(d.get("lon")),
             "display_name": d.get("display_name", "")} for d in data]


def geocode_best(query):
    """Get the single best {lat, lon, name} or an error dict."""
    res = geocode(query, limit=1)
    if isinstance(res, dict):
        return res
    if not res:
        return {"error": f"Couldn't find '{query}'."}
    r = res[0]
    return {"lat": r["lat"], "lon": r["lon"], "name": r["display_name"]}


def reverse(lat, lon):
    """Reverse geocode coordinates → human place name."""
    try:
        item = _get("/reverse", {"lat": lat, "lon": lon,
                                 "format": "json", "zoom": 12})
        return item.get("display_name", f"{lat},{lon}")
    except Exception as e:
        return f"reverse geocoding unavailable: {e}"


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "rev":
        print(reverse(sys.argv[2], sys.argv[3]))
    else:
        print(geocode_best(sys.argv[1] if len(sys.argv) > 1 else "Paris"))
