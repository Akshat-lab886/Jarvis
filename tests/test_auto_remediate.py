"""Unit tests for AgentLoop._auto_remediate pre-flight.

Covers: no-op when JARVIS_AUTO_UPGRADE is off / no missing caps,
installs only pip-only capabilities (skips OAuth/permission-gated ones),
and never raises on any failure path.

assess / expand / auto_install / reassess are patched at the
utils.capabilities level so no real pip/HTTP/capability probe runs.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _cs(name, status='missing', steps=None):
    """Build a lightweight CapabilityStatus stand-in."""
    m = MagicMock()
    m.name = name
    m.status = status
    m.upgrade_steps = steps or []
    return m


def _make_plan(missing):
    """expand() stand-in: derive auto_packages from pip_install steps."""
    plan = MagicMock()
    pkgs = []
    for cs in missing:
        for s in cs.upgrade_steps:
            if s.get('action') == 'pip_install':
                pkgs.extend(s.get('packages', []))
    plan.auto_packages = pkgs
    plan.steps = []
    plan.manual_steps = []
    return plan


class TestAutoRemediate(unittest.TestCase):

    def _run(self, prompt, missing=None, install_ok=(True, 'installed'),
             upgrade='1',
             reassess_missing=None):
        from utils.agent_loop import _auto_remediate
        missing = missing or []
        reassess_val = MagicMock(available=[], missing=reassess_missing or [])
        env = dict(os.environ)
        if upgrade:
            env['JARVIS_AUTO_UPGRADE'] = upgrade
        else:
            env.pop('JARVIS_AUTO_UPGRADE', None)

        with patch.dict(os.environ, env, clear=False):
            with patch('utils.capabilities.assess',
                       return_value=MagicMock(missing=missing)):
                with patch('utils.capabilities.expand',
                           side_effect=lambda n, o='': _make_plan(missing)):
                    with patch('utils.capabilities.auto_install',
                               return_value=install_ok):
                        with patch('utils.capabilities.reassess',
                                   return_value=reassess_val):
                            return _auto_remediate(prompt)

    # ---- cases ----

    def test_noop_when_upgrade_off(self):
        missing = [_cs('web_search', steps=[
            {'action': 'pip_install', 'packages': ['duckduckgo-search']}])]
        r = self._run("search the web", missing=missing, upgrade=None)
        self.assertIsNone(r)

    def test_noop_when_no_missing(self):
        r = self._run("simple chat")
        self.assertIsNone(r)

    def test_installs_pip_only_cap(self):
        """web_search missing via a single pip step -> auto-install runs
        and reassess is consulted afterward."""
        missing = [_cs('web_search', steps=[
            {'action': 'pip_install', 'packages': ['duckduckgo-search']}])]
        r = self._run("search the web", missing=missing)
        self.assertIsNotNone(r)
        self.assertEqual(len(r.available), 0)
        self.assertEqual(len(r.missing), 0)

    def test_skips_cap_with_manual_steps(self):
        """desktop_control has a manual (non-pip) step -> never installed."""
        missing = [_cs('desktop_control', steps=[
            {'step': 'Enable Accessibility permission',
             'instructions': 'System Settings -> Accessibility'},
            {'action': 'pip_install',
             'packages': ['pyobjc-framework-ApplicationServices']},
        ])]
        r = self._run("click the mouse", missing=missing)
        self.assertIsNone(r)

    def test_install_failure_is_swallowed(self):
        missing = [_cs('web_search', steps=[
            {'action': 'pip_install', 'packages': ['duckduckgo-search']}])]
        r = self._run("search the web", missing=missing,
                      install_ok=(False, 'pip error'))
        self.assertIsNone(r)

    def test_never_raises_on_probe_exception(self):
        """If assess() throws, _auto_remediate returns None (never raises)."""
        from utils.agent_loop import _auto_remediate
        with patch.dict(os.environ, {'JARVIS_AUTO_UPGRADE': '1'}):
            with patch('utils.capabilities.assess',
                       side_effect=RuntimeError("probe down")):
                r = _auto_remediate("search the web")
        self.assertIsNone(r)


if __name__ == '__main__':
    unittest.main(verbosity=2)
