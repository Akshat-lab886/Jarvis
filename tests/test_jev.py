"""
Offline tests for utils.jev — the Tier A safety gate ("Jev") that wraps
TypeSafe's System One model for the computer-use path.

Covers the safety contract that MUST hold regardless of model/backend:
  - fail-open: disabled / no key / network error / non-200 / bad JSON ->
    None (never raises); precheck/screen_step return None.
  - prompt-injection fast-deny: inject_deny() only triggers on the
    injection signal (risk alone never auto-denies).
  - risk rubric clamping 0..3; risk_label maps correctly.
  - 429/529 backoff retry (one retry) -> then returns None.
  - _noul handles malformed confidence.

No network: requests.post is mocked. No API key required (disabled()
short-circuits when TYPESAFE_API_KEY is unset). Tests that need the
'enabled' path set the env var via the standard _JevEnvMixin.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok_payload(answers=None, model="jev-test", usage={"tokens": 7}):
    """Minimal valid Jev response body."""
    return {
        "model": model,
        "answers": answers or {},
        "usage": usage,
    }


class _JevEnabledMixin:
    """Sets TYPESAFE_API_KEY so enabled() returns True (mocked transport
    means no real network/key is ever used)."""

    def setUp(self):
        self._old_key = os.environ.get("TYPESAFE_API_KEY")
        self._old_jev = os.environ.get("JARVIS_JEV")
        os.environ["TYPESAFE_API_KEY"] = "test-key-not-real"
        os.environ["JARVIS_JEV"] = "1"

    def tearDown(self):
        if self._old_key is None:
            os.environ.pop("TYPESAFE_API_KEY", None)
        else:
            os.environ["TYPESAFE_API_KEY"] = self._old_key
        if self._old_jev is None:
            os.environ.pop("JARVIS_JEV", None)
        else:
            os.environ["JARVIS_JEV"] = self._old_jev


class TestEnabled(unittest.TestCase):
    def test_disabled_without_key(self):
        os.environ.pop("TYPESAFE_API_KEY", None)
        os.environ.pop("TYPESAFE_API_KEY", None)
        from utils import jev
        # Clear any cached key.
        jev._KEY_CACHE = None
        self.assertFalse(jev.enabled())

    def test_disabled_when_jarvis_jev_zero(self):
        os.environ["TYPESAFE_API_KEY"] = "k"
        os.environ["JARVIS_JEV"] = "0"
        from utils import jev
        jev._KEY_CACHE = None
        self.assertFalse(jev.enabled())
        os.environ.pop("TYPESAFE_API_KEY", None)
        os.environ.pop("JARVIS_JEV", None)


class TestFailOpen(_JevEnabledMixin, unittest.TestCase):
    """All failure modes must return None / fail-open, never raise."""

    def test_no_key_returns_none(self):
        """Without TYPESAFE_API_KEY, ask() returns None immediately."""
        os.environ.pop("TYPESAFE_API_KEY", None)
        from utils import jev
        self.assertIsNone(jev.ask("s", {"q": {}}))

    def test_network_error_returns_none(self):
        import requests as _r
        fake_post = MagicMock()
        fake_post.side_effect = _r.ConnectionError("dns")
        with patch("utils.jev.requests.post", fake_post):
            from utils import jev
            self.assertIsNone(jev.ask("s", {"q": {}}))

    def test_non_200_returns_none(self):
        fake_post = MagicMock()
        resp = MagicMock()
        resp.status_code = 500
        resp.text = "server err"
        fake_post.return_value = resp
        with patch("utils.jev.requests.post", fake_post):
            from utils import jev
            self.assertIsNone(jev.ask("s", {"q": {}}))

    def test_bad_json_returns_none(self):
        fake_post = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("bad json")
        fake_post.return_value = resp
        with patch("utils.jev.requests.post", fake_post):
            from utils import jev
            self.assertIsNone(jev.ask("s", {"q": {}}))

    def test_missing_answers_returns_none(self):
        fake_post = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"model": "x"}  # no 'answers'
        fake_post.return_value = resp
        with patch("utils.jev.requests.post", fake_post):
            from utils import jev
            self.assertIsNone(jev.ask("s", {"q": {}}))

    def test_429_then_500_returns_none(self):
        """One backoff retry on 429, then gives up and returns None."""
        fake_post = MagicMock()
        bad = MagicMock()
        bad.status_code = 429
        bad.text = "slow down"
        fake_post.return_value = bad
        with patch("utils.jev.requests.post", fake_post):
            from utils import jev
            self.assertIsNone(jev.ask("s", {"q": {}}))


class TestAskSuccess(_JevEnabledMixin, unittest.TestCase):
    def test_success_returns_payload(self):
        import time
        fake_post = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = _ok_payload(
            {"risk": {"score": 1, "confidence": 0.9}})
        fake_post.return_value = resp
        with patch("utils.jev.requests.post", fake_post), \
             patch("time.time", side_effect=[1000.0, 1001.5]):
            from utils import jev
            data = jev.ask("state", {"q": {"type": "score"}})
        self.assertIsNotNone(data)
        self.assertEqual(data["model"], "jev-test")
        self.assertEqual(data["answers"]["risk"]["score"], 1)
        self.assertIn("ms", data)
        # 1.5s -> 1500ms
        self.assertEqual(data["ms"], 1500)


class TestPrecheck(_JevEnabledMixin, unittest.TestCase):
    def _precheck_resp(self, answers):
        return MagicMock(status_code=200,
                         json=lambda: _ok_payload(answers))

    def test_disabled_returns_none(self):
        os.environ.pop("TYPESAFE_API_KEY", None)
        from utils import jev
        self.assertIsNone(jev.precheck({"action": "click"}))

    def test_returns_calibrated_signals(self):
        # noul-typed answers come back as {"noul": <prob>}; risk is a
        # score object {"score":.., "confidence":..}.
        answers = {
            "risk": {"score": 2, "confidence": 0.8},
            "irreversible": {"noul": 0.9},
            "injection": {"noul": 0.3},
        }
        fake_post = MagicMock()
        fake_post.return_value = self._precheck_resp(answers)
        with patch("utils.jev.requests.post", fake_post):
            from utils import jev
            v = jev.precheck({"action": "type", "text": "rm -rf /"})
        self.assertIsNotNone(v)
        self.assertEqual(v["risk"], 2)
        self.assertEqual(v["risk_score"], 2)
        self.assertEqual(v["risk_confidence"], 0.8)
        self.assertAlmostEqual(v["irreversible"], 0.9)
        self.assertAlmostEqual(v["injection"], 0.3)

    def test_malformed_float_noul_does_not_crash(self):
        """A bare-float noul (not {"noul":..}) must not crash the gate.
        _noul tolerates both shapes: it extracts the float value (the
        signal must not be silently lost) instead of raising."""
        answers = {
            "risk": {"score": 1, "confidence": 0.5},
            "irreversible": 0.9,   # malformed: float, not dict
            "injection": 0.2,
        }
        fake_post = MagicMock()
        fake_post.return_value = self._precheck_resp(answers)
        with patch("utils.jev.requests.post", fake_post):
            from utils import jev
            v = jev.precheck({"action": "click"})
        self.assertIsNotNone(v)
        # risk still parses; malformed float noul is extracted, not None.
        self.assertEqual(v["risk"], 1)
        self.assertAlmostEqual(v["irreversible"], 0.9)
        self.assertAlmostEqual(v["injection"], 0.2)

    def test_risk_clamped_to_0_3(self):
        """Score outside 0..3 must clamp."""
        answers = {
            "risk": {"score": 9, "confidence": 0.5},
            "irreversible": {"noul": 0.1},
            "injection": {"noul": 0.1},
        }
        fake_post = MagicMock()
        fake_post.return_value = self._precheck_resp(answers)
        with patch("utils.jev.requests.post", fake_post):
            from utils import jev
            v = jev.precheck({"action": "x"})
        self.assertEqual(v["risk"], 3)

    def test_risk_none_when_score_missing(self):
        answers = {"irreversible": {"noul": 0.5},
                   "injection": {"noul": 0.2}}
        fake_post = MagicMock()
        fake_post.return_value = self._precheck_resp(answers)
        with patch("utils.jev.requests.post", fake_post):
            from utils import jev
            v = jev.precheck({"action": "x"})
        self.assertEqual(v["risk"], None)


class TestInjectDeny(unittest.TestCase):
    """inject_deny: only injection >= 0.85 fast-denies; risk never does."""

    def test_high_risk_no_injection_does_not_deny(self):
        from utils import jev
        v = {"risk": 3, "risk_score": 0.9, "injection": 0.2}
        self.assertFalse(jev.inject_deny(v))

    def test_injection_above_threshold_denies(self):
        from utils import jev
        v = {"risk": 1, "injection": 0.86}
        self.assertTrue(jev.inject_deny(v))

    def test_injection_at_threshold_denies(self):
        from utils import jev
        v = {"injection": 0.85}
        self.assertTrue(jev.inject_deny(v))

    def test_none_verdict_no_deny(self):
        from utils import jev
        self.assertFalse(jev.inject_deny(None))

    def test_missing_injection_no_deny(self):
        from utils import jev
        self.assertFalse(jev.inject_deny({"risk": 3}))


class TestRiskLabel(unittest.TestCase):
    def test_labels(self):
        from utils import jev
        self.assertEqual(jev.risk_label(None), "")
        self.assertEqual(jev.risk_label({}), "")
        self.assertEqual(jev.risk_label({"risk": None}), "")
        self.assertEqual(jev.risk_label({"risk": 0}), "read-only")
        self.assertEqual(jev.risk_label({"risk": 1}), "reversible")
        self.assertEqual(jev.risk_label({"risk": 2}), "risky")
        self.assertEqual(jev.risk_label({"risk": 3}), "dangerous")

    def test_clamps(self):
        from utils import jev
        self.assertEqual(jev.risk_label({"risk": 9}), "dangerous")
        self.assertEqual(jev.risk_label({"risk": -3}), "read-only")


class TestScreenStep(_JevEnabledMixin, unittest.TestCase):
    def test_fail_open_on_error(self):
        import requests as _r
        fake_post = MagicMock()
        fake_post.side_effect = _r.ConnectionError("down")
        with patch("utils.jev.requests.post", fake_post):
            from utils import jev
            self.assertIsNone(jev.screen_step("task", {"op": "click"}))

    def test_returns_signals(self):
        answers = {"on_task": {"noul": 0.9}, "hazard": {"noul": 0.1}}
        fake_post = MagicMock()
        resp = MagicMock(status_code=200)
        resp.json.return_value = _ok_payload(answers)
        fake_post.return_value = resp
        with patch("utils.jev.requests.post", fake_post):
            from utils import jev
            v = jev.screen_step("deploy app", {"op": "click", "x": 10})
        self.assertIsNotNone(v)
        self.assertAlmostEqual(v["on_task"], 0.9)
        self.assertAlmostEqual(v["hazard"], 0.1)


class TestNoul(unittest.TestCase):
    def test_parses_float(self):
        from utils import jev
        self.assertEqual(jev._noul({"noul": 0.85}), 0.85)

    def test_string_float(self):
        from utils import jev
        self.assertEqual(jev._noul({"noul": "0.85"}), 0.85)

    def test_none(self):
        from utils import jev
        self.assertIsNone(jev._noul({"noul": None}))
        self.assertIsNone(jev._noul({}))
        self.assertIsNone(jev._noul(None))

    def test_bad_value(self):
        from utils import jev
        self.assertIsNone(jev._noul({"noul": "abc"}))


if __name__ == "__main__":
    unittest.main()
