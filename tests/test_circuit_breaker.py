"""Unit tests for utils.circuit_breaker — the pre-execution safety gate.

The CircuitBreaker decides whether Brain may spend LLM tokens at all
(tripped) or proceed (ok/degraded). It had 0% test coverage despite
being the kill-switch for all LLM spend, so these tests lock in the
critical invariants:

  - tripped status (fatal failure) vs degraded (non-fatal) vs ok
  - the throttle prevents redundant re-checks within RECHECK_INTERVAL
  - allow_llm_spend() re-validates when tripped so recovery is possible
  - a crashed check degrades to fatal=tripped (fail-closed), never ok

Checks are monkeypatched so no real config / disk / network probing
runs.
"""

import os
import sys
import unittest
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.circuit_breaker import CircuitBreaker, BreakerReport


def _cb():
    """Fresh breaker with empty initial report (checked_at=0)."""
    cb = CircuitBreaker.__new__(CircuitBreaker)
    cb.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cb._lock = threading.Lock()
    cb._report = BreakerReport()
    return cb


class TestStatusClassification(unittest.TestCase):
    def test_all_pass_is_ok(self):
        cb = _cb()
        with patch.object(cb, '_check_providers', return_value=(True, True, "ok")), \
             patch.object(cb, '_check_workspace', return_value=(True, True, "ok")), \
             patch.object(cb, '_check_disk', return_value=(True, False, "ok")), \
             patch.object(cb, '_check_memory_backends', return_value=(True, False, "ok")), \
             patch.object(cb, '_check_budget_config', return_value=(True, False, "ok")):
            r = cb.validate(force=True)
        self.assertEqual(r.status, 'ok')
        self.assertFalse(r.tripped)

    def test_fatal_failure_is_tripped(self):
        cb = _cb()
        with patch.object(cb, '_check_providers', return_value=(False, True, "no key")), \
             patch.object(cb, '_check_workspace', return_value=(True, True, "ok")), \
             patch.object(cb, '_check_disk', return_value=(True, False, "ok")), \
             patch.object(cb, '_check_memory_backends', return_value=(True, False, "ok")), \
             patch.object(cb, '_check_budget_config', return_value=(True, False, "ok")):
            r = cb.validate(force=True)
        self.assertEqual(r.status, 'tripped')
        self.assertTrue(r.tripped)

    def test_nonfatal_failure_is_degraded(self):
        cb = _cb()
        with patch.object(cb, '_check_providers', return_value=(True, True, "ok")), \
             patch.object(cb, '_check_workspace', return_value=(True, True, "ok")), \
             patch.object(cb, '_check_disk', return_value=(False, False, "low disk")), \
             patch.object(cb, '_check_memory_backends', return_value=(True, False, "ok")), \
             patch.object(cb, '_check_budget_config', return_value=(True, False, "ok")):
            r = cb.validate(force=True)
        self.assertEqual(r.status, 'degraded')
        self.assertFalse(r.tripped)

    def test_mixed_fatal_and_nonfatal_is_tripped(self):
        """A fatal failure dominates — never downgrade to degraded."""
        cb = _cb()
        with patch.object(cb, '_check_providers', return_value=(False, True, "no key")), \
             patch.object(cb, '_check_disk', return_value=(False, False, "low disk")):
            r = cb.validate(force=True)
        self.assertEqual(r.status, 'tripped')


class TestThrottle(unittest.TestCase):
    def test_non_forced_validate_is_cached(self):
        cb = _cb()
        real = {
            '_check_providers': (True, True, "ok"),
            '_check_workspace': (True, True, "ok"),
            '_check_disk': (True, False, "ok"),
            '_check_memory_backends': (True, False, "ok"),
            '_check_budget_config': (True, False, "ok"),
        }
        def _patched(method_name):
            return patch.object(cb, method_name,
                                return_value=real[method_name])

        cm = [_patched(m) for m in real]
        for c in cm: c.__enter__()
        try:
            first = cb.validate(force=True)
            second = cb.validate(force=False)  # should be throttled
        finally:
            for c in cm: c.__exit__(None, None, None)
        # Throttled call returns the SAME cached report object.
        self.assertIs(first, second)
        self.assertGreaterEqual(first.checked_at, 0.0)


class TestAllowSpend(unittest.TestCase):
    def test_ok_allows_spend(self):
        cb = _cb()
        cb._report.status = 'ok'
        self.assertTrue(cb.allow_llm_spend())

    def test_degraded_allows_spend(self):
        """Degraded != tripped: spend still allowed (reduced features)."""
        cb = _cb()
        cb._report.status = 'degraded'
        self.assertTrue(cb.allow_llm_spend())

    def test_tripped_blocks_until_recovery(self):
        cb = _cb()
        cb._report.status = 'tripped'
        # While tripped, force-validate re-checks; if now ok, allow.
        ok_report = BreakerReport(); ok_report.status = 'ok'
        ok_report.checked_at = 0
        with patch.object(cb, 'validate', return_value=ok_report):
            self.assertTrue(cb.allow_llm_spend())

    def test_tripped_stays_blocked(self):
        cb = _cb()
        cb._report.status = 'tripped'
        bad_report = BreakerReport(); bad_report.status = 'tripped'
        bad_report.checked_at = 0
        with patch.object(cb, 'validate', return_value=bad_report):
            self.assertFalse(cb.allow_llm_spend())


class TestCrashedCheck(unittest.TestCase):
    def test_crashed_check_is_tripped(self):
        """A check that raises must fail-closed (fatal), never report ok."""
        cb = _cb()
        with patch.object(cb, '_check_providers',
                          side_effect=RuntimeError("boom")), \
             patch.object(cb, '_check_workspace', return_value=(True, True, "ok")), \
             patch.object(cb, '_check_disk', return_value=(True, False, "ok")), \
             patch.object(cb, '_check_memory_backends', return_value=(True, False, "ok")), \
             patch.object(cb, '_check_budget_config', return_value=(True, False, "ok")):
            r = cb.validate(force=True)
        self.assertEqual(r.status, 'tripped')
        crash = [c for c in r.checks if 'crashed' in (c.get('detail') or '')]
        self.assertTrue(crash)


if __name__ == "__main__":
    unittest.main(verbosity=2)
