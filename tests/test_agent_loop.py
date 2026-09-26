"""
Offline tests for the Phase 2 agent core (utils/agent_loop.py).

No network, no server import: a scripted fake router plays the LLM,
a recording fake executor stands in for the Executor, and a stub brain
carries only what the loop touches.
"""

import os
import sys
import json
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.llm.providers.base import ChatResult, ToolCall, Usage  # noqa: E402
from utils.agent_loop import AgentLoop, TOOL_SPECS, FORBIDDEN_TOOLS  # noqa: E402


# --------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------- #

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
        self.wrapup = wrapup          # served on tool-less wrap-up calls
        self.providers = {'groq': object()}   # non-empty fleet
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
                return self.wrapup     # agent loop's closing summary call
            result = self.script[len(self.seen_messages) - 1]
        if callable(result):
            result = result()
        return result


class StubBrain:
    def __init__(self, router):
        self.router = router
        self.history_log = []

    def _append_history(self, user, ai):
        self.history_log.append((user, ai))


def text_result(text):
    return ChatResult(text=text, finish_reason='stop', provider='groq',
                      model='fake-model', usage=Usage(10, 5))


# --------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------- #

class TestAgentLoop(unittest.TestCase):

    def _loop(self, script, wrapup=None):
        router = FakeRouter(script, wrapup=wrapup)
        brain = StubBrain(router)
        loop = AgentLoop(brain)
        loop._executor = FakeExecutor()
        return loop, router

    def test_direct_answer_no_tools(self):
        loop, router = self._loop([text_result("Hello, Sir.")])
        out = loop.run("hi")
        self.assertEqual(out, {"action": "chat", "response": "Hello, Sir."})
        self.assertEqual(loop._executor.calls, [])
        self.assertEqual(router.seen_messages[0][0]['role'], 'system')

    def test_tool_call_then_answer(self):
        tool_res = ChatResult(text='', finish_reason='tool_calls',
                              provider='groq', model='fake',
                              tool_calls=[ToolCall(id='c1',
                                                   name='get_weather',
                                                   arguments='{}')])
        loop, router = self._loop([tool_res, text_result("It's sunny.")])
        out = loop.run("weather?")
        self.assertEqual(out['response'], "It's sunny.")
        self.assertEqual(loop._executor.calls,
                         [{'action': 'get_weather'}])
        # The second turn must contain the tool result message.
        second = router.seen_messages[1]
        roles = [m['role'] for m in second]
        self.assertIn('tool', roles)
        tool_msg = [m for m in second if m['role'] == 'tool'][0]
        self.assertEqual(tool_msg['tool_call_id'], 'c1')
        self.assertIn('Sunny', tool_msg['content'])

    def test_streaming_final_flushes_events(self):
        def streamed():
            def gen():
                yield ChatResult(text="Good ", provider='groq',
                                 model='m', finish_reason='')
                yield ChatResult(text="evening.", provider='groq',
                                 model='m', finish_reason='stop')
            return gen()

        loop, router = self._loop([streamed])
        events = []
        out = loop.run("greet", ui_callback=lambda e, d:
                       events.append((e, d)))
        self.assertEqual(out['response'], "Good evening.")
        kinds = [e for e, _ in events]
        self.assertIn('ai_text_stream', kinds)
        self.assertEqual(kinds[-1], 'ai_text_stream_end')
        joined = ''.join(d['delta'] for e, d in events
                         if e == 'ai_text_stream')
        self.assertEqual(joined, "Good evening.")

    def test_any_action_escape_hatch(self):
        tool_res = ChatResult(finish_reason='tool_calls', provider='g',
                              model='m',
                              tool_calls=[ToolCall(id='c9',
                                                   name='any_action',
                                                   arguments=json.dumps(
                                                       {'action':
                                                        'set_volume',
                                                        'params':
                                                        {'value': 30}}))])
        loop, router = self._loop([tool_res, text_result("done")])
        loop.run("volume 30")
        self.assertEqual(loop._executor.calls,
                         [{'action': 'set_volume', 'value': 30}])

    def test_forbidden_inner_action_blocked(self):
        tool_res = ChatResult(finish_reason='tool_calls', provider='g',
                              model='m',
                              tool_calls=[ToolCall(id='cx',
                                                   name='any_action',
                                                   arguments=json.dumps(
                                                       {'action': 'chat'}))])
        loop, router = self._loop([tool_res, text_result("ok")])
        loop.run("try recursion")
        # The forbidden call never reached the executor.
        self.assertEqual(loop._executor.calls, [])

    def test_bad_json_arguments_become_error_result(self):
        tool_res = ChatResult(finish_reason='tool_calls', provider='g',
                              model='m',
                              tool_calls=[ToolCall(id='cb',
                                                   name='get_weather',
                                                   arguments='{bad json')])
        loop, router = self._loop([tool_res, text_result("recovered")])
        out = loop.run("weather?")
        self.assertEqual(out['response'], 'recovered')
        tool_msg = [m for m in router.seen_messages[1]
                    if m['role'] == 'tool'][0]
        self.assertTrue(tool_msg['content'].startswith('ERROR:'))

    def test_executor_exception_isolated(self):
        tool_res = ChatResult(finish_reason='tool_calls', provider='g',
                              model='m',
                              tool_calls=[ToolCall(id='ce', name='boom',
                                                   arguments='{}')])
        loop, router = self._loop([tool_res, text_result("handled")])
        out = loop.run("do it")
        self.assertEqual(out['response'], 'handled')
        tool_msg = [m for m in router.seen_messages[1]
                    if m['role'] == 'tool'][0]
        self.assertIn('ERROR executing boom', tool_msg['content'])

    def test_step_budget_triggers_wrapup(self):
        def make_tool(i):
            def _f():
                return ChatResult(
                    finish_reason='tool_calls', provider='g', model='m',
                    tool_calls=[ToolCall(id=f'c{i}', name='todo_list',
                                         arguments='{}')])
            return _f
        wrap = text_result("Wrap-up summary.")
        loop, router = self._loop(
            [make_tool(1), make_tool(2), make_tool(3)], wrapup=wrap)
        loop.max_steps = 3
        out = loop.run("loop forever")
        self.assertIsNotNone(out)
        self.assertEqual(out['response'], 'Wrap-up summary.')

    def test_history_recorded(self):
        loop, _ = self._loop([text_result("Noted, Sir.")])
        loop.run("remember this convo")
        self.assertEqual(loop.brain.history_log,
                         [("remember this convo", "Noted, Sir.")])


class TestAgentModeGating(unittest.TestCase):

    def test_mode_off_disables(self):
        loop = AgentLoop.__new__(AgentLoop)     # skip __init__ env read
        AgentLoop.__init__(loop, StubBrain(FakeRouter([])))
        with patch.dict(os.environ, {'JARVIS_AGENT_MODE': 'off'}):
            from utils.agent_loop import agent_tools_enabled
            self.assertFalse(agent_tools_enabled())

    def test_no_fleet_returns_none(self):
        router = FakeRouter([])
        router.providers = {}
        brain = StubBrain(router)
        loop = AgentLoop(brain)
        loop._executor = FakeExecutor()
        self.assertIsNone(loop.run("anything"))


class TestToolSpecs(unittest.TestCase):

    def test_specs_wellformed(self):
        names = [s['function']['name'] for s in TOOL_SPECS]
        self.assertEqual(len(names), len(set(names)))
        for spec in TOOL_SPECS:
            fn = spec['function']
            self.assertEqual(spec['type'], 'function')
            self.assertEqual(fn['parameters']['type'], 'object')
            for req in fn['parameters'].get('required', []):
                self.assertIn(req, fn['parameters']['properties'])

    def test_forbidden_never_offered(self):
        names = {s['function']['name'] for s in TOOL_SPECS}
        # The in-loop tools (reasoning: think/recall_deep/memory_about/
        # task_plan/delegate; autonomy: goal_set/goal_list/goal_done/
        # background_task/task_status/automate) ARE offered as native
        # tool specs while also being excluded from the any_action escape
        # hatch — a deliberate two-sided guard (see test_rlm.py).
        # Every OTHER forbidden tool must never be offered.
        _IN_LOOP = {'think', 'recall_deep', 'memory_about',
                    'task_plan', 'delegate',
                    'goal_set', 'goal_list', 'goal_done',
                    'background_task', 'task_status', 'automate',
                    'capability_check', 'capability_expand'}
        self.assertFalse((names - _IN_LOOP) & FORBIDDEN_TOOLS)

    def test_critic_recovery_after_two_tool_failures(self):
        """REGRESSION / recovery-path: when two consecutive tool calls
        fail (error_streak >= 2), the critic persona injects a strategy
        correction and the loop RE-PLANS instead of giving up or looping
        on the same broken call.

        Drives:
          turn 1 -> model emits 2 failing 'boom' calls -> error_streak=2
                  -> critique_failures() returns a correction
                  -> a [STRATEGY CORRECTION] user message is appended
                  -> continue (no final answer yet)
          turn 2 -> model calls get_weather (succeeds) -> error_streak=0
          turn 3 -> wrapup call -> final answer returned
        """
        calls = []

        def failing_turn():
            calls.append('critic_turn')
            return ChatResult(
                text='', finish_reason='tool_calls', provider='groq',
                model='fake',
                tool_calls=[ToolCall(id=None, name='boom', arguments='{}'),
                            ToolCall(id=None, name='boom', arguments='{}')])

        def recovery_turn():
            calls.append('recovery_turn')
            return ChatResult(
                text='', finish_reason='tool_calls', provider='groq',
                model='fake',
                tool_calls=[ToolCall(id='w1', name='get_weather',
                                     arguments='{}')])

        router = FakeRouter(
            script=[failing_turn, recovery_turn],
            # 3rd chat() call: no tools requested -> agent loop's
            # closing-summary wrap-up call.
            wrapup=text_result("The weather is sunny — recovered."))
        brain = StubBrain(router)
        loop = AgentLoop(brain)
        loop._executor = FakeExecutor()

        seen_correction = []

        real_critique = None
        from utils.rlm import reasoner as _r
        real_critique = _r.critique_failures

        def fake_critique(brain, prompt, failures):
            seen_correction.append(failures)
            return ("Try the 'get_weather' tool instead of 'boom'; the "
                    "previous approach keeps crashing the executor.")

        with patch('utils.rlm.reasoner.critique_failures',
                   side_effect=fake_critique):
            out = loop.run("do something")

        # Critic was consulted with the accumulated failure log.
        self.assertEqual(len(seen_correction), 1)
        self.assertEqual(seen_correction[0][-1][0], 'boom')  # (tool, err)

        # A [STRATEGY CORRECTION] user message was injected into the
        # transcript so the next turn can re-plan.
        all_msgs = [m for batch in router.seen_messages for m in batch]
        correction_msg = [m for m in all_msgs
                          if m.get('role') == 'user'
                          and 'STRATEGY CORRECTION' in str(m.get('content', ''))]
        self.assertTrue(correction_msg,
                        "strategy correction message was not injected")
        self.assertIn('get_weather', correction_msg[0]['content'])

        # The loop recovered: a real tool was eventually called and the
        # run produced a final answer rather than aborting.
        self.assertIn('recovery_turn', calls)
        self.assertEqual(loop._executor.calls, [{'action': 'boom'},
                                                {'action': 'boom'},
                                                {'action': 'get_weather'}])
        self.assertEqual(out, {"action": "chat",
                               "response": "The weather is sunny — recovered."})

    def test_two_failures_trigger_critic_only_once(self):
        """The critic flag (not nudged) must prevent repeated nudges
        within a single run — one correction, then the loop must rely on
        the model recovering (or exhausting budget) without spamming the
        critic persona."""
        nudge_count = []

        def fake_critique(brain, prompt, failures):
            nudge_count.append(1)
            return "stop using boom"

        # Turn 1: 3 failing boom calls -> error_streak hits 2 mid-turn;
        # critic should fire exactly ONCE at end of the turn.
        def bad_turn():
            return ChatResult(
                text='', finish_reason='tool_calls', provider='g', model='m',
                tool_calls=[ToolCall(id=None, name='boom', arguments='{}'),
                            ToolCall(id=None, name='boom', arguments='{}'),
                            ToolCall(id=None, name='boom', arguments='{}')])

        router = FakeRouter(
            script=[bad_turn, bad_turn],
            wrapup=text_result("recovered"))
        brain = StubBrain(router)
        loop = AgentLoop(brain)
        loop._executor = FakeExecutor()
        with patch('utils.rlm.reasoner.critique_failures',
                   side_effect=fake_critique):
            out = loop.run("plz help")
        self.assertEqual(len(nudge_count), 1)
        self.assertEqual(out['response'], "recovered")



if __name__ == '__main__':
    unittest.main()
