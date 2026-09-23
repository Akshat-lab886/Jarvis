"""
Phase 2 + Phase 3 desktop-loop tests — stubbed vision model, no screen.

Invariants that must survive chunked planning + AX grounding:

  * model calls drop (plan of 5 + done = 3 calls, not 6);
  * the Jev gate runs BEFORE every content-bearing action (type/key/
    hotkey) — chunking never skips a gate; pointer ops stay ungated;
  * hazard abort and off-task skip still fire; off-task re-plans;
  * legacy single-action JSON still parses (graceful degradation);
  * AX grounding fails open to coords-only and never keeps stale refs;
  * click_el runs through the background driver; hotkey runs hotkey();
  * max-steps + call-budget guards terminate the loop;
  * Phase 0 telemetry still lands in the result ([timing] line).

Screen, settle, driver, gateway and vision model are ALL stubbed:
nothing here moves the mouse, touches the desktop, or needs
permissions — safe to run anywhere the package imports.
"""

import json
import unittest
from contextlib import ExitStack
from unittest import mock

# Pin the repo-root config BEFORE the utils chain below: unittest
# discover puts tests/ at sys.path[0], the desktop-agent chain imports
# cv2, and cv2 inserts its own directory at sys.path[1] — so a bare
# `from config import Config` would resolve to
# site-packages/cv2/config.py (NameError: LOADER_DIR). Importing config
# first parks the real one in sys.modules.
import config  # noqa: F401

from utils import desktop_agent as da

PASS_VERDICT = {"on_task": 0.90, "hazard": 0.01}


def _plan(*actions, done=None):
    return json.dumps({"plan": list(actions), "done": done})


def _click(x=10, y=20):
    return {"type": "click", "x": x, "y": y}


class LoopHarness(unittest.TestCase):
    """Base harness: screen/settle/execute/AX/gateway/model stubbed."""

    def setUp(self):
        # driver stub — the real ax_tree would hang on the pending
        # Screen-Recording/Automation permission dialog
        self.driver = mock.Mock()
        self.driver.ax_tree.return_value = []
        p = mock.patch("utils.computer_use.get_driver",
                       return_value=self.driver)
        p.start()
        self.addCleanup(p.stop)
        # AX ping is a real osascript call (v21.1 circuit breaker) — stub
        # it to "granted" so tests never pay the real permission stall
        ps = mock.patch("utils.desktop_agent.subprocess.run",
                        return_value=mock.Mock(returncode=0,
                                               stdout="Finder\n"))
        ps.start()
        self.addCleanup(ps.stop)

    def run_agent(self, responses, max_steps=8, task="t",
                  gate=None, chunk=3):
        """Run run_task with everything stubbed.

        Returns (agent, executed, gates, trace, result):
          executed — list of action dicts handed to _execute
          gates    — [(action, timeout), ...] gateway calls
          trace    — ordered [("gate", type) | ("exec", type), ...]
        """
        executed, gates, trace = [], [], []
        gate_inner = gate or (lambda t, a, timeout=None: PASS_VERDICT)

        def wrapped_gate(t, a, timeout=None):
            gates.append((dict(a), timeout))
            trace.append(("gate", a.get("type")))
            return gate_inner(t, a, timeout)

        def fake_capture(s):
            s._last_img = None
            return ("b64", 1920, 1080, 1024, 576)

        def fake_settle(s, img):
            return (None, 120.0, True)

        def fake_execute(s, a, *dims):
            executed.append(dict(a))
            trace.append(("exec", a.get("type")))
            return f"did {a.get('type')}"

        def fake_ask(s, prompt, b64):
            if not responses:
                raise AssertionError(
                    f"model called more often than scripted "
                    f"(already consumed all {len(responses) and 'x' or ''}"
                    f"responses)")
            return responses.pop(0)

        agent = da.DesktopAgent()
        with ExitStack() as st:
            st.enter_context(mock.patch.object(
                da.DesktopAgent, "capture_screen", fake_capture))
            st.enter_context(mock.patch.object(
                da.DesktopAgent, "_settle", fake_settle))
            st.enter_context(mock.patch.object(
                da.DesktopAgent, "_execute", fake_execute))
            st.enter_context(mock.patch.object(
                da.DesktopAgent, "_ask", fake_ask))
            st.enter_context(mock.patch.object(
                da, "PLAN_CHUNK", chunk))
            st.enter_context(mock.patch(
                "utils.jev.enabled", lambda: True))
            st.enter_context(mock.patch(
                "utils.jev.screen_step", side_effect=wrapped_gate))
            st.enter_context(mock.patch.object(
                da.Config, "GOOGLE_API_KEY", "test-key"))
            result = agent.run_task(task, max_steps=max_steps)
        return agent, executed, gates, trace, result


# --------------------------------------------------------------------- #
# Phase 2 · chunked planning
# --------------------------------------------------------------------- #
class TestChunkedPlanning(LoopHarness):

    def test_model_calls_drop(self):
        # plan of 5 (chunk 3 →3 executed) + plan of2 + done =3 calls;
        # legacy pacing would need ≥6.
        responses = [
            _plan(*[_click(i) for i in range(1, 6)]),
            _plan(_click(6), _click(7)),
            _plan(done="Task complete: ok"),
        ]
        agent, executed, gates, trace, res = self.run_agent(
            responses, max_steps=8)
        self.assertEqual(len(executed), 5)
        self.assertEqual(agent.last_run["model_calls"], 3)
        self.assertIn("Task complete: ok", res)
        self.assertLess(agent.last_run["model_calls"], 6)
        # Phase 0 telemetry intact
        self.assertIn("model_calls=3", res)
        self.assertIn("[timing]", res)
        self.assertIn("settle=120ms", res)

    def test_gate_fires_before_every_content_action(self):
        responses = [
            _plan({"type": "type", "text": "hello"},
                  {"type": "key", "key": "enter"},
                  _click()),
            _plan(done="ok"),
        ]
        agent, executed, gates, trace, res = self.run_agent(responses)
        # type + key gated; bare click NOT gated (no injectable text)
        self.assertEqual([g[0]["type"] for g in gates], ["type", "key"])
        # every gateway call carries the explicit timeout cap
        self.assertTrue(all(g[1] == da.JEV_STEP_TIMEOUT_S
                            for g in gates))
        # exact interleaving: gate strictly precedes each gated action
        self.assertEqual(trace, [("gate", "type"), ("exec", "type"),
                                 ("gate", "key"), ("exec", "key"),
                                 ("exec", "click")])
        self.assertEqual(agent.last_run["gate_calls"], 2)

    def test_hotkey_is_gated(self):
        responses = [
            _plan({"type": "hotkey", "keys": ["command", "shift", "3"]}),
            _plan(done="ok"),
        ]
        agent, executed, gates, trace, res = self.run_agent(responses)
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0][0]["type"], "hotkey")
        self.assertEqual(executed[0]["type"], "hotkey")

    def test_off_task_skip_replans(self):
        def flaky_gate(t, a, timeout=None):
            return {"on_task": 0.01, "hazard": 0.01}   # confident skip
        responses = [
            _plan({"type": "type", "text": "drift"}),
            _plan(_click()),
            _plan(done="recovered"),
        ]
        agent, executed, gates, trace, res = self.run_agent(
            responses, gate=flaky_gate, max_steps=6)
        # step1 skipped, step2 re-planned & executed, then done
        self.assertEqual([a["type"] for a in executed], ["click"])
        self.assertIn("[gate]", res)
        self.assertIn("Task complete: recovered", res)
        self.assertGreaterEqual(agent.last_run["model_calls"], 3)

    def test_hazard_aborts_immediately(self):
        def bomb(t, a, timeout=None):
            return {"on_task": 0.9, "hazard": 0.99}
        responses = [_plan({"type": "type", "text": "rm -rf"})]
        agent, executed, gates, trace, res = self.run_agent(
            responses, gate=bomb)
        self.assertEqual(executed, [])
        self.assertIn("Safety gate (Jev) stopped step", res)
        self.assertIn("0.99", res)
        self.assertEqual(agent.last_run["model_calls"], 1)

    def test_legacy_single_action_json_parses(self):
        responses = [
            json.dumps({"type": "click", "x": 5, "y": 6}),
            json.dumps({"type": "done", "summary": "legacy ok"}),
        ]
        agent, executed, gates, trace, res = self.run_agent(responses)
        self.assertEqual(executed, [{"type": "click", "x": 5, "y": 6}])
        self.assertIn("Task complete: legacy ok", res)
        self.assertEqual(agent.last_run["model_calls"], 2)

    def test_garbage_reply_stops_with_error(self):
        responses = ["<h1>I cannot see the screen</h1>"]
        agent, executed, gates, trace, res = self.run_agent(responses)
        self.assertEqual(executed, [])
        self.assertIn("couldn't understand", res)

    def test_max_steps_budget(self):
        responses = [_plan(_click(1)), _plan(_click(2)), _plan(_click(3))]
        agent, executed, gates, trace, res = self.run_agent(
            responses, max_steps=2)
        self.assertEqual(len(executed), 2)
        self.assertIn("step limit (2)", res)

    def test_call_budget_stops_no_progress_loop(self):
        # every step gate-skipped → model keeps getting called until the
        # call budget trips instead of looping forever
        def always_skip(t, a, timeout=None):
            return {"on_task": 0.01, "hazard": 0.01}
        responses = [_plan({"type": "type", "text": "x"}) for _ in range(9)]
        agent, executed, gates, trace, res = self.run_agent(
            responses, gate=always_skip, max_steps=1)
        self.assertEqual(executed, [])
        self.assertIn("no progress", res)
        self.assertGreaterEqual(agent.last_run["model_calls"], 6)

    def test_plan_chunk_1_gives_legacy_pacing(self):
        responses = [_plan(_click(1), _click(2), _click(3)),
                     _plan(done="ok")]
        agent, executed, gates, trace, res = self.run_agent(
            responses, chunk=1)
        # chunk=1: only the first planned action runs before re-planning
        self.assertEqual(len(executed), 1)
        self.assertEqual(agent.last_run["model_calls"], 2)


# --------------------------------------------------------------------- #
# Phase 3 · widened action space (real _execute, pyautogui mocked)
# --------------------------------------------------------------------- #
class TestActionSpace(unittest.TestCase):

    def setUp(self):
        self.agent = da.DesktopAgent()
        self.dims = (1920, 1080, 1024, 576)

    def test_hotkey_chord(self):
        with mock.patch.object(da.pyautogui, "hotkey") as hk:
            msg = self.agent._execute(
                {"type": "hotkey", "keys": ["command", "shift", "3"]},
                *self.dims)
        hk.assert_called_once_with("command", "shift", "3")
        self.assertIn("command+shift+3", msg)

    def test_hotkey_string_form_and_min_two(self):
        with mock.patch.object(da.pyautogui, "hotkey") as hk:
            self.agent._execute(
                {"type": "hotkey", "keys": "command+v"}, *self.dims)
            hk.assert_called_once_with("command", "v")
            msg = self.agent._execute(
                {"type": "hotkey", "keys": ["command"]}, *self.dims)
        self.assertIn("2", msg)          # needs ≥2 keys, not executed
        self.assertEqual(hk.call_count, 1)

    def test_click_button_variants(self):
        with mock.patch.object(da.pyautogui, "click") as left, \
                mock.patch.object(da.pyautogui, "rightClick") as right, \
                mock.patch.object(da.pyautogui, "doubleClick") as dbl:
            self.agent._execute(_click(), *self.dims)
            self.agent._execute({"type": "click", "x": 1, "y": 2,
                                 "button": "right"}, *self.dims)
            self.agent._execute({"type": "click", "x": 3, "y": 4,
                                 "dbl": True}, *self.dims)
        left.assert_called_once_with(10, 20)
        right.assert_called_once_with(1, 2)
        dbl.assert_called_once_with(3, 4)

    def test_drag_and_scroll(self):
        with mock.patch.object(da.pyautogui, "moveTo") as mv, \
                mock.patch.object(da.pyautogui, "mouseDown") as dn, \
                mock.patch.object(da.pyautogui, "mouseUp") as up, \
                mock.patch.object(da.pyautogui, "scroll") as sc:
            self.agent._execute(
                {"type": "drag", "x": 0, "y": 0,
                 "to_x": 100, "to_y": 200}, *self.dims)
            self.agent._execute({"type": "scroll", "amount": -99},
                                *self.dims)
        self.assertEqual(mv.call_count, 2)
        dn.assert_called_once()
        up.assert_called_once()
        sc.assert_called_once_with(-20)      # clamped

    def test_wait_is_free(self):
        self.assertEqual(self.agent._execute({"type": "wait"},
                                             *self.dims), "Waited.")

    def test_click_el_uses_background_driver(self):
        self.agent._ax_map = {7: {"app": "TextEdit", "name": "Submit",
                                  "role": "button"}}
        drv = mock.Mock()
        drv.click_element.return_value = "Clicked Submit (background)."
        with mock.patch("utils.computer_use.get_driver",
                        return_value=drv):
            msg = self.agent._execute({"type": "click_el", "ref": 7},
                                      *self.dims)
            stale = self.agent._execute({"type": "click_el", "ref": 99},
                                        *self.dims)
        drv.click_element.assert_called_once_with(
            "TextEdit", "Submit", "button")
        self.assertEqual(msg, "Clicked Submit (background).")
        self.assertIn("Unknown element ref 99", stale)

    def test_click_clamps_to_screen(self):
        with mock.patch.object(da.pyautogui, "click") as ck:
            self.agent._execute({"type": "click", "x": 99999, "y": -5},
                                *self.dims)
        ck.assert_called_once_with(1919, 0)


# --------------------------------------------------------------------- #
# Plan parsing (both formats)
# --------------------------------------------------------------------- #
class TestParsePlan(unittest.TestCase):

    p = staticmethod(da.DesktopAgent._parse_plan)

    def test_plan_multi(self):
        acts, done = self.p(_plan(_click(1), _click(2)))
        self.assertEqual(len(acts), 2)
        self.assertIsNone(done)

    def test_empty_plan_with_done(self):
        acts, done = self.p(_plan(done="Task complete: z"))
        self.assertEqual(acts, [])
        self.assertEqual(done, "Task complete: z")

    def test_actions_win_over_done_field(self):
        acts, done = self.p(json.dumps(
            {"plan": [_click()], "done": "Task complete: x"}))
        self.assertEqual(len(acts), 1)
        self.assertIsNone(done)

    def test_legacy_top_level_type(self):
        acts, done = self.p(json.dumps({"type": "key", "key": "esc"}))
        self.assertEqual(acts[0]["type"], "key")

    def test_markdown_fenced_json(self):
        raw = "```json\n" + _plan(_click()) + "\n```"
        acts, done = self.p(raw)
        self.assertEqual(len(acts), 1)

    def test_garbage_and_wrong_shapes(self):
        for bad in ["hello", "<p>no</p>",
                    json.dumps({"plan": "not-a-list"}),
                    json.dumps({"plan": [], "done": None}),
                    json.dumps({"foo": 1})]:
            with self.subTest(bad=bad):
                self.assertEqual(self.p(bad), (None, None))

    def test_non_dict_plan_items_filtered(self):
        acts, _ = self.p(json.dumps(
            {"plan": [None, "x", {"type": "wait"}, {}]}))
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0]["type"], "wait")


# --------------------------------------------------------------------- #
# AX grounding (fail-open)
# --------------------------------------------------------------------- #
class TestAXBundle(LoopHarness):

    def test_fail_open_clears_stale_refs(self):
        agent = da.DesktopAgent()
        agent._ax_map = {99: {"app": "Old", "name": "stale",
                              "role": "button"}}
        self.driver.ax_tree.return_value = []          # permission denied
        text, ms = agent._ax_bundle()
        self.assertEqual(text, "")
        self.assertEqual(agent._ax_map, {})            # stale ref gone

    def test_elements_render_as_refs(self):
        agent = da.DesktopAgent()
        self.driver.ax_tree.return_value = [
            {"ref": 1, "role": "button", "name": "Save",
             "app": "TextEdit", "x": 40, "y": 80, "w": 90, "h": 24},
        ]
        text, ms = agent._ax_bundle()
        self.assertIn("[1] button", text)
        self.assertIn("'Save'", text)
        self.assertIn("(40,80,90,24)", text)
        self.assertIn(1, agent._ax_map)
        self.driver.ax_tree.assert_called_with(
            max_elements=da.AX_MAX_ELEMS)

    def test_char_budget_caps_list(self):
        agent = da.DesktopAgent()
        self.driver.ax_tree.return_value = [
            {"ref": i, "role": "button",
             "name": "element-with-a-long-name-" + str(i),
             "app": "X", "x": 1, "y": 2, "w": 3, "h": 4}
            for i in range(1, 41)
        ]
        text, ms = agent._ax_bundle()
        used = sum(len(l) + 1 for l in text.splitlines())
        self.assertLessEqual(used,
                             da.AX_LIST_MAX_CHARS + 80)  # 1-line overshoot
        self.assertLess(len(agent._ax_map), 40)

    def test_disabled_by_env(self):
        agent = da.DesktopAgent()
        with mock.patch.object(da, "AX_ENABLED", False):
            text, ms = agent._ax_bundle()
        self.assertEqual((text, ms), ("", 0.0))


# --------------------------------------------------------------------- #
# v21.1 · self-review regression tests
# --------------------------------------------------------------------- #
class TestSelfReviewFixes(LoopHarness):
    """Regressions for the issues the v21.1 self-review found."""

    def test_hotkey_cmd_alias_normalized(self):
        # MEASURED: 'cmd' is NOT in pyautogui.KEYBOARD_KEYS ('command'
        # is) and the eval prompt says "cmd+shift+3" — without the alias
        # the live hotkey op died with KeyError.
        agent = da.DesktopAgent()
        self.assertIsNone(agent._ax_ping)      # fresh-breaker state
        self.assertFalse(agent._ax_off)
        with mock.patch.object(da.pyautogui, "hotkey") as hk:
            agent._execute({"type": "hotkey",
                            "keys": ["Cmd", "shift", "3"]},
                           1920, 1080, 1024, 576)
        hk.assert_called_once_with("command", "shift", "3")

    def test_hotkey_string_form_parses_with_alias(self):
        agent = da.DesktopAgent()
        with mock.patch.object(da.pyautogui, "hotkey") as hk:
            agent._execute({"type": "hotkey", "keys": "cmd+shift+3"},
                           1920, 1080, 1024, 576)
        hk.assert_called_once_with("command", "shift", "3")

    def test_single_key_lowercase_and_alias(self):
        agent = da.DesktopAgent()
        with mock.patch.object(da.pyautogui, "press") as pk:
            agent._execute({"type": "key", "key": "Enter"},
                           1920, 1080, 1024, 576)
            agent._execute({"type": "key", "key": "control"},
                           1920, 1080, 1024, 576)
        self.assertEqual([c.args for c in pk.call_args_list],
                         [("enter",), ("ctrl",)])

    def test_ax_ping_failure_skips_tree(self):
        # denied Automation: the 2s-capped ping fails → the (up-to-12s
        # blocking) tree walk is NEVER attempted; AX off for the run
        agent = da.DesktopAgent()
        with mock.patch.object(da.subprocess, "run",
                               side_effect=OSError("no osascript")):
            text, _ = agent._ax_bundle()
        self.assertEqual(text, "")
        self.assertTrue(agent._ax_off)
        self.driver.ax_tree.assert_not_called()

    def test_ax_empty_tree_latches_off_no_repeated_stalls(self):
        agent = da.DesktopAgent()
        self.driver.ax_tree.return_value = []     # e.g. desktop frontmost
        text, _ = agent._ax_bundle()
        self.assertEqual(text, "")
        self.assertTrue(agent._ax_off)
        self.driver.ax_tree.reset_mock()
        text2, _ = agent._ax_bundle()
        self.assertEqual(text2, "")
        self.driver.ax_tree.assert_not_called()   # breaker held


if __name__ == "__main__":
    unittest.main()
