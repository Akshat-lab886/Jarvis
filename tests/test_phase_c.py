"""
Phase C tests: Docker sandbox engine selection, extensible tool registry,
and WebAgent DOM formatting. External services are mocked.
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.coder import Coder


def _make_tmp():
    return tempfile.mkdtemp(prefix="jarvis_phaseC_")


# ====================================================================== #
# Coder sandbox engine
# ====================================================================== #

class TestCoderSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = _make_tmp()
        self.coder = Coder(workspace_dir=os.path.join(self.tmp, 'ws'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_local_mode_never_docker(self):
        self.coder._docker_ok = True          # even with docker "available"
        with patch('utils.coder._SANDBOX_MODE', 'local'):
            self.assertFalse(self.coder._use_docker())

    def test_auto_uses_docker_when_available(self):
        self.coder._docker_ok = True
        with patch('utils.coder._SANDBOX_MODE', 'auto'):
            self.assertTrue(self.coder._use_docker())

    def test_auto_falls_back_without_daemon(self):
        self.coder._docker_ok = False
        with patch('utils.coder._SANDBOX_MODE', 'auto'):
            self.assertFalse(self.coder._use_docker())

    def test_local_run_tags_engine(self):
        res = self.coder._run_python("print('sandbox-check')", timeout=15)
        self.assertEqual(res.get('engine'), 'local')
        self.assertTrue(res['success'])
        self.assertIn('sandbox-check', res['stdout'])

    def test_results_shape_stable(self):
        res = self.coder.execute_with_retry("print('shape')", timeout=15)
        for key in ('success', 'output', 'stdout', 'stderr',
                    'returncode', 'retries_remaining'):
            self.assertIn(key, res)


# ====================================================================== #
# Tool registry
# ====================================================================== #

def _manifest(**over):
    m = {
        "name": "get_weather",
        "description": "Current weather for a city",
        "method": "GET",
        "url": "https://api.example.com/weather",
        "params": {
            "city": {"type": "string", "required": True,
                     "description": "City name"},
            "units": {"type": "string", "required": False},
        },
    }
    m.update(over)
    return m


class TestToolRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = _make_tmp()
        self.tools_dir = os.path.join(self.tmp, 'tools_registry')
        os.makedirs(self.tools_dir, exist_ok=True)
        self.reg = None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, manifest, fname=None):
        fname = fname or f"{manifest.get('name', 'tool')}.json"
        with open(os.path.join(self.tools_dir, fname), 'w') as f:
            json.dump(manifest, f)

    def _registry(self):
        from utils.tool_registry import ToolRegistry
        self.reg = ToolRegistry(tools_dir=self.tools_dir)
        return self.reg

    # --- validation ----------------------------------------------------- #
    def test_valid_manifest_loads(self):
        self._write(_manifest())
        reg = self._registry()
        self.assertEqual(reg.count(), 1)
        tool = reg.get_tool("get_weather")
        self.assertEqual(tool['method'], 'GET')
        self.assertTrue(tool['params']['city']['required'])

    def test_invalid_manifests_skipped(self):
        self._write(_manifest(name="Bad Name!"))           # bad slug
        self._write(_manifest(name="no_url", url="ftp://x"))  # bad scheme
        self._write({"name": "nodesc", "url": "https://x.com"})  # no desc
        reg = self._registry()
        self.assertEqual(reg.count(), 0)

    def test_hot_reload_on_ttl_expiry(self):
        self._write(_manifest())
        reg = self._registry()
        self.assertEqual(reg.count(), 1)
        reg._cache_ts = 0                  # force TTL expiry
        self._write(_manifest(name="second_tool"), "second_tool.json")
        self.assertEqual(reg.count(), 2)

    # --- catalog --------------------------------------------------------- #
    def test_catalog_rendering(self):
        self._write(_manifest())
        catalog = self._registry().render_catalog()
        self.assertIn("EXTERNAL TOOLS", catalog)
        self.assertIn("get_weather", catalog)
        self.assertIn("city", catalog)

    def test_empty_catalog_is_empty_string(self):
        self.assertEqual(self._registry().render_catalog(), "")

    # --- invocation ------------------------------------------------------- #
    def test_get_call_with_query_params(self):
        self._write(_manifest())
        fake_resp = MagicMock(status_code=200,
                              text='{"temp": 31}',
                              **{'json.side_effect': ValueError()})
        with patch('utils.tool_registry.requests.get',
                   return_value=fake_resp) as mock_get:
            ok, text = self._registry().call(
                "get_weather", args={"city": "Delhi"})
            self.assertTrue(ok)
            self.assertIn("31", text)
            _, kwargs = mock_get.call_args
            self.assertEqual(kwargs['params'], {"city": "Delhi"})

    def test_post_call_json_body(self):
        self._write(_manifest(
            name="ac_control", method="POST",
            url="https://home.local/api/ac",
            params={"temperature": {"type": "number", "required": True}}))
        fake_resp = MagicMock(status_code=200, text="ok")
        with patch('utils.tool_registry.requests.request',
                   return_value=fake_resp) as mock_req:
            ok, _ = self._registry().call(
                "ac_control", args={"temperature": 22})
            self.assertTrue(ok)
            _, kwargs = mock_req.call_args
            self.assertEqual(kwargs['json'], {"temperature": 22})

    def test_url_placeholder_substitution(self):
        self._write(_manifest(
            name="user_lookup", method="GET",
            url="https://api.example.com/users/{user_id}",
            params={"user_id": {"type": "number", "required": True}}))
        fake_resp = MagicMock(status_code=200, text="found")
        with patch('utils.tool_registry.requests.get',
                   return_value=fake_resp) as mock_get:
            ok, _ = self._registry().call("user_lookup",
                                          args={"user_id": 42})
            self.assertTrue(ok)
            args_, kwargs = mock_get.call_args
            self.assertIn("/users/42", args_[0])
            self.assertNotIn("user_id", kwargs.get('params') or {})

    def test_missing_required_arg_raises(self):
        self._write(_manifest())
        from utils.tool_registry import ToolError
        with self.assertRaises(ToolError) as cm:
            self._registry().call("get_weather", args={})
        self.assertIn("required", str(cm.exception).lower())

    def test_unknown_arg_raises(self):
        self._write(_manifest())
        from utils.tool_registry import ToolError
        with self.assertRaises(ToolError) as cm:
            self._registry().call("get_weather",
                                  args={"city": "X", "bogus": 1})
        self.assertIn("unexpected", str(cm.exception).lower())

    def test_unknown_tool_lists_known(self):
        self._write(_manifest())
        from utils.tool_registry import ToolError
        with self.assertRaises(ToolError) as cm:
            self._registry().call("ghost")
        self.assertIn("get_weather", str(cm.exception))

    def test_http_error_reported(self):
        self._write(_manifest())
        fake_resp = MagicMock(status_code=500, text="boom")
        with patch('utils.tool_registry.requests.get',
                   return_value=fake_resp):
            ok, text = self._registry().call("get_weather",
                                             args={"city": "X"})
            self.assertFalse(ok)
            self.assertIn("500", text)

    def test_auth_env_header_injected(self):
        self._write(_manifest(auth_env="TEST_TOOL_TOKEN"))
        os.environ["TEST_TOOL_TOKEN"] = "sekret123"
        try:
            fake_resp = MagicMock(status_code=200, text="fine")
            with patch('utils.tool_registry.requests.get',
                       return_value=fake_resp) as mock_get:
                ok, _ = self._registry().call("get_weather",
                                              args={"city": "X"})
                _, kwargs = mock_get.call_args
                self.assertEqual(
                    kwargs['headers']['Authorization'], "Bearer sekret123")
        finally:
            os.environ.pop("TEST_TOOL_TOKEN", None)


# ====================================================================== #
# WebAgent DOM outline formatting (pure unit — no browser launch)
# ====================================================================== #

class TestWebAgentDomFormat(unittest.TestCase):
    def _agent_shell(self):
        """WebAgent instance without starting its worker thread."""
        import importlib
        import utils.agent as agent_mod
        importlib.reload(agent_mod)   # fresh class, no threads started
        agent = object.__new__(agent_mod.WebAgent)
        agent.base_dir = self.tmp = _make_tmp()
        return agent, agent_mod

    def tearDown(self):
        # best effort cleanup of any tmp dirs created during reloads
        pass

    def test_read_dom_formatting(self):
        agent, mod = self._agent_shell()
        fake_page = MagicMock()
        fake_page.evaluate.return_value = {
            "title": "Example Shop",
            "url": "https://shop.example.com/cart",
            "elements": [
                {"tag": "a", "text": "Checkout", "id": "checkout-btn",
                 "name": "", "href": "/pay"},
                {"tag": "input", "text": "", "id": "",
                 "name": "coupon", "href": ""},
            ],
        }
        out = mod.WebAgent._read_dom(agent, fake_page, {"max_elements": 10})
        self.assertIn("Example Shop", out)
        self.assertIn("[0] <a> \"Checkout\" #checkout-btn → /pay", out)
        self.assertIn("name=coupon", out)
        shutil.rmtree(agent.base_dir, ignore_errors=True)


# ====================================================================== #

if __name__ == '__main__':
    print("=" * 60)
    print("JARVIS PHASE C — SANDBOX / TOOLS / BROWSER TESTS")
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
