"""Package tracking via WhereParcel (free apiKey; configured via
WHEREPARCEL_API_KEY). Also does lightweight carrier inference from the
tracking number when no key is present, so the tool degrades gracefully.

WhereParcel (whereparcel.com) aggregates USPS / FedEx / UPS / DHL /
Canada Post / AusPost etc. in one request. Requires a free API key.
"""
import os
import requests

_TIMEOUT = 15
_BASE = "https://api.whereparcel.com"
_KEY = (os.getenv("WHEREPARCEL_API_KEY") or "").strip()

# Cheap carrier inference from common tracking-number prefixes.
_CARRIERS = [
    ("UPS", ("1Z",)),
    ("FedEx", ("2313", "2314", "40", "43", "61", "62", "63", "66", "67")),
    ("USPS", ("9400", "9401", "9303", "9205", "9300", "9261")),
    ("DHL", ("JD0", "JT0")),
    ("CanadaPost", ("CR", "FV", "GP", "RY", "CX")),
    ("AusPost", ("LB", "LZ", "SA", "SV")),
]


def infer_carrier(number):
    n = (number or "").strip().upper().replace(" ", "")
    for carrier, prefixes in _CARRIERS:
        if any(n.startswith(p) for p in prefixes):
            return carrier
    return None


def track_package(number):
    """Track a package by number → human string. Degrades if unkeyed."""
    if not _KEY:
        carrier = infer_carrier(number) or "unknown carrier"
        return (f"Package tracking requires a free WhereParcel API key "
                f"(set WHEREPARCEL_API_KEY). It looks like a "
                f"{carrier} number — configure the key and I'll track it, Sir.")
    try:
        r = requests.get(f"{_BASE}/v1/lookup",
                         params={"tracking_number": (number or "").strip()},
                         headers={"apikey": _KEY, "Content-Type": "application/json"},
                         timeout=_TIMEOUT)
        if r.status_code != 200:
            return f"Package tracking unavailable (HTTP {r.status_code})."
        data = r.json()
        events = data.get("events") or data.get("tracking") or []
        if not events:
            return (f"No tracking events found for '{number}' "
                    f"({infer_carrier(number) or 'carrier unknown'}), Sir.")
        latest = events[0]
        status = latest.get("status_text") or latest.get("description") or \
            latest.get("status") or "in transit"
        loc = latest.get("location") or ""
        ts = latest.get("timestamp") or ""
        return (f"{infer_carrier(number) or 'Package'} '{number}': "
                f"{status}"
                + (f" at {loc}" if loc else "")
                + (f" ({ts})" if ts else "") + ".")
    except Exception as e:
        return f"Package tracking unavailable: {e}"


if __name__ == "__main__":
    import sys
    print(track_package(sys.argv[1] if len(sys.argv) > 1 else "9400111899223197222334"))
