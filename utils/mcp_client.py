"""
Jarvis MCP Client — Model Context Protocol over stdio.

Connects Jarvis to ANY MCP-compatible tool server (filesystem, git,
browser, databases, Slack, ...) declared in ``config/mcp_servers.json``::

    [
      {"name": "files",
       "command": "npx",
       "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
       "env": {}}
    ]

Protocol: newline-delimited JSON-RPC 2.0.
  1. client -> initialize {protocolVersion, capabilities, clientInfo}
  2. server -> result {capabilities, serverInfo}
  3. client -> notifications/initialized
  4. tools/list  -> {tools: [{name, description?, inputSchema?}]}
  5. tools/call {name, arguments} -> {content: [{type: text,...}], isError?}

Tools are exposed to the agent loop namespaced as
``mcp__<server>__<tool>`` so collisions across servers are impossible.

Design constraints honoured:
  * A broken/hung server can never block Jarvis: every request runs
    under a timeout; a dead pipe marks the server offline instantly.
  * No new dependencies — pure stdlib (subprocess + threads).
"""

import os
import json
import time
import uuid
import atexit
import logging
import threading
import subprocess

logger = logging.getLogger("Jarvis.MCP")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, 'config', 'mcp_servers.json')

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "jarvis", "version": "1.0"}


def _env_float(name, default, floor=None):
    """Parse a float env var safely: a malformed value falls back to the
    default instead of crashing the module at import, and a floor keeps
    a 0/negative value from producing an instant/negative subprocess
    timeout."""
    try:
        value = float(os.getenv(name, default))
    except (TypeError, ValueError):
        value = float(default)
    return value if floor is None else max(floor, value)


def _env_int(name, default, floor=None):
    try:
        value = int(os.getenv(name, default))
    except (TypeError, ValueError):
        value = int(default)
    return value if floor is None else max(floor, value)


START_TIMEOUT = _env_float('JARVIS_MCP_START_TIMEOUT', '15', floor=1.0)
CALL_TIMEOUT = _env_float('JARVIS_MCP_CALL_TIMEOUT', '30', floor=1.0)
MAX_SERVERS = _env_int('JARVIS_MCP_MAX_SERVERS', '8', floor=1)


def load_server_configs(path=None):
    """Parse the config file; returns [] when absent or malformed."""
    path = path or CONFIG_PATH
    try:
        if not os.path.exists(path):
            return []
        with open(path, 'r') as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data = data.get('servers', [])
        out = []
        for entry in data or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get('name', '')).strip()
            command = str(entry.get('command', '')).strip()
            if not name or not command:
                continue          # url-transport entries ignored for now
            out.append({
                "name": name,
                "command": command,
                "args": [str(a) for a in entry.get('args', [])],
                "env": {str(k): str(v)
                        for k, v in (entry.get('env') or {}).items()},
            })
        return out[:MAX_SERVERS]
    except Exception as e:
        logger.warning("MCP config unreadable (%s): %s", path, e)
        return []


class MCPServerError(Exception):
    pass


class _ServerHandle:
    """One spawned MCP server process + its JSON-RPC plumbing."""

    def __init__(self, cfg):
        self.name = cfg['name']
        self.cfg = cfg
        self.proc = None
        self.pending = {}                 # id -> [event, response-dict]
        self.pending_lock = threading.Lock()
        self.reader = None
        self.alive = False
        self.tools = None                 # cached list_tools() result
        self.last_error = ''
        self._send_lock = threading.Lock()

    # ---------------- lifecycle ---------------- #

    def start(self):
        env = dict(os.environ)
        env.update(self.cfg.get('env') or {})
        try:
            self.proc = subprocess.Popen(
                [self.cfg['command']] + list(self.cfg.get('args', [])),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env, text=True, bufsize=1)
        except Exception as e:
            self.last_error = f"spawn failed: {e}"
            logger.warning("MCP '%s' %s", self.name, self.last_error)
            return False

        self.alive = True
        self.reader = threading.Thread(
            target=self._read_loop, daemon=True,
            name=f"mcp-{self.name}-reader")
        self.reader.start()

        try:
            result = self.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            }, timeout=START_TIMEOUT)
        except Exception as e:
            self.last_error = f"initialize failed: {e}"
            # Guard: logging may raise BrokenPipeError / OSError during
            # early boot when stdout is redirected or the pipe is closed.
            # Never let a failing log call escape — the caller's exception
            # handler is responsible for reporting; we just suppress the
            # logging side-effect.
            try:
                logger.warning("MCP '%s' %s", self.name, self.last_error)
            except Exception:
                pass
            self.stop()
            return False

        self.notify("notifications/initialized")
        caps = (result or {}).get('capabilities')
        logger.info("MCP '%s' online (capabilities=%s)",
                    self.name, sorted((caps or {}).keys()))
        return True

    def stop(self):
        self.alive = False
        proc, self.proc = getattr(self, 'proc', None), None
        if proc:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    # ---------------- transport ---------------- #

    def _read_loop(self):
        """Route newline-delimited JSON-RPC messages to pending calls."""
        proc = self.proc
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line or not self.alive:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg_id = msg.get('id')
                if msg_id is not None:
                    with self.pending_lock:
                        waiter = self.pending.pop(msg_id, None)
                    if waiter:
                        waiter[1].append(msg)
                        waiter[0].set()
                # notifications (no id) are logged and dropped
                method = msg.get('method', '')
                if method:
                    logger.debug("MCP '%s' notification: %s",
                                 self.name, method)
        except Exception:
            pass
        finally:
            self.alive = False
            # Fail everyone still waiting.
            with self.pending_lock:
                waiters = list(self.pending.values())
                self.pending.clear()
            for event, sink in waiters:
                sink.append({"error": {"code": -32000,
                                       "message": "server died"}})
                event.set()

    def _send(self, payload):
        with self._send_lock:
            if not self.proc or not self.proc.stdin:
                raise MCPServerError(f"'{self.name}' is not running")
            try:
                self.proc.stdin.write(json.dumps(payload) + "\n")
                self.proc.stdin.flush()
            except Exception as e:
                self.alive = False
                raise MCPServerError(f"'{self.name}' write failed: {e}")

    def request(self, method, params=None, timeout=None):
        req_id = uuid.uuid4().hex[:12]      # unique even across restarts
        event = threading.Event()
        sink = []
        with self.pending_lock:
            self.pending[req_id] = [event, sink]
        try:
            self._send({"jsonrpc": "2.0", "id": req_id,
                        "method": method,
                        **({"params": params} if params else {})})
        except Exception:
            with self.pending_lock:
                self.pending.pop(req_id, None)
            raise
        if not event.wait(timeout or CALL_TIMEOUT):
            with self.pending_lock:
                self.pending.pop(req_id, None)
            raise MCPServerError(f"'{self.name}' timed out on {method}")
        resp = sink[0] if sink else {}
        if "error" in resp:
            raise MCPServerError(
                f"{method}: {resp['error'].get('message', 'error')}")
        return resp.get("result")

    def notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method,
                   **({"params": params} if params else {})}
        self._send(payload)

    # ---------------- MCP surface ---------------- #

    def list_tools(self, force=False):
        if self.tools is not None and not force:
            return self.tools
        result = self.request("tools/list") or {}
        tools = []
        for t in result.get('tools', []):
            if isinstance(t, dict) and t.get('name'):
                tools.append(t)
        self.tools = tools
        return tools

    def call(self, tool_name, arguments=None):
        result = self.request("tools/call",
                              {"name": tool_name,
                               "arguments": arguments or {}})
        parts = []
        for block in (result or {}).get('content', []) or []:
            if isinstance(block, dict) and block.get('type') == 'text':
                parts.append(block.get('text', ''))
        text = "\n".join(p for p in parts if p).strip()
        if (result or {}).get('isError'):
            raise MCPServerError(text or f"'{tool_name}' failed")
        return text or f"{tool_name}: done."


class MCPPool:
    """
    Owns every configured MCP server.  Lazy: nothing spawns until the
    first agent request that wants tools.
    """

    def __init__(self, configs=None):
        self.configs = configs if configs is not None \
            else load_server_configs()
        self.handles = {}                 # name -> _ServerHandle

    def ensure_started(self):
        """Start any configured-but-not-running servers."""
        started_any = False
        for cfg in self.configs:
            h = self.handles.get(cfg['name'])
            if h is None:
                h = _ServerHandle(cfg)
                self.handles[cfg['name']] = h
            if h.alive:
                continue
            if h.start():
                started_any = True
            # dead servers stay recorded with last_error for status()
        return started_any

    @staticmethod
    def namespace(server, tool):
        return f"mcp__{server}__{tool}"

    @staticmethod
    def parse_namespace(name):
        if not name.startswith('mcp__'):
            return None
        rest = name[5:]
        server, sep, tool = rest.partition('__')
        if not sep or not server or not tool:
            return None
        return server, tool

    def agent_tool_specs(self, max_tools=24):
        """
        OpenAI-format specs for every online server's tools, namespaced.
        Never raises; returns whatever came up in time.
        """
        try:
            self.ensure_started()
        except Exception as e:
            logger.warning("MCP startup problem: %s", e)
            return []
        specs = []
        for cfg in self.configs:
            h = self.handles.get(cfg['name'])
            if not h or not h.alive:
                continue
            try:
                for t in h.list_tools():
                    if len(specs) >= max_tools:
                        return specs
                    schema = t.get('inputSchema') or {
                        "type": "object", "properties": {}}
                    if not isinstance(schema, dict) \
                            or schema.get('type') != 'object':
                        schema = {"type": "object", "properties": {}}
                    specs.append({
                        "type": "function",
                        "function": {
                            "name": self.namespace(cfg['name'],
                                                   t['name']),
                            "description":
                                (t.get('description')
                                 or f"MPC tool {t['name']} on "
                                 f"{cfg['name']}")[:300],
                            "parameters": schema,
                        }})
            except Exception as e:
                logger.warning("MCP '%s' tools/list failed: %s",
                               cfg['name'], e)
                h.alive = False
        return specs

    def call(self, namespaced, arguments=None):
        target = self.parse_namespace(namespaced)
        if not target:
            raise MCPServerError(f"bad MCP tool name '{namespaced}'")
        server, tool = target
        h = self.handles.get(server)
        if h is None or not h.alive:
            raise MCPServerError(f"MCP server '{server}' is offline")
        return h.call(tool, arguments)

    def status(self):
        out = []
        for cfg in self.configs:
            h = self.handles.get(cfg['name'])
            entry = {"name": cfg['name'], "command": cfg['command']}
            if h is None:
                entry["state"] = "unstarted"
            elif h.alive:
                entry["state"] = "online"
                entry["tools"] = len(h.tools or [])
            else:
                entry["state"] = "offline"
                entry["error"] = h.last_error
            out.append(entry)
        return out

    def stop_all(self):
        for h in self.handles.values():
            h.stop()


# --------------------------------------------------------------------- #

_pool = None
_pool_lock = threading.Lock()


def get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = MCPPool()

            def _shutdown():
                try:
                    _pool.stop_all()
                except Exception:
                    pass
            atexit.register(_shutdown)
        return _pool


def reload_pool():
    """Re-read the config (after the user edited mcp_servers.json)."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.stop_all()
        _pool = None
    return get_pool()
