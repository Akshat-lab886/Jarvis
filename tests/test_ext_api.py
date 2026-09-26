"""
Tests for utils.ext_api — the fleet of free/gradual public-API integrations.
============================================================================

Covers:
  Existing integrations (regression): research, habit, eth_watch, ocr_image, pdf_url
  Tier A no-key APIs: weather, define, scripture, ip_locate, earthquakes,
                      joke, recipe, cocktail, cat_fact, dog_pic, quote, open_papers

Every function degrades gracefully: a network failure yields an "unavailable"
string, never an exception. We assert that contract + that each returns a
non-empty user-facing string.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock
from io import BytesIO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _NetMockMixin:
    """Shared helper: run an ext_api function with requests fully mocked
    to raise ConnectionError (simulate total network failure)."""

    def _patch_net(self, fn, *a, **k):
        """Run fn with all ext_api HTTP (via the pooled _http session) mocked
        to raise ConnectionError (simulate total network failure)."""
        import requests as _r
        with patch("utils.ext_api._http") as mock_http:
            m = MagicMock()
            m.get.side_effect = _r.exceptions.ConnectionError("net")
            m.post.side_effect = _r.exceptions.ConnectionError("net")
            mock_http.get = m.get
            mock_http.post = m.post
            return fn(*a, **k)


class TestExtApiDegradation(_NetMockMixin, unittest.TestCase):
    """All functions degrade gracefully under network failure."""

    def test_new_functions_exist_and_are_callable(self):
        from utils import ext_api
        for name in ["weather", "define", "scripture", "ip_locate",
                     "earthquakes", "joke", "recipe", "cocktail",
                     "cat_fact", "dog_pic", "quote", "open_papers",
                     "spot_price",
                     "research", "habit", "eth_watch", "ocr_image", "pdf_url"]:
            self.assertTrue(callable(getattr(ext_api, name)),
                            f"{name} not callable")

    def test_weather_degrades(self):
        from utils.ext_api import weather
        r = self._patch_net(weather, 51.5, -0.1)
        self.assertIn("unavailable", r.lower())

    def test_define_degrades(self):
        from utils.ext_api import define
        r = self._patch_net(define, "word")
        self.assertIn("unavailable", r.lower())

    def test_scripture_degrades(self):
        from utils.ext_api import scripture
        r = self._patch_net(scripture, "John 3:16")
        self.assertIn("unavailable", r.lower())

    def test_ip_locate_degrades(self):
        from utils.ext_api import ip_locate
        r = self._patch_net(ip_locate, "")
        self.assertIn("unavailable", r.lower())

    def test_earthquakes_degrades(self):
        from utils.ext_api import earthquakes
        r = self._patch_net(earthquakes, limit=5)
        self.assertIn("unavailable", r.lower())

    def test_joke_degrades(self):
        from utils.ext_api import joke
        r = self._patch_net(joke, "Any")
        self.assertIn("unavailable", r.lower())

    def test_recipe_degrades(self):
        from utils.ext_api import recipe
        r = self._patch_net(recipe, "pasta")
        self.assertIn("unavailable", r.lower())

    def test_cocktail_degrades(self):
        from utils.ext_api import cocktail
        r = self._patch_net(cocktail, "margarita")
        self.assertIn("unavailable", r.lower())

    def test_cat_fact_degrades(self):
        from utils.ext_api import cat_fact
        r = self._patch_net(cat_fact)
        self.assertIn("unavailable", r.lower())

    def test_dog_pic_degrades(self):
        from utils.ext_api import dog_pic
        r = self._patch_net(dog_pic)
        self.assertIn("unavailable", r.lower())

    def test_quote_degrades(self):
        from utils.ext_api import quote
        r = self._patch_net(quote, "")
        self.assertIn("unavailable", r.lower())

    def test_open_papers_degrades(self):
        from utils.ext_api import open_papers
        r = self._patch_net(open_papers, "transformer")
        self.assertIn("unavailable", r.lower())

    def test_existing_research_degrades(self):
        from utils.ext_api import research
        r = self._patch_net(research, "test")
        self.assertIn("unavailable", r.lower())


class TestExtApiParsing(unittest.TestCase):
    """Functions parse successful API responses correctly."""

    def test_define_parses_response(self):
        from utils import ext_api
        fake = MagicMock()
        fake.status_code = 200
        fake.json.return_value = [{
            "word": "serendipity",
            "phonetic": "/ˌsɛr.ənˈdɪp.ə.ti/",
            "meanings": [
                {"partOfSpeech": "noun",
                 "definitions": [{"definition": "the occurrence of events by chance"}]}
            ]
        }]
        with patch.object(ext_api._http, "get", return_value=fake):
            r = ext_api.define("serendipity")
        self.assertIn("serendipity", r)
        self.assertIn("noun", r)

    def test_joke_returns_value(self):
        from utils import ext_api
        fake = MagicMock()
        fake.json.return_value = {"value": "Chuck Norris counts to infinity. Twice."}
        with patch.object(ext_api._http, "get", return_value=fake):
            r = ext_api.joke()
        self.assertEqual(r, "Chuck Norris counts to infinity. Twice.")

    def test_recipe_parses_response(self):
        from utils import ext_api
        fake = MagicMock()
        fake.json.return_value = {"meals": [{
            "strMeal": "Spaghetti",
            "strArea": "Italian",
            "strCategory": "Pasta",
            "strInstructions": "Boil water.",
            "strIngredient1": "pasta", "strMeasure1": "200g",
            "strIngredient2": "tomato", "strMeasure2": "2",
        }]}
        with patch.object(ext_api._http, "get", return_value=fake):
            r = ext_api.recipe("pasta")
        self.assertIn("Spaghetti", r)
        self.assertIn("Italian", r)

    def test_quote_parses_response(self):
        from utils import ext_api
        fake = MagicMock()
        fake.status_code = 200
        fake.headers = {"content-type": "application/json"}
        fake.json.return_value = {"content": "To be or not to be", "author": "Hamlet"}
        with patch.object(ext_api._http, "get", return_value=fake):
            r = ext_api.quote()
        self.assertIn("To be or not to be", r)
        self.assertIn("Hamlet", r)

    def test_weather_parses_response(self):
        from utils import ext_api
        fake = MagicMock()
        fake.json.return_value = {
            "current": {"temperature_2m": 70.1, "weather_code": 0,
                        "wind_speed_10m": 10.5},
            "current_units": {"temperature_2m": "°F", "wind_speed_10m": "mph"},
            "daily": {"time": ["2026-09-25"], "temperature_2m_max": ["75.9"],
                      "temperature_2m_min": ["56.9"],
                      "precipitation_probability_max": ["0"]},
        }
        with patch.object(ext_api._http, "get", return_value=fake):
            r = ext_api.weather(51.5, -0.1, days=1)
        self.assertIn("70.1", r)


if __name__ == "__main__":
    unittest.main()

class TestExtApiSpotPrice(_NetMockMixin, unittest.TestCase):
    """spot_price: crypto via CoinGecko (no key), stocks via FMP (key),
    symbol validation, rate-limit degradation."""

    def test_spot_price_empty(self):
        from utils.ext_api import spot_price
        r = spot_price("")
        self.assertIn("ticker", r.lower())

    def test_spot_price_validates_symbols(self):
        from utils.ext_api import spot_price
        r = spot_price("AAPL;! DROP TABLE--")
        self.assertNotIn("AAPL:", r)

    def test_spot_price_crypto_parses(self):
        """BTC-USD routes to CoinGecko and parses price."""
        import utils.ext_api as ea
        fake = MagicMock()
        fake.json.return_value = {"bitcoin": {"usd": 84003}}
        with patch.object(ea._http, "get", return_value=fake):
            r = ea.spot_price("BTC-USD")
        self.assertIn("BTC-USD", r)
        self.assertIn("$84,003", r)

    def test_spot_price_stock_needs_key(self):
        """Without FMP_API_KEY, stock tickers report the key requirement."""
        import utils.ext_api as ea
        r = ea.spot_price("AAPL")
        self.assertIn("FMP_API_KEY", r)

    def test_spot_price_stock_with_key_parses(self):
        """With FMP_API_KEY env set, stocks resolve from FMP JSON."""
        import utils.ext_api as ea
        with patch.object(ea, "_FMP_KEY", "fakekey"):
            fake = MagicMock()
            fake.json.return_value = [{
                "symbol": "AAPL", "price": 220.5,
                "changesPercentage": -0.91, "currency": "USD"}]
            with patch.object(ea._http, "get", return_value=fake):
                r = ea.spot_price("AAPL")
        self.assertIn("AAPL", r)
        self.assertIn("220.5", r)

    def test_spot_price_crypto_unknown_symbol_n_a(self):
        """A crypto symbol NOT in _CG_IDS falls back to symbol.lower()
        as the coin ID; if CoinGecko has no match, 'n/a' is reported."""
        import utils.ext_api as ea
        fake = MagicMock()
        fake.json.return_value = {}  # no such coin
        with patch.object(ea._http, "get", return_value=fake):
            r = ea.spot_price("DOGE-USD")
        self.assertIn("DOGE-USD", r)
        self.assertIn("n/a", r)

    def test_spot_price_coingecko_rate_limit_degrades(self):
        """CoinGecko free-tier 429 returns {'status': {'error_code': 429}}
        — the crypto leg must report rate-limit, not crash."""
        import utils.ext_api as ea
        fake = MagicMock()
        fake.json.return_value = {"status": {"error_code": 429,
                                             "error_message": "rate limit"}}
        with patch.object(ea._http, "get", return_value=fake):
            r = ea.spot_price("BTC-USD")
        self.assertIn("rate-limit", r.lower())

    def test_spot_price_coingecko_ratelimit(self):
        """CoinGecko 429 -> graceful rate-limited message."""
        import utils.ext_api as ea
        fake = MagicMock()
        fake.json.return_value = {"status": {"error_code": 429,
                                             "error_message": "rl"}}
        with patch.object(ea._http, "get", return_value=fake):
            r = ea.spot_price("BTC-USD")
        self.assertIn("rate-limited", r.lower())


class TestIpLocateHardened(_NetMockMixin, unittest.TestCase):
    """ip_locate handles 429 + non-JSON + status >= 300."""

    def test_ip_locate_429(self):
        from utils import ext_api
        fake = MagicMock()
        fake.status_code = 429
        with patch.object(ext_api._http, "get", return_value=fake):
            r = ext_api.ip_locate()
        self.assertIn("rate-limited", r.lower())

    def test_ip_locate_non_json(self):
        from utils import ext_api
        fake = MagicMock()
        fake.status_code = 200
        fake.headers = {"content-type": "text/html"}
        with patch.object(ext_api._http, "get", return_value=fake):
            r = ext_api.ip_locate("8.8.8.8")
        self.assertIn("unexpected response", r.lower())

    def test_ip_locate_server_error(self):
        from utils import ext_api
        fake = MagicMock()
        fake.status_code = 500
        with patch.object(ext_api._http, "get", return_value=fake):
            r = ext_api.ip_locate("1.2.3.4")
        self.assertIn("HTTP 500", r)


class TestQuoteHardened(_NetMockMixin, unittest.TestCase):
    """quote handles 429 + non-JSON."""

    def test_quote_429(self):
        from utils import ext_api
        fake = MagicMock()
        fake.status_code = 429
        with patch.object(ext_api._http, "get", return_value=fake):
            r = ext_api.quote()
        self.assertIn("rate-limited", r.lower())


class TestSessionResilience(unittest.TestCase):
    """The shared _http session must retry transient failures (429/5xx)
    so composite calls (morning_briefing) don't fail on a single blip."""

    def test_session_has_retry_adapter(self):
        from utils import ext_api
        adapter = ext_api._http.get_adapter("https://api.example.com")
        retries = adapter.max_retries
        self.assertIn(429, retries.status_forcelist)
        self.assertIn(503, retries.status_forcelist)
        self.assertGreaterEqual(retries.total, 1)
        self.assertGreater(retries.backoff_factor, 0)

    def test_session_mounted_for_both_schemes(self):
        from utils import ext_api
        schemes = set(ext_api._http.adapters.keys())
        self.assertIn("http://", schemes)
        self.assertIn("https://", schemes)

    def test_session_has_browser_user_agent(self):
        from utils import ext_api
        self.assertIn("User-Agent", ext_api._http.headers)
        self.assertIn("Mozilla", ext_api._http.headers["User-Agent"])
