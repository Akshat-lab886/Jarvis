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


if __name__ == "__main__":
    import sys
    arg = sys.argv[1] if len(sys.argv) > 1 else "attention is all you need"
    print("RESEARCH:", research(arg))
    print("HABIT:", habit("streak"))
