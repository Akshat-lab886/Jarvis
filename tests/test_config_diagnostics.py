import os
import sys
import unittest
from unittest.mock import patch, MagicMock


class TestAutoUpgradeContradiction(unittest.TestCase):
    """JARVIS_AUTO_UPGRADE=1 + JARVIS_CAPABILITY_CHECK=0 is a live
    contradiction — packages install but the user sees nothing."""

    def test_no_warning_when_both_on(self):
        from utils.config_diagnostics import _check_auto_upgrade_contradiction
        with patch.dict(os.environ, {
            'JARVIS_AUTO_UPGRADE': '1',
            'JARVIS_CAPABILITY_CHECK': '1',
        }, clear=False):
            lines = _check_auto_upgrade_contradiction()
        self.assertEqual(lines, [])

    def test_no_warning_when_both_off(self):
        from utils.config_diagnostics import _check_auto_upgrade_contradiction
        with patch.dict(os.environ, {
            'JARVIS_AUTO_UPGRADE': '0',
            'JARVIS_CAPABILITY_CHECK': '0',
        }, clear=False):
            lines = _check_auto_upgrade_contradiction()
        self.assertEqual(lines, [])

    def test_no_warning_when_auto_upgrade_off(self):
        from utils.config_diagnostics import _check_auto_upgrade_contradiction
        with patch.dict(os.environ, {
            'JARVIS_AUTO_UPGRADE': '0',
            'JARVIS_CAPABILITY_CHECK': '0',
        }, clear=False):
            lines = _check_auto_upgrade_contradiction()
        self.assertEqual(lines, [])

    def test_contradiction_warns(self):
        from utils.config_diagnostics import _check_auto_upgrade_contradiction
        with patch.dict(os.environ, {
            'JARVIS_AUTO_UPGRADE': '1',
            'JARVIS_CAPABILITY_CHECK': '0',
        }, clear=False):
            lines = _check_auto_upgrade_contradiction()
        self.assertEqual(len(lines), 1)
        self.assertIn('JARVIS_CAPABILITY_CHECK', lines[0])
        self.assertIn('JARVIS_AUTO_UPGRADE', lines[0])


class TestOllamaUrlMisuse(unittest.TestCase):
    """OLLAMA_BASE_URL must not include /v1 or /api — it's the base only."""

    def test_clean_url_no_warning(self):
        from utils.config_diagnostics import _check_ollama_url_misuse
        with patch.dict(os.environ, {
            'OLLAMA_BASE_URL': 'http://localhost:11434',
        }, clear=False):
            lines = _check_ollama_url_misuse()
        self.assertEqual(lines, [])

    def test_disabled_url_no_warning(self):
        from utils.config_diagnostics import _check_ollama_url_misuse
        with patch.dict(os.environ, {
            'OLLAMA_BASE_URL': 'off',
        }, clear=False):
            lines = _check_ollama_url_misuse()
        self.assertEqual(lines, [])

    def test_v1_suffix_warns(self):
        from utils.config_diagnostics import _check_ollama_url_misuse
        with patch.dict(os.environ, {
            'OLLAMA_BASE_URL': 'http://localhost:11434/v1',
        }, clear=False):
            lines = _check_ollama_url_misuse()
        self.assertEqual(len(lines), 1)
        self.assertIn('/v1', lines[0])

    def test_chat_completions_suffix_warns(self):
        from utils.config_diagnostics import _check_ollama_url_misuse
        with patch.dict(os.environ, {
            'OLLAMA_BASE_URL': 'http://localhost:11434/v1/chat/completions',
        }, clear=False):
            lines = _check_ollama_url_misuse()
        self.assertEqual(len(lines), 1)
        self.assertIn('base URL only', lines[0])


if __name__ == '__main__':
    unittest.main()
