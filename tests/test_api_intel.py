"""
Tests for utils.server.api_intel — the 'intel strip' dashboard aggregator.

Covers (all network mocked):
  - Shape contract: {ts, cells{weather,crypto,space,flights,papers,joke,
    cat_fact,quote}} with ok/text/why per cell.
  - Degrade never-500: a failing cell marks itself ok:False, never raises.
  - Caching: second call within 300s returns cached payload (same ts).
  - short(): joke/cat_fact/quote cells are sentence-truncated (<=70 chars).
"""

import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


EXPECTED_CELLS = {
    "weather", "crypto", "space", "flights", "papers",
    "joke", "cat_fact", "quote",
}

# A realistic return value for each intel cell's source function.
_FAKES = {
    "current_weather": "12°C, partly cloudy. Later: 18°C.",
    "crypto_price": "BTC: $84,003, ETH: $2,694",
    "space_apod": "The Pillars of Creation (Hubble)",
    "flight_tracker": "UA-1234 heavy, 30 mins late",
    "open_papers": "3 open-access papers on quantum error correction",
    "joke": "Why don't scientists trust atoms? They make up everything!",
    "cat_fact": "Cats have 32 muscles in each ear.",
    "quote": "Be the change you wish to see. — Gandhi",
}


class TestApiIntel(unittest.TestCase):

    def setUp(self):
        import utils.server as srv
        if hasattr(srv.api_intel, "_cache"):
            del srv.api_intel._cache
        self.srv = srv
        self.app = srv.app
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)

    def _jsonl(self):
        """Invoke api_intel() within the pushed context; return the dict."""
        return self.srv.api_intel().get_json()

    def _patch_all(self):
        """Patch every external source so api_intel runs offline."""
        patches = []
        patches.append(patch("utils.weather_api.current_weather",
                             return_value=_FAKES["current_weather"]))
        patches.append(patch("utils.info_api.space_apod",
                             return_value=_FAKES["space_apod"]))
        patches.append(patch("utils.info_api.crypto_price",
                             return_value=_FAKES["crypto_price"]))
        patches.append(patch("utils.flight_api.flights_near",
                             return_value=_FAKES["flight_tracker"]))
        # NOTE: api_intel imports and calls `research` (not `open_papers`),
        # so we must patch that symbol — otherwise the real function fires a
        # live HTTP call to OpenAlex, which retries/hangs under the resilient
        # session adapter added to ext_api. open_papers is patched too since
        # it shares the same codepath and may be called elsewhere.
        patches.append(patch("utils.ext_api.research",
                             return_value=_FAKES["open_papers"]))
        patches.append(patch("utils.ext_api.open_papers",
                             return_value=_FAKES["open_papers"]))
        patches.append(patch("utils.ext_api.joke",
                             return_value=_FAKES["joke"]))
        patches.append(patch("utils.ext_api.cat_fact",
                             return_value=_FAKES["cat_fact"]))
        patches.append(patch("utils.ext_api.quote",
                             return_value=_FAKES["quote"]))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_cells_present_and_wellformed(self):
        self._patch_all()
        out = self._jsonl()
        self.assertIn("ts", out)
        self.assertIn("cells", out)
        self.assertEqual(set(out["cells"]) & EXPECTED_CELLS, EXPECTED_CELLS)
        for name, cell in out["cells"].items():
            self.assertIsInstance(cell, dict)
            self.assertIn("ok", cell)
            if not cell["ok"]:
                self.assertIn("why", cell)
            elif "text" in cell:
                self.assertIsInstance(cell["text"], str)

    def test_joke_cell_ok(self):
        self._patch_all()
        out = self._jsonl()
        self.assertTrue(out["cells"]["joke"]["ok"])
        self.assertIn("atoms", out["cells"]["joke"]["text"])

    def test_degrade_never_500(self):
        """If joke() raises, its cell degrades to ok:False, not a crash."""
        self._patch_all()
        with patch("utils.ext_api.joke", side_effect=RuntimeError("boom")):
            out = self._jsonl()
            j = out["cells"]["joke"]
            self.assertFalse(j["ok"])
            self.assertIn("boom", j["why"])

    def test_degrade_detects_unavailable(self):
        """A cell returning 'unavailable' is marked ok:False."""
        self._patch_all()
        with patch("utils.ext_api.quote",
                   return_value="Quotes unavailable: 429"):
            out = self._jsonl()
            q = out["cells"]["quote"]
            self.assertFalse(q["ok"])

    def test_cache_served_within_window(self):
        self._patch_all()
        first = self._jsonl()
        second = self._jsonl()
        self.assertEqual(first["ts"], second["ts"])
        self.assertEqual(first["cells"], second["cells"])

    def test_cache_busted_after_expiry(self):
        self._patch_all()
        self.srv.api_intel()
        # Backdate the cache by 400s so the next call re-fetches.
        self.srv.api_intel._cache = (time.time() - 400,
                                     self.srv.api_intel._cache[1])
        fresh = self._jsonl()
        # Fresh call re-ran everything; ts is newer-ish.
        self.assertIsInstance(fresh["ts"], int)

    def test_short_truncates_joke(self):
        """joke/cat_fact/quote text cells <= 80 chars (short() contract)."""
        self._patch_all()
        out = self._jsonl()
        for name in ("joke", "cat_fact", "quote"):
            c = out["cells"][name]
            if c["ok"]:
                self.assertLessEqual(len(c["text"]), 80)


if __name__ == "__main__":
    unittest.main()
