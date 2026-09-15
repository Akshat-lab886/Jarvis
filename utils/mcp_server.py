"""
J.A.R.V.I.S. — MCP Server (Hermes parity)
=========================================

Jarvis doesn't just CONSUME MCP tool servers (utils/mcp_client.py) —
it also OPERATES AS ONE, exporting its memory layers to external
applications (Claude Desktop, IDEs, other agents):

    jarvis_recall            RLM recursive recall — world model,
                             themes, session memories, playbook
    jarvis_search_memory     episodic + FTS5 search across every past
                             conversation
    jarvis_list_skills       the skill catalog (code + SKILL.md)
    jarvis_ask               full think→execute pipeline (opt-in via
                             JARVIS_MCP_EXPOSE_ASK=1 — spawns real
                             side effects, so it is off by default)

Protocol: newline-delimited JSON-RPC 2.0 over stdio, identical shape
to the client's (initialize → tools/list → tools/call).

Run it standalone:

    python -m utils.mcp_server

…or point any MCP host at it.  ``JARVIS_MCP_SERVER=0`` makes serve()
exit immediately (a kill switch for hosts that auto-spawn it).
"""

import os
import sys
import json
import logging

logger = logging.getLogger("Jarvis.MCPServer")

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "jarvis", "version": "1.0"}

MAX_RESULT_CHARS = 6000


def serve(stdin=None, stdout=None):
    """
    Main stdio loop.  Reads one JSON-RPC request per line, writes one
    response per line.  Returns when stdin closes.
    """
    if os.getenv('JARVIS_MCP_SERVER', '1') == '0':
        return 0
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    while True:
        line = stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            _write(stdout, _error(None, -32700, "parse error"))
            continue
        # Valid JSON but not an object (array / string / number / null):
        # JSON-RPC calls this an Invalid Request (-32600).  Guarding here
        # keeps a malformed host from crashing the whole stdio loop via
        # an unhandled AttributeError on the .get() below.
        if not isinstance(request, dict):
            _write(stdout, _error(None, -32600, "invalid request"))
            continue
        method = request.get('method', '')
        req_id = request.get('id')
        is_notification = req_id is None and method

        try:
            if method == 'initialize':
                _write(stdout, _result(req_id, {
                    'protocolVersion': PROTOCOL_VERSION,
                    'capabilities': {'tools': {}},
                    'serverInfo': SERVER_INFO,
                }))
            elif method == 'notifications/initialized':
                pass                       # notification — no response
            elif method == 'ping':
                _write(stdout, _result(req_id, {}))
            elif method == 'tools/list':
                _write(stdout, _result(req_id, {'tools': TOOLS}))
            elif method == 'tools/call':
                params = request.get('params') or {}
                arguments = params.get('arguments') or {}
                if not isinstance(arguments, dict):
                    _write(stdout, _error(
                        req_id, -32602,
                        "arguments must be a JSON object"))
                    continue
                _write(stdout, _result(
                    req_id, _call_tool(params.get('name'),
                                       arguments)))
            elif method.startswith('notifications/'):
                pass
            elif not is_notification:
                _write(stdout, _error(req_id, -32601,
                                      f"unknown method {method!r}"))
        except Exception as e:
            logger.debug("mcp request failed: %s", e)
            _write(stdout, _error(req_id, -32603, str(e)[:300]))
    return 0


# --------------------------------------------------------------------- #
# Tool catalog
# --------------------------------------------------------------------- #

TOOLS = [
    {
        "name": "jarvis_recall",
        "description": "Recursive memory recall of the user's history: "
                       "stable world model, long-term themes, session "
                       "summaries and distilled insights.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "What to recall about the user"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "jarvis_search_memory",
        "description": "Full-text search across every past Jarvis "
                       "conversation (FTS5), returning matching "
                       "exchanges with timestamps.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Search keywords"},
                "limit": {"type": "integer",
                          "description": "Max results (default 5)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "jarvis_list_skills",
        "description": "List Jarvis's reusable skills (executable and "
                       "procedural).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "jarvis_goals",
        "description": "List Jarvis's tracked goals with progress and "
                       "deadline countdowns.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "jarvis_tasks",
        "description": "Active background tasks (or recent history with "
                       "history=true).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "history": {"type": "boolean",
                            "description": "recent finished tasks"},
            },
        },
    },
    {
        "name": "jarvis_ask",
        "description": "Send a request through Jarvis's full "
                       "think→execute pipeline (real side effects). "
                       "Disabled unless JARVIS_MCP_EXPOSE_ASK=1.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "The request for Jarvis"},
            },
            "required": ["text"],
        },
    },
]


def _call_tool(name, arguments):
    """Execute one tool; returns an MCP tools/call result object."""
    try:
        if name == 'jarvis_recall':
            from utils.rlm import get_rlm
            text = get_rlm().recall(str(arguments.get('query', '')),
                                    max_chars=MAX_RESULT_CHARS)
            return _text(text or "No matching memories in the "
                                 "recursive hierarchy.")

        if name == 'jarvis_search_memory':
            from utils.transcript_search import get_archive
            hits = get_archive().search(
                str(arguments.get('query', '')),
                limit=int(arguments.get('limit', 5) or 5))
            if not hits:
                return _text("No matching conversations found.")
            lines = []
            for h in hits:
                lines.append(f"[{h.get('created', '')[:16]}] "
                             f"user: {h.get('user', '')[:200]}")
                lines.append(f"            jarvis: "
                             f"{h.get('assistant', '')[:200]}")
            return _text("\n".join(lines)[:MAX_RESULT_CHARS])

        if name == 'jarvis_list_skills':
            from utils.skill_forge import render_catalog
            return _text(render_catalog(max_chars=MAX_RESULT_CHARS)
                         or "No skills saved yet.")

        if name == 'jarvis_goals':
            from utils.goals import get_goals
            return _text(get_goals().render()[:MAX_RESULT_CHARS])

        if name == 'jarvis_tasks':
            from utils.server import executor
            if arguments.get('history'):
                hist = executor.task_manager.get_history(limit=8)
                lines = ["Recent tasks:"] + [
                    f"- {t.id}: {t.description[:80]}" for t in hist]
                return _text("\n".join(lines)[:MAX_RESULT_CHARS]
                             if hist else "No finished tasks yet.")
            active = executor.task_manager.get_active_tasks()
            lines = ["Active tasks:"] + [
                f"- {t.id}: {t.description[:80]} "
                f"({t.progress_pct()}%)" for t in active]
            return _text("\n".join(lines)[:MAX_RESULT_CHARS]
                         if active else "No active background tasks.")

        if name == 'jarvis_ask':
            if os.getenv('JARVIS_MCP_EXPOSE_ASK', '0') != '1':
                return _text("jarvis_ask is disabled on this host "
                             "(set JARVIS_MCP_EXPOSE_ASK=1 to enable).")
            from utils.server import brain, executor
            command = brain.think(str(arguments.get('text', '')))
            result = executor.execute_command(command, brain)
            return _text(str(result)[:MAX_RESULT_CHARS])

        return _error_result(f"unknown tool {name!r}")
    except Exception as e:
        return _error_result(str(e)[:400])


# --------------------------------------------------------------------- #
# JSON-RPC plumbing
# --------------------------------------------------------------------- #

def _write(stream, obj):
    stream.write(json.dumps(obj) + "\n")
    stream.flush()


def _result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def _text(text):
    return {"content": [{"type": "text", "text": str(text)}]}


def _error_result(message):
    return {"content": [{"type": "text", "text": f"ERROR: {message}"}],
            "isError": True}


if __name__ == '__main__':
    sys.exit(serve())
