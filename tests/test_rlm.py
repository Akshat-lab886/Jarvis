"""
Offline tests for the RLM (Recursive Language Model) core:
hierarchical memory, consolidation, recursive recall, reflection, the
reasoning layer (planner/critic/effort/verification), the persistent
task plan, sub-agent delegation, parallel tool execution, and their
integration into the agent loop.

No network, no server: a scripted fake brain plays the LLM, fakes
stand in for the router/executor (same pattern as test_agent_loop.py),
and every store uses temp dirs.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Hard-block any real provider calls from unit tests (.env may carry keys)
from config import Config
Config.GOOGLE_API_KEY = None

# Keep self-learning artifacts OUT of the real store during tests
import utils.self_learning as _sl
_sl.LESSON_FILE = os.path.join(
    tempfile.mkdtemp(prefix='jarvis_rlm_t_'), 'lessons.md')

from utils.llm.providers.base import ChatResult, ToolCall, Usage  # noqa: E402
from utils.agent_loop import (                                    # noqa: E402
    AgentLoop, TOOL_SPECS, MAX_STEPS_DEFAULT, TOOL_OUTPUT_CAP,
    FORBIDDEN_TOOLS,
)
from utils.rlm.memory import RecursiveMemory                       # noqa: E402
from utils.rlm.plan_state import PlanState                        # noqa: E402
from utils.rlm import reasoner                                    # noqa: E402
from utils.llm.keystore import Keystore                           # noqa: E402
from utils.llm.providers.anthropic_api import AnthropicProvider   # noqa: E402


def _make_tmp():
    return tempfile.mkdtemp(prefix="jarvis_rlm_")


# --------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------- #

class FakeBrain:
    """Scripted Brain.complete() stand-in keyed by agent profile."""

    def __init__(self, replies=None, default=None):
        self.replies = dict(replies or {})     # agent -> text | callable
        self.default = default
        self.calls = []

    def complete(self, prompt, system=None, timeout=60, max_tokens=None,
                 temperature=None, agent=None):
        self.calls.append(agent or 'default')
        reply = self.replies.get(agent, self.default)
        if callable(reply):
            reply = reply(prompt)
        return reply


class FakeExecutor:
    def __init__(self):
        self.mouth = type('M', (), {'suppress': False})()
        self.calls = []

    def execute_command(self, command, brain, original_text=None,
                        ui_callback=None):
        self.calls.append(dict(command))
        act = command.get('action')
        if act == 'boom':
            raise RuntimeError("exploded")
        if act == 'get_weather':
            return "Sunny, 25C in Delhi."
        return f"{act}: ok"


class FakeRouter:
    """Plays back scripted results, one per chat() call."""

    def __init__(self, script, wrapup=None):
        self.script = list(script)
        self.wrapup = wrapup
        self.providers = {'groq': object()}
        self.seen_messages = []
        self.lock = threading.Lock()

    def _chain(self, require=None, models=None):
        return [('groq', 'fake-model')]

    def chat(self, messages, *, require=None, models=None, tools=None,
             max_tokens=None, temperature=0.2, timeout=45, stream=False,
             purpose='test'):
        with self.lock:
            self.seen_messages.append(json.loads(json.dumps(
                [m for m in messages], default=str)))
            if tools is None and self.wrapup is not None:
                return self.wrapup
            result = self.script[len(self.seen_messages) - 1]
        if callable(result):
            result = result()
        return result


class StubBrain:
    def __init__(self, router, complete_replies=None):
        self.router = router
        self.history_log = []
        self.complete_replies = complete_replies or {}
        self.complete_calls = []

    def _append_history(self, user, ai):
        self.history_log.append((user, ai))

    def complete(self, prompt, system=None, timeout=60, max_tokens=None,
                 temperature=None, agent=None):
        self.complete_calls.append(agent or 'default')
        reply = self.complete_replies.get(agent)
        if callable(reply):
            reply = reply(prompt)
        return reply


def text_result(text):
    return ChatResult(text=text, finish_reason='stop', provider='groq',
                      model='fake-model', usage=Usage(10, 5))


def tool_result(name, arguments='{}', cid='c1'):
    return ChatResult(text='', finish_reason='tool_calls', provider='groq',
                      model='fake', tool_calls=[
                          ToolCall(id=cid, name=name, arguments=arguments)])


def _empty_plan(tmp):
    """A real PlanState pointed at a temp file (never the repo's)."""
    return PlanState(file_path=os.path.join(tmp, 'plan.json'))


# ====================================================================== #
# RecursiveMemory — hierarchy, consolidation, recall, reflection
# ====================================================================== #

class TestObserve(unittest.TestCase):

    def setUp(self):
        self.tmp = _make_tmp()
        self.rlm = RecursiveMemory(
            data_file=os.path.join(self.tmp, 'rlm.json'),
            use_vector=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_observe_creates_l0(self):
        note = self.rlm.observe("User: hello there\nJarvis: hi")
        self.assertIsNotNone(note)
        self.assertEqual(note['level'], 0)
        self.assertEqual(self.rlm.stats()['by_level'].get('event'), 1)

    def test_duplicate_observation_skipped(self):
        self.rlm.observe("User: same message\nJarvis: same reply")
        self.assertIsNone(self.rlm.observe(
            "User: same message\nJarvis: same reply"))
        self.assertEqual(self.rlm.stats()['by_level'].get('event'), 1)

    def test_disabled_is_silent(self):
        with patch.dict(os.environ, {'JARVIS_RLM': '0'}):
            self.assertIsNone(self.rlm.observe("anything"))
            self.assertEqual(self.rlm.recall("anything"), '')

    def test_empty_ignored(self):
        self.assertIsNone(self.rlm.observe("   "))


class TestConsolidation(unittest.TestCase):

    def setUp(self):
        self.tmp = _make_tmp()
        self.rlm = RecursiveMemory(
            data_file=os.path.join(self.tmp, 'rlm.json'),
            use_vector=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _events(self, n, topic="project Atlas"):
        for i in range(n):
            self.rlm.observe(
                f"User: update {i} on {topic} timeline\nJarvis: noted {i}")

    def test_l0_folds_into_l1(self):
        brain = FakeBrain({'archivist':
                           'Session summary: the user advanced project '
                           'Atlas and set a demo deadline.'})
        with patch.dict(os.environ, {'JARVIS_RLM_L0_BATCH': '4'}):
            self._events(4)
            did = self.rlm.consolidate(brain)
        self.assertEqual(did.get('l1'), 1)
        l1 = [n for n in self.rlm._notes if n['level'] == 1]
        self.assertEqual(len(l1), 1)
        self.assertIn('Atlas', l1[0]['text'])
        self.assertEqual(len(l1[0]['children']), 4)
        self.assertTrue(all(n['folded'] for n in self.rlm._notes
                            if n['level'] == 0))
        self.assertEqual(self.rlm.stats()['by_level'].get('summary'), 1)

    def test_below_threshold_no_fold(self):
        brain = FakeBrain({'archivist': 'should not be called'})
        with patch.dict(os.environ, {'JARVIS_RLM_L0_BATCH': '4'}):
            self._events(3)
            did = self.rlm.consolidate(brain)
        self.assertEqual(did.get('l1'), 0)
        self.assertEqual(brain.calls, [])   # no LLM spend below threshold

    def test_llm_failure_keeps_batch_pending(self):
        dead = FakeBrain(default=None)
        with patch.dict(os.environ, {'JARVIS_RLM_L0_BATCH': '4'}):
            self._events(4)
            self.assertEqual(self.rlm.consolidate(dead).get('l1'), 0)
            # nothing folded — retry with a live brain succeeds
            live = FakeBrain({'archivist': 'Recovered session summary.'})
            self.assertEqual(self.rlm.consolidate(live).get('l1'), 1)

    def test_full_cascade_to_l2_and_world_model(self):
        def archivist(prompt):
            if 'WORLD MODEL' in prompt:
                return ('The user is building project Atlas; morning '
                        'person, deadline-driven.')
            if 'SUMMARIES' in prompt:
                return 'Recurring theme: Atlas milestones and demos.'
            return 'Session summary: Atlas work continued.'

        brain = FakeBrain({'archivist': archivist})
        with patch.dict(os.environ, {'JARVIS_RLM_L0_BATCH': '2',
                                     'JARVIS_RLM_L1_BATCH': '2',
                                     'JARVIS_RLM_WORLD_TTL_H': '0'}):
            # 9 events → consolidate twice → two L1s → third pass folds
            # them into an L2 and refreshes the world model.
            self._events(9)
            self.rlm.consolidate(brain)
            self.rlm.consolidate(brain)
            did = self.rlm.consolidate(brain)
        self.assertEqual(did.get('l2'), 1)
        self.assertEqual(did.get('world'), 1)
        self.assertEqual(self.rlm.stats()['by_level'].get('abstraction'), 1)
        self.assertIn('Atlas', self.rlm.world_model)
        self.assertTrue(self.rlm.world_model_updated)

    def test_world_model_rate_limited(self):
        brain = FakeBrain({'archivist': 'World model text.'})
        import datetime as _dt
        with patch.dict(os.environ, {'JARVIS_RLM_WORLD_TTL_H': '999'}):
            self.rlm._store_insight("User likes tea", 'preference', 7)
            self.rlm.world_model = "Old model."
            self.rlm.world_model_updated = _dt.datetime.now().isoformat()
            # Fresh material exists but the TTL blocks a refresh.
            self.rlm._store_insight("User also likes coffee", 'preference',
                                    7)
            self.assertEqual(self.rlm.consolidate(brain).get('world'), 0)
            self.assertEqual(self.rlm.world_model, "Old model.")

    def test_prune_bounds_l0(self):
        brain = FakeBrain({'archivist': 'summary'})
        with patch.dict(os.environ, {'JARVIS_RLM_L0_BATCH': '4',
                                     'JARVIS_RLM_MAX_L0': '50'}):
            self._events(40)          # 4 batches of 10
            for _ in range(4):
                self.rlm.consolidate(brain)
            events = [n for n in self.rlm._notes if n['level'] == 0]
            self.assertLessEqual(len(events), 50)

    def test_prune_prefers_consumed_events_over_fresh(self):
        # When the event level outgrows its cap, consolidation must shed
        # the OLDEST FOLDED (already-summarised) events — never the raw
        # observations that still await folding.  (The old code removed
        # only *unfolded* events, so every fold permanently appended up
        # to batch*2 events and the level grew without bound.)
        brain = FakeBrain({'archivist': 'Session summary: the user '
                                        'advanced project Atlas work.'})
        self._events(64)
        with patch.dict(os.environ, {'JARVIS_RLM_L0_BATCH': '8',
                                     'JARVIS_RLM_MAX_L0': '60'}):
            self.rlm.consolidate(brain)      # folds id 1-16, then prunes
        events = [n for n in self.rlm._notes if n['level'] == 0]
        self.assertLessEqual(len(events), 60)
        fresh = {n['id'] for n in events if n['id'] > 16}
        # Every still-unfolded raw event survived the trim (none of the
        # newest observations were sacrificed for capacity).
        self.assertEqual(fresh, set(range(17, 65)))


class TestRecall(unittest.TestCase):

    def setUp(self):
        self.tmp = _make_tmp()
        self.rlm = RecursiveMemory(
            data_file=os.path.join(self.tmp, 'rlm.json'),
            use_vector=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build(self):
        brain = FakeBrain({'archivist':
                           'Session summary covering project Atlas '
                           'deadline and the client demo.'})
        with patch.dict(os.environ, {'JARVIS_RLM_L0_BATCH': '3'}):
            for i in range(3):
                self.rlm.observe(
                    f"User: update {i} on project Atlas timeline\n"
                    f"Jarvis: noted {i}")
            self.rlm.consolidate(brain)
        self.rlm.world_model = "The user runs project Atlas at a startup."
        self.rlm.world_model_updated = '2026-01-01T00:00:00'

    def test_recall_assembles_all_levels(self):
        self._build()
        out = self.rlm.recall("project Atlas")
        self.assertIn('WORLD MODEL', out)
        self.assertIn('Atlas', out)                       # world model
        self.assertIn('Session summary', out)             # parent (L1)
        self.assertIn('update 0', out)                    # event (L0)

    def test_recall_respects_budget(self):
        self._build()
        out = self.rlm.recall("project Atlas", max_chars=300)
        self.assertLessEqual(len(out), 300 + 80)

    def test_recall_empty_when_nothing_matches(self):
        self.rlm.observe("User: nothing relevant here\nJarvis: ok")
        out = self.rlm.recall("quantum chromodynamics")
        self.assertEqual(out, '')

    def test_recall_block_header(self):
        self._build()
        block = self.rlm.recall_block("project Atlas")
        self.assertTrue(block.startswith('RLM RECURSIVE MEMORY'))
        self.assertEqual(self.rlm.recall_block("zzz unmatched"), '')

    def test_recall_deep_richer_budget(self):
        self._build()
        deep = self.rlm.recall_deep("project Atlas")
        self.assertIn('RLM RECURSIVE MEMORY', deep)

    def test_unrelated_query_still_anchors_world_model(self):
        self._build()
        out = self.rlm.recall("what did we decide about lunch")
        self.assertIn('WORLD MODEL', out)

    def test_stats_shape(self):
        self._build()
        s = self.rlm.stats()
        self.assertEqual(s['by_level'].get('event'), 3)
        self.assertEqual(s['by_level'].get('summary'), 1)
        self.assertGreater(s['world_model_chars'], 0)
        self.assertIn('last_consolidation', s)


class TestReflection(unittest.TestCase):

    def setUp(self):
        self.tmp = _make_tmp()
        self.rlm = RecursiveMemory(
            data_file=os.path.join(self.tmp, 'rlm.json'),
            use_vector=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_extracts_and_stores_insights(self):
        brain = FakeBrain({'reflector': json.dumps({
            "insights": [
                {"text": "User prefers morning workouts before work",
                 "kind": "preference", "importance": 7},
                {"text": "User is training for an October marathon",
                 "kind": "goal", "importance": 8},
            ]})})
        stored = self.rlm.reflect(
            brain, exchanges=[("I always work out at 6am before work, "
                               "training for the October marathon", "ok")])
        self.assertEqual(len(stored), 2)
        insights = [n for n in self.rlm._notes if n['level'] == 3]
        self.assertEqual(len(insights), 2)
        kinds = {n['source'] for n in insights}
        self.assertIn('reflection:preference', kinds)
        self.assertIn('reflection:goal', kinds)

    def test_lessons_feed_self_learning(self):
        # Isolate the process-wide lesson store so the assertion is
        # order-independent and never reads lessons written by other
        # test modules sharing utils.self_learning.LESSON_FILE.
        import tempfile as _t
        _fd, _lesson_path = _t.mkstemp(
            suffix='.md', prefix='jarvis_rlm_lesson_')
        os.close(_fd)
        with patch.object(_sl, 'LESSON_FILE', _lesson_path):
            before = _sl._load_lessons()
            self.assertEqual(before, [])   # fresh, isolated store
            brain = FakeBrain({'reflector': json.dumps({
                "insights": [
                    {"text": "When the user mentions travel, verify the "
                             "timezone before scheduling",
                     "kind": "lesson", "importance": 6},
                ]})})
            self.rlm.reflect(brain, exchanges=[
                ("please book my 9am meeting but I'll be in Tokyo next week",
                 "booked")])
            after = _sl._load_lessons()
            self.assertEqual(len(after), len(before) + 1)
            self.assertIn('timezone', after[-1]['text'])
        try:
            os.remove(_lesson_path)
        except OSError:
            pass

    def test_trivial_exchanges_skipped(self):
        brain = FakeBrain({'reflector': 'should not be called'})
        out = self.rlm.reflect(brain, exchanges=[("hi", "hello")])
        self.assertEqual(out, [])
        self.assertNotIn('reflector', brain.calls)

    def test_bad_json_is_a_noop(self):
        brain = FakeBrain({'reflector': 'I refuse to emit JSON'})
        out = self.rlm.reflect(brain, exchanges=[
            ("here is a substantive exchange about my startup goals", "ok")])
        self.assertEqual(out, [])
        self.assertEqual([n for n in self.rlm._notes if n['level'] == 3], [])

    def test_duplicate_insight_reinforced_not_duplicated(self):
        payload = json.dumps({"insights": [
            {"text": "User prefers morning workouts", "kind": "preference",
             "importance": 7}]})
        brain = FakeBrain({'reflector': payload})
        self.rlm.reflect(brain, exchanges=[
            ("I work out every morning", "ok")])
        self.rlm.reflect(brain, exchanges=[
            ("as I said, morning workouts are my routine", "ok")])
        insights = [n for n in self.rlm._notes if n['level'] == 3]
        self.assertEqual(len(insights), 1)

    def test_observe_exchange_triggers_background_workers(self):
        spawned = []
        with patch.dict(os.environ, {'JARVIS_RLM_REFLECT_EVERY': '2',
                                     'JARVIS_RLM_L0_BATCH': '3'}):
            with patch.object(RecursiveMemory, '_spawn',
                              lambda self, fn, *a: spawned.append(fn.__name__)):
                brain = FakeBrain()
                for i in range(3):
                    self.rlm.observe_exchange(
                        f"question {i} about the budget", "answer",
                        brain=brain)
        self.assertIn('_reflect_worker', spawned)
        self.assertIn('_consolidate_worker', spawned)

    def test_observe_exchange_without_brain_never_threads(self):
        with patch.object(RecursiveMemory, '_spawn',
                          lambda self, fn, *a: self.fail("spawned!")):
            for i in range(10):
                self.rlm.observe_exchange(f"q{i}", f"a{i}", brain=None)


class TestPersistence(unittest.TestCase):

    def test_roundtrip(self):
        tmp = _make_tmp()
        path = os.path.join(tmp, 'rlm.json')
        try:
            rlm = RecursiveMemory(data_file=path, use_vector=False)
            brain = FakeBrain({'archivist': 'Session summary text.'})
            with patch.dict(os.environ, {'JARVIS_RLM_L0_BATCH': '2'}):
                rlm.observe("User: first exchange\nJarvis: reply")
                rlm.observe("User: second exchange\nJarvis: reply")
                rlm.consolidate(brain)
            rlm._store_insight("User likes concise answers", 'preference', 7)
            rlm.world_model = "Test world model."
            rlm.world_model_updated = '2026-01-01T00:00:00'

            revived = RecursiveMemory(data_file=path, use_vector=False)
            self.assertEqual(revived.stats()['by_level'].get('event'), 2)
            self.assertEqual(revived.stats()['by_level'].get('summary'), 1)
            self.assertEqual(revived.stats()['by_level'].get('insight'), 1)
            self.assertEqual(revived.world_model, "Test world model.")
            # ID sequence continues — no collisions on revive
            revived.observe("User: third exchange\nJarvis: reply")
            ids = [n['id'] for n in revived._notes]
            self.assertEqual(len(ids), len(set(ids)))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_corrupt_note_entry_does_not_break_subsystem(self):
        # A data file that is valid JSON but holds a non-dict entry in
        # `notes` must degrade, not crash every maintenance/recall pass.
        tmp = _make_tmp()
        path = os.path.join(tmp, 'rlm.json')
        try:
            good = {'notes': [{'id': 1, 'level': 0, 'text': 'ok note',
                               'created': '2026-01-01T00:00:00',
                               'folded': False, 'tags': []}],
                    'next_id': 2}
            good['notes'].append("corrupt-garbage")
            with open(path, 'w') as f:
                json.dump(good, f)

            rlm = RecursiveMemory(data_file=path, use_vector=False)
            # only dicts survive the load
            self.assertTrue(all(isinstance(n, dict) for n in rlm._notes))
            # consolidation still runs without raising
            with patch.dict(os.environ, {'JARVIS_RLM_L0_BATCH': '2'}):
                rlm.observe("User: more\nJarvis: data")
                rlm.observe("User: another\nJarvis: exchange")
                did = rlm.consolidate(FakeBrain(
                    {'archivist': 'Session summary text.'}))
            self.assertIsInstance(did, dict)
            # and recall is unaffected
            self.assertIsInstance(rlm.recall('exchange', max_chars=200),
                                  str)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ====================================================================== #
# Persistent agent task plan (Hermes parity)
# ====================================================================== #

class TestPlanState(unittest.TestCase):

    def setUp(self):
        self.tmp = _make_tmp()
        self.plan = _empty_plan(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_set_and_render(self):
        out = self.plan.set("Launch the demo",
                            ["prepare slides", "rehearse", "ship it"])
        self.assertIn('Launch the demo', out)
        self.assertIn('[ ] prepare slides', out)
        self.assertIn('progress: 0/3', out)

    def test_update_marks_progress(self):
        self.plan.set("Launch the demo", ["a", "b"])
        out = self.plan.update(1, "done")
        self.assertIn('[x] a', out)
        out = self.plan.update(2, "in_progress")
        self.assertIn('[>] b', out)
        self.assertIn('progress: 1/2', out)

    def test_update_validates(self):
        self.plan.set("goal", ["a"])
        self.assertTrue(self.plan.update(5, 'done').startswith('ERROR'))
        self.assertTrue(self.plan.update(1, 'banana').startswith('ERROR'))

    def test_clear(self):
        self.plan.set("goal", ["a"])
        self.assertIn('cleared', self.plan.clear().lower())
        self.assertEqual(self.plan.render(), '')

    def test_persistence_roundtrip(self):
        self.plan.set("Survive restarts", ["step one", "step two"])
        self.plan.update(1, 'done')
        revived = PlanState(file_path=self.plan.file_path)
        self.assertEqual(revived.goal, "Survive restarts")
        self.assertEqual(revived.steps[0]['status'], 'done')
        self.assertIn('[x] step one', revived.render())

    def test_persistence_disabled(self):
        with patch.dict(os.environ, {'JARVIS_AGENT_PLAN_STORE': '0'}):
            volatile = PlanState(file_path=self.plan.file_path)
            volatile.set("Volatile goal", ["a"])
            fresh = PlanState(file_path=self.plan.file_path)
        self.assertEqual(fresh.goal, '')      # nothing was written

    def test_set_requires_goal_and_steps(self):
        self.assertTrue(self.plan.set("", ["a"]).startswith('ERROR'))
        self.assertTrue(self.plan.set("g", []).startswith('ERROR'))


# ====================================================================== #
# Reasoning layer
# ====================================================================== #

class TestShouldPlan(unittest.TestCase):

    def test_simple_chat_false(self):
        self.assertFalse(reasoner.should_plan("hi"))
        self.assertFalse(reasoner.should_plan("what's the capital of Peru?"))
        self.assertFalse(reasoner.should_plan("play despacito"))

    def test_multi_step_true(self):
        self.assertTrue(reasoner.should_plan(
            "research the top 3 electric cars and compare their range "
            "and then summarize the prices"))
        self.assertTrue(reasoner.should_plan(
            "check my calendar and after that book a table for two"))

    def test_verb_signal_true(self):
        self.assertTrue(reasoner.should_plan(
            "please organize my downloads folder and analyze what's "
            "taking space in there carefully"))

    def test_long_prompt_true(self):
        self.assertTrue(reasoner.should_plan("x" * 450))


class TestDetectEffort(unittest.TestCase):

    def test_normal_by_default(self):
        self.assertEqual(reasoner.detect_effort("what time is it"), 'normal')

    def test_cue_escalates(self):
        for cue in ("think harder about this",
                    "reason step by step please",
                    "be thorough with the analysis"):
            self.assertEqual(reasoner.detect_effort(cue), 'deep', cue)

    def test_env_default_deep(self):
        with patch.dict(os.environ, {'JARVIS_REASON_EFFORT': 'deep'}):
            self.assertEqual(reasoner.detect_effort("anything"), 'deep')

    def test_env_default_normal(self):
        with patch.dict(os.environ, {'JARVIS_REASON_EFFORT': ''}):
            self.assertEqual(reasoner.detect_effort("anything"), 'normal')


class TestMakePlan(unittest.TestCase):

    def test_json_steps_flattened(self):
        brain = FakeBrain({'planner': json.dumps(
            {"steps": ["search flights", "compare prices", "book best"]})})
        plan = reasoner.make_plan(brain, "book a flight to Delhi")
        self.assertIn('search flights', plan)
        self.assertIn('book best', plan)
        self.assertNotIn('{', plan)

    def test_plain_text_kept(self):
        brain = FakeBrain({'planner': "1. do A\n2. do B"})
        plan = reasoner.make_plan(brain, "do a thing")
        self.assertIn('do A', plan)

    def test_failure_returns_none(self):
        self.assertIsNone(reasoner.make_plan(FakeBrain(default=None), "x"))
        self.assertIsNone(reasoner.make_plan(None, "x"))


class TestCritique(unittest.TestCase):

    def test_plain_correction(self):
        brain = FakeBrain({'critic':
                           "The weather API key is missing. Use "
                           "search_web for the forecast instead."})
        out = reasoner.critique_failures(
            brain, "what's the weather",
            [('get_weather', 'ERROR: no API key')])
        self.assertIn('search_web', out)

    def test_json_critic_flattened(self):
        brain = FakeBrain({'critic': json.dumps({
            "assessment": "The boom tool is broken",
            "revised_steps": [{"id": 1, "text": "Use search_web instead",
                               "depends_on": []}]})})
        out = reasoner.critique_failures(brain, "do it",
                                         [('boom', 'ERROR: exploded')])
        self.assertIn('boom', out)
        self.assertIn('search_web', out)

    def test_failure_returns_none(self):
        self.assertIsNone(reasoner.critique_failures(
            FakeBrain(default=None), "x", [('t', 'ERROR')]))
        self.assertIsNone(reasoner.critique_failures(
            FakeBrain(), "x", []))


class TestVerifyAnswer(unittest.TestCase):

    def test_approved(self):
        brain = FakeBrain({'critic': 'APPROVED'})
        ok, revised = reasoner.verify_answer(brain, "req", "draft answer")
        self.assertTrue(ok)
        self.assertIsNone(revised)

    def test_revised(self):
        brain = FakeBrain({'critic':
                           'REVISED: The complete corrected answer.'})
        ok, revised = reasoner.verify_answer(brain, "req", "draft")
        self.assertFalse(ok)
        self.assertEqual(revised, 'The complete corrected answer.')

    def test_unparsable_trusts_draft(self):
        brain = FakeBrain({'critic': 'hmm maybe fine whatever'})
        ok, revised = reasoner.verify_answer(brain, "req", "draft")
        self.assertTrue(ok)
        self.assertIsNone(revised)

    def test_failure_trusts_draft(self):
        ok, revised = reasoner.verify_answer(FakeBrain(default=None),
                                             "req", "draft")
        self.assertTrue(ok)
        self.assertIsNone(revised)


# ====================================================================== #
# Agent loop integration (reasoning-first behaviour)
# ====================================================================== #

class TestAgentLoopRLM(unittest.TestCase):

    def _loop(self, script, wrapup=None, complete_replies=None):
        router = FakeRouter(script, wrapup=wrapup)
        brain = StubBrain(router, complete_replies=complete_replies)
        loop = AgentLoop(brain)
        loop._executor = FakeExecutor()
        return loop, router

    def test_default_step_budget_raised(self):
        loop, _ = self._loop([])
        self.assertEqual(MAX_STEPS_DEFAULT, 10)
        self.assertEqual(loop.max_steps, 10)

    def test_think_tool_never_reaches_executor(self):
        loop, router = self._loop([
            tool_result('think', json.dumps(
                {'thought': 'Check weather first, then summarize.'})),
            text_result("All planned out, Sir."),
        ])
        out = loop.run("what's the weather like and should I bike?")
        self.assertEqual(out['response'], "All planned out, Sir.")
        self.assertEqual(loop._executor.calls, [])
        tool_msg = [m for m in router.seen_messages[1]
                    if m['role'] == 'tool'][0]
        self.assertIn('Reasoning noted', tool_msg['content'])

    def test_recall_deep_tool_reads_recursive_memory(self):
        class FakeRLM:
            def __init__(self):
                self.queries = []

            def recall_deep(self, query):
                self.queries.append(query)
                return ("RLM RECURSIVE MEMORY:\nWORLD MODEL: the user is "
                        "building project Atlas.")

        fake_rlm = FakeRLM()
        with patch('utils.rlm.get_rlm', return_value=fake_rlm):
            loop, router = self._loop([
                tool_result('recall_deep', json.dumps(
                    {'query': 'project atlas history'})),
                text_result("You decided demo day is Friday, Sir."),
            ])
            out = loop.run("what did we decide about project atlas?")
        self.assertEqual(fake_rlm.queries, ['project atlas history'])
        self.assertEqual(loop._executor.calls, [])
        tool_msg = [m for m in router.seen_messages[1]
                    if m['role'] == 'tool'][0]
        self.assertIn('project Atlas', tool_msg['content'])

    def test_task_plan_tool_manages_persistent_plan(self):
        tmp = _make_tmp()
        plan = _empty_plan(tmp)
        try:
            with patch('utils.rlm.get_plan_state', return_value=plan):
                loop, router = self._loop([
                    tool_result('task_plan', json.dumps({
                        'op': 'set', 'goal': 'Launch demo',
                        'steps': ['prepare slides', 'rehearse']})),
                    text_result("Plan recorded, Sir."),
                ])
                loop.run("plan my demo launch")
                self.assertEqual(loop._executor.calls, [])
                self.assertEqual(plan.goal, 'Launch demo')
                self.assertEqual(len(plan.steps), 2)
                tool_msg = [m for m in router.seen_messages[1]
                            if m['role'] == 'tool'][0]
                self.assertIn('Launch demo', tool_msg['content'])
                self.assertIn('[ ] prepare slides', tool_msg['content'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_task_plan_injected_into_system_prompt(self):
        tmp = _make_tmp()
        plan = _empty_plan(tmp)
        plan.set("Finish the quarterly report", ["gather data",
                                                 "write summary"])
        try:
            with patch('utils.rlm.get_plan_state', return_value=plan):
                loop, router = self._loop([text_result("On it, Sir.")])
                loop.run("continue my report")
            system = router.seen_messages[0][0]
            self.assertIn('ACTIVE TASK PLAN', system['content'])
            self.assertIn('Finish the quarterly report',
                          system['content'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_delegate_tool_uses_subagent(self):
        loop, router = self._loop([
            tool_result('delegate', json.dumps({
                'task': 'Summarize the attached research on batteries',
                'persona': 'researcher'})),
            text_result("Here is the summary, Sir."),
        ], complete_replies={'researcher':
                             'Battery density improved 12% year over '
                             'year.'})
        out = loop.run("summarize the battery research for me")
        self.assertEqual(out['response'], "Here is the summary, Sir.")
        self.assertEqual(loop._executor.calls, [])
        self.assertIn('researcher', loop.brain.complete_calls)
        tool_msg = [m for m in router.seen_messages[1]
                    if m['role'] == 'tool'][0]
        self.assertIn('Battery density', tool_msg['content'])

    def test_delegate_failure_surfaces_error(self):
        loop, router = self._loop([
            tool_result('delegate', json.dumps({'task': 'do research'})),
            text_result("Recovered, Sir."),
        ], complete_replies={'researcher': None})
        loop.run("do some research for me")
        tool_msg = [m for m in router.seen_messages[1]
                    if m['role'] == 'tool'][0]
        self.assertTrue(tool_msg['content'].startswith('ERROR'))

    def test_critic_nudge_after_two_errors(self):
        def boom(i):
            def _f():
                return tool_result('boom', '{}', cid=f'e{i}')
            return _f

        loop, router = self._loop(
            [boom(1), boom(2), text_result("Recovered, Sir.")],
            complete_replies={'critic':
                              'The boom tool is broken — use search_web.'})
        out = loop.run("fetch the thing")
        self.assertEqual(out['response'], "Recovered, Sir.")
        # The correction must be visible to the model on the next turn.
        third = router.seen_messages[2]
        nudges = [m for m in third
                  if m['role'] == 'user'
                  and 'STRATEGY CORRECTION' in str(m.get('content'))]
        self.assertEqual(len(nudges), 1)
        self.assertIn('search_web', nudges[0]['content'])
        self.assertIn('critic', loop.brain.complete_calls)

    def test_single_error_does_not_nudge(self):
        loop, router = self._loop([
            tool_result('boom', '{}'),
            text_result("Handled, Sir."),
        ], complete_replies={'critic': 'should not be asked'})
        loop.run("do the thing")
        self.assertNotIn('critic', loop.brain.complete_calls)

    def test_plan_injected_for_complex_prompts(self):
        with patch('utils.rlm.reasoner.should_plan', return_value=True), \
                patch('utils.rlm.reasoner.make_plan',
                      return_value='- step one\n- step two'):
            loop, router = self._loop([text_result("Done, Sir.")])
            out = loop.run("research and compare electric cars")
        self.assertEqual(out['response'], "Done, Sir.")
        system = router.seen_messages[0][0]
        self.assertEqual(system['role'], 'system')
        self.assertIn('WORKING PLAN', system['content'])
        self.assertIn('step one', system['content'])

    def test_plan_disabled_by_env(self):
        with patch.dict(os.environ, {'JARVIS_AGENT_PLAN': '0'}), \
                patch('utils.rlm.reasoner.should_plan', return_value=True), \
                patch('utils.rlm.reasoner.make_plan',
                      return_value='- should not appear'):
            loop, router = self._loop([text_result("Done, Sir.")])
            loop.run("research and compare electric cars")
        system = router.seen_messages[0][0]
        self.assertNotIn('WORKING PLAN', system['content'])

    def test_deep_reasoning_mode(self):
        loop, router = self._loop([text_result("Deep answer, Sir.")])
        out = loop.run("think harder about the fermi paradox")
        self.assertEqual(out['response'], "Deep answer, Sir.")
        system = router.seen_messages[0][0]
        self.assertIn('DEEP REASONING MODE', system['content'])

    def test_deep_mode_forces_verification(self):
        loop, router = self._loop([
            tool_result('get_weather', '{}', cid='v1'),
            text_result("Draft answer, Sir."),
        ], complete_replies={'critic':
                             'REVISED: The verified deep answer.'})
        out = loop.run("think carefully about whether I need an umbrella")
        self.assertEqual(out['response'], 'The verified deep answer.')
        self.assertIn('critic', loop.brain.complete_calls)

    def test_answer_verified_after_tool_work(self):
        loop, router = self._loop([
            tool_result('todo_list', '{}', cid='a'),
            tool_result('note_list', '{}', cid='b'),
            text_result("Draft answer."),
        ], complete_replies={'critic':
                             'REVISED: The complete verified answer.'})
        out = loop.run("check my todos and notes")
        self.assertEqual(out['response'], 'The complete verified answer.')

    def test_answer_approved_left_untouched(self):
        loop, router = self._loop([
            tool_result('todo_list', '{}', cid='a'),
            tool_result('note_list', '{}', cid='b'),
            text_result("Draft answer."),
        ], complete_replies={'critic': 'APPROVED'})
        out = loop.run("check my todos and notes")
        self.assertEqual(out['response'], "Draft answer.")

    def test_verification_disabled_by_env(self):
        with patch.dict(os.environ, {'JARVIS_AGENT_VERIFY': '0'}):
            loop, router = self._loop([
                tool_result('todo_list', '{}', cid='a'),
                tool_result('note_list', '{}', cid='b'),
                text_result("Draft answer."),
            ], complete_replies={'critic': 'REVISED: never seen'})
            out = loop.run("check my todos and notes")
        self.assertEqual(out['response'], "Draft answer.")
        self.assertNotIn('critic', loop.brain.complete_calls)

    def test_no_verification_without_tool_work(self):
        loop, router = self._loop([text_result("Just chat, Sir.")],
                                  complete_replies={'critic':
                                                    'REVISED: nope'})
        out = loop.run("tell me a joke")
        self.assertEqual(out['response'], "Just chat, Sir.")
        self.assertNotIn('critic', loop.brain.complete_calls)

    def test_reasoning_directive_present(self):
        loop, router = self._loop([text_result("Yes, Sir.")])
        loop.run("hello")
        system = router.seen_messages[0][0]
        self.assertIn('REASON FIRST', system['content'])
        self.assertIn('recall_deep', system['content'])
        self.assertIn('task_plan', system['content'])

    def test_parallel_tool_execution_overlaps(self):
        loop, _ = self._loop([])
        timeline = []
        lock = threading.Lock()

        def fake_exec(name, arguments):
            with lock:
                timeline.append(('start', name))
            time.sleep(0.25)
            with lock:
                timeline.append(('end', name))
            return f"{name}: ok"

        loop._exec_tool = fake_exec
        calls = [(0, ToolCall(id='p1', name='alpha', arguments='{}')),
                 (1, ToolCall(id='p2', name='beta', arguments='{}'))]
        t0 = time.time()
        results = loop._run_tool_calls(calls)
        elapsed = time.time() - t0
        self.assertEqual(results[0], 'alpha: ok')
        self.assertEqual(results[1], 'beta: ok')
        self.assertLess(elapsed, 0.45)      # overlapped, not 2×0.25s

    def test_parallel_disabled_runs_sequential(self):
        loop, _ = self._loop([])

        def fake_exec(name, arguments):
            time.sleep(0.25)
            return f"{name}: ok"

        loop._exec_tool = fake_exec
        calls = [(0, ToolCall(id='s1', name='alpha', arguments='{}')),
                 (1, ToolCall(id='s2', name='beta', arguments='{}'))]
        with patch.dict(os.environ, {'JARVIS_AGENT_PARALLEL': '0'}):
            t0 = time.time()
            results = loop._run_tool_calls(calls)
            elapsed = time.time() - t0
        self.assertEqual(results, ['alpha: ok', 'beta: ok'])
        self.assertGreaterEqual(elapsed, 0.5)

    def test_multiple_tool_calls_in_one_turn(self):
        two_calls = ChatResult(text='', finish_reason='tool_calls',
                               provider='groq', model='fake',
                               tool_calls=[
                                   ToolCall(id='m1', name='get_weather',
                                            arguments='{}'),
                                   ToolCall(id='m2', name='todo_list',
                                            arguments='{}')])
        loop, router = self._loop([two_calls, text_result("Both, Sir.")])
        out = loop.run("weather and my todos please")
        self.assertEqual(out['response'], "Both, Sir.")
        self.assertEqual([c['action'] for c in loop._executor.calls],
                         ['get_weather', 'todo_list'])
        tool_msgs = [m for m in router.seen_messages[1]
                     if m['role'] == 'tool']
        self.assertEqual([m['tool_call_id'] for m in tool_msgs],
                         ['m1', 'm2'])
        self.assertIn('Sunny', tool_msgs[0]['content'])

    def test_tool_output_cap_default_raised(self):
        self.assertGreaterEqual(TOOL_OUTPUT_CAP, 4000)

    def test_tool_output_cap_env_tunable(self):
        import importlib
        import utils.agent_loop as al
        try:
            with patch.dict(os.environ, {'JARVIS_AGENT_TOOL_CAP': '900'}):
                importlib.reload(al)
                self.assertEqual(al.TOOL_OUTPUT_CAP, 900)
        finally:
            # reload AFTER the patch exits → default cap restored
            importlib.reload(al)
        self.assertEqual(al.TOOL_OUTPUT_CAP, 4000)


class TestToolSpecsRLM(unittest.TestCase):

    def test_reasoning_tools_offered(self):
        names = {s['function']['name'] for s in TOOL_SPECS}
        for expected in ('think', 'recall_deep', 'task_plan', 'delegate'):
            self.assertIn(expected, names)
        think = [s for s in TOOL_SPECS
                 if s['function']['name'] == 'think'][0]
        self.assertEqual(
            think['function']['parameters']['required'], ['thought'])

    def test_in_loop_tools_not_reachable_via_any_action(self):
        for name in ('think', 'recall_deep', 'task_plan', 'delegate'):
            self.assertIn(name, FORBIDDEN_TOOLS)

    def test_profiles_registered(self):
        from utils.agents import get_profile
        for name in ('archivist', 'reflector'):
            profile = get_profile(name)
            self.assertIsNotNone(profile, name)
            self.assertFalse(profile.tools_allowed, name)


# ====================================================================== #
# Brain integration (bare Brain — no network, no real providers)
# ====================================================================== #

class TestBrainIntegration(unittest.TestCase):

    def _bare_brain(self):
        from utils.brain import Brain
        b = Brain.__new__(Brain)
        b.active = True
        b.clients = []
        b.models = []
        b.history = []
        b.history_lock = threading.Lock()
        b.MAX_HISTORY = 20
        b.router = None
        # Attributes _append_history touches; mirror the harness in
        # tests/test_memory_rag.py::_new_brain.
        b.total_exchanges = 0
        b._digest_lock = threading.Lock()
        b.digest_text = ""
        b.digest_upto = 0
        b._evicted_buffer = []
        # Never write to the real conversation history file in tests.
        b._save_history = lambda: None
        return b

    def test_rlm_block_watchdog_returns_text(self):
        b = self._bare_brain()
        with patch('utils.rlm.get_rlm') as fake:
            fake.return_value.recall_block.return_value = \
                "RLM RECURSIVE MEMORY: hi"
            out = b._rlm_block("anything")
        self.assertEqual(out, "RLM RECURSIVE MEMORY: hi")

    def test_rlm_block_slow_recall_returns_empty(self):
        from utils.brain import Brain
        b = self._bare_brain()

        class SlowRLM:
            def recall_block(self, prompt):
                time.sleep(30)
                return "too late"

        with patch('utils.rlm.get_rlm', return_value=SlowRLM()), \
                patch.object(Brain, '_VAULT_TIMEOUT', 0.2):
            out = b._rlm_block("anything")
        self.assertEqual(out, '')

    def test_append_history_observes_into_rlm(self):
        b = self._bare_brain()
        b.digest_text = ""
        b._evicted_buffer = []
        with patch('utils.rlm.get_rlm') as fake:
            b._append_history("what is my deadline", "Friday, Sir.")
            fake.return_value.observe_exchange.assert_called_once()
            args = fake.return_value.observe_exchange.call_args[0]
            self.assertEqual(args[0], "what is my deadline")
            self.assertIs(args[2], b)     # brain handed over for workers


# ====================================================================== #
# PrimeAgent-level upgrades: entity graph, temporal scoring,
# supersession, subject recall, cross-session continuity
# ====================================================================== #

import datetime                                              # noqa: E402

from utils.rlm.entities import EntityGraph, extract          # noqa: E402


def _age(note, **delta):
    """Rewind a note's creation timestamp into the past."""
    note['created'] = (datetime.datetime.now()
                       - datetime.timedelta(**delta)).isoformat()


class TestEntityExtraction(unittest.TestCase):

    def test_multiword_names_extracted(self):
        names = extract("Met with Akshat Pratap about the Jarvis "
                        "refactor today.")
        self.assertIn('Akshat Pratap', names)

    def test_sentence_start_not_single_word_entity(self):
        # 'Delhi' opens the sentence → not treated as an entity; the
        # mid-sentence city still is.
        names = extract("Delhi is hot this week; I prefer Shimla.")
        self.assertNotIn('Delhi', names)
        self.assertIn('Shimla', names)

    def test_pathlike_identifiers(self):
        names = extract("Fixed the bug in utils/executor.py and added "
                        "snake_case_helper tests.")
        self.assertIn('utils/executor.py', names)
        self.assertIn('snake_case_helper', names)

    def test_blocklist_excluded(self):
        names = extract("Jarvis said ok to the user about Monday.")
        self.assertEqual(names, [])

    def test_empty_and_garbage_safe(self):
        self.assertEqual(extract(''), [])
        self.assertEqual(extract(None), [])
        self.assertEqual(extract('123 456 !!!'), [])


class TestEntityGraph(unittest.TestCase):

    def test_record_and_about(self):
        g = EntityGraph()
        g.record(1, "User discussed project Atlas with Priya Sharma.",
                 '2026-08-01T10:00:00')
        ent = g.about('atlas')
        self.assertIsNotNone(ent)
        self.assertEqual(ent['display'], 'Atlas')
        self.assertEqual(ent['count'], 1)
        self.assertIn(1, ent['note_ids'])
        self.assertEqual(g.about('priya sharma')['display'],
                         'Priya Sharma')

    def test_cooccurrence_edges(self):
        g = EntityGraph()
        for i in range(3):
            g.record(i, "Atlas sync with Priya Sharma.", f'2026-08-0{i+1}')
        self.assertIn('Priya Sharma', g.related('Atlas'))

    def test_match_longest_contained(self):
        g = EntityGraph()
        g.record(1, "Priya Sharma joined the Atlas project.")
        self.assertEqual(g.match('what about priya sharma'),
                         'Priya Sharma')
        self.assertEqual(g.match('nothing here'), '')

    def test_persistence_roundtrip(self):
        g = EntityGraph()
        g.record(1, "Atlas planning with Priya Sharma.",
                 '2026-08-01T10:00:00')
        revived = EntityGraph.from_dict(g.to_dict())
        self.assertEqual(revived.about('atlas')['count'], 1)
        self.assertIn('Priya Sharma', revived.related('Atlas'))

    def test_disabled_records_nothing(self):
        g = EntityGraph()
        with patch.dict(os.environ, {'JARVIS_RLM_ENTITIES': '0'}):
            self.assertEqual(g.record(1, "Atlas with Priya Sharma.",
                                       '2026-08-01'), [])
        self.assertEqual(len(g), 0)

    def test_bounded_growth(self):
        g = EntityGraph()
        for i in range(30):
            g.record(i, f"Note {i} mentions Atlas and Priya Sharma.",
                     f'2026-08-01T00:{i:02d}')
        self.assertEqual(len(g.about('atlas')['note_ids']), 24)


class TestTemporalScoring(unittest.TestCase):

    def setUp(self):
        self.tmp = _make_tmp()
        self.rlm = RecursiveMemory(
            data_file=os.path.join(self.tmp, 'rlm.json'),
            use_vector=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _note(self, age_days, importance=5):
        return {'created': (datetime.datetime.now()
                            - datetime.timedelta(days=age_days)).isoformat(),
                'importance': importance, 'access_count': 0}

    def test_recency_beats_stale(self):
        fresh = self.rlm._temporal_mult(self._note(0))
        stale = self.rlm._temporal_mult(self._note(60))
        self.assertGreater(fresh, stale)

    def test_importance_counts(self):
        high = self.rlm._temporal_mult(self._note(0, importance=9))
        low = self.rlm._temporal_mult(self._note(0, importance=2))
        self.assertGreater(high, low)

    def test_decay_has_a_floor(self):
        # Durable knowledge never scores as irrelevant, however old.
        ancient = self.rlm._temporal_mult(self._note(3650, importance=1))
        self.assertGreater(ancient, 0.25)

    def test_recency_weight_zero_disables_decay(self):
        with patch.dict(os.environ, {'JARVIS_RLM_W_REC': '0'}):
            a = self.rlm._temporal_mult(self._note(0))
            b = self.rlm._temporal_mult(self._note(400))
        self.assertAlmostEqual(a, b)

    def test_reuse_boosts_score(self):
        used = self._note(0)
        used['access_count'] = 4
        self.assertGreater(self.rlm._temporal_mult(used),
                           self.rlm._temporal_mult(self._note(0)))

    def test_ranking_prefers_recent(self):
        self.rlm.observe("User: atlas checkpoint one\nJarvis: noted")
        self.rlm.observe("User: atlas checkpoint two\nJarvis: noted")
        with self.rlm._lock:
            _age(self.rlm._notes[0], days=90)
        ranked = self.rlm._rank_notes(self.rlm._notes, "atlas")
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0][1]['text'],
                         "User: atlas checkpoint two\nJarvis: noted")

    def test_subject_query_matches_tracked_entity(self):
        self.rlm.observe("User: atlas checkpoint one\nJarvis: noted")
        self.rlm.observe("User: random chatter about food\nJarvis: ok")
        # The capitalized mention in the first note tracked 'Atlas';
        # the graph steers the query to it.
        self.assertEqual(self.rlm.entities.match('atlas'), 'Atlas')
        ranked = self.rlm._rank_notes(self.rlm._notes, "atlas")
        self.assertTrue(ranked)
        self.assertIn('atlas', ranked[0][1]['text'].lower())


class TestSupersession(unittest.TestCase):

    def setUp(self):
        self.tmp = _make_tmp()
        self.rlm = RecursiveMemory(
            data_file=os.path.join(self.tmp, 'rlm.json'),
            use_vector=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_contradiction_supersedes_old_fact(self):
        old = self.rlm._store_insight("The user lives in Berlin.",
                                      kind='user_model')
        new = self.rlm._store_insight("The user now lives in Tokyo.",
                                      kind='user_model')
        self.assertIsNotNone(old)
        self.assertIsNotNone(new)
        self.assertEqual(old['superseded_by'], new['id'])
        self.assertIn(old['id'], new['supersedes'])

    def test_superseded_never_recalled(self):
        self.rlm._store_insight("The user lives in Berlin.",
                                kind='user_model')
        self.rlm._store_insight("The user now lives in Tokyo.",
                                kind='user_model')
        block = self.rlm.recall("where does the user live?")
        self.assertIn('Tokyo', block)
        self.assertNotIn('Berlin', block)

    def test_near_duplicate_still_reinforced(self):
        first = self.rlm._store_insight("The user lives in Berlin.",
                                        kind='user_model')
        dup = self.rlm._store_insight("The user lives in Berlin.",
                                      kind='user_model')
        self.assertIsNone(dup)          # reinforced, not duplicated
        self.assertFalse(first.get('superseded_by'))
        insights = [n for n in self.rlm._notes
                    if n.get('level') == 3]
        self.assertEqual(len(insights), 1)

    def test_unrelated_and_unmarked_kept(self):
        # No change marker → same-topic facts coexist (preferences are
        # additive, not contradictory).
        a = self.rlm._store_insight("The user likes pizza.",
                                    kind='preference')
        b = self.rlm._store_insight("The user likes pasta.",
                                    kind='preference')
        self.assertFalse(a.get('superseded_by'))
        self.assertFalse(b.get('superseded_by'))

    def test_stats_counts_superseded(self):
        self.rlm._store_insight("The user lives in Berlin.",
                                kind='user_model')
        self.rlm._store_insight("The user now lives in Tokyo.",
                                kind='user_model')
        self.assertEqual(self.rlm.stats()['superseded'], 1)

    def test_prune_drops_superseded_first(self):
        for i in range(6):
            self.rlm._store_insight(
                f"The user keeps widget {i} in the drawer.",
                kind='fact')
            self.rlm._store_insight(
                f"The user now keeps widget {i} on the shelf.",
                kind='fact')
        # 6 superseded + 6 live; cap the insight pool at 8 → the 6
        # stale ones must go first.
        with patch.object(self.rlm, 'MAX_INSIGHTS', 8):
            removed = self.rlm._prune()
        self.assertGreaterEqual(removed, 4)
        superseded_left = sum(1 for n in self.rlm._notes
                              if n.get('superseded_by'))
        self.assertLessEqual(superseded_left, 2)


class TestSubjectRecall(unittest.TestCase):

    def setUp(self):
        self.tmp = _make_tmp()
        self.rlm = RecursiveMemory(
            data_file=os.path.join(self.tmp, 'rlm.json'),
            use_vector=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recall_about_gathers_subject(self):
        self.rlm.observe("User: status update on project Atlas?\n"
                         "Jarvis: the demo is on Friday.")
        self.rlm._store_insight("Atlas is the user's stealth startup "
                                "project.", kind='fact')
        block = self.rlm.recall_about("Atlas")
        self.assertIn('MEMORY ABOUT Atlas', block)
        self.assertIn('stealth startup', block)
        self.assertIn('demo is on Friday', block)

    def test_recall_about_unknown_subject(self):
        self.assertEqual(self.rlm.recall_about("Zaphod Beeblebrox"), '')

    def test_recall_about_excludes_superseded(self):
        self.rlm._store_insight("Atlas launches in June.", kind='fact')
        self.rlm._store_insight("Atlas now launches in September.",
                                kind='fact')
        block = self.rlm.recall_about("Atlas")
        self.assertIn('September', block)
        self.assertNotIn('June', block)

    def test_entity_digest_lists_subjects(self):
        self.rlm.observe("User: status update on project Atlas?\n"
                         "Jarvis: the demo is on Friday.")
        digest = self.rlm.entity_digest()
        self.assertIn('KNOWN SUBJECTS', digest)
        self.assertIn('Atlas', digest)

    def test_entity_digest_empty_when_nothing_tracked(self):
        self.assertEqual(self.rlm.entity_digest(), '')

    def test_entities_persist_with_store(self):
        self.rlm.observe("User: project Atlas kickoff\nJarvis: noted")
        path = os.path.join(self.tmp, 'rlm.json')
        revived = RecursiveMemory(data_file=path, use_vector=False)
        self.assertGreater(len(revived.entities), 0)
        self.assertIn('Atlas', [d for d, _c, _l
                                in revived.entities.top(10)])


class TestContinuity(unittest.TestCase):

    def setUp(self):
        self.tmp = _make_tmp()
        self.rlm = RecursiveMemory(
            data_file=os.path.join(self.tmp, 'rlm.json'),
            use_vector=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_warm_session_returns_empty(self):
        self.rlm.observe("User: working on Atlas\nJarvis: on it")
        self.assertEqual(self.rlm.continuity_block(), '')

    def test_cold_session_bridges_last_exchanges(self):
        self.rlm.observe("User: working on Atlas demo\nJarvis: on it")
        with self.rlm._lock:
            for n in self.rlm._notes:
                _age(n, hours=3)
        block = self.rlm.continuity_block()
        self.assertIn('SESSION CONTINUITY', block)
        self.assertIn('Atlas', block)

    def test_gap_is_env_tunable(self):
        self.rlm.observe("User: working on Atlas\nJarvis: on it")
        with self.rlm._lock:
            for n in self.rlm._notes:
                _age(n, minutes=10)
        self.assertEqual(self.rlm.continuity_block(), '')
        with patch.dict(os.environ, {'JARVIS_RLM_CONTINUITY_GAP_S': '300'}):
            self.assertNotEqual(self.rlm.continuity_block(), '')

    def test_no_notes_returns_empty(self):
        self.assertEqual(self.rlm.continuity_block(), '')

    def test_summary_leads_the_block(self):
        for i in range(4):
            self.rlm.observe(f"User: atlas step {i}\nJarvis: done {i}")
        brain = FakeBrain({'archivist':
                           'Atlas: user finished the demo prep.'})
        with patch.dict(os.environ, {'JARVIS_RLM_L0_BATCH': '4'}):
            self.rlm.consolidate(brain)
        with self.rlm._lock:
            for n in self.rlm._notes:
                _age(n, hours=5)
        block = self.rlm.continuity_block()
        self.assertIn('LAST SESSION SUMMARY', block)
        self.assertIn('demo prep', block)


class TestBrainContinuity(unittest.TestCase):

    def _bare_brain(self):
        from utils.brain import Brain
        b = Brain.__new__(Brain)
        b.active = True
        b.clients = []
        b.models = []
        b.history = []
        b.history_lock = threading.Lock()
        b.MAX_HISTORY = 20
        b.router = None
        # Attributes _append_history touches; mirror the harness in
        # tests/test_memory_rag.py::_new_brain.
        b.total_exchanges = 0
        b._digest_lock = threading.Lock()
        b.digest_text = ""
        b.digest_upto = 0
        b._evicted_buffer = []
        # Never write to the real conversation history file in tests.
        b._save_history = lambda: None
        return b

    def test_continuity_block_returned_once(self):
        b = self._bare_brain()
        with patch('utils.rlm.get_rlm') as fake:
            fake.return_value.continuity_block.return_value = \
                "SESSION CONTINUITY: we left Atlas mid-demo."
            first = b._rlm_continuity_block()
            second = b._rlm_continuity_block()
        self.assertIn('Atlas', first)
        self.assertEqual(second, '')       # once per session

    def test_warm_rlm_returns_empty(self):
        b = self._bare_brain()
        with patch('utils.rlm.get_rlm') as fake:
            fake.return_value.continuity_block.return_value = ''
            self.assertEqual(b._rlm_continuity_block(), '')


class TestMemoryAboutTool(unittest.TestCase):

    def _loop(self, script, wrapup=None):
        router = FakeRouter(script, wrapup=wrapup)
        brain = StubBrain(router)
        loop = AgentLoop(brain)
        loop._executor = FakeExecutor()
        return loop, router

    def test_spec_registered_and_guarded(self):
        names = {t['function']['name'] for t in TOOL_SPECS}
        self.assertIn('memory_about', names)
        self.assertIn('memory_about', FORBIDDEN_TOOLS)

    def test_entity_query_in_loop(self):
        class FakeRLM:
            def __init__(self):
                self.entities_queried = []

            def recall_about(self, entity):
                self.entities_queried.append(entity)
                return f"MEMORY ABOUT {entity}: two open threads."

            def entity_digest(self):
                return "KNOWN SUBJECTS: Atlas."

        fake = FakeRLM()
        with patch('utils.rlm.get_rlm', return_value=fake):
            loop, router = self._loop([
                tool_result('memory_about', json.dumps({'entity': 'Atlas'})),
                text_result("Atlas has two open threads, Sir."),
            ])
            out = loop.run("what's the state of Atlas?")
        self.assertEqual(fake.entities_queried, ['Atlas'])
        self.assertEqual(loop._executor.calls, [])
        tool_msg = [m for m in router.seen_messages[1]
                    if m['role'] == 'tool'][0]
        self.assertIn('two open threads', tool_msg['content'])

    def test_no_entity_lists_subjects(self):
        class FakeRLM:
            def recall_about(self, entity):
                return ''

            def entity_digest(self):
                return "KNOWN SUBJECTS: Atlas, Priya Sharma."

        with patch('utils.rlm.get_rlm', return_value=FakeRLM()):
            loop, router = self._loop([
                tool_result('memory_about', json.dumps({})),
                text_result("You are tracking Atlas and Priya Sharma."),
            ])
            loop.run("who am I tracking?")
        tool_msg = [m for m in router.seen_messages[1]
                    if m['role'] == 'tool'][0]
        self.assertIn('KNOWN SUBJECTS', tool_msg['content'])


class TestAnthropicConversion(unittest.TestCase):
    """OpenAI-style → Anthropic message conversion must keep native tool
    conversations intact (tool_use + tool_result blocks, alternating
    roles) — pure/offline, no network."""

    def _system_and_roles(self, msgs):
        system, out = AnthropicProvider._convert_messages(msgs)
        return system, [m['role'] for m in out], out

    def test_plain_chat_preserved(self):
        msgs = [{"role": "system", "content": "You are Jarvis."},
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello, Sir."}]
        system, roles, out = self._system_and_roles(msgs)
        self.assertEqual(system, "You are Jarvis.")
        self.assertEqual(roles, ['user', 'assistant'])
        self.assertEqual(out[0]['content'][0]['text'], 'Hi')

    def test_assistant_tool_calls_become_tool_use(self):
        msgs = [{"role": "user", "content": "weather?"},
                {"role": "assistant", "content": "",
                 "tool_calls": [{"id": "call_1", "type": "function",
                                 "function": {"name": "get_weather",
                                              "arguments":
                                                  '{"city": "Delhi"}'}}]},
                {"role": "tool", "tool_call_id": "call_1",
                 "content": "Sunny, 25C"},
                {"role": "assistant", "content": "It is sunny in Delhi."}]
        system, roles, out = self._system_and_roles(msgs)
        # The old converter dropped assistant tool_calls entirely and
        # emitted no user turn for the tool result.
        self.assertEqual(roles, ['user', 'assistant', 'user', 'assistant'])
        use = [b for b in out[1]['content']
               if b.get('type') == 'tool_use']
        self.assertEqual(len(use), 1)
        self.assertEqual(use[0]['id'], 'call_1')
        self.assertEqual(use[0]['name'], 'get_weather')
        self.assertEqual(use[0]['input'], {'city': 'Delhi'})
        result = [b for b in out[2]['content']
                  if b.get('type') == 'tool_result']
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['tool_use_id'], 'call_1')
        self.assertEqual(result[0]['content'], 'Sunny, 25C')

    def test_consecutive_tool_results_coalesce_into_one_user_turn(self):
        msgs = [{"role": "user", "content": "do two things"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "t1", "arguments": "{}"}},
                    {"id": "c2", "type": "function",
                     "function": {"name": "t2", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "r1"},
                {"role": "tool", "tool_call_id": "c2", "content": "r2"},
                {"role": "assistant", "content": "done"}]
        _system, roles, out = self._system_and_roles(msgs)
        self.assertEqual(roles, ['user', 'assistant', 'user', 'assistant'])
        user_results = out[2]['content']
        self.assertEqual(len([b for b in user_results
                              if b.get('type') == 'tool_result']), 2)


class TestKeystore(unittest.TestCase):

    def test_delete_keeps_remaining_keys_0600(self):
        tmp = _make_tmp()
        path = os.path.join(tmp, 'providers.json')
        ks = Keystore(path=path)
        ks.set('groq', 'g-secret-123')
        ks.set('anthropic', 'a-secret-456')
        # Deleting one key rewrites the overlay that still holds ANOTHER
        # secret — that rewrite must stay 0600 (set() is; a plain open()
        # would drop it to the default umask, often world-readable 0644).
        ks.delete('groq')
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(ks.get('anthropic'), 'a-secret-456')
        with patch.dict(os.environ, {'GROQ_API_KEY': ''}):
            self.assertIsNone(ks.get('groq'))


if __name__ == '__main__':
    unittest.main()
