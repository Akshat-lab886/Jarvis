"""
Jarvis Capability Awareness Tests
==================================

Covers:
  - Capability registry: all capabilities load, check functions work
  - Task assessment: keyword matching, available/missing classification
  - Upgrade planner: concrete steps for known gaps
  - Format functions: human-readable output
  - Kill switch: JARVIS_CAPABILITY_CHECK=0 bypasses assessment
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestCapabilityRegistry(unittest.TestCase):
    """All capabilities are registered and check functions exist."""

    def test_all_capabilities_loaded(self):
        from utils.capabilities import _CAPABILITIES
        self.assertGreaterEqual(len(_CAPABILITIES), 15)

    def test_each_has_required_fields(self):
        from utils.capabilities import _CAPABILITIES
        names = set()
        for cap in _CAPABILITIES:
            self.assertTrue(cap.name, f"Capability missing name")
            self.assertTrue(cap.description, f"{cap.name} missing description")
            self.assertTrue(cap.category, f"{cap.name} missing category")
            self.assertTrue(callable(cap.check), f"{cap.name} check not callable")
            self.assertIsInstance(cap.keywords, list)
            self.assertNotIn(cap.name, names, f"Duplicate capability: {cap.name}")
            names.add(cap.name)

    def test_categories_are_valid(self):
        from utils.capabilities import _CAPABILITIES
        valid = {'core', 'information', 'communication',
                 'development', 'device', 'knowledge'}
        for cap in _CAPABILITIES:
            self.assertIn(cap.category, valid,
                          f"{cap.name} has invalid category: {cap.category}")

    def test_smart_home_always_ready(self):
        from utils.capabilities import check
        cs = check('smart_home')
        self.assertEqual(cs.status, 'ready')

    def test_check_unknown_returns_missing(self):
        from utils.capabilities import check
        cs = check('totally_fake_capability')
        self.assertEqual(cs.status, 'missing')
        self.assertIn('Unknown', cs.description)


class TestTaskAssessment(unittest.TestCase):
    """Keyword-based task→capability mapping."""

    def test_email_task_needs_email(self):
        from utils.capabilities import assess
        result = assess("send an email to john@example.com")
        cap_names = [c.name for c in result.available + result.missing]
        self.assertIn('email', cap_names)
        self.assertIn('llm_reasoning', cap_names)

    def test_browse_task_needs_browser(self):
        from utils.capabilities import assess
        result = assess("browse this website and click the login button")
        cap_names = [c.name for c in result.available + result.missing]
        self.assertIn('browser', cap_names)

    def test_mobile_task_needs_mobile_dev(self):
        from utils.capabilities import assess
        result = assess("build me a flutter mobile app")
        cap_names = [c.name for c in result.available + result.missing]
        self.assertIn('mobile_dev', cap_names)

    def test_smart_home_needs_smart_home(self):
        from utils.capabilities import assess
        result = assess("turn on the living room light")
        cap_names = [c.name for c in result.available + result.missing]
        self.assertIn('smart_home', cap_names)

    def test_simple_chat_needs_only_llm(self):
        from utils.capabilities import assess
        result = assess("what is the capital of France")
        cap_names = [c.name for c in result.available + result.missing]
        self.assertIn('llm_reasoning', cap_names)
        # Should NOT require browser, email, etc.
        self.assertNotIn('browser', cap_names)
        self.assertNotIn('email', cap_names)

    def test_calendar_task_needs_calendar(self):
        from utils.capabilities import assess
        result = assess("what's on my calendar tomorrow")
        cap_names = [c.name for c in result.available + result.missing]
        self.assertIn('calendar', cap_names)

    def test_code_task_needs_code_execution(self):
        from utils.capabilities import assess
        result = assess("run this python script for me")
        cap_names = [c.name for c in result.available + result.missing]
        self.assertIn('code_execution', cap_names)

    def test_proactive_needs_proactive(self):
        from utils.capabilities import assess
        result = assess("give me a morning briefing")
        cap_names = [c.name for c in result.available + result.missing]
        self.assertIn('proactive', cap_names)

    def test_assessment_always_includes_llm(self):
        from utils.capabilities import assess
        result = assess("hello world")
        cap_names = [c.name for c in result.available + result.missing]
        self.assertIn('llm_reasoning', cap_names)


class TestUpgradePlanner(unittest.TestCase):
    """Upgrade plans are generated for known gaps."""

    def test_email_has_upgrade_steps(self):
        from utils.capabilities import UPGRADE_STEPS
        self.assertIn('email', UPGRADE_STEPS)
        steps = UPGRADE_STEPS['email']
        self.assertGreater(len(steps), 0)
        self.assertIn('instructions', steps[0])

    def test_browser_has_upgrade_steps(self):
        from utils.capabilities import UPGRADE_STEPS
        self.assertIn('browser', UPGRADE_STEPS)
        steps = UPGRADE_STEPS['browser']
        self.assertTrue(any('playwright' in s.get('step', '').lower()
                           or 'playwright' in str(s.get('packages', []))
                           for s in steps))

    def test_mobile_dev_has_upgrade_steps(self):
        from utils.capabilities import UPGRADE_STEPS
        self.assertIn('mobile_dev', UPGRADE_STEPS)

    def test_upgrade_plan_in_assessment(self):
        from utils.capabilities import assess
        result = assess("send an email")
        # If email is missing, there should be upgrade steps
        missing_names = [c.name for c in result.missing]
        if 'email' in missing_names:
            self.assertGreater(len(result.upgrade_plan), 0)

    def test_format_upgrade_plan(self):
        from utils.capabilities import format_upgrade_plan
        text = format_upgrade_plan(['browser'])
        self.assertIn('Playwright', text)
        self.assertTrue(len(text) > 10)


class TestFormatting(unittest.TestCase):
    """Human-readable output functions."""

    def test_format_assessment(self):
        from utils.capabilities import AssessmentResult, CapabilityStatus, format_assessment
        result = AssessmentResult(
            task="test",
            available=[CapabilityStatus('llm', 'LLM', 'core', 'ready', 'API key set')],
            missing=[CapabilityStatus('email', 'Email', 'communication', 'missing', 'No creds')],
            upgrade_plan=[{'step': 'Set up email', 'instructions': 'Do X'}],
        )
        text = format_assessment(result)
        self.assertIn('✅', text)
        self.assertIn('❌', text)
        self.assertIn('Upgrade plan', text)
        self.assertIn('Set up email', text)

    def test_list_all_returns_dicts(self):
        from utils.capabilities import list_all
        caps = list_all()
        self.assertIsInstance(caps, list)
        self.assertGreater(len(caps), 0)
        for c in caps:
            self.assertIn('name', c)
            self.assertIn('status', c)
            self.assertIn('category', c)
            self.assertIn('detail', c)
            self.assertIn(c['status'], ('ready', 'missing', 'degraded'))


class TestTier1Capabilities(unittest.TestCase):
    """Tier 1: stdlib-only subsystems route, check, and stay quiet."""

    def test_registry_grew_past_tier0(self):
        from utils.capabilities import _CAPABILITIES
        self.assertGreaterEqual(len(_CAPABILITIES), 30)

    def test_scheduling_routes(self):
        from utils.capabilities import assess
        result = assess("remind me in 20 minutes to call mom")
        names = [c.name for c in result.available + result.missing]
        self.assertIn('scheduling', names)

    def test_goal_tracking_routes(self):
        from utils.capabilities import assess
        result = assess("set a goal to finish the report")
        names = [c.name for c in result.available + result.missing]
        self.assertIn('goal_tracking', names)

    def test_transcript_search_routes(self):
        from utils.capabilities import assess
        result = assess("what did we discuss yesterday")
        names = [c.name for c in result.available + result.missing]
        self.assertIn('transcript_search', names)

    def test_checkpoints_routes_and_ready(self):
        from utils.capabilities import assess, check
        result = assess("rollback my changes")
        names = [c.name for c in result.available + result.missing]
        self.assertIn('checkpoints', names)
        self.assertEqual(check('checkpoints').status, 'ready')

    def test_budget_guard_routes_and_ready(self):
        from utils.capabilities import assess, check
        result = assess("how much have i spent on api")
        names = [c.name for c in result.available + result.missing]
        self.assertIn('budget_guard', names)
        self.assertEqual(check('budget_guard').status, 'ready')

    def test_word_boundaries_no_false_positives(self):
        # 'extract action items' must NOT trip smart_home's old 'ac'/'action'
        # keywords; 'capital of France' must NOT trip file_ops/git_ops/voice.
        from utils.capabilities import assess
        result = assess("extract action items from the call recording")
        names = [c.name for c in result.available + result.missing]
        self.assertNotIn('smart_home', names)
        result = assess("what is the capital of France")
        names = [c.name for c in result.available + result.missing]
        self.assertNotIn('file_ops', names)
        self.assertNotIn('git_ops', names)
        self.assertNotIn('voice', names)

    def test_plurals_still_route(self):
        from utils.capabilities import assess
        result = assess("turn on the living room lights")
        names = [c.name for c in result.available + result.missing]
        self.assertIn('smart_home', names)
        result = assess("set a reminder to drink water")
        names = [c.name for c in result.available + result.missing]
        self.assertIn('scheduling', names)

    def test_tier1_checks_never_raise(self):
        from utils.capabilities import check
        for name in ('scheduling', 'goal_tracking', 'transcript_search',
                     'self_improvement', 'checkpoints', 'budget_guard',
                     'relationships', 'code_health', 'multi_model',
                     'ambient_context'):
            cs = check(name, _use_cache=False)
            self.assertIn(cs.status, ('ready', 'missing', 'degraded'), name)


class TestTier2Capabilities(unittest.TestCase):
    """Tier 2: pip/credential-gated checks return structured upgrade plans."""

    def test_tier2_routing(self):
        from utils.capabilities import assess
        cases = {
            "push this to github": 'git_ops',
            "organize my downloads": 'file_ops',
            "transcribe this meeting": 'meeting_notes',
            "what can i cook with leftovers": 'vision_cooking',
            "give me a second opinion on this design": 'multi_model',
        }
        for task, expected in cases.items():
            with self.subTest(task=task):
                result = assess(task)
                names = [c.name for c in result.available + result.missing]
                self.assertIn(expected, names)

    def test_tier2_checks_never_raise(self):
        from utils.capabilities import check
        for name in ('remote_access', 'meeting_notes', 'git_ops', 'file_ops',
                     'desktop_vision', 'wake_free', 'vision_cooking'):
            cs = check(name, _use_cache=False)
            self.assertIn(cs.status, ('ready', 'missing', 'degraded'), name)

    def test_expand_remote_access_has_auto_and_manual(self):
        from utils.capabilities import expand
        plan = expand(['remote_access'])
        self.assertIn('python-telegram-bot', plan['auto_packages'])
        self.assertGreater(len(plan['manual_steps']), 0)

    def test_expand_git_ops_has_auto_and_manual(self):
        from utils.capabilities import expand
        plan = expand(['git_ops'])
        self.assertIn('PyGithub', plan['auto_packages'])
        self.assertGreater(len(plan['manual_steps']), 0)

    def test_expand_file_ops_auto(self):
        from utils.capabilities import expand
        plan = expand(['file_ops'])
        self.assertIn('beautifulsoup4', plan['auto_packages'])

    def test_expand_desktop_vision_auto(self):
        from utils.capabilities import expand
        plan = expand(['desktop_vision'])
        self.assertIn('pyautogui', plan['auto_packages'])


class TestKillSwitch(unittest.TestCase):
    """JARVIS_CAPABILITY_CHECK=0 bypasses assessment."""

    def test_disabled_returns_empty(self):
        from utils.capabilities import assess
        with patch.dict(os.environ, {'JARVIS_CAPABILITY_CHECK': '0'}):
            result = assess("send an email to someone")
            self.assertEqual(len(result.available), 0)
            self.assertEqual(len(result.missing), 0)

    def test_enabled_default(self):
        from utils.capabilities import enabled
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('JARVIS_CAPABILITY_CHECK', None)
            self.assertTrue(enabled())

    def test_disabled(self):
        from utils.capabilities import enabled
        with patch.dict(os.environ, {'JARVIS_CAPABILITY_CHECK': '0'}):
            self.assertFalse(enabled())


class TestExpansionPlanner(unittest.TestCase):
    """Capability expansion: plan generation and auto-install."""

    def test_expand_returns_plan_dict(self):
        from utils.capabilities import expand
        plan = expand(['web_search'], 'search the web')
        self.assertIn('goal_title', plan)
        self.assertIn('steps', plan)
        self.assertIn('auto_packages', plan)
        self.assertIn('manual_steps', plan)
        self.assertIn('web_search', plan['goal_title'])

    def test_expand_pip_package_flagged_auto(self):
        from utils.capabilities import expand
        plan = expand(['web_search'])
        self.assertIn('duckduckgo-search', plan['auto_packages'])
        auto_steps = [s for s in plan['steps'] if s['auto']]
        self.assertGreater(len(auto_steps), 0)
        self.assertEqual(auto_steps[0]['verify'], 'web_search')

    def test_expand_manual_step(self):
        from utils.capabilities import expand
        plan = expand(['email'])
        # Email has manual steps (OAuth setup), no auto packages
        self.assertEqual(len(plan['auto_packages']), 0)
        self.assertGreater(len(plan['manual_steps']), 0)

    def test_expand_multiple_capabilities(self):
        from utils.capabilities import expand
        plan = expand(['web_search', 'email', 'browser'])
        self.assertIn('3 capabilities', plan['goal_title'])
        self.assertGreater(len(plan['steps']), 3)

    def test_expand_unknown_capability(self):
        from utils.capabilities import expand
        plan = expand(['fake_capability'])
        self.assertGreater(len(plan['steps']), 0)
        self.assertIn('fake_capability', plan['steps'][0]['text'])

    def test_format_expansion_plan(self):
        from utils.capabilities import expand, format_expansion_plan
        plan = expand(['web_search'])
        text = format_expansion_plan(plan)
        self.assertIn('Expansion plan', text)
        self.assertIn('Auto-installable', text)

    def test_expansion_to_steps(self):
        from utils.capabilities import expand, expansion_to_steps
        plan = expand(['web_search', 'email'])
        steps = expansion_to_steps(plan)
        self.assertIsInstance(steps, list)
        self.assertEqual(len(steps), len(plan['steps']))

    def test_auto_install_blocked_by_default(self):
        from utils.capabilities import auto_install
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('JARVIS_AUTO_UPGRADE', None)
            ok, output = auto_install(['duckduckgo-search'])
            self.assertFalse(ok)
            self.assertIn('not enabled', output)

    def test_auto_install_empty_list(self):
        from utils.capabilities import auto_install
        ok, output = auto_install([])
        self.assertTrue(ok)

    def test_auto_install_enabled(self):
        from utils.capabilities import auto_install
        with patch.dict(os.environ, {'JARVIS_AUTO_UPGRADE': '1'}):
            # This will actually try to install — just check it doesn't crash
            ok, output = auto_install(['duckduckgo-search'])
            self.assertIsInstance(ok, bool)
            self.assertIsInstance(output, str)

    def test_reassess_returns_assessment(self):
        from utils.capabilities import reassess
        result = reassess("send an email")
        self.assertTrue(hasattr(result, 'available'))
        self.assertTrue(hasattr(result, 'missing'))


class TestStartupReport(unittest.TestCase):
    """start_server emits a concise headline readiness banner; make sure
    it stays readable and never raises."""

    def test_report_contains_headline_caps(self):
        from utils.capabilities import startup_report, _HEADLINE_CAPS
        banner = startup_report(_use_cache=False)
        # Every headline capability should appear by its description text.
        from utils.capabilities import _CAPABILITIES
        descs = {c.name: c.description for c in _CAPABILITIES}
        for name in _HEADLINE_CAPS:
            if name in descs:
                self.assertIn(descs[name], banner)

    def test_report_has_checkmark_or_cross(self):
        from utils.capabilities import startup_report
        banner = startup_report(_use_cache=False)
        # At least one ready (✅) and the banner is non-empty.
        self.assertTrue(banner)
        self.assertIn('✅', banner)

    def test_disabled_returns_kill_switch_message(self):
        from utils.capabilities import startup_report
        with patch.dict(os.environ, {'JARVIS_CAPABILITY_CHECK': '0'}):
            banner = startup_report(_use_cache=False)
        self.assertIn('disabled', banner.lower())

    def test_report_suggests_dashboard_when_anything_missing(self):
        """If any headline cap is not ready, the banner points the user
        to a remediation path."""
        from utils.capabilities import startup_report
        banner = startup_report(_use_cache=False)
        if '❌' in banner:
            self.assertIn('capability_check', banner)


if __name__ == '__main__':
    unittest.main()
