"""
Guardrails, learning, memory-layer and execution-hardening tests:
circuit breaker, session budget, HITL approvals, transcript FTS archive,
event bus, self-learning, privilege separation, AST scanner, hibernation.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import datetime
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_tmp():
    return tempfile.mkdtemp(prefix="jarvis_guard_")


# ====================================================================== #
# G1 — Circuit breaker
# ====================================================================== #

class TestCircuitBreaker(unittest.TestCase):
    def setUp(self):
        self.tmp = _make_tmp()
        from utils.circuit_breaker import CircuitBreaker
        self.cls = CircuitBreaker
        self.breaker = CircuitBreaker(base_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch.dict(os.environ, {'JARVIS_DISABLE_VECTOR': '1'})
    @patch('utils.circuit_breaker.CircuitBreaker._check_providers')
    def test_ok_when_all_pass(self, mock_providers):
        mock_providers.return_value = (True, False, "configured: Groq")
        report = self.breaker.validate(force=True)
        self.assertEqual(report.status, 'ok')

    @patch('utils.circuit_breaker.CircuitBreaker._check_providers')
    def test_trips_without_providers(self, mock_providers):
        mock_providers.return_value = (False, True, "no LLM API key found")
        report = self.breaker.validate(force=True)
        self.assertEqual(report.status, 'tripped')
        self.assertFalse(self.breaker.allow_llm_spend())

    def test_degraded_on_low_disk(self):
        # Real providers exist in .env; force disk check failure only
        with patch.object(self.cls, '_check_disk',
                          return_value=(False, False, "low disk: 10MB free")):
            report = self.breaker.validate(force=True)
        self.assertIn(report.status, ('degraded', 'tripped'))

    def test_throttling(self):
        r1 = self.breaker.validate(force=True)
        before = r1.checked_at
        r2 = self.breaker.validate()          # within cooldown → cached
        self.assertEqual(r1.checked_at, r2.checked_at)


# ====================================================================== #
# G2 — Session budget
# ====================================================================== #

class TestBudget(unittest.TestCase):
    def _budget(self, usd='1.0', tokens='1000'):
        from utils.budget import Budget
        with patch.dict(os.environ,
                        {'JARVIS_BUDGET_USD': usd,
                         'JARVIS_BUDGET_TOKENS': tokens}):
            b = Budget()
            b.warn_pct = 50
            return b

    def test_heuristic_token_estimation_and_cost(self):
        b = self._budget(usd='100')
        # gpt-oss-20b priced 0.10/1k → 800 chars ≈ 200 tok ≈ $0.02
        b.record(model='openai/gpt-oss-20b', input_text='x' * 400,
                 output_text='y' * 400)
        self.assertEqual(b.spent_tokens, 200)
        self.assertAlmostEqual(b.spent_usd, 0.02, places=4)

    def test_usage_object_preferred(self):
        b = self._budget(usd='100')
        usage = type('U', (), {'prompt_tokens': 100,
                               'completion_tokens': 50})()
        b.record(model='gemini-flash', input_text='', output_text='',
                 usage=usage)
        self.assertEqual(b.spent_tokens, 150)

    def test_warn_then_halt(self):
        b = self._budget(usd='0.05', tokens='100000')   # tiny USD cap
        price_model = 'llama'                            # 0.20/1k → 250tok=$0.05
        with patch.dict(os.environ, {'JARVIS_WARN_AT_PCT': '50'}):
            b.warn_pct = 50
            b.record(model=price_model, input_text='a' * 500,
                     output_text='')               # ~$0.025 → 50% warn
            self.assertTrue(b.warned)
            b.guard()                              # under limit → fine
            b.record(model=price_model, input_text='a' * 500,
                     output_text='')               # crosses 100%
            self.assertTrue(b.halted)
            b.record(model=price_model, input_text='a' * 10,
                     output_text='')
            with self.assertRaises(Exception):
                b.guard()

    def test_reset_clears_state(self):
        b = self._budget(usd='100')
        b.record(model='qwen', input_text='x' * 100, output_text='')
        b.reset()
        self.assertEqual(b.spent_tokens, 0)
        self.assertEqual(b.status()['halted'], False)


# ====================================================================== #
# G3 — Approvals
# ====================================================================== #

class TestApprovals(unittest.TestCase):
    def setUp(self):
        from utils.approvals import ApprovalManager
        self.mgr_cls = ApprovalManager

    def test_requires_gated_by_env(self):
        with patch.dict(os.environ, {'JARVIS_APPROVALS': 'off'}):
            mgr = self.mgr_cls(timeout=1)
            self.assertFalse(mgr.requires('send_email'))
        with patch.dict(os.environ, {'JARVIS_APPROVALS': 'critical'}):
            mgr = self.mgr_cls(timeout=1)
            self.assertTrue(mgr.requires('send_email'))
            self.assertFalse(mgr.requires('get_weather'))

    def test_approve_flow_unblocks(self):
        with patch.dict(os.environ, {'JARVIS_APPROVALS': 'critical'}):
            emitted = []
            mgr = self.mgr_cls(timeout=5,
                               emit_fn=lambda e, p: emitted.append(p))

            result = {}

            def blocker():
                result['approved'] = mgr.request({'action': 'send_email',
                                                  'recipient': 'x@y.com'})

            t = threading.Thread(target=blocker, daemon=True)
            t.start()
            deadline = time.time() + 3
            while mgr.pending_count() == 0 and time.time() < deadline:
                time.sleep(0.02)
            aid = next(iter(mgr._pending))
            self.assertTrue(mgr.resolve(aid, True))
            t.join(timeout=5)
            self.assertTrue(result['approved'])
            self.assertEqual(emitted[0]['action'], 'send_email')

    def test_timeout_denies(self):
        with patch.dict(os.environ, {'JARVIS_APPROVALS': 'critical'}):
            mgr = self.mgr_cls(timeout=0.3)
            approved, note = mgr.request({'action': 'delete_file',
                                          'target': 'important.txt'})
            self.assertFalse(approved)
            self.assertIn('timed out', note)

    def test_summary_includes_fields(self):
        from utils.approvals import _summarize
        s = _summarize({'action': 'dev_command', 'project': 'webapp',
                        'command': 'npm install'})
        self.assertIn('dev_command', s)
        self.assertIn('npm install', s)


# ====================================================================== #
# M1 — Transcript FTS archive
# ====================================================================== #

class TestTranscriptArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = _make_tmp()
        from utils.transcript_search import TranscriptArchive
        self.arc = TranscriptArchive(
            db_path=os.path.join(self.tmp, 't.db'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_index_and_search_roundtrip(self):
        self.arc.index_exchange("what is the wifi password",
                                "The guest wifi password is falcon99")
        self.arc.index_exchange("book a dentist appointment",
                                "Scheduled Tuesday at 3pm")
        hits = self.arc.search("wifi password", limit=3)
        self.assertTrue(hits)
        self.assertIn('falcon99', json.dumps(hits))
        misses = self.arc.search("quantum entanglement lab", limit=3)
        self.assertEqual(misses, [])

    def test_count_and_snippet(self):
        self.arc.index_exchange("alpha beta gamma", "delta epsilon")
        self.assertEqual(self.arc.count(), 1)
        hits = self.arc.search("alpha")
        self.assertIn('snippet', hits[0])

    def test_malformed_query_safe(self):
        self.arc.index_exchange("hello world", "hi")
        # quotes/operators must not crash
        self.assertIsInstance(self.arc.search('"unclosed AND OR'), list)


# ====================================================================== #
# M2 — Event bus
# ====================================================================== #

class TestEventBus(unittest.TestCase):
    def setUp(self):
        from utils.event_bus import EventBus
        self.bus = EventBus()

    def test_sync_publish_returns_handler_result(self):
        seen = []
        self.bus.handler = lambda ev: f"handled:{ev.text}"
        result = self.bus.publish(
            self.bus.from_dashboard("ping", reply=seen.append))
        self.assertEqual(result, "handled:ping")
        self.assertEqual(seen, ["handled:ping"])

    def test_background_publish_replies_async(self):
        done = threading.Event()
        box = []

        def handler(ev):
            box.append(ev.text)
            return "ok-bg"

        self.bus.handler = handler
        self.bus.publish(self.bus.from_webhook({'text': 'async'}),
                         background=True)
        deadline = time.time() + 3
        while not box and time.time() < deadline:
            time.sleep(0.02)

    def test_handler_exception_responds_error(self):
        got = []
        def boom(ev):
            raise RuntimeError("core exploded")
        self.bus.handler = boom
        self.bus.publish(self.bus.from_telegram_text("hi",
                                                     reply=got.append))
        self.assertTrue(got and 'Processing failed' in got[0])

    def test_observer_failure_does_not_block(self):
        calls = []
        self.bus.observers.append(lambda ev: 1 / 0)
        self.bus.handler = lambda ev: calls.append(1) or "fine"
        result = self.bus.publish(self.bus.from_dashboard("x"))
        self.assertEqual(result, "fine")

    def test_normalizers_shape_events(self):
        ev = self.bus.from_telegram_photo("caption", image_path='/i.jpg',
                                          chat_id=42)
        self.assertEqual((ev.source, ev.kind), ('telegram', 'photo'))
        self.assertEqual(ev.meta['image_path'], '/i.jpg')
        ev2 = self.bus.from_webhook(b'{"text": "raw bytes"}')
        self.assertEqual(ev2.text, "raw bytes")


# ====================================================================== #
# L1 — Self-learning
# ====================================================================== #

class TestSelfLearning(unittest.TestCase):
    def setUp(self):
        self.tmp = _make_tmp()
        self.patcher = patch.object(
            __import__('utils.self_learning', fromlist=['x']),
            'LESSON_FILE', os.path.join(self.tmp, 'lessons.md'))
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_add_dedupe_and_render(self):
        from utils import self_learning as sl
        self.assertTrue(sl.add_lesson("Prefer requests over urllib.", 'ops'))
        self.assertFalse(sl.add_lesson("prefer requests over urllib.",
                                       'ops'))     # dup
        block = sl.render_lessons()
        self.assertIn("SELF-LEARNED HEURISTICS", block)
        self.assertIn("requests", block)

    def test_cap_prunes_oldest(self):
        from utils import self_learning as sl
        sl.MAX_LESSONS = 5
        for i in range(8):
            sl.add_lesson(f"lesson number {i} unique text", scope='t')
        lessons = sl._load_lessons()
        self.assertLessEqual(len(lessons), 5)
        self.assertIn("lesson number 7", [l['text'] for l in
                                          lessons][-1])

    def test_disabled_flag_blocks_everything(self):
        from utils import self_learning as sl
        with patch.dict(os.environ, {'JARVIS_AUTO_LEARN': '0'}):
            self.assertFalse(sl.add_lesson("should not persist"))
            self.assertEqual(sl.render_lessons(), "")

    def test_extract_skill_saves_on_planner_json(self):
        from utils import self_learning as sl
        from utils.skills import SkillRegistry
        reg = SkillRegistry(skills_dir=os.path.join(self.tmp, 'skills'))

        class FakeBrain:
            def complete(self, prompt, agent=None, **kw):
                if agent == 'planner':
                    return json.dumps({
                        "name": "double_it",
                        "description": "Doubles a number",
                        "params": {"n": "number"},
                        "template": "print({{n}} * 2)"})
                return ""

        note = sl.extract_and_save_skill(FakeBrain(), "math task",
                                         "compute doubling",
                                         "print(x * 2)", reg)
        self.assertIn("learned new skill", note)
        skill = reg.get_skill("double_it")
        self.assertIsNotNone(skill)
        self.assertTrue(skill['metadata']['auto_generated'])

    def test_extract_skill_declines_nonreusable(self):
        from utils import self_learning as sl
        from utils.skills import SkillRegistry
        reg = SkillRegistry(skills_dir=os.path.join(self.tmp, 'skills'))

        class FakeBrain:
            def complete(self, prompt, agent=None, **kw):
                return "NO"

        self.assertEqual(sl.extract_and_save_skill(
            FakeBrain(), "one-off crunch", "step", "print(1)", reg), '')


class TestPrivilegeSeparation(unittest.TestCase):
    def test_only_operator_has_tool_privileges(self):
        from utils.agents import get_profile
        privileged = ['operator']
        unprivileged = ['planner', 'researcher', 'coder', 'critic',
                        'synthesizer']
        for name in privileged:
            self.assertTrue(get_profile(name).tools_allowed, name)
        for name in unprivileged:
            self.assertFalse(get_profile(name).tools_allowed, name)


# ====================================================================== #
# S1 — AST scanner
# ====================================================================== #

class TestAstScanner(unittest.TestCase):
    def setUp(self):
        from utils.coder import Coder
        self.scan = Coder.ast_scan

    def test_clean_code_passes(self):
        code = ("import math\n"
                "def area(r):\n"
                "    return math.pi * r * r\n"
                "print(area(2))\n")
        self.assertEqual(self.scan(code), [])

    def test_forbidden_imports_flagged(self):
        code = "import socket\nimport subprocess\nprint('boom')"
        violations = self.scan(code)
        self.assertTrue(any('socket' in v for v in violations))
        self.assertTrue(any('subprocess' in v for v in violations))

    def test_eval_and_dunder_flagged(self):
        code = "eval('2+2')\nobj = []\nprint(obj.__class__)"
        violations = self.scan(code)
        self.assertTrue(any('eval' in v for v in violations))
        self.assertTrue(any('__class__' in v for v in violations))

    def test_scanner_can_be_disabled(self):
        with patch.dict(os.environ, {'JARVIS_STRICT_SCAN': '0'}):
            self.assertEqual(self.scan("import socket"), [])

    def test_run_python_blocks_scanned_script(self):
        tmp = _make_tmp()
        try:
            from utils.coder import Coder
            coder = Coder(workspace_dir=os.path.join(tmp, 'ws'))
            res = coder.execute_with_retry(
                "import socket\nsocket.socket()")
            self.assertFalse(res['success'])
            self.assertIn('AST scanner', res['stderr'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ====================================================================== #
# S2 — Hibernation
# ====================================================================== #

class TestHibernation(unittest.TestCase):
    def _manager(self, idle_minutes=30):
        from utils.hibernation import HibernationManager
        return HibernationManager(idle_minutes=idle_minutes)

    def test_wakes_on_touch(self):
        mgr = self._manager()
        state = {'suspended': 0, 'woke': 0}
        mgr.register_service(
            'svc',
            lambda: state.__setitem__('suspended', state['suspended'] + 1),
            lambda: state.__setitem__('woke', state['woke'] + 1))
        mgr.last_activity = time.time() - 3600   # long idle
        mgr._suspend_all()
        self.assertTrue(mgr.hibernating)
        self.assertEqual(state['suspended'], 1)
        mgr.touch()
        self.assertFalse(mgr.hibernating)
        self.assertEqual(state['woke'], 1)

    def test_monitor_loop_suspends_after_idle(self):
        # Monitor polls at max(2.0, idle/4) s, so the first suspend for a
        # ~0.6s idle window lands at ~2s.  Wait on the real signal
        # (mgr.hibernating) rather than a brittle callback count, and give
        # the daemon thread headroom under full-suite GIL contention.
        mgr = self._manager(idle_minutes=0.01)   # ~0.6s
        counts = {'n': 0}
        mgr.register_service(
            's',
            lambda: counts.__setitem__('n', counts['n'] + 1),
            lambda: None)
        mgr.start()
        try:
            deadline = time.time() + 8
            while not mgr.hibernating and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(mgr.hibernating)
            self.assertGreaterEqual(counts['n'], 1)
        finally:
            mgr.stop()

    def test_disabled_when_zero_minutes(self):
        mgr = self._manager(idle_minutes=0)
        mgr.start()
        self.assertIsNone(mgr._thread)
        mgr.stop()


# ====================================================================== #

if __name__ == '__main__':
    print("=" * 60)
    print("JARVIS GUARDRAILS / LEARNING / MEMORY-LAYER TESTS")
    print("=" * 60)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromModule(
        sys.modules[__name__]))
    print("\n" + "=" * 60)
    if result.wasSuccessful():
        print(f"ALL {result.testsRun} TESTS PASSED ✅")
    else:
        for test, trace in result.failures + result.errors:
            print(f"  ❌ {test}\n{trace[:300]}")
    print("=" * 60)
