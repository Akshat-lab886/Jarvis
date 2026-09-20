"""Security URL / file guard for JARVIS downloads.

Layered, degrade-gracefully threat checks layered onto the downloader:

 1. URLhaus (no key)   — malicious-URL blocklist, always available.
 2. Google Safe Browsing (key) — fast phishing/malware URL verdict.
 3. VirusTotal (key)  — scan a downloaded file hash or a URL.

Every check is optional. When a layer has no key it is skipped, and the
guard returns a verdict with ``enabled`` counts so the caller can log
how thorough the scan was without ever hard-failing the download.

Keys (read from .env / env):
    SAFE_BROWSING_API_KEY, VIRUSTOTAL_API_KEY
"""
import os
import re
import hashlib
import requests

_TIMEOUT = 6
_UA = "JARVIS-assistant/1.0 (personal assistant)"

URLHAUS_LOOKUP = "https://urlhaus-api.abuse.ch/v1/url/"
SAFE_BROWSING = ("https://safebrowsing.googleapis.com/v4/"
                 "threatMatches:find")
VT_URL = "https://www.virustotal.com/api/v3/urls"
VT_FILE = "https://www.virustotal.com/api/v3/files"


def _key(name):
    return (os.getenv(name) or "").strip()


def urlhaus_lookup(url):
    """Check a URL against the no-key URLhaus blocklist. Returns verdict."""
    try:
        r = requests.post(URLHAUS_LOOKUP, data={"url": url}, timeout=_TIMEOUT,
                          headers={"User-Agent": _UA})
        data = r.json()
        if data.get("query_status") == "ok":
            return {"flag": True, "source": "URLhaus",
                    "detail": data.get("url_info", {}).get("blacklist", "")}
        if data.get("query_status") == "no_results":
            return {"flag": False, "source": "URLhaus", "detail": "clean"}
        return {"flag": False, "source": "URLhaus", "detail": "unlisted"}
    except Exception:
        return {"flag": False, "source": "URLhaus", "detail": "unreachable"}


def safe_browsing_check(url):
    """Google Safe Browsing verdict. Requires SAFE_BROWSING_API_KEY."""
    key = _key("SAFE_BROWSING_API_KEY")
    if not key:
        return {"enabled": False, "source": "google-safebrowsing",
                "flag": False, "detail": "no key"}
    try:
        body = {"client": {"clientId": "jarvis", "clientVersion": "1.0"},
                "threatInfo": {
                    "threatTypes": ["MALWARE", "SOCIAL_ENGINEERING",
                                    "UNWANTED_SOFTWARE",
                                    "POTENTIALLY_HARMFUL_APPLICATION"],
                    "platformTypes": ["ANY_PLATFORM"],
                    "threatEntryTypes": ["URL"],
                    "threatEntries": [{"url": url}]}}
        r = requests.post(SAFE_BROWSING + "?key=" + key, json=body,
                          timeout=_TIMEOUT)
        if r.status_code == 200 and r.json().get("matches"):
            return {"enabled": True, "source": "google-safebrowsing",
                    "flag": True, "detail": "blocklisted"}
        return {"enabled": True, "source": "google-safebrowsing",
                "flag": False, "detail": "clean"}
    except Exception:
        return {"enabled": True, "source": "google-safebrowsing",
                "flag": False, "detail": "unreachable"}


def virustotal_hash(hash_value):
    """VirusTotal file-hash scan. Requires VIRUSTOTAL_API_KEY."""
    key = _key("VIRUSTOTAL_API_KEY")
    if not key:
        return {"enabled": False, "flag": False, "detail": "no key"}
    try:
        r = requests.get(f"{VT_FILE}/{hash_value}",
                         headers={"x-apikey": key}, timeout=_TIMEOUT)
        if r.status_code == 200:
            attr = r.json().get("data", {}).get("attributes", {})
            stats = attr.get("last_analysis_stats", {})
            det = int(stats.get("malicious", 0)) + int(
                stats.get("suspicious", 0))
            return {"enabled": True, "flag": det > 0, "detail": f"{det} detections"}
        return {"enabled": True, "flag": False, "detail": "not found"}
    except Exception:
        return {"enabled": True, "flag": False, "detail": "unreachable"}


def check_url(url):
    """Run every active URL layer and summarize. Never throws."""
    verdicts = [urlhaus_lookup(url), safe_browsing_check(url)]
    verdicts = [v for v in verdicts if v.get("enabled", True)]
    flagged = [v for v in verdicts if v.get("flag")]
    summary = {
        "url": url,
        "flagged": bool(flagged),
        "layers": len(verdicts),
        "sources": [v["source"] for v in verdicts],
        "detail": ("; ".join(f"{v['source']}: {v['detail']}"
                             for v in flagged) if flagged
                   else "no threats found"),
    }
    return summary


def check_file(path):
    """Scan a local file by SHA-256 via VirusTotal (if keyed)."""
    hash_value = "sha256:" + _sha256(path)
    return virustotal_hash(hash_value)


def _sha256(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except Exception:
        return ""
    return h.hexdigest()


def posture():
    """Compact one-line security posture for diagnostics/logging."""
    keys = [n for n in ("SAFE_BROWSING_API_KEY", "VIRUSTOTAL_API_KEY")
            if _key(n)]
    return (f"urlguard: URLhaus always-on; "
            f"safe-browsing={'on' if _key('SAFE_BROWSING_API_KEY') else 'off'}; "
            f"virustotal={'on' if _key('VIRUSTOTAL_API_KEY') else 'off'}; "
            f"configured keys: {', '.join(keys) or 'none (Safe Browsing/VT off)'}")


if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    print(check_url(url))
    print(posture())
