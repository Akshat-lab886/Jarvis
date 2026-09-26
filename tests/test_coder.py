"""Unit tests for utils.coder.Coder — the sandboxed Python execution engine.

Covers three Tier A surface areas that previously had 0% test coverage:

- validate_safety: the denylist that blocks catastrophic shell/process
  primitives. Regression-guarded so a typo can't silently re-open an
  execution vector.
- ast_scan: the AST pre-execution scanner (defense-in-depth) that blocks
  forbidden imports and dangerous attributes statically. This is the
  *real* safety gate that runs even when the docker sandbox isn't
  available (local-engine fallback), so it must not regress.
- execute_with_retry: the retry wrapper that (a) previously NEVER retried
  despite its name and (b) now must avoid infinite loops on persistent
  timeouts / deterministic script failures.

Tests patch the filesystem-backed workspace and _run_python so nothing
real executes; the retry/loop guards are verified deterministically.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.coder import Coder, _is_timeout


class TestValidateSafety(unittest.TestCase):
    """The keyword denylist — catastrophic primitives must stay blocked."""

    def setUp(self):
        self.coder = Coder(workspace_dir="/tmp/jarvis_test_coder")

    def test_safe_code_passes(self):
        self.assertEqual(self.coder.validate_safety("print('hello')"),
                         (True, "Safe"))
        self.assertEqual(self.coder.validate_safety("x = 1 + 2"),
                         (True, "Safe"))

    def test_fork_bomb_blocked(self):
        ok, msg = self.coder.validate_safety(":(){ :|:& };:")
        self.assertFalse(ok)
        self.assertIn("Safety Protocol", msg)

    def test_rm_rf_root_blocked(self):
        ok, msg = self.coder.validate_safety("import os; os.system('rm -rf /')")
        self.assertFalse(ok)

    def test_mkf_blocked(self):
        ok, msg = self.coder.validate_safety("os.system('mkfs /dev/sda')")
        self.assertFalse(ok)

    def test_eval_exec_blocked(self):
        """String-built eval/exec let generated code escape the denylist
        via eval('__imp'+'ort__("os")...'). Must be blocked outright."""
        ok, _ = self.coder.validate_safety("eval('2+2')")
        self.assertFalse(ok)
        ok, _ = self.coder.validate_safety("exec('x=1')")
        self.assertFalse(ok)

    def test_dynamic_import_blocked(self):
        ok, _ = self.coder.validate_safety("__import__('os').system('id')")
        self.assertFalse(ok)

    def test_subprocess_rm_blocked(self):
        ok, _ = self.coder.validate_safety("subprocess.run(['rm', '-rf', '/'], check=True)")
        self.assertFalse(ok)

    def test_os_popen_blocked(self):
        ok, _ = self.coder.validate_safety("os.popen('whoami').read()")
        self.assertFalse(ok)


class TestAstScan(unittest.TestCase):
    """Static AST gate — the real safety net when docker is unavailable."""

    def setUp(self):
        self.coder = Coder(workspace_dir="/tmp/jarvis_test_coder2")

    def test_clean_code_no_violations(self):
        self.assertEqual(self.coder.ast_scan("x = 1 + 2\nprint(x)"), [])

    def test_forbidden_import_subprocess(self):
        violations = self.coder.ast_scan("import subprocess")
        self.assertTrue(violations)
        self.assertTrue(any("subprocess" in v for v in violations))

    def test_forbidden_import_socket(self):
        violations = self.coder.ast_scan("from socket import socket")
        self.assertTrue(violations)

    def test_dangerous_attr_system(self):
        violations = self.coder.ast_scan("import os\nos.system('id')")
        self.assertTrue(any("system" in v for v in violations))

    def test_syntax_error_returned_not_crashed(self):
        """ast_scan must surface syntax errors as violations, not raise."""
        violations = self.coder.ast_scan("def f(:")
        self.assertTrue(violations)
        self.assertIn("syntax error", violations[0])

    def test_strict_scan_env(self):
        with patch.dict(os.environ, {"JARVIS_STRICT_SCAN": "0"}):
            self.assertEqual(self.coder.ast_scan("import subprocess"), [])


class TestIsTimeout(unittest.TestCase):
    def test_timeout_returncode_minus_one(self):
        self.assertTrue(_is_timeout(
            {'returncode': -1, 'stderr': 'Code execution timed out (limit: 30s).'}))
    def test_timeout_returncode_124(self):
        self.assertTrue(_is_timeout(
            {'returncode': 124, 'stderr': 'Execution timeout'}))
    def test_app_crash_not_timeout(self):
        self.assertFalse(_is_timeout(
            {'returncode': 1, 'stderr': 'ZeroDivisionError'}))
    def test_success_not_timeout(self):
        self.assertFalse(_is_timeout({'returncode': 0, 'stderr': ''}))


class TestExecuteWithRetry(unittest.TestCase):
    """The retry loop guard semantics — the most loop-prone path.

    Per agentic-coding guidance (Addy Ossani "80% Problem", arXiv
    2607.01641 "Uncovering Infinite Agentic Loops"), retrying must NEVER
    re-run deterministic failures, and must NOT spin on persistent
    timeouts. These tests verify both invariants directly.
    """

    def setUp(self):
        self.coder = Coder(workspace_dir="/tmp/jarvis_test_coder3")

    def test_immediate_success_no_retry(self):
        """A successful first run must report max_retries remaining and
        never invoke the runner again."""
        self.coder._run_python = MagicMock(
            return_value={'success': True, 'returncode': 0,
                          'stdout': '42', 'stderr': ''})
        res = self.coder.execute_with_retry("print(42)", max_retries=2)
        self.assertTrue(res['success'])
        self.assertEqual(res['retries_remaining'], 2)
        self.assertEqual(self.coder._run_python.call_count, 1)

    def test_deterministic_failure_does_not_retry(self):
        """A nonzero exit (real script crash) is NOT transient — retrying
        identical bytes is pointless and risks an infinite loop."""
        self.coder._run_python = MagicMock(
            return_value={'success': False, 'returncode': 1,
                          'stdout': '', 'stderr': 'ZeroDivisionError: div by zero'})
        res = self.coder.execute_with_retry("1/0", max_retries=3)
        self.assertFalse(res['success'])
        self.assertEqual(self.coder._run_python.call_count, 1)
        self.assertEqual(res['retries_remaining'], 3)

    def test_transient_infra_error_retries_then_succeeds(self):
        """subprocess-level errors (rc=-1, no 'timed out' text) are
        considered transient and DO get retried."""
        self.coder._run_python = MagicMock(
            side_effect=[
                {'success': False, 'returncode': -1,
                 'stdout': '', 'stderr': 'OSError: [Errno 11] Resource temporarily unavailable'},
                {'success': True, 'returncode': 0,
                 'stdout': 'ok', 'stderr': ''},
            ])
        res = self.coder.execute_with_retry("print('ok')", max_retries=2)
        self.assertTrue(res['success'])
        self.assertEqual(self.coder._run_python.call_count, 2)
        self.assertEqual(res['retries_remaining'], 1)

    def test_persistent_timeout_does_not_spin(self):
        """A code-level hang that times out twice must NOT retry up to
        max_retries — that would burn max_retries*timeout seconds
        re-running the same infinite loop. The guard caps at one retry."""
        to = {'success': False, 'returncode': -1,
              'stdout': '', 'stderr': 'Code execution timed out (limit: 30s).'}
        self.coder._run_python = MagicMock(return_value=to)
        res = self.coder.execute_with_retry("while True: pass",
                                            max_retries=5, timeout=1)
        self.assertFalse(res['success'])
        # One initial + ONE retry (persistent-timeout guard stops further
        # retries so a self-hanging script can't burn max_retries*timeout).
        self.assertEqual(self.coder._run_python.call_count, 2)
        # retries_remaining reflects how many of the 5 budget were NOT
        # consumed (2 runs = 1 retry used → 4 remaining).
        self.assertEqual(res['retries_remaining'], 4)

    def test_retries_remaining_never_negative(self):
        """Even if max_retries is exceeded, the field must clamp to 0."""
        to = {'success': False, 'returncode': -1,
              'stdout': '', 'stderr': 'subprocess spawn error'}
        self.coder._run_python = MagicMock(return_value=to)
        res = self.coder.execute_with_retry("x = 1", max_retries=1)
        self.assertEqual(res['retries_remaining'], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
