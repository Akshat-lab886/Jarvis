"""
Jarvis Agent Core — native tool-calling loop (Phase 2).

Replaces single-shot JSON routing for capable models with an iterative
agent loop:

    user prompt ──▶ LLM(tools) ──▶ tool_calls? ──▶ execute via Executor
                         ▲                              │
                         └────── results as messages ───┘
                         └─▶ no tools → final answer (streamed)

Design constraints honoured from Phase 1:
  * ANY provider works — OpenAI-style tool_calls, Gemini function calls
    and Anthropic tool_use are normalised to ChatResult.tool_calls by
    the providers; models WITHOUT tool support simply never emit any
    and the first turn's text becomes the final answer.
  * Guardrails stay centralised: every LLM call passes through the
    router (budget + circuit breaker + cooldowns); every side effect
    goes through Executor.execute_command so approvals, privacy trust
    levels and audit logging remain in force.
  * Rollback switch: JARVIS_AGENT_MODE=off restores pure legacy routing.
"""

import os
import json
import time
import logging

logger = logging.getLogger("Jarvis.AgentLoop")

MAX_STEPS_DEFAULT = 6
TOOL_OUTPUT_CAP = 1500        # chars of tool result fed back to the model

# Actions that must never be exposed as tools (recursion, meta, or
# already handled deterministically before the loop runs).
FORBIDDEN_TOOLS = {
    'chat', 'clear_history', 'help', 'conversation_history', 'last_topic',
    'analyze_photo',          # vision recursion guard
    'verify_identity', 'lock_system',
}


# --------------------------------------------------------------------- #
# Tool schemas (OpenAI function format; providers translate natively)
# --------------------------------------------------------------------- #

def _fn(name, description, props, required=None):
    return {"type": "function", "function": {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": props,
                       "required": required or []},
    }}


TOOL_SPECS = [
    _fn("search_web", "Search the live web for current information: "
        "news, prices, weather, sports, recent events.",
        {"query": {"type": "string"}}, ["query"]),
    _fn("read_webpage", "Fetch and summarize a specific URL.",
        {"url": {"type": "string"}}, ["url"]),
    _fn("recall", "Search the user's episodic memory (preferences, "
        "goals, facts, past conversations).",
        {"query": {"type": "string"}}, ["query"]),
    _fn("remember", "Store a lasting fact about the user.",
        {"text": {"type": "string"},
         "category": {"type": "string",
                      "enum": ["fact", "preference", "health", "goal",
                               "context", "routine"]}},
        ["text"]),
    _fn("todo_add", "Add an item to the to-do list.",
        {"text": {"type": "string"}}, ["text"]),
    _fn("todo_done", "Mark a to-do item done by keyword.",
        {"text": {"type": "string"}}, ["text"]),
    _fn("todo_list", "List the current to-do items.", {}),
    _fn("note_save", "Save a note.", {"text": {"type": "string"}},
        ["text"]),
    _fn("note_list", "List saved notes.", {}),
    _fn("set_reminder", "Set a reminder; keep the natural time phrase "
        "in the text ('in 20 minutes to …', 'tomorrow at 9am to …').",
        {"text": {"type": "string"}}, ["text"]),
    _fn("list_reminders", "List active reminders.", {}),
    _fn("check_calendar", "Show upcoming calendar events.", {}),
    _fn("add_event", "Create a calendar event from natural language.",
        {"text": {"type": "string"}}, ["text"]),
    _fn("check_email", "Check the Gmail inbox for recent messages.", {}),
    _fn("send_email", "Send an email. Ask the user if the address is "
        "unknown.", {"recipient": {"type": "string"},
                     "message": {"type": "string"}},
        ["recipient", "message"]),
    _fn("system_info", "CPU/RAM/disk/uptime snapshot of this machine.", {}),
    _fn("get_battery", "Battery level and charging state.", {}),
    _fn("open_app", "Launch a desktop application by name.",
        {"target": {"type": "string"}}, ["target"]),
    _fn("open_web", "Open a URL in the browser.",
        {"target": {"type": "string"}}, ["target"]),
    _fn("play_youtube", "Play a song/video on YouTube.",
        {"target": {"type": "string"}}, ["target"]),
    _fn("news_headlines", "Today's top headlines.", {}),
    _fn("morning_briefing", "Full morning briefing (calendar, email, "
        "weather, schedule insights).", {}),
    _fn("daily_summary", "Summarize everything the user did today.", {}),
    _fn("get_weather", "Local weather.", {}),
    _fn("get_stock", "Current stock quote.",
        {"symbol": {"type": "string"}}, ["symbol"]),
    _fn("run_skill", "Run a saved skill by name.",
        {"name": {"type": "string"},
         "params": {"type": "object"}}, ["name"]),
    _fn("list_skills", "List installed skills.", {}),
    _fn("complex_task", "Start a multi-step mission that runs in the "
        "background with its own planner; use for big goals.",
        {"task": {"type": "string"},
         "steps": {"type": "array",
                   "items": {"type": "string"}}},
        ["task"]),
    _fn("any_action", "Escape hatch: run ANY other Jarvis action not "
        "listed here. Pass the exact action name and its parameters.",
        {"action": {"type": "string"},
         "params": {"type": "object",
                    "description": "action parameters as an object"}},
        ["action"]),
]

_TOOL_NAMES = {s["function"]["name"] for s in TOOL_SPECS}


def agent_tools_enabled():
    """True when at least one curated tool can be offered."""
    mode = os.getenv('JARVIS_AGENT_MODE', 'auto').strip().lower()
    return mode != 'off'


def agent_mode_forced():
    return os.getenv('JARVIS_AGENT_MODE', 'auto').strip().lower() == 'on'


# --------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------- #

class AgentLoop:

    def __init__(self, brain):
        self.brain = brain
        self.max_steps = MAX_STEPS_DEFAULT
        try:
            self.max_steps = max(2, int(
                os.getenv('JARVIS_AGENT_MAX_STEPS', str(MAX_STEPS_DEFAULT))))
        except ValueError:
            pass

    # ---------------- executor bridge ---------------- #

    @property
    def executor(self):
        # Lazy import breaks the brain↔executor cycle; by call time the
        # server module is fully initialised.
        from utils.server import executor
        return executor

    def _exec_tool(self, name, arguments):
        """
        Execute one tool call through the Executor with TTS/UI suppressed.
        Returns the string result fed back to the model.
        """
        try:
            args = json.loads(arguments or '{}')
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError:
            return f"ERROR: arguments were not valid JSON: {arguments[:200]}"

        command = dict(args)
        if name == 'any_action':
            inner = str(command.pop('action', '')).strip()
            params = command.pop('params', None)
            if isinstance(params, dict):
                command.update(params)
            if not inner or inner in FORBIDDEN_TOOLS:
                return f"ERROR: action '{inner}' is not permitted."
            command['action'] = inner
            name = inner
        else:
            command['action'] = name

        executor = self.executor
        was_suppressed = getattr(executor.mouth, 'suppress', False)
        executor.mouth.suppress = True

        def quiet_ui(event, data):
            # Surface activity on the status strip without polluting
            # the transcript (the final answer is emitted once, later).
            if event == 'status':
                logger.info("tool %s: %s", name,
                            data.get('message', ''))

        try:
            result = executor.execute_command(
                command, self.brain, ui_callback=quiet_ui)
        except Exception as e:
            result = f"ERROR executing {name}: {e}"
        finally:
            executor.mouth.suppress = was_suppressed

        out = str(result or '').strip() or f"{name}: done."
        return out[:TOOL_OUTPUT_CAP]

    def _emit(self, ui_callback, event, data):
        if ui_callback:
            try:
                ui_callback(event, data)
            except Exception:
                pass

    # ---------------- one model turn ---------------- #

    def _turn(self, messages, ui_callback, stream):
        """
        Request one completion with tools attached.  Returns a dict:
          {'text': str, 'tool_calls': [ToolCall], 'finish': str}
        Streams when possible; text deltas flush live only once the turn
        resolves as a FINAL answer (no tool calls), so intermediate
        reasoning never leaks into the transcript.
        """
        router = getattr(self.brain, 'router', None)
        if router is None or len(router.providers) == 0:
            return None

        buffered = []
        saw_tool_calls = None
        finish = ''
        usage = None
        provider = model = ''

        result = router.chat(messages, require={'tools'},
                             tools=list(TOOL_SPECS), stream=stream)

        if hasattr(result, 'tool_calls'):          # blocking ChatResult
            return {'text': result.text or '',
                    'tool_calls': result.tool_calls or [],
                    'finish': result.finish_reason or 'stop',
                    'usage': result.usage}

        # Streaming iterator of ChatResult chunks.
        for chunk in result:
            provider = chunk.provider or provider
            model = chunk.model or model
            finish = chunk.finish_reason or finish
            if chunk.tool_calls:
                saw_tool_calls = list(chunk.tool_calls)
            elif chunk.text:
                buffered.append(chunk.text)
        text = ''.join(buffered)
        if saw_tool_calls:
            self._emit(ui_callback, 'status',
                       {'message': f'agent → {len(saw_tool_calls)} '
                                   f'tool call(s)'})
            return {'text': '', 'tool_calls': saw_tool_calls,
                    'finish': finish or 'tool_calls', 'usage': None}
        # Final answer — replay the buffer as a live stream burst.
        for piece in buffered:
            self._emit(ui_callback, 'ai_text_stream', {'delta': piece})
        self._emit(ui_callback, 'ai_text_stream_end', {})
        return {'text': text, 'tool_calls': [],
                'finish': finish or 'stop', 'usage': None}

    # ---------------- public entry ---------------- #

    def run(self, prompt, image_path=None, system_instruction=None,
            history_snapshot=None, ui_callback=None):
        """
        Run the agentic loop.  Returns a pipeline-compatible command
        dict ``{"action": "chat", "response": ...}`` on success, or
        None when the fleet cannot serve tools (caller falls back to
        legacy routing).
        """
        if not agent_tools_enabled():
            return None
        router = getattr(self.brain, 'router', None)
        if router is None or len(router.providers) == 0:
            return None
        try:
            chain = router._chain(require={'tools'})
        except Exception:
            chain = []
        if not chain:
            return None

        stream_enabled = os.getenv('JARVIS_AGENT_STREAM', '1') != '0'

        messages = [{"role": "system",
                     "content": system_instruction or "You are Jarvis."}]
        for old_user, old_ai in (history_snapshot or []):
            messages.append({"role": "user", "content": old_user})
            messages.append({"role": "assistant", "content": old_ai})

        content = prompt
        if image_path:
            paths = image_path if isinstance(image_path, list) \
                else [image_path]
            parts = [{"type": "text", "text": prompt}]
            for p in paths:
                if p and os.path.exists(p):
                    import base64
                    with open(p, 'rb') as fh:
                        b64 = base64.b64encode(fh.read()).decode()
                    parts.append({"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{b64}"}})
            content = parts
        messages.append({"role": "user", "content": content})

        t0 = time.time()
        steps = 0
        while steps < self.max_steps:
            steps += 1
            try:
                turn = self._turn(
                    messages, ui_callback,
                    stream=stream_enabled and steps <= self.max_steps - 1)
            except Exception as e:
                logger.warning("agent turn failed (%d/%d): %s",
                               steps, self.max_steps, e)
                if steps == 1:
                    return None       # let legacy path take over cleanly
                break

            if turn is None:
                return None

            if turn['tool_calls']:
                # Assistant message requesting tools…
                messages.append({
                    "role": "assistant",
                    "content": turn['text'] or '',
                    "tool_calls": [
                        {"id": tc.id or f"call_{steps}_{i}",
                         "type": "function",
                         "function": {"name": tc.name,
                                      "arguments": tc.arguments}}
                        for i, tc in enumerate(turn['tool_calls'])],
                })
                # …then each result as a tool message.
                for i, tc in enumerate(turn['tool_calls']):
                    self._emit(ui_callback, 'status',
                               {'message': f'[RUN] {tc.name}'})
                    out = self._exec_tool(tc.name, tc.arguments)
                    self._emit(ui_callback, 'status',
                               {'message': f'[ OK ] {tc.name}'})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id or f"call_{steps}_{i}",
                        "content": out,
                    })
                continue

            # No tools → this is the final answer.
            final = (turn['text'] or '').strip()
            if final:
                self.brain._append_history(prompt, final)
                logger.info("agent loop done in %d step(s), %.1fs",
                            steps, time.time() - t0)
                return {"action": "chat", "response": final}
            break   # empty turn — fall through to legacy handling

        # Step budget exhausted: force a closing summary without tools.
        try:
            messages.append({"role": "user", "content":
                             "Summarize the results above for the user "
                             "now, concisely. Do not call any more tools."})
            router = getattr(self.brain, 'router', None)
            result = router.chat(messages, temperature=0.2, timeout=45)
            final = (getattr(result, 'text', '') or '').strip()
            if final:
                self.brain._append_history(prompt, final)
                return {"action": "chat", "response": final}
        except Exception as e:
            logger.warning("agent wrap-up failed: %s", e)
        return None


# --------------------------------------------------------------------- #

_loop = None


def get_agent_loop(brain):
    global _loop
    if _loop is None:
        _loop = AgentLoop(brain)
    return _loop
