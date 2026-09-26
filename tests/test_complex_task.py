"""
Integration tests for Jarvis ComplexTaskManager.

All external dependencies (Brain, Coder, web reader) are mocked.
Persistence is isolated into a per-test temp directory.
"""

import os
import sys
import time
import json
import shutil
import tempfile
import threading
import datetime
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Keep self-learning artifacts OUT of the real store during tests
import tempfile as _tmpmod
import utils.self_learning as _sl
_sl.LESSON_FILE = os.path.join(_tmpmod.mkdtemp(prefix='jarvis_t_'), 'lessons.md')

from utils.complex_task import (
    ComplexTaskManager, ComplexTask, TaskStep,
    StepType, StepStatus, TaskStatus,
    _detect_cycle, _topological_layers, _classify_step,
    normalize_step_specs,
)
from utils.coder import Coder


# ====================================================================== #
# Mocks
# ====================================================================== #

class MockBrain:
    """Implements both think() (agent loop) and complete() (raw)."""
    def __init__(self):
        self.call_count = 0
        self.complete_calls = []
        self.complete_should_fail = False

    def think(self, prompt, image_path=None):
        self.call_count += 1
        return {"action": "chat", "response": f"Mock: {prompt[:60]}"}

    def complete(self, prompt, system=None, timeout=None, **kw):
        if self.complete_should_fail:
            return None
        self.complete_calls.append(prompt)
        return f"MockComplete: {prompt[:60]}"


class MockMouth:
    def __init__(self):
        self.spoken = []
    def speak(self, text):
        self.spoken.append(text)


class MockExecutor:
    def __init__(self, brain=None, data_dir=None):
        self.brain = brain or MockBrain()
        self.coder = type('MockCoder', (), {
            'execute_with_retry': lambda self, code, **kw: {'success': True, 'output': 'OK'}
        })()
        self.mouth = MockMouth()
        self._data_dir = data_dir

    @property
    def task_manager(self):
        if not hasattr(self, '_tm'):
            self._tm = ComplexTaskManager(
                executor=self, brain=self.brain,
                ui_callback=lambda e, d: None,
                data_dir=self._data_dir,
            )
        return self._tm


def _wait(tm, task_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = tm.get_task(task_id)
        if t and t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            return t
        time.sleep(0.05)
    return tm.get_task(task_id)


def _make_tmp():
    return tempfile.mkdtemp(prefix="jarvis_test_")


# Patch web reader so no network calls happen
_MOCK_WEB = patch('utils.web_reader.get_answer_from_web',
                  return_value="Mock web content")


class IsolatedTestCase(unittest.TestCase):
    """Base class giving each test a temp persistence dir."""

    def setUp(self):
        self.tmp = _make_tmp()
        self.executor = MockExecutor(data_dir=self.tmp)
        self.tm = self.executor.task_manager

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ====================================================================== #
# DAG helper tests
# ====================================================================== #

class TestDAGHelpers(unittest.TestCase):
    def test_no_deps(self):
        sm = {'1': {'id': '1', 'text': 'A', 'depends_on': []},
              '2': {'id': '2', 'text': 'B', 'depends_on': []}}
        self.assertFalse(_detect_cycle(sm)[0])
        layers = _topological_layers(sm)
        self.assertEqual(len(layers), 1)
        self.assertEqual(set(layers[0]), {'1', '2'})

    def test_chain(self):
        sm = {'1': {'id': '1', 'text': 'A', 'depends_on': []},
              '2': {'id': '2', 'text': 'B', 'depends_on': ['1']},
              '3': {'id': '3', 'text': 'C', 'depends_on': ['2']}}
        self.assertFalse(_detect_cycle(sm)[0])
        self.assertEqual(len(_topological_layers(sm)), 3)

    def test_diamond(self):
        sm = {'1': {'id': '1', 'text': 'A', 'depends_on': []},
              '2': {'id': '2', 'text': 'B', 'depends_on': []},
              '3': {'id': '3', 'text': 'C', 'depends_on': ['1', '2']}}
        self.assertFalse(_detect_cycle(sm)[0])
        layers = _topological_layers(sm)
        self.assertEqual(len(layers), 2)
        self.assertEqual(set(layers[0]), {'1', '2'})
        self.assertEqual(set(layers[1]), {'3'})

    def test_cycle(self):
        sm = {'1': {'id': '1', 'text': 'A', 'depends_on': ['2']},
              '2': {'id': '2', 'text': 'B', 'depends_on': ['1']}}
        cycle, _ = _detect_cycle(sm)
        self.assertTrue(cycle)

    def test_self_dep_removed(self):
        """Self-dependencies are repaired away (dropped with a warning),
        leaving a plain runnable step rather than a poisoned graph."""
        sm = {'1': {'id': '1', 'text': 'A', 'depends_on': [1]}}
        layers = _topological_layers(sm)
        self.assertEqual(len(layers), 1)
        self.assertEqual(layers[0], ['1'])

    def test_complex(self):
        sm = {
            '1': {'id': '1', 'text': 'A', 'depends_on': []},
            '2': {'id': '2', 'text': 'B', 'depends_on': []},
            '3': {'id': '3', 'text': 'C', 'depends_on': []},
            '4': {'id': '4', 'text': 'D', 'depends_on': ['1']},
            '5': {'id': '5', 'text': 'E', 'depends_on': ['2']},
            '6': {'id': '6', 'text': 'F', 'depends_on': ['3', '4', '5']},
        }
        self.assertFalse(_detect_cycle(sm)[0])
        layers = _topological_layers(sm)
        self.assertEqual(len(layers), 3)
        self.assertEqual(set(layers[0]), {'1', '2', '3'})
        self.assertEqual(set(layers[1]), {'4', '5'})
        self.assertEqual(set(layers[2]), {'6'})

    # --- Regression: type mismatches between keys and deps ------------- #

    def test_int_deps_vs_str_keys(self):
        """LLMs emit int deps while layers used str keys — deps were
        silently dropped and everything collapsed into one layer."""
        sm = {'1': {'id': '1', 'text': 'A', 'depends_on': []},
              '2': {'id': '2', 'text': 'B', 'depends_on': [1]}}
        layers = _topological_layers(sm)
        self.assertEqual(len(layers), 2)
        self.assertEqual(set(layers[0]), {'1'})
        self.assertEqual(set(layers[1]), {'2'})

    def test_str_ids_vs_int_deps_chain(self):
        sm = {1: {'id': 1, 'text': 'A', 'depends_on': []},
              2: {'id': 2, 'text': 'B', 'depends_on': ["1"]},
              3: {'id': 3, 'text': 'C', 'depends_on': [2]}}
        self.assertFalse(_detect_cycle(sm)[0])
        self.assertEqual(len(_topological_layers(sm)), 3)


class TestClassify(unittest.TestCase):
    def test_code(self):
        self.assertEqual(_classify_step("Write Python code to sort"), StepType.CODE)
    def test_search(self):
        self.assertEqual(_classify_step("Research latest AI news"), StepType.SEARCH)
    def test_chat(self):
        self.assertEqual(_classify_step("Explain quantum computing"), StepType.CHAT)

    def test_process_not_treated_as_code(self):
        """'process' is a common English verb, not a code-intent signal.
        'Process the customer order' was misclassified as StepType.CODE
        (which would execute Python against a non-code instruction)."""
        self.assertEqual(_classify_step("Process the customer order"),
                         StepType.CHAT)
        self.assertEqual(_classify_step("Process the user's request"),
                         StepType.CHAT)

    def test_tool_action_verbs(self):
        """Concrete tool verbs map to TOOL, not code."""
        self.assertEqual(_classify_step("Download the spreadsheet"),
                         StepType.TOOL)
        self.assertEqual(_classify_step("Send email to the team"),
                         StepType.TOOL)

    def test_tie_prefers_code(self):
        """When a step matches both CODE and SEARCH equally, CODE wins
        (the documented bias — favors executable plans)."""
        # 'compare' is SEARCH; 'analyze data' is CODE — one each, tie.
        self.assertEqual(_classify_step("Compare and analyze data sets"),
                         StepType.CODE)


class TestSerialization(unittest.TestCase):
    def test_step_roundtrip(self):
        s = TaskStep(3, "X", depends_on=[1, 2])
        d = s.to_dict()
        s2 = TaskStep.from_dict(d)
        self.assertEqual(s2.depends_on, [1, 2])

    def test_task_roundtrip(self):
        t = ComplexTask('x', 'd', [TaskStep(1, 'A'), TaskStep(2, 'B', depends_on=[1])])
        t2 = ComplexTask.from_dict(t.to_dict())
        self.assertTrue(t2.has_dependencies())

    def test_mixed_id_roundtrip(self):
        s = TaskStep("a1", "X", depends_on=[1, "b2"])
        s2 = TaskStep.from_dict(s.to_dict())
        self.assertEqual(s2.depends_on, [1, "b2"])


# ====================================================================== #
# normalize_step_specs
# ====================================================================== #

class TestNormalize(unittest.TestCase):
    def test_plain_strings(self):
        mode, specs = normalize_step_specs(["A", "B", "C"])
        self.assertEqual(mode, "sequential")
        self.assertEqual([s['id'] for s in specs], [1, 2, 3])
        self.assertTrue(all(s['depends_on'] == [] for s in specs))

    def test_dicts_without_deps(self):
        mode, specs = normalize_step_specs([{"text": "X"}, {"text": "Y"}])
        self.assertEqual(mode, "sequential")
        self.assertEqual(len(specs), 2)

    def test_full_deps(self):
        mode, specs = normalize_step_specs([
            {"id": 1, "text": "A", "depends_on": []},
            {"id": 2, "text": "B", "depends_on": [1]},
            {"id": 3, "text": "C", "depends_on": [1, 2]},
        ])
        self.assertEqual(mode, "deps")
        self.assertEqual(specs[2]['depends_on'], [1, 2])

    def test_string_ids_canonicalized(self):
        """String ids/deps from the LLM must not corrupt the graph."""
        mode, specs = normalize_step_specs([
            {"id": "1", "text": "A", "depends_on": []},
            {"id": "2", "text": "B", "depends_on": ["1"]},
        ])
        self.assertEqual(mode, "deps")
        self.assertEqual(specs[0]['id'], 1)          # "1" -> 1
        self.assertEqual(specs[1]['id'], 2)          # "2" -> 2
        self.assertEqual(specs[1]['depends_on'], [1])  # "1" -> 1

    def test_unknown_dep_dropped(self):
        mode, specs = normalize_step_specs([
            {"id": 1, "text": "A", "depends_on": [99]},
        ])
        self.assertEqual(mode, "sequential")   # dep was bogus → no deps left
        self.assertEqual(specs[0]['depends_on'], [])

    def test_self_dep_removed(self):
        mode, specs = normalize_step_specs([
            {"id": 1, "text": "A", "depends_on": [1]},
        ])
        self.assertEqual(mode, "sequential")

    def test_empty_text_dropped(self):
        mode, specs = normalize_step_specs(["", "   ", {"text": ""}, "Real"])
        self.assertEqual(mode, "sequential")
        self.assertEqual(len(specs), 1)

    def test_mixed_strings_and_dicts(self):
        mode, specs = normalize_step_specs([
            "Plain",
            {"id": 10, "text": "Fancy", "type": "search"},
        ])
        self.assertEqual(len(specs), 2)
        self.assertEqual(specs[0]['id'], 1)
        self.assertEqual(specs[1]['id'], 10)
        self.assertEqual(specs[1].get('type'), 'search')

    def test_alternate_text_keys(self):
        mode, specs = normalize_step_specs([
            {"description": "Do the thing"},
            {"step": "Another thing"},
        ])
        self.assertEqual([s['text'] for s in specs],
                         ["Do the thing", "Another thing"])

    def test_non_list_input(self):
        mode, specs = normalize_step_specs({"text": "Solo"})
        self.assertEqual(len(specs), 1)

    def test_float_ids(self):
        mode, specs = normalize_step_specs([
            {"id": 1.0, "text": "A", "depends_on": []},
            {"id": 2.0, "text": "B", "depends_on": [1.0]},
        ])
        self.assertEqual(mode, "deps")


# ====================================================================== #
# Execution
# ====================================================================== #

class TestSequential(IsolatedTestCase):
    @_MOCK_WEB
    def test_basic(self, *_):
        tm = self.tm
        task = tm.create_task("Summarize AI", [
            "Explain what AI is", "List top 3 applications",
        ])
        ok, msg = tm.start_task(task.id)
        self.assertTrue(ok, msg)
        t = _wait(tm, task.id)
        self.assertEqual(t.status, TaskStatus.COMPLETED)
        for s in t.steps:
            self.assertEqual(s.status, StepStatus.COMPLETED)

    @_MOCK_WEB
    def test_empty_steps_guarded(self, *_):
        task = self.tm.create_task("Nothing", [])
        self.assertEqual(len(task.steps), 1)   # falls back to description

    @_MOCK_WEB
    def test_uses_complete_not_think(self, *_):
        """Internal step execution must not pollute agent history."""
        tm = self.tm
        task = tm.create_task("T", ["A", "B"])
        tm.start_task(task.id)
        _wait(tm, task.id)
        self.assertGreaterEqual(len(tm.brain.complete_calls), 2)
        self.assertEqual(tm.brain.call_count, 0)  # think() never called


class TestDAG(IsolatedTestCase):
    @_MOCK_WEB
    def test_int_id_dag_executes_all_steps(self, *_):
        """
        REGRESSION for the killer bug: with int IDs + int deps, layers
        collapsed and step_by_id never matched, so tasks reported
        COMPLETED with every step still PENDING.
        """
        tm = self.tm
        lock = threading.Lock()
        executed = []
        orig = tm._dispatch_step

        def tracked(task, step):
            r = orig(task, step)
            with lock:
                executed.append(step.id)
            return r

        tm._dispatch_step = tracked
        task = tm.create_task_with_deps("Compare", [
            {"id": 1, "text": "Explain React", "depends_on": []},
            {"id": 2, "text": "Explain Vue", "depends_on": []},
            {"id": 3, "text": "Compare both", "depends_on": [1, 2]},
        ])
        ok, msg = tm.start_task(task.id)
        self.assertTrue(ok, msg)
        t = _wait(tm, task.id)

        self.assertEqual(t.status, TaskStatus.COMPLETED)
        for s in t.steps:
            self.assertEqual(s.status, StepStatus.COMPLETED,
                             f"step {s.id} not completed: {s.error}")
            self.assertIsNotNone(s.result)
        # Dependency order respected
        self.assertLess(executed.index(1), executed.index(3))
        self.assertLess(executed.index(2), executed.index(3))

    @_MOCK_WEB
    def test_steps_run_truly_in_parallel(self, *_):
        """Two independent steps must be IN FLIGHT simultaneously."""
        tm = self.tm
        started = {1: threading.Event(), 2: threading.Event()}
        release = threading.Event()
        lock = threading.Lock()
        threads_seen = {}
        orig = tm._dispatch_step

        def gated(task, step):
            if step.id in started:
                started[step.id].set()
                release.wait(timeout=5)
            r = orig(task, step)
            if step.id in started:
                with lock:
                    # get_ident is unique per live thread; names can
                    # repeat across per-step watchdog pools
                    threads_seen[step.id] = threading.get_ident()
            return r

        tm._dispatch_step = gated
        task = tm.create_task_with_deps("Compare", [
            {"id": 1, "text": "Explain React", "depends_on": []},
            {"id": 2, "text": "Explain Vue", "depends_on": []},
            {"id": 3, "text": "Compare both", "depends_on": [1, 2]},
        ])
        self.assertTrue(tm.start_task(task.id)[0])
        self.assertTrue(started[1].wait(2))
        self.assertTrue(
            started[2].wait(2),
            "Step 2 never started while step 1 blocked — no parallelism"
        )
        release.set()

        t = _wait(tm, task.id)
        self.assertEqual(t.status, TaskStatus.COMPLETED)
        self.assertNotEqual(threads_seen[1], threads_seen[2])

    @_MOCK_WEB
    def test_mixed_type_ids(self, *_):
        tm = self.tm
        task = tm.create_task_with_deps("Mixed", [
            {"id": "1", "text": "A", "depends_on": []},
            {"id": 2, "text": "B", "depends_on": ["1"]},   # str dep vs int id
        ])
        self.assertTrue(task.has_dependencies())
        tm.start_task(task.id)
        t = _wait(tm, task.id)
        self.assertEqual(t.status, TaskStatus.COMPLETED)
        self.assertTrue(all(s.status == StepStatus.COMPLETED
                            for s in t.steps))

    @_MOCK_WEB
    def test_string_ids_non_numeric(self, *_):
        tm = self.tm
        task = tm.create_task_with_deps("Chain", [
            {"id": "a", "text": "A", "depends_on": []},
            {"id": "b", "text": "B", "depends_on": ["a"]},
            {"id": "c", "text": "C", "depends_on": ["b"]},
        ])
        tm.start_task(task.id)
        t = _wait(tm, task.id)
        self.assertEqual(t.status, TaskStatus.COMPLETED)
        self.assertTrue(all(s.status == StepStatus.COMPLETED
                            for s in t.steps))


class TestCancel(IsolatedTestCase):
    @_MOCK_WEB
    def test_cancel(self, *_):
        tm = self.tm
        task = tm.create_task("Long", ["A", "B", "C", "D"])
        tm.start_task(task.id)
        time.sleep(0.2)
        tm.cancel_task(task.id)
        t = _wait(tm, task.id, timeout=3)
        self.assertEqual(t.status, TaskStatus.CANCELLED)


class TestPauseResume(IsolatedTestCase):
    @_MOCK_WEB
    def test_pause_resume(self, *_):
        """Deterministic: hold step 2 hostage so pause lands mid-run."""
        tm = self.tm
        step2_running = threading.Event()
        release = threading.Event()
        orig = tm._dispatch_step

        def gated(task, step):
            if step.id == 2:
                step2_running.set()
                release.wait(timeout=5)
            return orig(task, step)

        tm._dispatch_step = gated
        task = tm.create_task("P", ["A", "B", "C"])
        self.assertTrue(tm.start_task(task.id)[0])
        self.assertTrue(step2_running.wait(2))

        self.assertIn("paus", tm.pause_task(task.id).lower())
        self.assertEqual(tm.get_task(task.id).status, TaskStatus.PAUSED)

        release.set()
        time.sleep(0.15)   # let step 2 finish; checkpoint must hold
        t = tm.get_task(task.id)
        self.assertEqual(t.status, TaskStatus.PAUSED,
                         "Task kept running while paused")
        self.assertEqual(t.steps[2].status, StepStatus.PENDING)

        self.assertIn("resum", tm.resume_task(task.id).lower())
        t = _wait(tm, task.id)
        self.assertEqual(t.status, TaskStatus.COMPLETED)
        self.assertEqual(t.steps[2].status, StepStatus.COMPLETED)


class TestErrorIsolation(IsolatedTestCase):
    @_MOCK_WEB
    def test_failed_step_continues(self, *_):
        tm = self.tm
        tm.MAX_RETRIES_PER_STEP = 0   # keep test fast
        orig = tm._dispatch_step

        def fail_step2(task, step):
            if step.id == 2:
                raise RuntimeError("Step 2 failed")
            return orig(task, step)

        tm._dispatch_step = fail_step2
        task = tm.create_task("Err", ["A", "B", "C"])
        tm.start_task(task.id)
        t = _wait(tm, task.id)
        self.assertEqual(t.status, TaskStatus.COMPLETED)
        self.assertEqual(t.steps[0].status, StepStatus.COMPLETED)
        self.assertEqual(t.steps[1].status, StepStatus.FAILED)
        self.assertIsNotNone(t.steps[1].error)
        self.assertEqual(t.steps[2].status, StepStatus.COMPLETED)


class TestDepFailurePropagation(IsolatedTestCase):
    @_MOCK_WEB
    def test_dependents_skipped_cascading(self, *_):
        """Steps whose dependencies failed/skipped must NOT run."""
        tm = self.tm
        tm.MAX_RETRIES_PER_STEP = 0
        orig = tm._dispatch_step
        ran = []
        lock = threading.Lock()

        def maybe_fail(task, step):
            if step.id == 2:
                raise RuntimeError("boom")
            r = orig(task, step)
            with lock:
                ran.append(step.id)
            return r

        tm._dispatch_step = maybe_fail
        task = tm.create_task_with_deps("Cascade", [
            {"id": 1, "text": "ok", "depends_on": []},
            {"id": 2, "text": "boom", "depends_on": []},
            {"id": 3, "text": "needs 1", "depends_on": [1]},
            {"id": 4, "text": "needs 2", "depends_on": [2]},
            {"id": 5, "text": "needs 4", "depends_on": [4]},
        ])
        tm.start_task(task.id)
        t = _wait(tm, task.id)

        statuses = {s.id: s.status for s in t.steps}
        self.assertEqual(statuses[1], StepStatus.COMPLETED)
        self.assertEqual(statuses[2], StepStatus.FAILED)
        self.assertEqual(statuses[3], StepStatus.COMPLETED)
        self.assertEqual(statuses[4], StepStatus.SKIPPED,
                         "dependent of failed step must be skipped")
        self.assertEqual(statuses[5], StepStatus.SKIPPED,
                         "skip must cascade transitively")
        self.assertIn("dependency", (t.steps[4].error or "").lower())
        # Skipped steps never dispatched
        self.assertNotIn(4, ran)
        self.assertNotIn(5, ran)


class TestTimeout(IsolatedTestCase):
    @_MOCK_WEB
    def test_hung_step_fails_fast_and_task_continues(self, *_):
        tm = self.tm
        tm.STEP_TIMEOUT = 0.25          # watchdog fires quickly
        tm.MAX_RETRIES_PER_STEP = 0     # timeouts don't retry anyway
        orig = tm._dispatch_step

        def hung_second_step(task, step):
            if step.id == 2:
                time.sleep(1.5)         # far beyond budget
            return orig(task, step)

        tm._dispatch_step = hung_second_step
        task = tm.create_task("TO", ["A", "B", "C"])
        start = time.time()
        tm.start_task(task.id)
        t = _wait(tm, task.id, timeout=4)
        elapsed = time.time() - start

        self.assertEqual(t.status, TaskStatus.COMPLETED)
        self.assertEqual(t.steps[1].status, StepStatus.FAILED)
        self.assertIn("budget", t.steps[1].error.lower())
        self.assertEqual(t.steps[0].status, StepStatus.COMPLETED)
        self.assertEqual(t.steps[2].status, StepStatus.COMPLETED)
        # Must have bailed at ~0.25s, not waited the full 1.5s hang
        self.assertLess(elapsed, 1.4)


# ====================================================================== #
# Persistence / restart recovery
# ====================================================================== #

class TestPersistence(IsolatedTestCase):
    @_MOCK_WEB
    def test_save_load(self, *_):
        tm = self.tm
        task = tm.create_task("P", ["A", "B"])
        task.steps[0].status = StepStatus.COMPLETED
        task.steps[0].result = "Done"
        task.status = TaskStatus.COMPLETED
        task.completed_at = datetime.datetime.now().isoformat()
        tm._save_task(task)

        tm2 = ComplexTaskManager(executor=tm.executor,
                                 brain=tm.brain, data_dir=self.tmp)
        loaded = tm2.get_task(task.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.status, TaskStatus.COMPLETED)
        self.assertEqual(loaded.steps[0].result, "Done")


class TestRestartRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = _make_tmp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_task(self, task):
        path = os.path.join(self.tmp, f"{task.id}.json")
        with open(path, 'w') as f:
            json.dump(task.to_dict(), f)
        return path

    def test_interrupted_running_task_parked_as_paused(self):
        """A task mid-flight when the process died must come back as
        PAUSED (manually resumable), not as a ghost RUNNING task."""
        task = ComplexTask('rt1', 'Interrupted work',
                           [TaskStep(1, 'A'), TaskStep(2, 'B')])
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.datetime.now().isoformat()
        task.steps[0].status = StepStatus.RUNNING
        self._write_task(task)

        executor = MockExecutor(data_dir=self.tmp)
        tm = executor.task_manager
        loaded = tm.get_task('rt1')
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.status, TaskStatus.PAUSED)
        self.assertEqual(loaded.steps[0].status, StepStatus.PENDING)
        self.assertIn("interrupt", (loaded.steps[0].error or "").lower())
        # And it can actually be resumed and finished
        self.assertTrue(tm.start_task('rt1')[0])
        t = _wait(tm, 'rt1')
        self.assertEqual(t.status, TaskStatus.COMPLETED)

    def test_pending_task_parked_as_paused(self):
        task = ComplexTask('rt2', 'Never started', [TaskStep(1, 'A')])
        task.status = TaskStatus.PENDING
        self._write_task(task)
        tm = MockExecutor(data_dir=self.tmp).task_manager
        self.assertEqual(tm.get_task('rt2').status, TaskStatus.PAUSED)

    def test_old_terminal_tasks_pruned(self):
        task = ComplexTask('old1', 'Ancient history', [TaskStep(1, 'A')])
        task.status = TaskStatus.COMPLETED
        old = datetime.datetime.now() - datetime.timedelta(days=30)
        task.created_at = old.isoformat()
        task.completed_at = old.isoformat()
        path = self._write_task(task)
        self.assertTrue(os.path.exists(path))

        tm = MockExecutor(data_dir=self.tmp).task_manager
        self.assertIsNone(tm.get_task('old1'))
        self.assertFalse(os.path.exists(path))       # pruned from disk

    def test_recent_terminal_tasks_kept(self):
        task = ComplexTask('new1', 'Recent win', [TaskStep(1, 'A')])
        task.status = TaskStatus.COMPLETED
        task.completed_at = datetime.datetime.now().isoformat()
        path = self._write_task(task)
        tm = MockExecutor(data_dir=self.tmp).task_manager
        self.assertIsNotNone(tm.get_task('new1'))
        self.assertTrue(os.path.exists(path))

    def test_corrupt_file_skipped(self):
        with open(os.path.join(self.tmp, 'bad.json'), 'w') as f:
            f.write("{not json at all")
        tm = MockExecutor(data_dir=self.tmp).task_manager  # must not raise
        self.assertEqual(len(tm.get_active_tasks()), 0)


class TestHistory(unittest.TestCase):
    def test_stats(self):
        executor = MockExecutor(data_dir=_make_tmp())
        tm = executor.task_manager
        now = datetime.datetime.now().isoformat()
        for i, (st, desc) in enumerate([
            (TaskStatus.COMPLETED, "Done 1"),
            (TaskStatus.FAILED, "Fail 1"),
            (TaskStatus.COMPLETED, "Done 2"),
            (TaskStatus.CANCELLED, "Can 1"),
        ]):
            t = ComplexTask(f'h{i}', desc, [TaskStep(1, 'A'), TaskStep(2, 'B')])
            t.status = st
            t.created_at = now
            t.completed_at = now
            tm._tasks[f'h{i}'] = t

        self.assertEqual(len(tm.get_active_tasks()), 0)
        self.assertEqual(len(tm.get_history()), 4)
        stats = tm.get_history_stats()
        self.assertEqual(stats['completed'], 2)
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(stats['cancelled'], 1)


# ====================================================================== #
# Brain-format handling via the public API
# ====================================================================== #

class TestBrainFormat(IsolatedTestCase):
    def test_string_sequential(self):
        tm = self.tm
        steps = ["A", "B"]
        mode, specs = normalize_step_specs(steps)
        self.assertEqual(mode, "sequential")
        t = tm.create_task("T", steps)
        self.assertFalse(t.has_dependencies())

    def test_dict_parallel(self):
        tm = self.tm
        steps = [{"id": 1, "text": "A", "depends_on": []},
                 {"id": 2, "text": "B", "depends_on": [1]}]
        t = tm.create_task_with_deps("T", steps)
        self.assertTrue(t.has_dependencies())

    def test_dicts_without_dep_keys_stay_sequential(self):
        """Old executor treated any dict list without depends_on on
        step[0] by stringifying dicts — now they become clean steps."""
        tm = self.tm
        steps = [{"id": 1, "text": "A"}, {"id": 2, "text": "B"}]
        t = tm.create_task_with_deps("T", steps)
        self.assertFalse(t.has_dependencies())
        self.assertEqual(t.steps[0].text, "A")      # not "{'id': 1, ...}"
        self.assertEqual(t.steps[1].id, 2)


# ====================================================================== #
# Autonomy hooks — goal progress + completion announcements.
# The _run_task hook calls task._goal_progress_for + notifier.announce;
# both are fully stubbed here (no goals store writes, no sockets).
# ====================================================================== #

class TestGoalProgressHook(unittest.TestCase):
    def test_progress_for_unlinked_task_empty(self):
        import utils.complex_task as ct
        import utils.goals as goals_mod

        class FakeStore:
            def list(self, include_done=False):
                return [{'id': 'g1', 'task_ids': ['other-task'],
                         'title': 'Other'}]

        orig = goals_mod.get_goals
        goals_mod.get_goals = lambda *a, **k: FakeStore()
        try:
            mgr = ct.ComplexTaskManager.__new__(
                ct.ComplexTaskManager)
            mgr.get_task = lambda tid: None
            self.assertEqual(mgr._goal_progress_for('nope'), [])
        finally:
            goals_mod.get_goals = orig

    def test_progress_averages_linked_tasks(self):
        import utils.complex_task as ct
        import utils.goals as goals_mod

        class FakeTask:
            def __init__(self, pct):
                self._pct = pct
            def progress_pct(self):
                return self._pct

        class FakeStore:
            def list(self, include_done=False):
                return [{'id': 'g1', 'task_ids': ['t1', 't2'],
                         'title': 'Goal'}]

        orig = goals_mod.get_goals
        goals_mod.get_goals = lambda *a, **k: FakeStore()
        try:
            mgr = ct.ComplexTaskManager.__new__(ct.ComplexTaskManager)
            mgr.get_task = lambda tid: {'t1': FakeTask(100),
                                        't2': FakeTask(50)}.get(tid)
            self.assertEqual(mgr._goal_progress_for('t1'),
                             [('g1', 75)])
            # back-compat alias resolves identically
            self.assertEqual(mgr._goal_progress('t1'), [('g1', 75)])
        finally:
            goals_mod.get_goals = orig

    def test_run_task_hook_announces_and_links(self):
        # A completed task with no linked goal still announces via the
        # notifier (stubbed) and never raises without a goals store.
        import utils.complex_task as ct
        import utils.notify as notify_mod

        announced = []
        orig_nb = notify_mod.get_notifier

        class FakeNb:
            def announce(self, text, priority='normal'):
                announced.append((text, priority))
                return True

        notify_mod.get_notifier = lambda *a, **k: FakeNb()
        try:
            mgr = ct.ComplexTaskManager.__new__(ct.ComplexTaskManager)
            mgr._lock = threading.Lock()
            mgr._active_threads = {}
            mgr._tasks = {}
            mgr._pause_events = {}
            mgr._context_locks = {}
            mgr.get_task = lambda tid: mgr._tasks.get(tid)
            mgr._save_task = lambda t: None
            mgr._emit_task_update = lambda t: None
            mgr._emit = lambda e, d: None
            import utils.self_learning as sl
            orig_review = sl.review_task
            sl.review_task = lambda *a, **k: None
            try:
                import utils.goals as goals_mod
                orig_goals = goals_mod.get_goals
                orig_check = goals_mod.check_goal_completion
                orig_on = goals_mod.enabled
                goals_mod.get_goals = lambda *a, **k: (_ for _ in ()).throw(
                    RuntimeError("no store"))
                goals_mod.check_goal_completion = lambda **k: []
                goals_mod.enabled = lambda: True
                try:
                    task = ct.ComplexTask(
                        't1', 'Do the thing',
                        [ct.TaskStep(1, 'step one')])
                    task.status = ct.TaskStatus.RUNNING
                    mgr._tasks = {'t1': task}
                    mgr.brain = MockBrain()
                    mgr.executor = MockExecutor()
                    # No LLM, no subprocess: pretend the step ran.
                    mgr._execute_step = lambda t, s: (
                        setattr(s, 'status',
                                ct.StepStatus.COMPLETED),
                        setattr(s, 'result', 'ok'))
                    mgr._reflect = lambda t: None
                    mgr._synthesize = lambda t, recovery=None: "Done."
                    mgr._run_task('t1')
                    self.assertEqual(task.status, ct.TaskStatus.COMPLETED)
                    self.assertTrue(
                        any('Background task done' in a[0]
                            for a in announced))
                finally:
                    goals_mod.get_goals = orig_goals
                    goals_mod.check_goal_completion = orig_check
                    goals_mod.enabled = orig_on
            finally:
                sl.review_task = orig_review
        finally:
            notify_mod.get_notifier = orig_nb


# ====================================================================== #
# Coder (real execution engine)
# ====================================================================== #

class TestCoder(unittest.TestCase):
    def setUp(self):
        self.ws = _make_tmp()
        self.coder = Coder(workspace_dir=self.ws)

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_success_stdout(self):
        res = self.coder.execute_with_retry("print('hello')")
        self.assertTrue(res['success'])
        self.assertIn('hello', res['output'])

    def test_stderr_warning_is_not_failure(self):
        """stderr output with exit code 0 is a SUCCESS (warnings etc.)"""
        code = ("import sys\n"
                "print('just a warning', file=sys.stderr)\n"
                "print('done')")
        res = self.coder.execute_with_retry(code)
        self.assertTrue(res['success'])
        self.assertIn('done', res['stdout'])
        self.assertIn('warning', res['stderr'])

    def test_printing_error_word_is_not_failure(self):
        """Printing the word 'Error' must not count as failure."""
        res = self.coder.execute_with_retry("print('Error: not really')")
        self.assertTrue(res['success'])

    def test_real_crash_detected(self):
        res = self.coder.execute_with_retry("x = [1]\nprint(x[5])")
        self.assertFalse(res['success'])
        self.assertIn('IndexError', res['stderr'])
        self.assertNotEqual(res['returncode'], 0)

    def test_unsafe_code_rejected(self):
        res = self.coder.execute_with_retry("os.system('rm -rf /')")
        self.assertFalse(res['success'])

    def test_dangerous_builtins_rejected(self):
        """eval/exec/__import__ are blocked by the safety denylist so
        string-built payloads can't escape it."""
        for payload in [
            "eval('1+1')",
            "exec('import os; os.system(\"id\")')",
            "__import__('os').system('id')",
            "globals()['__builtins__']",
        ]:
            res = self.coder.execute_with_retry(payload)
            self.assertFalse(res['success'],
                             f"'{payload}' should have been rejected")

    def test_exec_shell_variants_rejected(self):
        """Alternate execution shells (os.popen, os.exec*, os.spawn*,
        commands.*) are denied — they bypass the os.system denylist."""
        for payload in [
            "os.popen('rm -rf /')",
            "os.execvp('python', ['python'])",
            "os.spawnl(os.P_NOWAIT, 'evil', 'evil')",
            "commands.getoutput('id')",
            "os.posix_spawn('sh', ['sh'], {})",
        ]:
            res = self.coder.execute_with_retry(payload)
            self.assertFalse(res['success'],
                             f"'{payload}' should have been rejected")

    def test_file_destruction_primitives_rejected(self):
        """os.remove/os.unlink/os.rmdir on quoted paths are denied."""
        for payload in [
            "os.remove('/etc/passwd')",
            "os.unlink('/tmp/x')",
            "os.rmdir('/home/user')",
        ]:
            res = self.coder.execute_with_retry(payload)
            self.assertFalse(res['success'],
                             f"'{payload}' should have been rejected")

    def test_safe_code_still_passes(self):
        """Legit generated code (file I/O in workspace, print, loops)
        is NOT blocked by the denylist — no false positives."""
        for payload in [
            "print('hello')",
            "x = [1, 2, 3]; print(sum(x))",
            "for i in range(3): print(i)",
        ]:
            res = self.coder.execute_with_retry(payload)
            self.assertTrue(res['success'],
                            f"'{payload}' should be allowed")

    def test_timeout_detected(self):
        res = self.coder.execute_with_retry(
            "while True: pass", timeout=1
        )
        self.assertFalse(res['success'])
        self.assertIn('timed out', res['stderr'].lower())


# ====================================================================== #

if __name__ == '__main__':
    print("=" * 60)
    print("JARVIS COMPLEX TASK — INTEGRATION TESTS")
    print("=" * 60)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromModule(sys.modules[__name__]))
    print("\n" + "=" * 60)
    if result.wasSuccessful():
        print(f"ALL {result.testsRun} TESTS PASSED ✅")
    else:
        for test, trace in result.failures + result.errors:
            print(f"  ❌ {test}\n{trace[:300]}")
    print("=" * 60)
