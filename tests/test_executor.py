"""
Offline tests for utils.executor.JarvisExecutor.execute_command defensive
layers — none of which are exercised by test_agent_loop (which uses a
FakeExecutor stub). Covers:

  - Unknown action: returns "Unknown action: {action}" (no crash).
  - Exception mid-action: returns "System error during execution: {e}",
    never lets an exception escape execute_command.
  - Empty action: routes to the 'chat' bucket (original_text memory
    capture).
  - ui_callback receives ai_text events on unknown-action path.
  - Audit trail is invoked even when the action raises.
  - Privacy denials surface the "⛔ Action '{a}' denied" message.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _NoAudioMouth:
    """Mouth stand-in that records speaks without pygame/audio."""
    def __init__(self):
        self.said = []
        self.suppress = False
    def speak(self, text, channel=None):
        if not self.suppress:
            self.said.append(text)


class _AllowPrivacy:
    """Privacy gate that always allows."""
    def can_execute(self, action, command):
        return ('allow', None)


class _DenyPrivacy:
    """Privacy gate that always denies."""
    def can_execute(self, action, command):
        return ('deny', 'too risky')


class _CapturingAuditor:
    """AuditLogger stand-in that records every .log() call."""
    def __init__(self):
        self.calls = []
    def log(self, action, command=None, outcome='ok', duration_ms=0,
            detail=None, origin=''):
        self.calls.append({'action': action, 'command': command,
                           'outcome': outcome, 'detail': detail,
                           'origin': origin})
    def flush(self):
        return self.calls


_HEAVY = ["SmartHome", "Coder", "SkillRegistry", "ToolRegistry",
          "CommandLog", "TodoList", "NotePad", "RelationshipManager",
          "FridgeVision", "MeetingTranscriber", "EpisodicMemory",
          "Memory", "Mouth", "PrivacyFramework"]


def _build_executor(privacy_cls=_AllowPrivacy):
    """Build a real JarvisExecutor with heavy deps stubbed, then replace
    mouth/episodic with no-ops and wire get_audit to a capturing auditor."""
    auditor = _CapturingAuditor()
    patches = [patch(f"utils.executor.{name}") for name in _HEAVY]
    patches.append(patch("utils.executor.get_audit", return_value=auditor))
    for p in patches:
        p.start()
    # Patch PrivacyFramework to the chosen policy class last.
    priv_p = patch("utils.executor.PrivacyFramework", privacy_cls)
    priv_p.start()
    try:
        from utils.executor import JarvisExecutor
        ex = JarvisExecutor()
    finally:
        priv_p.stop()
    ex.mouth = _NoAudioMouth()
    ex.privacy = privacy_cls()
    ex.auditor = auditor
    ex.episodic = MagicMock()
    ex.episodic.count.return_value = 1
    ex.episodic.auto_capture = MagicMock()
    ex.episodic.consolidate = MagicMock()
    # Re-point get_audit inside execute_command to our auditor.
    patch("utils.executor.get_audit", return_value=auditor).start()
    return ex, auditor


class TestExecutorDefensive(unittest.TestCase):

    def test_unknown_action_returns_error_string(self):
        ex, _ = _build_executor()
        res = ex.execute_command({'action': 'does_not_exist_xyz'},
                                 brain=MagicMock())
        self.assertEqual(res, "Unknown action: does_not_exist_xyz")

    def test_unknown_action_calls_ui_callback(self):
        ex, _ = _build_executor()
        seen = []
        res = ex.execute_command(
            {'action': 'ghost_action'}, brain=MagicMock(),
            ui_callback=lambda ev, data: seen.append((ev, data)))
        self.assertEqual(res, "Unknown action: ghost_action")
        self.assertTrue(any(ev == 'ai_text' and 'ghost_action' in data.get('text', '')
                            for ev, data in seen))

    def test_privacy_gate_exception_is_fail_closed(self):
        """A privacy gate that throws must fail-closed (deny), not crash the
        executor or leak a generic system-error string."""
        class _ExplodingPrivacy:
            def can_execute(self, action, command):
                raise RuntimeError("gate exploded")
        ex, auditor = _build_executor(_ExplodingPrivacy)
        res = ex.execute_command({'action': 'chat'}, brain=MagicMock())
        # Privacy gate has its own inner except -> fail-closed denial.
        self.assertIn("privacy gate error", res)
        self.assertIn("denied", res)
        log = [a for a in auditor.calls if a['action'] == 'chat'][0]
        self.assertEqual(log['outcome'], 'denied')
        self.assertIn('gate exploded', log['detail'])
        self.assertTrue(ex.mouth.said)  # result spoken to user

    def test_unknown_action_still_audited(self):
        ex, auditor = _build_executor()
        ex.execute_command({'action': 'mystery_tool'}, brain=MagicMock())
        self.assertTrue(any(a['action'] == 'mystery_tool' for a in auditor.calls))
        log = [a for a in auditor.calls if a['action'] == 'mystery_tool'][0]
        self.assertEqual(log['outcome'], 'error')

    def test_action_handler_exception_wrapped_as_system_error(self):
        """An unexpected exception in a known action body is caught by the
        outer try/except and returned as 'System error...', never raised.

        We use todo_list (no inner try/except) and patch tasks.list to
        blow up — proving the outer guard wraps handler exceptions."""
        ex, auditor = _build_executor()
        # TodoList is a MagicMock in _build_executor; make its list() raise.
        ex.tasks.list.side_effect = ValueError("kaboom in task list")
        res = ex.execute_command({'action': 'todo_list'},
                                 brain=MagicMock())
        self.assertIn("System error during execution", res)
        self.assertIn("kaboom", res)
        # Even the system-error path is audited.
        log = [a for a in auditor.calls if a['action'] == 'todo_list'][0]
        self.assertEqual(log['outcome'], 'error')
        self.assertIn('kaboom', log['detail'])

    def test_empty_action_treated_as_chat(self):
        """action missing/empty -> chat bucket: memory capture fires."""
        ex, _ = _build_executor()
        ex.execute_command({}, brain=MagicMock(), original_text="hello jarvis")
        self.assertTrue(ex.episodic.auto_capture.called)


class TestPrivacyDenial(unittest.TestCase):

    def test_denied_action_returns_blocked_message(self):
        ex, _ = _build_executor(_DenyPrivacy)
        res = ex.execute_command({'action': 'delete_file'},
                                 brain=MagicMock(), ui_callback=lambda *a: None)
        self.assertIn("denied by privacy policy", res)


if __name__ == "__main__":
    unittest.main()
