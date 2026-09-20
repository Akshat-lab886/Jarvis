"""Currency conversion via Frankfurter — ECB reference rates, no API key.

Frankfurter (frankfurter.app) mirrors the ECB reference rates: reliable,
no key, daily updates. Supports ``amount`` between ISO codes and history.
"""
import requests

_TIMEOUT = 8
_BASE = "https://api.frankfurter.app"

CODES = {"usd", "eur", "gbp", "jpy", "chf", "cad", "aud", "nzd", "inr",
         "cny", "krw", "brl", "try", "sek", "nok", "dkk", "zar", "sgd",
         "hkd", "mxn"}


def _get(path, params=None):
    resp = requests.get(_BASE + path, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def convert(amount, frm="USD", to="EUR"):
    """Convert amount between currencies → human string."""
    frm, to = frm.upper(), to.upper()
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return f"Invalid amount: {amount!r}"
    try:
        data = _get("/latest", params={"from": frm, "to": to,
                                       "amount": amount})
        rate = (data.get("rates") or {}).get(to)
        if rate is None:
            return f"I don't have a rate for {frm} → {to}, Sir."
        date = data.get("date", "")
        return (f"{amount:,.2f} {frm} = {float(rate):,.2f} {to} "
                f"(ECB rate, {date}).")
    except Exception as e:
        return f"Currency conversion unavailable: {e}"


def list_rates(base="EUR"):
    """Return a compact list of major-currency rates vs base."""
    try:
        data = _get("/latest", params={"from": base.upper()})
        rates = data.get("rates", {})
        keep = {c: v for c, v in rates.items() if c.lower() in CODES}
        order = sorted(keep, key=lambda c: -keep[c])
        return (f"Rates vs {base.upper()} (ECB, {data.get('date', '')}): "
                + ", ".join(f"{c} {keep[c]:.3f}" for c in order))
    except Exception as e:
        return f"Rates unavailable: {e}"


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(convert(sys.argv[1],
                      sys.argv[2] if len(sys.argv) > 2 else "USD",
                      sys.argv[3] if len(sys.argv) > 3 else "EUR"))
    else:
        print(list_rates())
        print(convert(100, "USD", "EUR"))
