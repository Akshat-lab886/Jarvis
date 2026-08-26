"""
Offline end-to-end tests for the MCP client (utils/mcp_client.py).

A REAL fake MCP server subprocess (speaking newline-delimited
JSON-RPC 2.0 over stdio) is spawned and exercised through the full
client stack: handshake, tools/list, tools/call, namespacing, agent
tool-spec conversion — plus the AgentLoop executing an MCP tool for
real through FakeRouter scripting.
"""

import os
import sys
import json
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.mcp_client import (_ServerHandle, MCPPool,  # noqa: E402
                              load_server_configs, MCPServerError)

FAKE_SERVER = r'''
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = msg.get('method', '')
    mid = msg.get('id')
    if method == 'initialize':
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "0.1"}}}),
              flush=True)
    elif method == 'tools/list':
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
            "tools": [
                {"name": "echo",
                 "description": "Echo back the input text",
                 "inputSchema": {"type": "object",
                                 "properties": {"text": {"type": "string"}},
                                 "required": ["text"]}},
                {"name": "boom",
                 "description": "Always fails",
                 "inputSchema": {"type": "object", "properties": {}}}
            ]}}), flush=True)
    elif method == 'tools/call':
        args = (msg.get('params') or {}).get('arguments') or {}
        if msg['params']['name'] == 'boom':
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "kaboom"}],
                "isError": True}}), flush=True)
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text",
                             "text": f"echo: {args.get('text', '')}"}]}}),
                  flush=True)
'''


class TestMCPClient(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmpdir = tempfile.mkdtemp(prefix='jarvis_mcp_')
        cls.server_path = os.path.join(cls.tmpdir, 'fake_mcp_server.py')
        with open(cls.server_path, 'w') as fh:
            fh.write(FAKE_SERVER)
        cfg = [{"name": "test", "command": sys.executable,
                "args": [cls.server_path], "env": {}}]
        cls.cfg_path = os.path.join(cls.tmpdir, 'mcp_servers.json')
        with open(cls.cfg_path, 'w') as fh:
            json.dump(cfg, fh)
        cls.pool = MCPPool(configs=load_server_configs(cls.cfg_path))
        assert cls.pool.ensure_started(), "fake server failed to start"

    @classmethod
    def tearDownClass(cls):
        cls.pool.stop_all()

    # ---------------------------------------------------------------- #

    def test_handshake_and_tool_list(self):
        tools = self.pool.agent_tool_specs()
        names = [s['function']['name'] for s in tools]
        self.assertIn('mcp__test__echo', names)
        self.assertIn('mcp__test__boom', names)
        echo = [s for s in tools
                if s['function']['name'] == 'mcp__test__echo'][0]
        self.assertEqual(echo['function']['parameters']['required'],
                         ['text'])

    def test_call_round_trip(self):
        out = self.pool.call('mcp__test__echo', {'text': 'jarvis'})
        self.assertEqual(out, 'echo: jarvis')

    def test_error_flag_raises(self):
        with self.assertRaises(MCPServerError):
            self.pool.call('mcp__test__boom', {})

    def test_bad_namespace_rejected(self):
        with self.assertRaises(MCPServerError):
            self.pool.call('no_prefix', {})
        with self.assertRaises(MCPServerError):
            self.pool.call('mcp__missingserver__tool', {})

    def test_parse_namespace(self):
        self.assertEqual(MCPPool.parse_namespace('mcp__a__b'), ('a', 'b'))
        self.assertIsNone(MCPPool.parse_namespace('plain'))
        self.assertIsNone(MCPPool.parse_namespace('mcp__noseparator'))

    def test_status_reflects_online_server(self):
        st = {s['name']: s for s in self.pool.status()}
        self.assertEqual(st['test']['state'], 'online')

    def test_dead_server_fails_fast(self):
        h = _ServerHandle({"name": "ghost", "command": sys.executable,
                           "args": ["-c", "pass"]})
        h.start()          # process exits immediately
        deadline = time.time() + 5
        while h.alive and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(h.alive)
        with self.assertRaises(Exception):
            h.request("tools/list", timeout=3)


class TestMCPInAgentLoop(unittest.TestCase):
    """The full path: model calls mcp__tool → pool → real subprocess."""

    @classmethod
    def setUpClass(cls):
        cls.test = TestMCPClient('test_handshake_and_tool_list')
        cls.test.setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls.test.pool.stop_all()

    def test_agent_loop_executes_mcp_tool(self):
        from utils.agent_loop import AgentLoop
        from utils.llm.providers.base import ChatResult, ToolCall

        class R:
            providers = {'groq': object()}

            def __init__(self, script):
                self.script, self.i = script, -1

            def _chain(self, require=None, models=None):
                return [('groq', 'm')]

            def chat(self, messages, **kw):
                self.i += 1
                return self.script[self.i]

        tool_turn = ChatResult(finish_reason='tool_calls', provider='g',
                               model='m',
                               tool_calls=[ToolCall(
                                   id='m1', name='mcp__test__echo',
                                   arguments=json.dumps(
                                       {'text': 'hello fleet'}))])
        final = ChatResult(text='The fleet says: echo: hello fleet',
                           finish_reason='stop', provider='g', model='m')
        router = R([tool_turn, final])

        brain = type('B', (), {'router': router,
                               '_append_history': lambda s, u, a: None})()
        loop = AgentLoop(brain)

        class M:
            suppress = False
        loop._executor = type('E', (), {'mouth': M(),
                                        'execute_command': lambda *a,
                                        **k: ''})()
        # Route MCP calls of THIS test to the shared fake-server pool.
        import utils.mcp_client as mc
        orig_pool = mc._pool
        mc._pool = self.test.pool
        try:
            out = loop.run("use the echo tool")
        finally:
            mc._pool = orig_pool
        self.assertEqual(out['response'],
                         'The fleet says: echo: hello fleet')


if __name__ == '__main__':
    unittest.main()
