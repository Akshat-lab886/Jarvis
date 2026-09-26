import os
import sys
import unittest
from unittest.mock import patch


class TestComputeHealthPayload(unittest.TestCase):
    """server._compute_health_payload builds the startup capability report +
    config diagnostics once at boot and replays on every connect. Must never
    raise, even when a sub-check fails."""

    def test_returns_expected_shape(self):
        from utils.server import _compute_health_payload
        payload = _compute_health_payload()
        for key in ('banner', 'ready', 'missing', 'total', 'warnings',
                    'headline_missing'):
            self.assertIn(key, payload)
        self.assertIsInstance(payload['banner'], str)
        self.assertIsInstance(payload['ready'], int)
        self.assertIsInstance(payload['missing'], int)
        self.assertIsInstance(payload['total'], int)
        self.assertIsInstance(payload['warnings'], list)
        self.assertIsInstance(payload['headline_missing'], list)

    def test_counts_reconcile(self):
        from utils.server import _compute_health_payload
        payload = _compute_health_payload()
        self.assertEqual(payload['ready'] + payload['missing'],
                         payload['total'])

    def test_never_raises_on_capability_kill_switch(self):
        from utils import server
        with patch.dict(os.environ, {'JARVIS_CAPABILITY_CHECK': '0'}):
            payload = server._compute_health_payload()
        self.assertIsInstance(payload, dict)
        self.assertIn('ready', payload)
        # With checks disabled the banner carries the kill-switch message
        # and counts stay consistent (0 + 0 == 0).
        self.assertEqual(payload['ready'] + payload['missing'],
                         payload['total'])

    def test_health_payload_global_initialized_in_boot(self):
        # start_server caches into module global; just ensure the name exists.
        from utils import server
        self.assertTrue(hasattr(server, '_HEALTH_PAYLOAD'))


if __name__ == '__main__':
    unittest.main()
