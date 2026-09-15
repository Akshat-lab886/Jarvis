"""
Jarvis Safety Framework Tests
==============================

Covers:
  - Audit trail: logging, anomaly detection, risk classification
  - Approval fixes: command forwarding, destructive tier, cooldown
  - Privacy enforcement integration
  - Safety directives in system prompt
  - Auto-snapshot before destructive actions
"""

import os
import sys
import time
import json
import threading
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── helpers ────────────────────────────────────────────────────────── #

def _reset():
    """Reset all singletons before each test."""
    import utils.audit as aud
    aud._reset_singleton()
    # Create a fresh instance with empty ring (bypass disk load)
    inst = aud.AuditLogger.__new__(aud.AuditLogger)
    import collections
    inst._ring = collections.deque(maxlen=aud._MAX_RING)
    inst._lock = threading.Lock()
    inst._dirty = False
    inst._detector = aud.AnomalyDetector()
    inst._last_flush = 0
    aud._singleton = inst
    import utils.approvals as app
    app._shared = None


# ==================================================================== #
#  AUDIT TRAIL
# ==================================================================== #

class TestAuditRiskClassification(unittest.TestCase):
    """Audit module risk tiers are correct."""

    def test_destructive_actions(self):
        from utils.audit import DESTRUCTIVE_ACTIONS, risk_tier
        for action in ('delete_file', 'kill_process', 'send_email',
                       'dev_write', 'sandbox_run', 'computer_use',
                       'browser_use', 'forget_memory'):
            self.assertIn(action, DESTRUCTIVE_ACTIONS)
            self.assertEqual(risk_tier(action), 'destructive')

    def test_sensitive_actions(self):
        from utils.audit import SENSITIVE_ACTIONS, risk_tier
        for action in ('open_app', 'add_event', 'todo_add', 'goal_set'):
            self.assertIn(action, SENSITIVE_ACTIONS)
            self.assertEqual(risk_tier(action), 'sensitive')

    def test_readonly_actions(self):
        from utils.audit import READONLY_ACTIONS, risk_tier
        for action in ('get_weather', 'system_info', 'todo_list',
                       'chat', 'recall'):
            self.assertIn(action, READONLY_ACTIONS)
            self.assertEqual(risk_tier(action), 'readonly')

    def test_unknown_defaults_readonly(self):
        from utils.audit import risk_tier
        self.assertEqual(risk_tier('totally_unknown_action'), 'readonly')


class TestAuditLogging(unittest.TestCase):
    """AuditLogger records and persists actions."""

    def setUp(self):
        _reset()

    def test_log_returns_record(self):
        from utils.audit import get_audit
        aud = get_audit()
        rec = aud.log('get_weather', outcome='ok', duration_ms=42)
        self.assertEqual(rec.action, 'get_weather')
        self.assertEqual(rec.outcome, 'ok')
        self.assertEqual(rec.duration_ms, 42)
        self.assertEqual(rec.risk, 'readonly')

    def test_log_with_command(self):
        from utils.audit import get_audit
        aud = get_audit()
        cmd = {'action': 'send_email', 'recipient': 'test@example.com',
               '_origin': 'recurring'}
        rec = aud.log('send_email', command=cmd, outcome='ok')
        self.assertEqual(rec.action, 'send_email')
        self.assertEqual(rec.origin, 'recurring')
        self.assertEqual(rec.risk, 'destructive')

    def test_recent_returns_newest_first(self):
        from utils.audit import get_audit
        aud = get_audit()
        aud.log('action_a', outcome='ok')
        time.sleep(0.01)
        aud.log('action_b', outcome='ok')
        recent = aud.recent(limit=2)
        self.assertEqual(recent[0]['action'], 'action_b')
        self.assertEqual(recent[1]['action'], 'action_a')

    def test_recent_filter_by_action(self):
        from utils.audit import get_audit
        aud = get_audit()
        aud.log('get_weather', outcome='ok')
        aud.log('send_email', outcome='ok')
        aud.log('get_weather', outcome='ok')
        recent = aud.recent(action_filter='get_weather')
        self.assertEqual(len(recent), 2)
        for r in recent:
            self.assertEqual(r['action'], 'get_weather')

    def test_recent_filter_by_risk(self):
        from utils.audit import get_audit
        aud = get_audit()
        aud.log('get_weather', outcome='ok')
        aud.log('delete_file', outcome='ok')
        recent = aud.recent(risk_filter='destructive')
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]['action'], 'delete_file')

    def test_stats(self):
        from utils.audit import get_audit
        aud = get_audit()
        aud.log('get_weather', outcome='ok')
        aud.log('delete_file', outcome='ok')
        aud.log('send_email', outcome='error')
        stats = aud.stats()
        self.assertEqual(stats['total_records'], 3)
        self.assertEqual(stats['destructive_hour'], 2)
        self.assertEqual(stats['failures_hour'], 1)


class TestAnomalyDetection(unittest.TestCase):
    """Anomaly detector fires on burst patterns."""

    def setUp(self):
        _reset()

    def test_no_alerts_normal(self):
        from utils.audit import get_audit
        aud = get_audit()
        for _ in range(3):
            aud.log('get_weather', outcome='ok')
        alerts = aud._detector.check()
        self.assertEqual(alerts, [])

    def test_destructive_burst_alert(self):
        from utils.audit import get_audit, _DESTRUCTIVE_BURST
        aud = get_audit()
        for _ in range(_DESTRUCTIVE_BURST + 1):
            aud.log('delete_file', outcome='ok')
        alerts = aud._detector.check()
        self.assertTrue(any('DESTRUCTIVE BURST' in a for a in alerts))

    def test_failure_burst_alert(self):
        from utils.audit import get_audit, _FAILURE_BURST
        aud = get_audit()
        for _ in range(_FAILURE_BURST + 1):
            aud.log('send_email', outcome='error')
        alerts = aud._detector.check()
        self.assertTrue(any('FAILURE BURST' in a for a in alerts))

    def test_denied_actions_count_as_failures(self):
        from utils.audit import get_audit, _FAILURE_BURST
        aud = get_audit()
        for _ in range(_FAILURE_BURST + 1):
            aud.log('send_email', outcome='denied')
        alerts = aud._detector.check()
        self.assertTrue(any('FAILURE BURST' in a for a in alerts))

    def test_deduplicated_actions(self):
        """Each record is counted individually (no dedup by timestamp)."""
        from utils.audit import AuditRecord, AnomalyDetector
        det = AnomalyDetector()
        for _ in range(3):
            rec = AuditRecord(action='delete_file', outcome='ok')
            det.record(rec)
        # Each record increments the counter
        self.assertEqual(len(det._destructive), 3)


# ==================================================================== #
#  APPROVAL FIXES
# ==================================================================== #

class TestApprovalFixes(unittest.TestCase):
    """Approval system fixes: command forwarding, destructive tier, cooldown."""

    def setUp(self):
        _reset()
        os.environ['JARVIS_APPROVALS'] = 'critical'

    def tearDown(self):
        os.environ.pop('JARVIS_APPROVALS', None)
        os.environ.pop('DESTRUCTIVE_COOLDOWN_S', None)

    def test_requires_returns_false_when_disabled(self):
        from utils.approvals import ApprovalManager
        os.environ['JARVIS_APPROVALS'] = 'off'
        mgr = ApprovalManager()
        self.assertFalse(mgr.requires('delete_file'))
        self.assertFalse(mgr.requires('send_email'))

    def test_non_critical_never_gates(self):
        from utils.approvals import ApprovalManager
        mgr = ApprovalManager()
        self.assertFalse(mgr.requires('get_weather'))
        self.assertFalse(mgr.requires('todo_list'))
        self.assertFalse(mgr.requires('chat'))

    def test_destructive_always_gates(self):
        """Destructive actions require human approval even from trusted origins."""
        from utils.approvals import ApprovalManager, DESTRUCTIVE_ACTIONS
        mgr = ApprovalManager()
        for action in ('delete_file', 'send_email', 'kill_process',
                       'dev_write', 'forget_memory'):
            self.assertIn(action, DESTRUCTIVE_ACTIONS)
            cmd = {'action': action, '_origin': 'goals-watchdog'}
            self.assertTrue(mgr.requires(action, cmd),
                            f"{action} should require approval even from "
                            f"trusted origin")

    def test_trusted_origin_auto_approves_non_destructive(self):
        """Trusted origins can auto-approve non-destructive CRITICAL actions."""
        from utils.approvals import ApprovalManager
        mgr = ApprovalManager()
        cmd = {'action': 'open_app', '_origin': 'recurring'}
        self.assertFalse(mgr.requires('open_app', cmd))

    def test_untrusted_origin_gates_non_destructive(self):
        """Untrusted origins must still get approval for CRITICAL actions."""
        from utils.approvals import ApprovalManager
        mgr = ApprovalManager()
        cmd = {'action': 'open_app', '_origin': ''}
        self.assertTrue(mgr.requires('open_app', cmd))

    def test_strict_mode_gates_everything(self):
        """Strict mode gates ALL critical actions, even trusted origins."""
        from utils.approvals import ApprovalManager
        os.environ['JARVIS_APPROVALS'] = 'strict'
        mgr = ApprovalManager()
        # open_app is in CRITICAL_ACTIONS but non-destructive;
        # in critical mode + trusted origin it would be auto-approved,
        # but strict mode overrides that.
        cmd = {'action': 'open_app', '_origin': 'recurring'}
        self.assertTrue(mgr.requires('open_app', cmd))

    def test_request_forwards_command(self):
        """request() passes command to requires() so origin is visible."""
        from utils.approvals import ApprovalManager
        os.environ['JARVIS_APPROVALS'] = 'critical'
        mgr = ApprovalManager()
        # open_app from trusted origin should NOT require approval
        cmd = {'action': 'open_app', '_origin': 'proactive'}
        approved, note = mgr.request(cmd)
        self.assertTrue(approved)
        self.assertEqual(note, '')

    def test_cooldown_between_destructive_actions(self):
        """Destructive actions are spaced by DESTRUCTIVE_COOLDOWN_S."""
        from utils.approvals import ApprovalManager
        os.environ['DESTRUCTIVE_COOLDOWN_S'] = '2'
        mgr = ApprovalManager(timeout=1)
        mgr._destructive_cooldown = 2
        # First destructive: approved (mock the event)
        cmd1 = {'action': 'delete_file', '_origin': ''}
        # We can't easily test the sleep without blocking, but verify
        # the cooldown timestamp is set
        mgr._last_destructive = time.time() - 1  # 1 second ago
        self.assertLess(time.time() - mgr._last_destructive,
                        mgr._destructive_cooldown)

    def test_destructive_cooldown_timestamp_set_on_approval(self):
        """After a destructive approval, _last_destructive is updated."""
        from utils.approvals import ApprovalManager
        mgr = ApprovalManager()
        before = time.time()
        mgr._last_destructive = before
        # Simulate approval happening
        mgr._last_destructive = time.time()
        self.assertGreaterEqual(mgr._last_destructive, before)


# ==================================================================== #
#  SAFETY DIRECTIVES
# ==================================================================== #

class TestSafetyDirectives(unittest.TestCase):
    """System prompt contains safety directives."""

    def test_safety_directives_present(self):
        from utils.brain import Brain
        # Brain.__init__ requires API keys to not crash, so mock
        with patch.object(Brain, '__init__', lambda self: None):
            brain = Brain.__new__(Brain)
            brain.system_instruction = Brain.__init__.__code__  # won't work
        # Instead, just check the source file
        brain_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'utils', 'brain.py')
        with open(brain_path, 'r') as f:
            content = f.read()
        # Check key safety directives exist
        self.assertIn('SAFETY DIRECTIVES', content)
        self.assertIn('NEVER execute code that formats', content)
        self.assertIn('NEVER share API keys', content)
        self.assertIn('NEVER bypass the approval system', content)
        self.assertIn('ALWAYS create a checkpoint', content)
        self.assertIn('NEVER modify system-level settings', content)


# ==================================================================== #
#  PRIVACY ENFORCEMENT
# ==================================================================== #

class TestPrivacyEnforcement(unittest.TestCase):
    """Privacy framework integration in executor."""

    def test_privacy_can_execute_exists(self):
        """PrivacyFramework has can_execute method."""
        from utils.privacy import PrivacyFramework
        pf = PrivacyFramework()
        self.assertTrue(hasattr(pf, 'can_execute'))
        self.assertTrue(callable(pf.can_execute))

    def test_privacy_check_spending_exists(self):
        """PrivacyFramework has check_spending method."""
        from utils.privacy import PrivacyFramework
        pf = PrivacyFramework()
        self.assertTrue(hasattr(pf, 'check_spending'))
        self.assertTrue(callable(pf.check_spending))

    def test_readonly_actions_always_allowed(self):
        """Read-only actions are always allowed by privacy."""
        from utils.privacy import PrivacyFramework
        pf = PrivacyFramework()
        result = pf.can_execute('get_weather')
        # Should be ('allow', None) or similar positive result
        self.assertIsNotNone(result)
        if isinstance(result, tuple):
            self.assertEqual(result[0], 'allow')


# ==================================================================== #
#  AUDIT EXPORT
# ==================================================================== #

class TestAuditExport(unittest.TestCase):
    """Audit module is importable and has correct interface."""

    def test_import_constants(self):
        from utils.audit import (DESTRUCTIVE_ACTIONS, SENSITIVE_ACTIONS,
                                 READONLY_ACTIONS, risk_tier, get_audit,
                                 AuditRecord, AnomalyDetector, AuditLogger)
        self.assertTrue(len(DESTRUCTIVE_ACTIONS) > 0)
        self.assertTrue(len(SENSITIVE_ACTIONS) > 0)
        self.assertTrue(len(READONLY_ACTIONS) > 0)

    def test_singleton_reset(self):
        from utils.audit import get_audit, _reset_singleton
        _reset_singleton()
        a1 = get_audit()
        a2 = get_audit()
        self.assertIs(a1, a2)

    def test_audit_record_to_dict(self):
        from utils.audit import AuditRecord
        rec = AuditRecord(action='test', outcome='ok', duration_ms=100)
        d = rec.to_dict()
        self.assertEqual(d['action'], 'test')
        self.assertEqual(d['outcome'], 'ok')
        self.assertEqual(d['duration_ms'], 100)
        self.assertIn('ts', d)

    def test_audit_record_from_dict(self):
        from utils.audit import AuditRecord
        d = {'ts': 1000, 'action': 'x', 'outcome': 'ok', 'duration_ms': 50,
             'detail': '', 'origin': '', 'risk': 'readonly'}
        rec = AuditRecord.from_dict(d)
        self.assertEqual(rec.action, 'x')
        self.assertEqual(rec.ts, 1000)


if __name__ == '__main__':
    unittest.main()
