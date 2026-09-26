"""Unit tests for utils/mobile_studio.py blueprint generation.

Covers the JSON-unwrapping regression: when brain.think wraps the
blueprint JSON inside the chat envelope {"action":"chat","response":
"<json text>"}, generate_app_blueprint must now EXTRACT + parse the
inner JSON rather than returning the opaque envelope.

Brain is patched at the import site (``from utils.brain import Brain``)
so no real LLM call is made.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


_BLUEPRINT = {
    "project_name": "FlutterDemo",
    "dependencies": ["http"],
    "files_to_create": [
        {"path": "lib/main.dart", "description": "entry point"},
    ],
}


def _chat_envelope_json():
    """Simulate brain.think wrapping the JSON in a chat envelope."""
    import json
    return {"action": "chat",
            "response": json.dumps(_BLUEPRINT)}


def _chat_envelope_text():
    """brain.think returns prose in the response field (no JSON)."""
    return {"action": "chat", "response": "Sorry, I couldn't build that."}


class TestBlueprintExtraction(unittest.TestCase):

    def _mgr(self):
        from utils.mobile_studio import MobileManager
        return MobileManager()

    def test_direct_dict_returned(self):
        """When the brain returns the blueprint dict directly (project_name
        at top level), it is passed through unchanged."""
        with patch('utils.brain.Brain') as mk:
            mk.return_value.think.return_value = dict(_BLUEPRINT)
            mgr = self._mgr()
            out = mgr.generate_app_blueprint("a todo app")
        self.assertEqual(out.get('project_name'), 'FlutterDemo')

    def test_wrapped_json_is_unwrapped(self):
        """REGRESSION: a chat envelope hiding the JSON must be extracted.
        Previously the envelope itself was returned, so the executor's
        'project_name' in blueprint check failed and the build was
        reported as a failure even though the brain produced valid JSON."""
        with patch('utils.brain.Brain') as mk:
            mk.return_value.think.return_value = _chat_envelope_json()
            mgr = self._mgr()
            out = mgr.generate_app_blueprint("a todo app")
        self.assertEqual(out.get('project_name'), 'FlutterDemo')
        self.assertIn('files_to_create', out)
        # The envelope keys must NOT leak through.
        self.assertNotIn('action', out)
        self.assertNotIn('response', out)

    def test_non_json_envelope_returns_error(self):
        """When the brain wraps prose (no JSON) we degrade to the error
        dict instead of returning the opaque envelope."""
        with patch('utils.brain.Brain') as mk:
            mk.return_value.think.return_value = _chat_envelope_text()
            mgr = self._mgr()
            out = mgr.generate_app_blueprint("a todo app")
        self.assertIn('error', out)

    def test_brain_error_returns_error_dict(self):
        with patch('utils.brain.Brain') as mk:
            mk.return_value.think.side_effect = RuntimeError("llm down")
            mgr = self._mgr()
            out = mgr.generate_app_blueprint("a todo app")
        self.assertIn('error', out)
        self.assertIn('llm down', out['error'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
