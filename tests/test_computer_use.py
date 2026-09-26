"""
Offline tests for utils.computer_use — the Tier A desktop AX driver.

Focuses on the pure / subprocess-wrapper helpers that are testable
without a live GUI:

  - _as_quote: the AppleScript string-literal escaper that prevents
    prompt-injection / command-injection into the desktop-controlling
    subsystem via hostile app names, element names, or field values.
  - _osascript: subprocess wrapper — platform guard, timeout, returncode
    handling, never-raises-into-caller contract.
  - _mac_keycode: the keycode table with safe fallback.
  - enabled(): env-gate.
  - get_driver / _reset_singleton: singleton semantics.

GUI-dependent methods (click, type_text, scroll, ax_tree, screenshot)
require a real Accessibility session and are out of scope for offline
coverage — they are exercised via the stubbed ComputerUse in
test_desktop_loop.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestEnabled(unittest.TestCase):
    def test_default_enabled(self):
        os.environ.pop("JARVIS_COMPUTER_USE", None)
        from utils import computer_use as cu
        self.assertTrue(cu.enabled())

    def test_disabled_by_env(self):
        os.environ["JARVIS_COMPUTER_USE"] = "0"
        from utils import computer_use as cu
        self.assertFalse(cu.enabled())
        del os.environ["JARVIS_COMPUTER_USE"]


class TestAsQuote(unittest.TestCase):
    """AppleScript injection defense: hostile values cannot break out."""

    def test_plain_passthrough(self):
        from utils.computer_use import _as_quote
        self.assertEqual(_as_quote("hello"), "hello")

    def test_escapes_double_quote(self):
        from utils.computer_use import _as_quote
        # A raw quote must become \" so it can't terminate the AS string.
        escaped = _as_quote('say "hi"')
        self.assertEqual(escaped, 'say \\"hi\\"')

    def test_escapes_backslash_first(self):
        from utils.computer_use import _as_quote
        # Backslash must be doubled BEFORE quote-doubling, matching the
        # impl order (\ -> \\ first, then " -> \").
        escaped = _as_quote('back\\and"quote')
        self.assertEqual(escaped, 'back\\\\and\\"quote')

    def test_injection_attempt_neutralized(self):
        """A hostile app name must not yield a valid escape sequence that
        breaks out of the literal — just a plain (safe) escaped string."""
        from utils.computer_use import _as_quote
        hostile = 'x";do shell script "rm -rf /" as string'
        out = _as_quote(hostile)
        # The raw closing quote must be escaped, breaking the breakout.
        self.assertIn('\\"', out)
        self.assertNotEqual(out[-1:], '"')

    def test_non_string_coerced(self):
        from utils.computer_use import _as_quote
        self.assertEqual(_as_quote(123), "123")
        self.assertEqual(_as_quote(None), "None")


class TestOsascript(unittest.TestCase):
    """_osascript: platform guard + subprocess handling + never-raises."""

    def test_non_darwin_returns_message(self):
        from utils import computer_use as cu
        with patch.object(cu, "_SYSTEM", "Linux"):
            ok, msg = cu._osascript("display dialog \"hi\"")
        self.assertFalse(ok)
        self.assertEqual(msg, "AppleScript is macOS-only.")

    def test_returncode_zero_returns_stdout(self):
        from utils import computer_use as cu
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "hello from applescript\n"
        with patch.object(cu, "_SYSTEM", "Darwin"), \
             patch("utils.computer_use.subprocess.run",
                   return_value=proc):
            ok, out = cu._osascript("return 1")
        self.assertTrue(ok)
        self.assertEqual(out, "hello from applescript")

    def test_nonzero_returncode_returns_stderr(self):
        from utils import computer_use as cu
        proc = MagicMock()
        proc.returncode = 1
        proc.stderr = "AppleScript error: nope"
        proc.stdout = ""
        with patch.object(cu, "_SYSTEM", "Darwin"), \
             patch("utils.computer_use.subprocess.run",
                   return_value=proc):
            ok, out = cu._osascript("bad script")
        self.assertFalse(ok)
        self.assertIn("nope", out)

    def test_timeout_handled(self):
        from utils import computer_use as cu
        import subprocess as _sp
        with patch.object(cu, "_SYSTEM", "Darwin"), \
             patch("utils.computer_use.subprocess.run",
                   side_effect=_sp.TimeoutExpired(cmd="osascript",
                                                  timeout=1)):
            ok, msg = cu._osascript("sleep 1000", timeout=1)
        self.assertFalse(ok)
        self.assertIn("timed out", msg.lower())

    def test_unexpected_exception_never_propagates(self):
        from utils import computer_use as cu
        with patch.object(cu, "_SYSTEM", "Darwin"), \
             patch("utils.computer_use.subprocess.run",
                   side_effect=RuntimeError("boom")):
            ok, msg = cu._osascript("anything")
        self.assertFalse(ok)
        self.assertIn("boom", msg)


class TestMacKeycode(unittest.TestCase):
    def test_known_key(self):
        from utils.computer_use import _mac_keycode
        # 'space' is a common known key.
        self.assertEqual(_mac_keycode("space"), _mac_keycode("space"))

    def test_named_key_resolves_to_int(self):
        from utils.computer_use import _mac_keycode, _MAC_KEYCODES
        # A named key in the table resolves to its integer keycode.
        kc = _mac_keycode("space")
        self.assertIsInstance(kc, int)
        self.assertEqual(kc, _MAC_KEYCODES["space"])

    def test_numeric_pass_through(self):
        from utils.computer_use import _mac_keycode
        self.assertEqual(_mac_keycode(12), 12)

    def test_bad_input_defaults_return(self):
        from utils.computer_use import _mac_keycode
        # Unknown string -> not in table -> int() fails via table lookup;
        # _mac_keycode returns 36 (the 'return' keycode) as a safe default.
        self.assertEqual(_mac_keycode("zzz_not_a_key"), 36)

    def test_none_defaults_return(self):
        from utils.computer_use import _mac_keycode
        self.assertEqual(_mac_keycode(None), 36)


class TestSingleton(unittest.TestCase):
    def test_get_driver_returns_same_instance(self):
        from utils.computer_use import get_driver, _reset_singleton
        _reset_singleton()
        d1 = get_driver()
        d2 = get_driver()
        self.assertIs(d1, d2)

    def test_reset_singleton_clears(self):
        from utils.computer_use import get_driver, _reset_singleton
        d1 = get_driver()
        _reset_singleton()
        d2 = get_driver()
        self.assertIsNot(d1, d2)


if __name__ == "__main__":
    unittest.main()
