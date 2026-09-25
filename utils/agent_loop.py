"""
Jarvis Agent Core — native tool-calling loop (Phase 2 + RLM reasoning).

Replaces single-shot JSON routing for capable models with an iterative,
reasoning-first agent loop:

    user prompt ──▶ [planner draft] ─▶ LLM(tools) ──▶ tool_calls?
                         ▲                   │            │
                         │                   │      execute via Executor
                         │                   │            │
                         │        ┌──────────┴────────────┘
                         │        ▼
                         │   results as messages ──▶ next turn
                         │        │
                         │   repeated tool ERRORS ──▶ critic strategy
                         │                              correction
                         └─▶ no tools → final answer (streamed)

RLM (Recursive/Reasoning Language Model) upgrades on the base loop:
  * ``think`` tool — a zero-side-effect scratchpad so the model can
    lay out an approach before acting (reasoning-first turns)
  * ``recall_deep`` tool — recursive long-term memory retrieval across
    every abstraction level (weeks/months of narrative context)
  * ``task_plan`` tool — a persistent, agent-owned multi-step plan
    that survives restarts (continuity across sessions)
  * ``delegate`` tool — hand self-contained subtasks to specialized
    privilege-separated subagents (researcher/coder/synthesizer)
  * parallel tool calls — independent calls of one turn execute
    concurrently while side effects stay serialized
    (JARVIS_AGENT_PARALLEL=0 forces sequential)
  * plan injection — complex prompts get a short planner draft in the
    system message (JARVIS_AGENT_PLAN=0 disables)
  * failure recovery — after repeated tool errors the critic persona
    proposes a strategy correction injected mid-loop
  * answer verification — after real tool work the critic checks the
    final answer against the request and may replace it
    (JARVIS_AGENT_VERIFY=0 disables)
  * deep-reasoning mode — "think harder"-style cues or
    JARVIS_REASON_EFFORT=deep intensify thinking, raise the step
    budget and force verification
  * generous step budget + tool-output cap for long-horizon work

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
import threading
import logging

from utils.rlm.reasoner import plan_enabled

logger = logging.getLogger("Jarvis.AgentLoop")

MAX_STEPS_DEFAULT = 10

# Chars of tool result fed back to the model.  Long-horizon research
# needs real payloads, not telegraphic stubs (JARVIS_AGENT_TOOL_CAP).
try:
    TOOL_OUTPUT_CAP = max(500, int(os.getenv('JARVIS_AGENT_TOOL_CAP',
                                             '4000')))
except ValueError:
    TOOL_OUTPUT_CAP = 4000

# Serializes executor-bound side effects when tool calls of one turn
# run concurrently — real-world actions never interleave.
_EXEC_LOCK = threading.RLock()


def parallel_enabled():
    """JARVIS_AGENT_PARALLEL=0 forces strictly sequential tools."""
    return os.getenv('JARVIS_AGENT_PARALLEL', '1') != '0'

# Actions that must never be exposed as tools (recursion, meta, or
# already handled deterministically before the loop runs).
FORBIDDEN_TOOLS = {
    'chat', 'clear_history', 'help', 'conversation_history', 'last_topic',
    'analyze_photo',          # vision recursion guard
    'verify_identity', 'lock_system',
    # In-loop tools: callable directly, never via any_action.
    # (RLM reasoning: think/recall_deep/memory_about/task_plan/delegate;
    # autonomy: goal_set/goal_list/goal_done/background_task/task_status/
    # automate.)
    'think', 'recall_deep', 'memory_about', 'task_plan', 'delegate',
    'goal_set', 'goal_list', 'goal_done', 'background_task', 'task_status',
    'automate', 'capability_check', 'capability_expand',
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
    # --- RLM reasoning tools (zero side effects, handled in-loop) --- #
    _fn("think", "Private scratchpad for reasoning. Lay out your "
        "approach, weigh options, or analyze tool results BEFORE your "
        "next move. Use it for any multi-step or uncertain task; keep "
        "each thought to 1-3 sentences. The user never sees this.",
        {"thought": {"type": "string"}}, ["thought"]),
    _fn("recall_deep", "Search Jarvis's recursive long-term memory "
        "across ALL abstraction levels: past weeks of conversations, "
        "session summaries, long-term themes, distilled insights and "
        "the world model. Use for anything about the user's history, "
        "past projects, evolving preferences, or 'what did we decide "
        "about X'.",
        {"query": {"type": "string"}}, ["query"]),
    _fn("memory_about", "Query long-term memory about ONE subject — a "
        "person, project or topic — via the entity graph: every "
        "insight, session memory and event that mentions it, plus "
        "related subjects. Omit 'entity' to list the subjects memory "
        "currently tracks. Use before 'tell me about X' or 'what's the "
        "status of X' questions.",
        {"entity": {"type": "string",
                    "description": "subject name (fuzzy-matched)"}}, []),
    _fn("task_plan", "Your persistent multi-step plan — it survives "
        "across conversations and restarts. set: create a plan (goal + "
        "steps) for any task needing 3+ steps. update: mark a step "
        "pending|in_progress|done|skipped as you work. get: review. "
        "clear: finish/abandon.",
        {"op": {"type": "string",
                "enum": ["set", "update", "get", "clear"]},
         "goal": {"type": "string"},
         "steps": {"type": "array", "items": {"type": "string"}},
         "step_index": {"type": "integer",
                        "description": "1-based step number (op=update)"},
         "status": {"type": "string",
                    "enum": ["pending", "in_progress", "done",
                             "skipped"]}},
        ["op"]),
    _fn("delegate", "Hand one self-contained subtask to a specialized "
        "subagent and get its finished result: researcher (facts/"
        "analysis), coder (runnable Python), synthesizer (polished "
        "prose/summaries). Use for bulky independent chunks of work so "
        "you can proceed in parallel.",
        {"task": {"type": "string",
                  "description": "complete, self-contained instruction"},
         "persona": {"type": "string",
                     "enum": ["researcher", "coder", "synthesizer"]}},
        ["task"]),
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
    _fn("get_weather", "Current weather + severe-weather alerts for a "
        "city. Omit 'city' to use the detected location.",
        {"city": {"type": "string", "description": "optional city/place"}},
        []),
    _fn("convert_currency", "Convert an amount between currencies (e.g. "
        "100 USD to EUR). ECB reference rates.",
        {"amount": {"type": "number"},
         "from": {"type": "string"},
         "to": {"type": "string"}},
        ["amount", "from", "to"]),
    _fn("check_url", "Scan a URL or file path for malware/phishing "
        "(URLhaus + Google Safe Browsing + VirusTotal when keyed).",
        {"url": {"type": "string",
                 "description": "URL to scan, or 'file:<path>' for a "
                                "local file hash scan"}},
        ["url"]),
    _fn("geocode", "Convert a place name to latitude/longitude, or "
        "reverse: a lat,lon to a place name. Set reverse=true for the "
        "reverse direction.",
        {"query": {"type": "string"},
         "lat": {"type": "number"},
         "lon": {"type": "number"},
         "reverse": {"type": "boolean"}},
        []),
    _fn("get_stock", "Current stock quote.",
        {"symbol": {"type": "string"}}, ["symbol"]),
    _fn("run_skill", "Run a saved skill by name.",
        {"name": {"type": "string"},
         "params": {"type": "object"}}, ["name"]),
    _fn("list_skills", "List installed skills.", {}),
    _fn("read_skill", "Load the FULL instructions of a skill before "
        "following it (progressive disclosure).",
        {"name": {"type": "string"}}, ["name"]),
    _fn("python_rpc", "Execute Python in Jarvis's PERSISTENT sandboxed "
        "session — variables survive between calls, so load data once "
        "and query it repeatedly. Each call is safety-scanned.",
        {"code": {"type": "string"},
         "reset": {"type": "boolean",
                   "description": "true to clear the namespace first"}},
        ["code"]),
    _fn("sandbox_run", "Run code or a shell command on a different "
        "execution backend: local, docker, ssh, daytona, singularity, "
        "vercel or modal.",
        {"code": {"type": "string"},
         "command": {"type": "string"},
         "backend": {"type": "string",
                     "enum": ["auto", "local", "docker", "ssh", "daytona",
                              "singularity", "vercel", "modal"]}},
        []),
    _fn("run_code", "Execute code in many languages (Python, JS, C, C++, "
        "Rust, Go…) via a hosted executor and return the output. Use to "
        "verify code you generate, e.g. coder.py results, in a language "
        "the local Python sandbox cannot run. 'lang' may be a name or "
        "file extension (.py, .js, .cpp).",
        {"code": {"type": "string"},
         "lang": {"type": "string"},
         "stdin": {"type": "string"}},
        ["code"]),
    _fn("track_flight", "Track a live flight by callsign (e.g. UAL 123, "
        "BAW456) or list overhead aircraft near a lat/lon. Use to answer "
        "'track my flight' or 'what is flying overhead'.",
        {"callsign": {"type": "string"},
         "lat": {"type": "number"},
         "lon": {"type": "number"},
         "near": {"type": "boolean",
                  "description": "true to list aircraft near lat/lon"}},
        []),
    _fn("track_package", "Track a delivery package by tracking number. "
        "Carrier is auto-detected. (Requires WhereParcel key for live "
        "status; falls back to identifying the carrier.)",
        {"number": {"type": "string"}}, ["number"]),
    _fn("food_lookup", "Look up a food's nutrition by barcode or name "
        "(Open Food Facts). Ties into fridge_vision scans.",
        {"code": {"type": "string",
                  "description": "barcode (digits) or a product name"}},
        ["code"]),
    _fn("recipe", "Find meal ideas / a recipe by name or main ingredient "
        "(TheMealDB). e.g. 'chicken', 'rice', 'pasta'.",
        {"query": {"type": "string"}}, ["query"]),
    _fn("define_word", "Look up a word's definition, pronunciation and "
        "synonyms (Free Dictionary).",
        {"word": {"type": "string"}}, ["word"]),
    _fn("lookup_book", "Search books by title/author → title, author, year "
        "(Open Library).",
        {"query": {"type": "string"}}, ["query"]),
    _fn("check_crypto", "Live price for a cryptocurrency (CoinGecko) by "
        "name or ticker, e.g. 'btc', 'ethereum', 'sol'.",
        {"symbol": {"type": "string"}}, ["symbol"]),
    _fn("public_holidays", "Public holidays for a country (ISO-2 code, "
        "e.g. US/GB/IN).",
        {"country": {"type": "string"},
         "year": {"type": "integer"}}, []),
    _fn("movie_lookup", "Search movies by title → name, year, rating "
        "(TMDb).",
        {"title": {"type": "string"}}, ["title"]),
    _fn("space_report", "NASA APOD (photo of the day) or the ISS's "
        "current position above Earth.",
        {"what": {"type": "string",
                  "enum": ["apod", "iss", "both"], "description": "apod "
                  "= astronomy picture of the day; iss = station position"}},
        []),
    _fn("research_papers", "Search academic papers on a topic (OpenAlex) "
        "-> title, authors, year, citations.",
        {"query": {"type": "string"}}, ["query"]),
    _fn("open_papers", "Search OPEN-ACCESS research papers (CORE/OpenAlex) "
        "with abstract + link. Use for free full-text.",
        {"query": {"type": "string"},
         "limit": {"type": "integer", "description": "max results (default 3)"}},
        ["query"]),
    _fn("weather", "Live weather + forecast from Open-Meteo (no key). "
        "Give lat/lon.",
        {"lat": {"type": "number"}, "lon": {"type": "number"},
         "days": {"type": "integer", "description": "forecast days, default 1"}},
        ["lat", "lon"]),
    _fn("define", "Dictionary definition, phonetics, and part of speech "
        "(Free Dictionary API, no key).",
        {"word": {"type": "string"}}, ["word"]),
    _fn("scripture", "Look up a Bible verse by reference (bible-api.com, "
        "no key, KJV).",
        {"ref": {"type": "string", "description": "e.g. 'John 3:16'"}},
        ["ref"]),
    _fn("ip_locate", "Geolocate a public IP or the caller's IP "
        "(ipapi.co, free ~1k/day).",
        {"ip": {"type": "string", "description": "IP to look up, or empty for yours"}},
        []),
    _fn("earthquakes", "Recent earthquakes near a location (USGS, no key).",
        {"limit": {"type": "integer"},
         "radius_km": {"type": "integer"},
         "lat": {"type": "number"},
         "lon": {"type": "number"}},
        []),
    _fn("joke", "A random Chuck Norris fact (no key).",
        {"category": {"type": "string"}}, []),
    # 'recipe' (Meal DB), 'define_word' (Free Dictionary), 'cocktail',
    # 'lookup_book' (Open Library) already register above (~282-290).
    _fn("cocktail", "Search cocktail recipes by name (The Cocktail DB, no key).",
        {"query": {"type": "string"}}, ["query"]),
    _fn("cat_fact", "A random cat fact (no key).", {}, []),
    _fn("dog_pic", "Fetch a random dog picture URL (Dog CEO, no key).", {}, []),
    _fn("quote", "A random inspirational quote, optionally by tag "
        "(Quotable, no key).",
        {"tag": {"type": "string", "description": "optional tag filter"}},
        []),
    _fn("eth_watch", "Check an Ethereum address's balance (Etherscan).",
        {"address": {"type": "string"}}, ["address"]),
    _fn("ocr_image", "Extract text from an image or PDF scan (OCR.Space). "
        "Pass a /static path or data URL.",
        {"image": {"type": "string"}}, ["image"]),
    _fn("gen_pdf", "Render a URL or HTML to a PDF file (pdflayer).",
        {"url": {"type": "string"},
         "fname": {"type": "string"},
         "page_size": {"type": "string"}}, ["url"]),
    _fn("track_habit", "Track a habit metric on a Pixela streak graph "
        "(needs PIXELA_USER).",
        {"metric": {"type": "string"}}, ["metric"]),
    _fn("computer_use", "Background desktop control: click, type, press "
        "keys, scroll, screenshot, or click a named UI element of a "
        "background app.",
        {"op": {"type": "string",
                "enum": ["click", "click_element", "set_field", "type",
                         "key", "scroll", "screenshot", "activate"]},
         "app": {"type": "string"}, "element": {"type": "string"},
         "value": {"type": "string"}, "text": {"type": "string"},
         "key": {"type": "string"}, "x": {"type": "integer"},
         "y": {"type": "integer"}, "amount": {"type": "integer"}},
        ["op"]),
    _fn("ask_moa", "Mixture of Agents: send the question to several "
        "DIFFERENT model providers and merge their answers into one "
        "reliable response. Use for high-stakes or contested questions.",
        {"prompt": {"type": "string"}}, ["prompt"]),
    _fn("checkpoint", "Snapshot or restore the workspace: create "
        "before risky edits, rollback if something breaks, list to "
        "review.",
        {"op": {"type": "string",
                "enum": ["create", "list", "rollback", "drop"]},
         "label": {"type": "string"},
         "selector": {"type": "string",
                      "description": "snapshot id/label for "
                                     "rollback/drop"}},
        ["op"]),
    _fn("hyperframe", "Turn written instructions into a web mockup "
        "storyboard (and a video when ffmpeg is available).",
        {"instructions": {"type": "string"}}, ["instructions"]),
    _fn("complex_task", "Start a multi-step mission that runs in the "
        "background with its own planner; use for big goals.",
        {"task": {"type": "string"},
         "steps": {"type": "array",
                   "items": {"type": "string"}}},
        ["task"]),
    _fn("goal_set", "Set a tracked goal with an optional deadline "
        "('finish the report by Friday', 'ship v2 in 3 days'). The goal "
        "persists, links the background tasks you start for it, "
        "auto-completes when they finish, and escalates as the deadline "
        "nears. Use for anything the user wants DONE, not just answered.",
        {"title": {"type": "string",
                   "description": "the objective, deadline phrase "
                                  "included when known"},
         "deadline": {"type": "string",
                      "description": "explicit ISO deadline (optional; "
                                     "parsed from title when omitted)"},
         "priority": {"type": "string",
                      "enum": ["low", "normal", "high"]}},
        ["title"]),
    _fn("goal_list", "List open goals with progress and deadline "
        "countdowns.",
        {}),
    _fn("goal_done", "Mark a goal complete by id fragment or title "
        "keyword.",
        {"goal": {"type": "string"}}, ["goal"]),
    _fn("background_task", "Run a task WITHOUT blocking this turn: "
        "plan it into steps and launch it on the background task "
        "engine, then keep going. The task reports progress to the "
        "dashboard and announces completion. Pass goal_id to link it "
        "to a tracked goal. Use when the user says 'in the "
        "background', 'while you do X', or for anything long-running.",
        {"task": {"type": "string"},
         "steps": {"type": "array",
                   "items": {"type": "string"}},
         "goal_id": {"type": "string",
                     "description": "linked goal id (optional)"}},
        ["task"]),
    _fn("task_status", "Check background tasks: active ones, recent "
        "history, or one task's detail by id.",
        {"task_id": {"type": "string",
                     "description": "specific task id (optional)"},
         "history": {"type": "boolean",
                     "description": "true for recent finished tasks"}},
        []),
    _fn("capability_check", "Check if Jarvis has the capabilities "
        "needed for a task. Returns what's available and what's missing, "
        "with upgrade instructions. Use before attempting tasks that "
        "might need specific tools, APIs, or services that may not be "
        "configured.",
        {"task": {"type": "string",
                  "description": "the task to assess capabilities for"}},
        ["task"]),
    _fn("capability_expand", "Create a plan to acquire missing "
        "capabilities. When capability_check reveals gaps, use this to "
        "generate concrete upgrade steps. Set auto=true to auto-install "
        "pip packages (requires JARVIS_AUTO_UPGRADE=1). Returns a plan "
        "with auto-installable packages and manual setup instructions.",
        {"capabilities": {"type": "array",
                          "items": {"type": "string"},
                          "description": "list of missing capability names"},
         "auto": {"type": "boolean",
                  "description": "auto-install pip packages if possible"},
         "task": {"type": "string",
                  "description": "original task (for tracking)"}},
        ["capabilities"]),
    _fn("automate", "Create a recurring automation in plain words "
        "('every day at 9am brief me', 'every 30 minutes check the "
        "build'). Persists across restarts; fires the action on "
        "schedule and reports the result.",
        {"schedule": {"type": "string",
                      "description": "full schedule phrase including "
                                     "the action"}}, ["schedule"]),
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
        self._stop_requested = threading.Event()
        self._running = False

    def stop(self):
        """Request a cooperative abort of the in-flight agent run."""
        self._stop_requested.set()

    # ---------------- executor bridge ---------------- #

    @property
    def executor(self):
        # Lazy import breaks the brain↔executor cycle; by call time the
        # server module is fully initialised.  Tests may inject a stand-in
        # via ``loop._executor``.
        injected = getattr(self, '_executor', None)
        if injected is not None:
            return injected
        from utils.server import executor
        return executor

    def _exec_tool(self, name, arguments):
        """
        Execute one tool call through the Executor with TTS/UI suppressed.
        MCP tools (``mcp__server__tool``) route to the MCP pool instead.
        Returns the string result fed back to the model.
        """
        try:
            args = json.loads(arguments or '{}')
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError:
            return f"ERROR: arguments were not valid JSON: {arguments[:200]}"

        # ---- RLM reasoning tools (in-loop, zero side effects) -------- #
        if name == 'think':
            # Pure scratchpad: acknowledge and let the model continue.
            # The user never sees tool results, so the thought stays
            # private to the reasoning trace.
            return ("Reasoning noted. Proceed with the next tool call, "
                    "or give your final answer when the goal is met.")

        if name == 'recall_deep':
            try:
                from utils.rlm import get_rlm
                out = get_rlm().recall_deep(str(args.get('query') or ''))
            except Exception as e:
                out = f"ERROR recalling memory: {e}"
            out = str(out or '').strip()
            return out[:TOOL_OUTPUT_CAP] or \
                "No long-term memories matched that query."

        if name == 'memory_about':
            try:
                from utils.rlm import get_rlm
                entity = str(args.get('entity') or '').strip()
                out = (get_rlm().recall_about(entity) if entity
                       else get_rlm().entity_digest())
            except Exception as e:
                out = f"ERROR querying memory: {e}"
            out = str(out or '').strip()
            return out[:TOOL_OUTPUT_CAP] or \
                "Nothing in long-term memory about that yet."

        if name == 'task_plan':
            try:
                from utils.rlm import get_plan_state
                plan = get_plan_state()
                op = str(args.get('op') or 'get').strip().lower()
                if op == 'set':
                    out = plan.set(args.get('goal'),
                                   args.get('steps') or [])
                elif op == 'update':
                    out = plan.update(args.get('step_index'),
                                      str(args.get('status') or ''))
                elif op == 'clear':
                    out = plan.clear()
                else:
                    out = plan.get()
            except Exception as e:
                out = f"ERROR managing plan: {e}"
            return str(out)[:TOOL_OUTPUT_CAP]

        if name == 'delegate':
            task = str(args.get('task') or '').strip()
            persona = str(args.get('persona') or 'researcher').strip()
            if not task:
                return "ERROR: delegate needs a task."
            # Privilege-separated subagents only: never let the model
            # reach the archivist/reflector (RLM memory writers) or the
            # operator (side-effecting tool persona) through this tool.
            # Archivist/reflector writes go through the memory subsystem
            # (consolidate/reflect), and operator fires real actions —
            # both bypass the executor's guardrails if reached here.
            if persona.lower() not in (
                    'researcher', 'coder', 'synthesizer'):
                return (f"ERROR: unknown persona '{persona}'. "
                        "Use researcher, coder or synthesizer.")
            try:
                out = self.brain.complete(
                    task, agent=persona, timeout=90, max_tokens=2000)
            except Exception as e:
                out = f"ERROR delegating to {persona}: {e}"
            out = str(out or '').strip()
            if not out:
                return (f"ERROR: the {persona} subagent returned "
                        "nothing useful.")
            return out[:TOOL_OUTPUT_CAP]

        # ---- Autonomy tools (goals + background tasks, in-loop) --- #
        if name in ('goal_set', 'goal_list', 'goal_done'):
            try:
                from utils.goals import get_goals
                store = get_goals()
                if name == 'goal_set':
                    g = store.set(args.get('title'),
                                  deadline=str(args.get('deadline') or ''),
                                  priority=args.get('priority'))
                    out = (f"Goal [{g['id']}] {g['title']} — "
                           f"{g.get('deadline') or 'no deadline'}."
                           if g else "ERROR: goals are disabled.")
                elif name == 'goal_done':
                    out = self._goal_done(store, str(args.get('goal') or ''))
                else:
                    out = store.render()
            except Exception as e:
                out = f"ERROR managing goals: {e}"
            return str(out)[:TOOL_OUTPUT_CAP]

        if name == 'background_task':
            try:
                out = self._launch_background(
                    str(args.get('task') or ''),
                    args.get('steps') or [],
                    str(args.get('goal_id') or '').strip())
            except Exception as e:
                out = f"ERROR launching background task: {e}"
            return str(out)[:TOOL_OUTPUT_CAP]

        if name == 'task_status':
            try:
                out = self._task_status(
                    str(args.get('task_id') or '').strip(),
                    bool(args.get('history')))
            except Exception as e:
                out = f"ERROR reading tasks: {e}"
            return str(out)[:TOOL_OUTPUT_CAP]

        if name == 'automate':
            try:
                out = self.executor.recurring.add(
                    str(args.get('schedule') or ''))
            except Exception as e:
                out = f"ERROR creating automation: {e}"
            return str(out)[:TOOL_OUTPUT_CAP]

        # ---- Capability awareness ----------------------------------- #
        if name == 'capability_check':
            try:
                from utils.capabilities import assess, format_assessment
                task = str(args.get('task') or '').strip()
                if not task:
                    return "ERROR: capability_check needs a task description."
                result = assess(task)
                out = format_assessment(result)
            except Exception as e:
                out = f"ERROR checking capabilities: {e}"
            return str(out)[:TOOL_OUTPUT_CAP]

        if name == 'capability_expand':
            try:
                from utils.capabilities import (
                    expand, auto_install, format_expansion_plan,
                    reassess as cap_reassess)
                cap_names = args.get('capabilities') or []
                if isinstance(cap_names, str):
                    cap_names = [c.strip() for c in cap_names.split(',')]
                cap_names = [c for c in cap_names if c]
                if not cap_names:
                    return "ERROR: capability_expand needs capability names."
                do_auto = bool(args.get('auto'))
                orig_task = str(args.get('task') or '')
                plan = expand(cap_names, orig_task)
                # Auto-install pip packages if requested and enabled
                install_result = ''
                if do_auto and plan['auto_packages']:
                    ok, output = auto_install(plan['auto_packages'])
                    install_result = (
                        f"\n\nAuto-install: {'OK' if ok else 'FAILED'}\n{output}")
                    if ok:
                        # Re-assess after install
                        reassess_result = cap_reassess(orig_task)
                        still_missing = [c.name for c in reassess_result.missing]
                        if not still_missing:
                            install_result += (
                                "\n\n✅ All capabilities now available! "
                                "Proceed with the original task.")
                        else:
                            install_result += (
                                f"\n\nStill missing: {', '.join(still_missing)}")
                out = format_expansion_plan(plan) + install_result
            except Exception as e:
                out = f"ERROR expanding capabilities: {e}"
            return str(out)[:TOOL_OUTPUT_CAP]

        # ---- MCP external tools ------------------------------------ #
        if name.startswith('mcp__'):
            try:
                from utils.mcp_client import get_pool, MCPServerError
                out = get_pool().call(name, args)
                return str(out or f"{name}: done.")[:TOOL_OUTPUT_CAP]
            except Exception as e:
                return f"ERROR executing {name}: {e}"[:TOOL_OUTPUT_CAP]

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
        mouth = getattr(executor, 'mouth', None)

        def quiet_ui(event, data):
            # Surface activity on the status strip without polluting
            # the transcript (the final answer is emitted once, later).
            if event == 'status':
                logger.info("tool %s: %s", name,
                            data.get('message', ''))

        def _quiet_execute():
            """Run one tool with TTS suppressed for its whole body.

            ``mouth.suppress`` is a process-global boolean checked by
            mouth.speak on every channel, so the read/set/restore must
            happen INSIDE the executor lock: tool calls of one turn run
            concurrently (_run_tool_calls), and toggling the flag
            outside the lock lets two threads tear it — one thread's
            finally can write a stale ``True`` back, leaving Jarvis
            muted for the process lifetime, or a thread can run its
            whole body with ``suppress`` already reset by the other
            thread's finally, so its action text is spoken mid-loop."""
            can_suppress = mouth is not None and hasattr(mouth, 'suppress')
            was = getattr(mouth, 'suppress', False) if can_suppress else False
            if can_suppress:
                mouth.suppress = True
            try:
                return executor.execute_command(
                    command, self.brain, ui_callback=quiet_ui)
            finally:
                if can_suppress:
                    mouth.suppress = was

        try:
            with _EXEC_LOCK:   # side effects never interleave across
                               # concurrent tool executions
                result = _quiet_execute()
        except Exception as e:
            result = f"ERROR executing {name}: {e}"

        out = str(result or '').strip() or f"{name}: done."
        return out[:TOOL_OUTPUT_CAP]

    # ---------------- autonomy helpers (in-loop, zero extra tools) --- #

    @staticmethod
    def _goal_done(store, ref):
        """Complete a goal by id fragment or title keyword."""
        ref = str(ref or '').strip().lower()
        if not ref:
            return "ERROR: goal_done needs a goal id or keyword."
        for g in store.list(include_done=False):
            if ref in g.get('id', '').lower() \
                    or ref in g.get('title', '').lower():
                store.complete(g['id'])
                return f"Goal complete: {g['title']}."
        return f"ERROR: no open goal matches '{ref}'."

    def _launch_background(self, task_text, steps, goal_id=''):
        """Plan + launch a background ComplexTask; link to a goal."""
        from utils.complex_task import normalize_step_specs
        task_text = str(task_text or '').strip()
        if not task_text:
            return "ERROR: background_task needs a task description."
        _mode, specs = normalize_step_specs(steps or [task_text])
        if not specs:
            specs = [{"id": 1, "text": task_text, "depends_on": []}]
        tm = self.executor.task_manager
        tm.brain = self.brain
        task_obj = tm.create_task(
            task_text, [s['text'] for s in specs])
        ok, msg = tm.start_task(task_obj.id)
        if not ok:
            return f"ERROR: {msg}"
        note = ""
        if goal_id:
            try:
                from utils.goals import get_goals
                if get_goals().link_task(goal_id, task_obj.id):
                    note = f" Linked to goal {goal_id}."
                else:
                    note = (f" (goal {goal_id} not found — "
                            f"task runs unlinked).")
            except Exception:
                pass
        return (f"Background task {task_obj.id} started "
                f"({len(specs)} steps).{note} It reports to the "
                f"dashboard and announces completion — carry on.")

    def _task_status(self, task_id='', history=False):
        """Active tasks, recent history, or one task's detail."""
        tm = self.executor.task_manager
        if task_id:
            t = tm.get_task(task_id)
            if t is None:
                # id fragments: match by prefix for model convenience
                for cand in tm.get_recent_tasks(limit=30):
                    if cand.id.startswith(task_id):
                        t = cand
                        break
            if t is None:
                return f"ERROR: no task '{task_id}'."
            _sv = t.status.value if hasattr(t.status, 'value') else t.status
            lines = [f"Task {t.id}: {t.description}",
                     f"status: {_sv} ({t.progress_pct()}%)"]
            for s in t.steps:
                st = s.status.value if hasattr(s.status, 'value') \
                    else s.status
                lines.append(f"  - [{st}] {s.text[:80]}")
            if t.final_summary:
                lines.append(f"summary: {t.final_summary[:300]}")
            return "\n".join(lines)
        if history:
            hist = tm.get_history(limit=8)
            if not hist:
                return "No finished tasks yet."
            return "Recent tasks:\n" + "\n".join(
                f"- {t.id}: {t.description[:60]} "
                f"({t.status.value if hasattr(t.status, 'value') else t.status})"
                for t in hist)
        active = tm.get_active_tasks()
        if not active:
            return "No active background tasks."
        return "Active tasks:\n" + "\n".join(
            f"- {t.id}: {t.description[:60]} ({t.progress_pct()}%)"
            for t in active)

    def _emit(self, ui_callback, event, data):
        if ui_callback:
            try:
                ui_callback(event, data)
            except Exception:
                pass

    def _run_tool_calls(self, calls):
        """
        Execute one turn's tool calls, concurrently when enabled
        (JARVIS_AGENT_PARALLEL=0 forces sequential).  Results come
        back indexed by call order — message order is preserved even
        though execution overlaps.  Executor-bound side effects stay
        serialized via _EXEC_LOCK, so parallelism only overlaps the
        genuinely independent work (web/MCP/LLM/vector lookups).
        """
        if len(calls) > 1 and parallel_enabled():
            from concurrent.futures import ThreadPoolExecutor
            results = [None] * len(calls)
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {
                    pool.submit(self._exec_tool, tc.name, tc.arguments): i
                    for i, tc in calls}
                for future, i in futures.items():
                    try:
                        results[i] = future.result()
                    except Exception as e:
                        results[i] = f"ERROR executing tool: {e}"
            return results
        return [self._exec_tool(tc.name, tc.arguments)
                for _i, tc in calls]

    # ---------------- one model turn ---------------- #

    def _turn(self, messages, ui_callback, stream, tools=None):
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

        # JARVIS_AGENT_MODEL pins the agent brain, e.g.
        # "groq:openai/gpt-oss-120b" — strongest model for multi-step
        # reasoning while chat stays on cheap defaults.  Ignored when
        # the pin matches nothing in the current fleet (full failover).
        preferred = None
        pin = os.getenv('JARVIS_AGENT_MODEL', '').strip()
        if pin:
            try:
                if router._chain(models=[pin]):
                    preferred = [pin]
            except Exception:
                preferred = None

        result = router.chat(messages, require={'tools'},
                             tools=tools or list(TOOL_SPECS),
                             models=preferred, stream=stream)

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
        # Weak models may still wrap replies in legacy action-JSON;
        # normalize BEFORE streaming so the UI never sees raw JSON.
        stripped = text.strip()
        if stripped.startswith('{') and '"action"' in stripped[:60]:
            try:
                wrapped = json.loads(stripped)
                if isinstance(wrapped, dict) \
                        and wrapped.get('action') == 'chat':
                    inner = str(wrapped.get('response', '')).strip()
                    if inner:
                        buffered = [inner]
                        text = inner
            except json.JSONDecodeError:
                pass
        # Final answer — replay the buffer as a live stream burst.
        for piece in buffered:
            self._emit(ui_callback, 'ai_text_stream', {'delta': piece})
        self._emit(ui_callback, 'ai_text_stream_end', {})
        return {'text': text, 'tool_calls': [],
                'finish': finish or 'stop', 'usage': None}

    # ---------------- public entry ---------------- #

    def emit_step(self, ui_callback, step, status, name='', args=None):
        """Push one tool-call frame to any socket-connected inspector."""
        self._emit(ui_callback, 'agent_step', {
            'step': step, 'status': status,
            'name': name, 'args': args or '',
        })

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

        # Fleet tools = curated core + any live MCP servers' tools.
        fleet_tools = list(TOOL_SPECS)
        try:
            from utils.mcp_client import get_pool
            mcp_specs = get_pool().agent_tool_specs()
            if mcp_specs:
                fleet_tools.extend(mcp_specs)
                logger.info("MCP adds %d external tool(s)",
                            len(mcp_specs))
        except Exception as e:
            logger.warning("MCP tool discovery skipped: %s", e)
        # Prompt-budget guard: cap the fleet but NEVER drop curated
        # core tools (the [:40] cap predates the 41-tool spec list and
        # silently dropped the any_action escape hatch).  Trim MCP
        # extras first; any_action stays last so the loop keeps its
        # fallback path.
        try:
            _max_fleet = max(41, int(os.getenv('JARVIS_AGENT_MAX_TOOLS',
                                               '64')))
        except ValueError:
            _max_fleet = 64
        if len(fleet_tools) > _max_fleet:
            _any = [s for s in fleet_tools
                    if s.get('function', {}).get('name') == 'any_action']
            _rest = [s for s in fleet_tools
                     if s.get('function', {}).get('name') != 'any_action']
            fleet_tools = _rest[:_max_fleet - 1] + _any[:1]

        # Override the legacy "output ONLY action JSON" law: in agent
        # mode the model talks to tools natively and answers like Jarvis.
        # The old law sits in the FIRST sentence of the persona, where
        # weak models weight it heaviest — neutralize it there too.
        sys_text = system_instruction or "You are Jarvis."
        sys_text = sys_text.replace(
            "Output ONLY valid parsed JSON - never markdown blocks, "
            "never 'Action:' format.",
            "You act through native TOOL CALLS. Never print "
            "'Action:' lines or raw action JSON.", 1)
        agent_directive = (
            "\n\nAGENT MODE ACTIVE — you have REAL TOOLS attached. "
            "REASON FIRST: for anything multi-step, uncertain or "
            "complex, call the `think` tool to lay out your approach "
            "before acting. Call tools whenever they help fulfil the "
            "request; chain multiple calls if needed. Use "
            "`recall_deep` for the user's long-term history and "
            "context beyond this conversation. Use `task_plan` to "
            "set, update and track progress on multi-step plans. "
            "ACT AUTONOMOUSLY: when the user states a GOAL ('finish X "
            "by Friday', 'get Y done'), call `goal_set` to track it, "
            "then DO the work — use `background_task` (linked via "
            "goal_id) for anything long-running instead of narrating "
            "a plan and stopping. For 'remind me every …' or 'every "
            "day at …' requests call `automate`, never a one-shot "
            "reminder. Use `task_status` to check on background work "
            "before claiming progress. When a tool errors, "
            "do not repeat it unchanged — change strategy. When no "
            "tool is required (conversation, opinion, explanation), "
            "simply answer directly. Address the user as 'Sir'. "
            "NEVER reply with 'Action: <name>' text — instead CALL "
            "the matching tool; for anything unlisted use the "
            "any_action tool.")
        sys_text += agent_directive
        # Open goals ride along so "continue", "how's it going" and
        # deadline questions resolve without a recall round-trip.
        try:
            from utils.goals import get_goals, enabled as _goals_on
            if _goals_on():
                _goals_block = get_goals().render()
                if _goals_block and 'No open goals' not in _goals_block:
                    sys_text += f"\n\n{_goals_block}\nWork these goals " \
                        "forward when relevant; mark done via goal_done."
        except Exception as e:
            logger.debug("goals injection skipped: %s", e)

        # Reasoning-effort control (Hermes-style depth): explicit
        # "think harder" cues — or JARVIS_REASON_EFFORT=deep — deepen
        # the whole run: heavier think directive, +4 step budget and
        # mandatory answer verification.
        effort = 'normal'
        try:
            from utils.rlm.reasoner import detect_effort
            effort = detect_effort(prompt)
        except Exception:
            pass
        budget = self.max_steps + (4 if effort == 'deep' else 0)
        if effort == 'deep':
            self._emit(ui_callback, 'status',
                       {'message': 'reasoning → deep mode'})
            sys_text += (
                "\n\nDEEP REASONING MODE — the user asked for careful "
                "thought. Use the `think` tool before EACH significant "
                "action and again before answering. Consider "
                "alternatives and edge cases; verify facts with tools "
                "rather than assuming; prefer measured accuracy over "
                "speed.")

        # RLM reasoning layer: complex prompts start from a planner
        # draft (JARVIS_AGENT_PLAN=0 disables).  The plan is advice,
        # not law — the model revises it as tool results arrive.
        if plan_enabled():
            try:
                from utils.rlm.reasoner import should_plan, make_plan
                if should_plan(prompt):
                    plan_text = make_plan(self.brain, prompt)
                    if plan_text:
                        sys_text += (
                            "\n\nWORKING PLAN (drafted by your planner — "
                            "follow it, revising as you learn more from "
                            f"tool results):\n{plan_text}")
                        self._emit(ui_callback, 'status', {
                            'message': 'planner → working plan ready'})
            except Exception as e:
                logger.debug("plan injection skipped: %s", e)

        # Persistent task plan: surface the agent's own open plan so
        # multi-session tasks continue where they left off.
        try:
            from utils.rlm import get_plan_state
            plan_render = get_plan_state().render()
            if plan_render:
                sys_text += (f"\n\n{plan_render}\nMark step statuses "
                             "with the task_plan tool as you progress.")
        except Exception as e:
            logger.debug("plan state injection skipped: %s", e)

        messages = [{"role": "system", "content": sys_text}]
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
        tool_calls_made = 0     # real tool executions this run
        error_streak = 0        # consecutive failing tool results
        failure_log = []        # [(tool_name, error_text)]
        workflow_steps = []     # [(tool_name, args)] — skill-forge telemetry
        nudged = False          # critic correction already injected
        self._stop_requested.clear()
        self._running = True
        try:
            while steps < budget:
                if self._stop_requested.is_set():
                    self._emit(ui_callback, 'status',
                               {'message': 'STOP requested — aborting run'})
                    self.emit_step(ui_callback, steps, 'stopped')
                    break
                steps += 1
                try:
                    turn = self._turn(
                        messages, ui_callback,
                        stream=stream_enabled and steps <= budget - 1,
                        tools=fleet_tools)
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
                    # …then each result as a tool message.  Independent
                    # calls of one turn may run concurrently (order of the
                    # RESULT messages is always the model's call order).
                    calls = list(enumerate(turn['tool_calls']))
                    for _i, tc in calls:
                        self.emit_step(ui_callback, steps, 'call',
                                       tc.name, tc.arguments)
                    outputs = self._run_tool_calls(calls)
                    for i, tc in calls:
                        out = outputs[i]
                        tool_calls_made += 1
                        workflow_steps.append((tc.name, tc.arguments))
                        failed = str(out).startswith('ERROR')
                        self.emit_step(ui_callback, steps,
                                       'fail' if failed else 'ok',
                                       tc.name)
                        if failed:
                            error_streak += 1
                            failure_log.append((tc.name, str(out)[:200]))
                        else:
                            error_streak = 0
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id or f"call_{steps}_{i}",
                            "content": out,
                        })

                # RLM failure recovery: after repeated tool errors the
                # critic persona proposes a strategy correction, injected
                # as a high-visibility note for the next turn.
                if error_streak >= 2 and not nudged:
                    nudged = True
                    correction = None
                    try:
                        from utils.rlm.reasoner import critique_failures
                        correction = critique_failures(
                            self.brain, prompt, failure_log[-3:])
                    except Exception as e:
                        logger.debug("critique skipped: %s", e)
                    if correction:
                        logger.info("agent loop critic correction: %s",
                                    correction[:120])
                        self._emit(ui_callback, 'status', {
                            'message': 'critic → strategy correction'})
                        messages.append({
                            "role": "user",
                            "content":
                                "[STRATEGY CORRECTION from your critic "
                                f"subagent] {correction}\n"
                                "Continue the task with the corrected "
                                "approach — do not repeat the failed "
                                "call unchanged.",
                        })
                    continue

                # Tool-using turns loop back for the next model turn —
                # only a tool-free turn can be a final answer below.
                if turn['tool_calls']:
                    continue

                # No tools → this is the final answer.  Weak models may
                # still wrap their reply in legacy action-JSON; unwrap it.
                final = (turn['text'] or '').strip()
                if final.startswith('{') and '"action"' in final[:60]:
                    try:
                        wrapped = json.loads(final)
                        if isinstance(wrapped, dict) \
                                and wrapped.get('action') == 'chat':
                            inner = str(wrapped.get('response', '')).strip()
                            if inner:
                                final = inner
                    except json.JSONDecodeError:
                        pass
                if final:
                    # Hermes-style self-check: after real tool work (or in
                    # deep mode), the critic confirms the answer actually
                    # satisfies the request — one bounded pass, and an
                    # approved/unparsable verdict never delays the reply.
                    try:
                        from utils.rlm.reasoner import verify_enabled, \
                            verify_answer
                        needs_check = (tool_calls_made >= 2
                                       or (effort == 'deep'
                                           and tool_calls_made >= 1))
                        if verify_enabled() and needs_check:
                            ok, revised = verify_answer(self.brain, prompt,
                                                        final)
                            if not ok and revised:
                                logger.info("critic revised final answer")
                                self._emit(ui_callback, 'status', {
                                    'message': 'critic → answer revised'})
                                final = revised
                    except Exception as e:
                        logger.debug("verify pass skipped: %s", e)
                    self.brain._append_history(prompt, final)
                    logger.info("agent loop done in %d step(s), %.1fs",
                                steps, time.time() - t0)
                    # Skill-forge telemetry: successful complex runs become
                    # candidates for autonomous skill creation (background,
                    # bounded — never blocks the reply).
                    if tool_calls_made >= 3:
                        try:
                            from utils.skill_forge import get_forge
                            get_forge().record_workflow(
                                prompt, workflow_steps, success=True)
                        except Exception as e:
                            logger.debug("forge telemetry skipped: %s", e)
                    return {"action": "chat", "response": final}
                break   # empty turn — fall through to legacy handling
        finally:
            self._running = False

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
