"""
Phase A tests: subagent registry, skill registry, and reflection loops.

All external dependencies are mocked; persistence uses temp dirs.
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

# Keep self-learning artifacts OUT of the real store during tests
import tempfile as _tmpmod
import utils.self_learning as _sl
_sl.LESSON_FILE = os.path.join(_tmpmod.mkdtemp(prefix='jarvis_t_'), 'lessons.md')

from utils.agents import get_profile, register_profile, PROFILES, AgentProfile
from utils.skills import SkillRegistry, SkillError
from utils.coder import Coder
from utils.complex_task import (
    ComplexTaskManager, TaskStatus, StepStatus,
)
from utils.brain import Brain

# Brain.complete checks Config.GOOGLE_API_KEY; force cloud-Gemini off so
# the fake OpenAI-compatible client path is exercised deterministically.
from config import Config


def _make_tmp():
    return tempfile.mkdtemp(prefix="jarvis_phaseA_")


# ====================================================================== #
# Mocks
# ====================================================================== #

class MockMouth:
    def __init__(self):
        self.spoken = []
    def speak(self, text):
        self.spoken.append(text)


class ScriptedBrain:
    """
    Mock brain with per-agent scripting.

    complete(agent='critic') returns the scripted critic response;
    everything else returns generic text.
    """
    def __init__(self, critic_response=None):
        self.critic_response = critic_response
        self.critic_calls = 0
        self.calls = []

    def think(self, prompt, image_path=None):
        return {"action": "chat", "response": f"Mock: {prompt[:40]}"}

    def complete(self, prompt, system=None, timeout=None, **kw):
        self.calls.append({"agent": kw.get("agent"), "prompt": prompt})
        if kw.get("agent") == "critic" and self.critic_response is not None:
            self.critic_calls += 1
            return self.critic_response
        return f"MockComplete: {prompt[:50]}"


class FakeExecutor:
    """Minimal executor surface required by ComplexTaskManager."""
    def __init__(self, brain):
        self.brain = brain
        self.mouth = MockMouth()
        self.coder = type('MockCoder', (), {
            'execute_with_retry': lambda self, code, **kw:
                {'success': True, 'output': 'OK'}
        })()


def _wait(tm, task_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = tm.get_task(task_id)
        if t and t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                              TaskStatus.CANCELLED):
            return t
        time.sleep(0.05)
    return tm.get_task(task_id)


# ====================================================================== #
# Agent profiles
# ====================================================================== #

class TestAgentProfiles(unittest.TestCase):
    def test_builtin_profiles_exist(self):
        for name in ("planner", "researcher", "coder", "critic",
                     "synthesizer"):
            p = get_profile(name)
            self.assertIsInstance(p, AgentProfile, name)
            self.assertTrue(p.system_prompt.strip())
            self.assertTrue(p.description.strip())

    def test_unknown_profile_returns_none(self):
        self.assertIsNone(get_profile("nonexistent_agent"))

    def test_lookup_is_case_insensitive(self):
        self.assertEqual(get_profile("CODER").name, "coder")

    def test_empty_name_returns_none(self):
        self.assertIsNone(get_profile(None))
        self.assertIsNone(get_profile(""))

    def test_register_override(self):
        custom = AgentProfile("tester", "test persona", "You are a tester.",
                              temperature=0.7, max_tokens=111)
        try:
            register_profile(custom)
            self.assertIs(get_profile("tester"), custom)
        finally:
            PROFILES.pop("tester", None)


# ====================================================================== #
# Brain.complete profile resolution
# ====================================================================== #

class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Response:
    def __init__(self, content="fake-reply"):
        self.choices = [_Choice(content)]


class _Completions:
    def __init__(self, sink):
        self._sink = sink

    def create(self, model=None, messages=None, **kw):
        self._sink.append({"model": model, "messages": messages, **kw})
        return _Response()


class _Chat:
    def __init__(self, sink):
        self.completions = _Completions(sink)


class _FakeClient:
    def __init__(self, sink):
        self.chat = _Chat(sink)


def _bare_brain():
    """A Brain instance without __init__ (no keys, no history loading)."""
    b = Brain.__new__(Brain)
    b.active = True
    b.clients = []
    b.models = ["fake-model"]
    return b


class TestCompleteRouting(unittest.TestCase):
    def setUp(self):
        self.brain = _bare_brain()
        self.sink = []
        self.brain.clients = [_FakeClient(self.sink)]

    @patch.object(Config, 'GOOGLE_API_KEY', None)
    def test_default_completion_shape(self):
        out = self.brain.complete("hello")
        self.assertEqual(out, "fake-reply")
        call = self.sink[-1]
        system = call["messages"][0]["content"]
        self.assertIn("task-execution engine", system)

    @patch.object(Config, 'GOOGLE_API_KEY', None)
    def test_coder_agent_uses_profile(self):
        out = self.brain.complete("write something", agent='coder')
        self.assertEqual(out, "fake-reply")
        call = self.sink[-1]
        system = call["messages"][0]["content"]
        self.assertIn("CODER subagent", system)
        self.assertEqual(call["temperature"], 0.0)   # coder profile temp

    @patch.object(Config, 'GOOGLE_API_KEY', None)
    def test_explicit_overrides_beat_profile(self):
        self.brain.complete("x", agent='coder', temperature=0.5,
                            max_tokens=99)
        call = self.sink[-1]
        self.assertEqual(call["temperature"], 0.5)
        self.assertEqual(call["max_tokens"], 99)
        # System prompt still comes from the profile when not overridden
        self.assertIn("CODER subagent",
                      call["messages"][0]["content"])

    @patch.object(Config, 'GOOGLE_API_KEY', None)
    def test_explicit_system_beats_profile(self):
        self.brain.complete("x", agent='coder',
                            system="CUSTOM SYSTEM")
        self.assertEqual(self.sink[-1]["messages"][0]["content"],
                         "CUSTOM SYSTEM")

    @patch.object(Config, 'GOOGLE_API_KEY', None)
    def test_unknown_agent_falls_back_to_default(self):
        self.brain.complete("x", agent='who_dis')
        system = self.sink[-1]["messages"][0]["content"]
        self.assertIn("task-execution engine", system)

    @patch.object(Config, 'GOOGLE_API_KEY', None)
    def test_no_clients_returns_none(self):
        self.brain.clients = []
        self.assertIsNone(self.brain.complete("x"))


# ====================================================================== #
# Skill registry
# ====================================================================== #

class TestSkillRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = _make_tmp()
        self.reg = SkillRegistry(skills_dir=os.path.join(self.tmp, "skills"))
        self.ws = os.path.join(self.tmp, "ws")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _coder(self):
        return Coder(workspace_dir=self.ws)

    # --- save / validation --------------------------------------------- #
    def test_save_and_get_roundtrip(self):
        msg = self.reg.save_skill(
            "Word Count", "Counts words", "print('words:', len('{{text}}'.split()))",
            params={"text": "text to count"},
        )
        self.assertIn("saved", msg.lower())
        skill = self.reg.get_skill("word_count")
        self.assertIsNotNone(skill)
        self.assertEqual(skill["params"], {"text": "text to count"})
        self.assertIn("{{text}}", skill["code"])

    def test_save_rejects_bad_syntax(self):
        with self.assertRaises(SkillError):
            self.reg.save_skill("broken", "", "def oops(:\n    pass")

    def test_save_rejects_empty(self):
        with self.assertRaises(SkillError):
            self.reg.save_skill("", "d", "print(1)")
        with self.assertRaises(SkillError):
            self.reg.save_skill("x", "d", "   ")

    def test_placeholders_auto_declared(self):
        self.reg.save_skill("greet", "greets",
                            "print('{{name}}')")   # no params arg
        skill = self.reg.get_skill("greet")
        self.assertIn("name", skill["params"])

    def test_slugify(self):
        self.reg.save_skill("My Cool Skill!", "d", "print(1)")
        self.assertIsNotNone(self.reg.get_skill("my_cool_skill"))

    # --- list / delete / catalog --------------------------------------- #
    def test_list_delete_catalog(self):
        self.assertEqual(self.reg.list_skills(), [])
        self.assertEqual(self.reg.render_catalog(), "")

        self.reg.save_skill("alpha", "First skill", "print(1)")
        self.reg.save_skill("beta", "Second skill", "print(2)",
                            params={"x": "a value"})
        skills = self.reg.list_skills()
        self.assertEqual(len(skills), 2)
        names = {s["name"] for s in skills}
        self.assertEqual(names, {"alpha", "beta"})

        catalog = self.reg.render_catalog()
        self.assertIn("AVAILABLE SKILLS", catalog)
        self.assertIn("alpha", catalog)
        self.assertIn("beta", catalog)

        self.assertIn("deleted", self.reg.delete_skill("alpha").lower())
        self.assertIsNone(self.reg.get_skill("alpha"))
        self.assertEqual(len(self.reg.list_skills()), 1)

    # --- execution ------------------------------------------------------ #
    def test_run_with_params(self):
        self.reg.save_skill(
            "echo_it", "echoes text",
            "t = '''{{text}}'''\nprint(f'ECHO:{t.upper()}')",
            params={"text": "the text"},
        )
        res = self.reg.run("echo_it", params={"text": "hello"},
                           coder=self._coder())
        self.assertTrue(res['success'], res['output'])
        self.assertIn("ECHO:HELLO", res['output'])
        # usage telemetry bumped
        self.assertEqual(self.reg.get_skill("echo_it")["runs"], 1)

    def test_run_missing_param_raises(self):
        self.reg.save_skill("needs_x", "d", "print('{{x}}')",
                            params={"x": "required"})
        with self.assertRaises(SkillError) as cm:
            self.reg.run("needs_x", params={}, coder=self._coder())
        self.assertIn("missing", str(cm.exception).lower())

    def test_run_unknown_param_raises(self):
        self.reg.save_skill("only_a", "d", "print('{{a}}')",
                            params={"a": "val"})
        with self.assertRaises(SkillError) as cm:
            self.reg.run("only_a", params={"a": "1", "b": "2"},
                         coder=self._coder())
        self.assertIn("unexpected", str(cm.exception).lower())

    def test_run_unknown_skill_raises(self):
        with self.assertRaises(SkillError) as cm:
            self.reg.run("ghost_skill", coder=self._coder())
        self.assertIn("unknown", str(cm.exception).lower())

    def test_numeric_params_json_encoded(self):
        self.reg.save_skill("times_two", "d",
                            "n = {{n}}\nprint(n * 2)",
                            params={"n": "number"})
        res = self.reg.run("times_two", params={"n": 21},
                           coder=self._coder())
        self.assertTrue(res['success'])
        self.assertIn("42", res['output'])


# ====================================================================== #
# Reflection loop
# ====================================================================== #

_CRITIC_REVISE = json.dumps({
    "assessment": "original step used a bad approach",
    "revised_steps": [
        {"id": 1, "text": "recovery: retry with corrected approach",
         "depends_on": []}
    ],
})

_CRITIC_DECLINE = json.dumps({
    "assessment": "impossible without more data",
    "no_revision": True,
})


class ReflectionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = _make_tmp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _manager(self, critic_response=None, events=None):
        brain = ScriptedBrain(critic_response=critic_response)
        executor = FakeExecutor(brain)
        tm = ComplexTaskManager(
            executor=executor, brain=brain,
            ui_callback=(lambda e, d: events.append((e, d)))
                        if events is not None else None,
            data_dir=self.tmp,
        )
        tm.MAX_RETRIES_PER_STEP = 0
        orig = tm._dispatch_step

        def fail_boom(task, step):
            if "boom" in step.text.lower():
                raise RuntimeError("boom failure")
            return orig(task, step)

        tm._dispatch_step = fail_boom
        return tm, executor


class TestReflection(ReflectionBase):
    def test_failed_step_triggers_recovery_child(self):
        events = []
        tm, executor = self._manager(_CRITIC_REVISE, events)

        task = tm.create_task("Research X", [
            {"id": 1, "text": "good step A", "depends_on": []},
            {"id": 2, "text": "BOOM step B", "depends_on": [1]},
            {"id": 3, "text": "good step C", "depends_on": [2]},
        ])
        tm.start_task(task.id)
        t = _wait(tm, task.id)

        self.assertEqual(t.status, TaskStatus.COMPLETED)
        self.assertTrue(t.reflected)

        children = [x for x in tm._tasks.values() if x.parent_id == t.id]
        self.assertEqual(len(children), 1,
                         "critic revision must spawn exactly one child")
        child = children[0]
        self.assertEqual(child.status, TaskStatus.COMPLETED)
        self.assertTrue(
            all(s.status == StepStatus.COMPLETED for s in child.steps),
            f"recovery steps should succeed: "
            f"{[(s.id, s.status.value, s.error) for s in child.steps]}")

        # reflection event was emitted for the dashboard
        kinds = [e for e, _d in events]
        self.assertIn('task_reflection', kinds)

        # recovery results folded into parent context + synthesizer
        # actually received the recovery note
        self.assertIn("Recovery attempt", t.context)
        synth_prompts = [c["prompt"] for c in tm.brain.calls
                         if c.get("agent") == "synthesizer"]
        self.assertTrue(synth_prompts, "synthesizer must be invoked")
        self.assertTrue(
            any("rescu" in p.lower() or "recovery" in p.lower()
                for p in synth_prompts),
            "synthesis prompt must include the recovery outcome")
        self.assertTrue(t.final_summary)

    def test_critic_decline_leaves_no_children(self):
        tm, _executor = self._manager(_CRITIC_DECLINE)

        task = tm.create_task("D", ["BOOM only step"])
        tm.start_task(task.id)
        t = _wait(tm, task.id)

        self.assertEqual(t.status, TaskStatus.COMPLETED)
        self.assertTrue(t.reflected)
        children = [x for x in tm._tasks.values() if x.parent_id == t.id]
        self.assertEqual(children, [])

    def test_unparsable_critic_output_is_safe(self):
        tm, _executor = self._manager(critic_response=None)  # generic text
        task = tm.create_task("U", ["BOOM step"])
        tm.start_task(task.id)
        t = _wait(tm, task.id)
        self.assertEqual(t.status, TaskStatus.COMPLETED)
        self.assertTrue(t.reflected)
        self.assertEqual([x for x in tm._tasks.values()
                          if x.parent_id == t.id], [])

    def test_clean_task_never_reflects(self):
        tm, _executor = self._manager(_CRITIC_REVISE)
        orig = tm._dispatch_step
        task = tm.create_task("Clean", ["fine one", "fine two"])
        tm.start_task(task.id)
        t = _wait(tm, task.id)
        self.assertEqual(t.status, TaskStatus.COMPLETED)
        self.assertFalse(t.reflected)
        brain = tm.brain
        self.assertEqual(brain.critic_calls, 0)
        self.assertEqual([x for x in tm._tasks.values()
                          if x.parent_id == t.id], [])

    def test_reflection_bounded_to_one_cycle(self):
        """Even when the recovery plan ALSO fails, no second cycle runs
        and the parent still terminates cleanly."""
        critic_loop = json.dumps({
            "assessment": "try again",
            "revised_steps": [
                {"id": 1, "text": "BOOM again", "depends_on": []}],
        })
        tm, _executor = self._manager(critic_loop)
        task = tm.create_task("Loop", ["BOOM first"])
        tm.start_task(task.id)
        t = _wait(tm, task.id)

        self.assertEqual(t.status, TaskStatus.COMPLETED)
        self.assertTrue(t.reflected)
        children = [x for x in tm._tasks.values() if x.parent_id == t.id]
        self.assertEqual(len(children), 1)
        # Child's own steps failed but child never spawned grandchildren
        grandchildren = [x for x in tm._tasks.values()
                         if x.parent_id == children[0].id]
        self.assertEqual(grandchildren, [])
        self.assertFalse(children[0].reflected)


# ====================================================================== #

if __name__ == '__main__':
    print("=" * 60)
    print("JARVIS PHASE A — AGENTS / SKILLS / REFLECTION TESTS")
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
