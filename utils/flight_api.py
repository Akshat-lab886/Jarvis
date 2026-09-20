"""Flight tracking via OpenSky Network (no auth, public ADS-B data).

OpenSky's free API returns live aircraft positions/flights (ICAO24
transponder id, callsign, origin, position, altitude, velocity).
No key for the limited anonymous tier — good for "track my flight".

Usage:
  track_flight(callsign)      e.g. "UAL 123" / "UAL123" / "BAW123"
  flights_near(lat, lon, rad)  aircraft within `rad` km of a point
  flight_status(icao24)        status by unique transponder id
"""
import requests

_TIMEOUT = 15
OPEN_SKY = "https://opensky-network.org/api/states/all"
_OWN_STATES = "https://opensky-network.org/api/states/own"
_UA = "JARVIS-assistant/1.0 (personal assistant)"


def _states(time_secs=0):
    """Fetch the full public state vector (anonymous tier)."""
    r = requests.get(OPEN_SKY, params={"time": time_secs},
                     headers={"User-Agent": _UA}, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _fmt_state(s):
    """Map an OpenSky state vector (list) to a dict."""
    return {
        "icao24": s[0],
        "callsign": (s[1] or "").strip() or None,
        "origin": s[2],
        "lat": s[6], "lon": s[5],
        "alt_m": s[7], "velocity_mps": s[9],
        "on_ground": s[8], "squawk": s[14],
    }


def _normalize_callsign(cs):
    cs = (cs or "").strip().upper().replace(" ", "")
    return cs


def track_flight(callsign):
    """Find live state for a callsign (e.g. 'UAL123', 'BAW 456')."""
    target = _normalize_callsign(callsign)
    try:
        data = _states()
    except Exception as e:
        return f"Flight tracking unavailable: {e}"
    states = data.get("states") or []
    for raw in states:
        tgt = _normalize_callsign(raw[1])
        if tgt == target or (target and tgt == target[:6]):
            s = _fmt_state(raw)
            if s["on_ground"]:
                return (f"{target} is on the ground (last seen "
                        f"near {s['lat']:.2f}, {s['lon']:.2f}).")
            alt_km = (s['alt_m'] or 0) / 1000
            spd = (s['velocity_mps'] or 0) * 3.6
            return (f"{target} is airborne at {alt_km:.1f} km, "
                    f"{spd:.0f} km/h, near {s['lat']:.2f}, {s['lon']:.2f}.")
    return (f"I couldn't see an aircraft matching '{callsign}' in the "
            f"live OpenSky feed right now, Sir. (Flight may be outside "
            f"coverage or on the ground unpowered.)")


def flights_near(lat, lon, radius_km=50):
    """List aircraft within `radius_km` of a point."""
    try:
        data = _states()
    except Exception as e:
        return f"Flight tracking unavailable: {e}"
    from math import radians, sin, cos, asin, sqrt
    def havers(a, b):
        R = 6371
        dlat = radians(b[0] - a[0]); dlon = radians(b[1] - a[1])
        x = sin(dlat/2)**2 + cos(radians(a[0]))*cos(radians(b[0]))*sin(dlon/2)**2
        return 2*R*asin(sqrt(x))
    out = []
    for raw in (data.get("states") or []):
        s = _fmt_state(raw)
        if s["on_ground"] or s["lat"] is None or s["lon"] is None:
            continue
        if havers((lat, lon), (s["lat"], s["lon"])) <= radius_km:
            out.append(f"{s['callsign'] or s['icao24']} "
                       f"{(s['alt_m'] or 0)/1000:.1f}km alt")
    return (f"{len(out)} aircraft within {radius_km} km: "
            + (", ".join(out[:8]) if out else "none."))


def flight_status(icao24):
    """Status by unique transponder id: airborne vs ground + position."""
    try:
        data = _states()
    except Exception as e:
        return f"Flight tracking unavailable: {e}"
    for raw in (data.get("states") or []):
        if raw[0].lower() == str(icao24).lower():
            s = _fmt_state(raw)
            alt = 'no altitude'
            if s["alt_m"] is not None:
                alt = f"{s['alt_m']/1000:.1f} km"
            return (f"ICAO24 {icao24}: "
                    f"{'on the ground' if s['on_ground'] else 'airborne'} "
                    f"at {s['lat']:.2f}, {s['lon']:.2f}, {alt}.")
    return f"No live state for transponder {icao24}."


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 2 and sys.argv[1] == "near":
        print(flights_near(float(sys.argv[2]), float(sys.argv[3])))
    elif len(sys.argv) > 1:
        print(track_flight(sys.argv[1]))
    else:
        # example: list something near a major airport (Heathrow)
        print(flights_near(51.4700, -0.4543, 80))
