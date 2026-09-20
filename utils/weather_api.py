"""Weather via Open-Meteo (no key) + US NWS severe-weather alerts (no key).

Weather module for JARVIS following the survey's Tier-1 live/real-time
picks. Open-Meteo needs no API key; NWS adds official severe-weather
alerts for US locations.

Key resolution order for coordinates:
  1. explicit ``lat``/``lon`` args
  2. ``city`` → geocode (Nominatim) if provided
  3. IP-to-latlon via ip-api (free, server-side fine for weather)
"""
import requests

_TIMEOUT = 8
_UA = "JARVIS-assistant/1.0 (personal assistant)"

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
NWS_API = "https://api.weather.gov"


def _get(url, params=None, headers=None):
    resp = requests.get(url, params=params,
                        headers=headers or {"User-Agent": _UA},
                        timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _ip_coords():
    """Best-effort IP → (lat, lon). Returns (None, None) on any failure."""
    try:
        data = _get("https://ip-api.com/json/",
                    params={"fields": "lat,lon,status"})
        if data.get("status") == "success":
            return data["lat"], data["lon"]
    except Exception:
        pass
    return None, None


def _geocode(city):
    try:
        data = _get("https://nominatim.openstreetmap.org/search",
                    params={"q": city, "format": "json", "limit": 1},
                    headers={"User-Agent": _UA})
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None, None


def _resolve(lat=None, lon=None, city=None):
    if lat is None or lon is None:
        lat, lon = _geocode(city) if city else (None, None)
    if lat is None or lon is None:
        lat, lon = _ip_coords()
    return lat, lon


def current_weather(city=None, lat=None, lon=None):
    """Return a concise human-readable weather report (str)."""
    lat, lon = _resolve(lat, lon, city)
    if lat is None or lon is None:
        return "I couldn't determine your location, Sir."
    try:
        data = _get(OPEN_METEO, params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                       "precipitation,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
            "forecast_days": 3, "timezone": "auto",
        })
    except Exception as e:
        return f"Weather unavailable: {e}"
    cur = data.get("current", {})
    daily = data.get("daily") or {}
    codes = {
        0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
        45: "foggy", 48: "foggy", 51: "light drizzle", 53: "drizzle",
        61: "light rain", 63: "rain", 65: "heavy rain", 71: "snow",
        80: "showers", 95: "thunderstorm",
    }
    wc = int(cur.get("weather_code") or 0)
    desc = codes.get(wc, "variable conditions")
    parts = [f"Currently {desc}."
             f" Temp {cur.get('temperature_2m')}°C"
             f" (feels like {cur.get('apparent_temperature')}°C).",
             f"Humidity {cur.get('relative_humidity_2m')}%. "
             f"Wind {cur.get('wind_speed_10m')} km/h."]
    hi = (daily.get("temperature_2m_max") or [None])[0]
    lo = (daily.get("temperature_2m_min") or [None])[0]
    if hi is not None and lo is not None:
        parts.append(f"Today {lo}–{hi}°C.")
    return " ".join(parts)


def us_alerts(lat=None, lon=None, state=None):
    """Fetch US NWS severe-weather alerts. Returns str ('' if none)."""
    try:
        if lat is not None and lon is not None:
            pts = _get(f"{NWS_API}/points/{lat:.4f},{lon:.4f}",
                       headers={"User-Agent": _UA})
            zone = pts.get("properties", {}).get("forecastZone", "")
            if not zone:
                return ""
            data = _get(f"{NWS_API}/alerts/active/zone/{zone}",
                        headers={"User-Agent": _UA})
        else:
            area = f"/{state}" if state else ""
            data = _get(f"{NWS_API}/alerts/active{area}",
                        headers={"User-Agent": _UA})
        feats = data.get("features", [])
        if not feats:
            return ""
        lines = []
        for f in feats[:5]:
            p = f.get("properties", {})
            lines.append(f"{p.get('event')} ({p.get('severity')}): "
                         f"{p.get('headline')}")
        return " | ".join(lines)
    except Exception:
        return ""


def weather_report(city=None, lat=None, lon=None, include_alerts=True):
    """Combined weather + alert report for the agent/UI."""
    rep = current_weather(city=city, lat=lat, lon=lon)
    if include_alerts:
        a = us_alerts(lat=lat, lon=lon)
        if a:
            rep += " ALERTS: " + a
    return rep


if __name__ == "__main__":
    print(weather_report())
