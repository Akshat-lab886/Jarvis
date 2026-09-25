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


class TestExtApiDegradation(unittest.TestCase):
    """All functions degrade gracefully under network failure."""

    def _patch_net(self, fn, *a, **k):
        """Run fn with requests.get/post fully mocked to raise."""
        with patch("utils.ext_api.requests") as mock_req:
            mock_req.exceptions = __import__("requests").exceptions
            m = MagicMock()
            m.get.side_effect = __import__("requests").exceptions.ConnectionError("net")
            m.post.side_effect = __import__("requests").exceptions.ConnectionError("net")
            mock_req.get = m.get
            mock_req.post = m.post
            return fn(*a, **k)

    def test_new_functions_exist_and_are_callable(self):
        from utils import ext_api
        for name in ["weather", "define", "scripture", "ip_locate",
                     "earthquakes", "joke", "recipe", "cocktail",
                     "cat_fact", "dog_pic", "quote", "open_papers",
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
        with patch.object(ext_api.requests, "get", return_value=fake):
            r = ext_api.define("serendipity")
        self.assertIn("serendipity", r)
        self.assertIn("noun", r)

    def test_joke_returns_value(self):
        from utils import ext_api
        fake = MagicMock()
        fake.json.return_value = {"value": "Chuck Norris counts to infinity. Twice."}
        with patch.object(ext_api.requests, "get", return_value=fake):
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
        with patch.object(ext_api.requests, "get", return_value=fake):
            r = ext_api.recipe("pasta")
        self.assertIn("Spaghetti", r)
        self.assertIn("Italian", r)

    def test_quote_parses_response(self):
        from utils import ext_api
        fake = MagicMock()
        fake.json.return_value = {"content": "To be or not to be", "author": "Hamlet"}
        with patch.object(ext_api.requests, "get", return_value=fake):
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
        with patch.object(ext_api.requests, "get", return_value=fake):
            r = ext_api.weather(51.5, -0.1, days=1)
        self.assertIn("70.1", r)


if __name__ == "__main__":
    unittest.main()
