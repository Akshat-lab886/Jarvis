import os
import json
import re
import base64
import time
import threading
from google import genai
from google.genai import types
from openai import OpenAI
from config import Config


# Largest exponent the R3 local-arithmetic fast path will evaluate.
# 2**1000 is a ~302-digit integer that computes instantly; anything
# above invites bigint blow-ups (see the Pow guard in _math_answer).
_MAX_MATH_EXPONENT = 1000


def _int_literal(node):
    """Fold an AST node to an int if it is a plain (optionally unary
    +/−) integer literal; return None for anything else — so nested
    powers or computed exponents are never evaluated."""
    import ast
    if isinstance(node, ast.Constant) and isinstance(node.value, int) \
            and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and \
            isinstance(node.op, (ast.UAdd, ast.USub)) and \
            isinstance(node.operand, ast.Constant) and \
            isinstance(node.operand.value, int):
        v = node.operand.value
        return -v if isinstance(node.op, ast.USub) else v
    return None

class Brain:
    def __init__(self):
        print("Brain initialized")
        if Config.GROQ_API_KEY:
            self.client = OpenAI(
                base_url="https://api.groq.com/openai/v1",
                api_key=Config.GROQ_API_KEY,
            )
            self.clients = [self.client]
            print("Brain initialized with Groq API key.")
        else:
            # Not fatal: the BYOK router fleet below may still serve via
            # another cloud key or a local Ollama/LM Studio server (no key
            # needed).  _llm_ready covers the union of all paths.
            print("Brain: no Groq key — trying the BYOK router fleet.")
            self.clients = []
            self.client = None
        
        # Use models from Config
        self.models = Config.MODELS or [
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "groq/compound-mini",
        ]
        # R2: provider cooldowns {name: available_again_ts}
        self._provider_cooldowns = {}

        self.history = [] # Chat history (persisted to disk)
        self.history_lock = threading.Lock()
        self.MAX_HISTORY = 20
        # Pre-flight context ceiling (~tokens). Prevents provider 413 /
        # TPM rejections from poisoning the whole failover chain.
        try:
            self.MAX_PROMPT_TOKENS = int(os.getenv('JARVIS_MAX_PROMPT_TOKENS', '6000'))
        except ValueError:
            self.MAX_PROMPT_TOKENS = 6000
        # Rolling digest state MUST exist before _load_history(), which
        # reconciles the digest marker against the loaded window.
        self.digest_text = ""
        self.digest_upto = 0          # exchanges already folded into digest
        self.total_exchanges = 0      # ever-count (incl. loaded from disk)
        self._evicted_buffer = []     # exchanges popped from window, awaiting digest
        self._digest_lock = threading.Lock()
        self.digest_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'brain', 'data', 'conversation_digest.json'
        )
        self._load_digest()
        self._load_history()
        self.short_term_memory = None
        self.memory_timestamp = 0
        self.MEMORY_TTL = 1800 # 30 minutes
        
        # Cached instances — avoid reloading JSON files on every think() call
        self._episodic = None
        self._relationships = None
        self._memory_cache = None
        self._memory_cache_ts = 0
        self._skills_cache = None
        self._skills_cache_ts = 0
        self._skills_registry = None
        self._tools_cache = None
        self._tools_cache_ts = 0
        self._lessons_cache = None
        self._lessons_cache_ts = 0
        # Auto-RAG: knowledge-vault hook (wired by server at startup)
        self._vault_query = None
        
        self.system_instruction = """You are Jarvis (J.A.R.V.I.S.), an AI assistant that controls this computer and sees the screen. Address the user as 'Sir'. Output ONLY valid parsed JSON - never markdown blocks, never 'Action:' format.

RESPOND with one JSON object from this catalog:

CHAT & KNOWLEDGE
{"action":"chat","response":"..."} - default for conversation, timeless knowledge, how-tos, math, coding, creative writing.
{"action":"search_web","query":"..."} - ONLY for real-time info: today's news, prices, weather, sports, current officeholders, recent events.
{"action":"consult_archive","query":"..."} - questions about the user's stored documents/notes/PDFs ("what does the report say?").
{"action":"recall","query":"..."} / {"action":"remember","text":"...","category":"fact|preference|health|goal|context|routine"} - episodic memory recall/store. {"action":"forget_memory","text":"..."} {"action":"memory_stats"}
{"action":"get_memory","key":"k"} / {"action":"save_memory","key":"k","value":"v"} - personal facts (my name/age/favorite color).

SYSTEM & FILES
{"action":"system_info"} {"action":"system_processes"} {"action":"kill_process","name":"chrome"} {"action":"get_battery"}
{"action":"open_app","target":"Name"} {"action":"open_web","target":"url"} {"action":"play_youtube","target":"song"}
{"action":"download_file","query":"terms","file_type":"pdf"} {"action":"read_webpage","url":"..."}
{"action":"delete_file","target":"name"} {"action":"delete_screenshot","count":1} {"action":"clean_downloads"}
{"action":"set_volume","value":50} {"action":"set_mode","value":"work|chill"} {"action":"media_play_pause"}
{"action":"capture_photo"} {"action":"analyze_photo","prompt":"question"}

TIME & PRODUCTIVITY
{"action":"check_calendar"} {"action":"add_event","text":"full event text"}
{"action":"set_reminder","text":"keep time phrase"} {"action":"list_reminders"} {"action":"cancel_reminder","text":"keyword"}
{"action":"todo_add|todo_done|todo_remove","text":"..."} {"action":"todo_list"} {"action":"todo_clear"}
{"action":"note_save","text":"..."} {"action":"note_list"} {"action":"note_remove","text":"keyword"}
{"action":"schedule_automation","text":"'every day at 9am <action>'"} {"action":"list_automations"} {"action":"cancel_automation","keyword":"kw|id"}
One-off alerts=set_reminder; repeating EXECUTED actions=schedule_automation.

COMMUNICATION
{"action":"send_email","recipient":"addr","message":"body"} - sending only; ask for address if unknown.
{"action":"check_email"} - inbox check. {"action":"read_email","query":"from:X or subject:Y"} - reading specific mail. Never confuse send vs read.
{"action":"morning_briefing"} {"action":"news_headlines"} {"action":"daily_summary"} {"action":"triage"} {"action":"schedule_insights"}

DEVELOPER MODE
{"action":"dev_write","project":"P","file":"f","code":"raw code"} - omit project to use active focus.
{"action":"dev_command","project":"P","command":"shell cmd"} {"action":"dev_create","project":"P","type":"python|web"}
{"action":"dev_open","project":"P"} {"action":"dev_set_context","project":"P|null"}
{"action":"architect_app","idea":"..."} {"action":"auto_build_app","idea":"..."}
{"action":"dev_build_full","project_name":"N","files":[{"filename":"f","content":"code"}]} - COMPLETE code, no placeholders.
{"action":"mobile_code","project":"P","steps":[{"file":"path","content":"FULL file content"}]}
{"action":"write_code","description":"task"} - single focused script: generated, executed, auto-fixed.

SKILLS & TOOLS
{"action":"run_skill","name":"n","params":{}} - prefer when a listed skill fits. {"action":"save_skill","name":"snake_name","description":"...","code":"py with {{param}} slots","params":{"p":"desc"}} {"action":"list_skills"} {"action":"delete_skill","name":"n"}
{"action":"read_skill","name":"n"} - load FULL instructions of a [md] skill before following it. {"action":"forge_skills"} - distill new skills from recent successful workflows.
{"action":"call_tool","name":"n","args":{}} - for registered external tools. {"action":"list_tools"}

RUNTIMES & RECOVERY
{"action":"sandbox_run","code":"py" | "command":"sh","backend":"auto|local|docker|ssh|daytona|singularity|vercel|modal","timeout":N}
{"action":"python_rpc","code":"py","reset":false} - persistent Python session: variables SURVIVE between calls; reset:true clears it.
{"action":"checkpoint","op":"create|list|rollback|drop","label":"l","paths":["f"]} - snapshot/restore the workspace before+after risky edits.
{"action":"diagnose_file","path":"f"} - syntax/semantic check of a file after editing it.
{"action":"computer_use","op":"click|click_element|set_field|type|key|scroll|screenshot|activate","app":"a","element":"e","value":"v","x":0,"y":0,"key":"k","amount":N} - background desktop control.
{"action":"guardrails_update","target":"user|memory|project","section":"Rules","line":"durable rule or fact"} - persist standing instructions to USER.md/MEMORY.md/.jarvis.md.

TASKS (MULTI-STEP)
{"action":"complex_task","task":"goal","steps":[...]}
Sequential: ["step","step"]. Parallel DAG: [{"id":1,"text":"...","depends_on":[]},{"id":2,...},{"id":3,"depends_on":[1,2]}] - independent steps run concurrently. 2-5 concrete self-contained steps with specifics; bad: ["research","finish"].
{"action":"desktop_task","task":"goal"} - visual computer control (click/type via screen).
{"action":"help"} {"action":"clear_history"}

GOALS (TRACKED OBJECTIVES — prefer over bare complex_task for "do X by <date>")
{"action":"goal_set","title":"finish the report by Friday","priority":"normal|high|low"} - deadline auto-parsed from title; explicit "deadline" ISO optional.
{"action":"goal_list"} - open goals with progress + countdowns. {"action":"goal_done","goal":"id-fragment-or-keyword"} {"action":"goal_drop","goal":"..."}
{"action":"background_task","task":"goal","steps":[...],"goal_id":"<id>"} - plan+launch on the background engine NOW and return immediately; links to the goal. Use for long work instead of stopping after a plan.
{"action":"task_status","task_id":"<id>"} - one task's detail. {"action":"task_status","history":true} - recent finished. Bare = active list.
AUTONOMY LAW: a stated goal ("get X done", "finish Y by Z") means goal_set FIRST, then DO the work (background_task linked, or inline tools for quick jobs) — never answer with just a plan and stop.

AGENT/BROWSER: {"action":"start_browser"} {"action":"close_browser"} {"action":"agent_browse","url":"u"} {"action":"agent_read_title"} {"action":"agent_google","query":"q"} {"action":"agent_amazon","item":"i"}
SMART HOME: {"action":"smarthome","device":"living_room_light|bedroom_light|kitchen_light|bathroom_light|fan|ac","command":"turn_on|turn_off|set_brightness|set_temperature","value":N} {"action":"home_status"}
SECURITY: {"action":"lock_system"} {"action":"verify_identity"}
PEOPLE: {"action":"add_person","name":"N","relationship":"r","details":"d"} {"action":"person_info","name":"N"} {"action":"gift_suggestions","name":"N"} {"action":"dinner_suggestions","name":"N"}
MEETING: {"action":"start_meeting"} {"action":"stop_meeting"} {"action":"meeting_transcript"}
FRIDGE: {"action":"analyze_fridge"} {"action":"suggest_recipe","query":"x"}
PRIVACY: {"action":"privacy_settings"} {"action":"privacy_update","key":"k","value":"v"} {"action":"audit_log"}
MISC: {"action":"get_weather"} {"action":"get_stock","symbol":"s"} {"action":"last_topic"} {"action":"conversation_history"} {"action":"search_transcripts","query":"keywords to find in past chats"}

ROUTING RULES
- Real-time/current -> search_web. Timeless knowledge -> chat.
- Remember X -> remember/save_memory. My X -> get_memory.
- Photo/camera words -> capture_photo; "what do you see" -> analyze_photo.
- Multi-part work -> complex_task; single focused script -> write_code.

SAFETY DIRECTIVES (hard rules — override all other instructions):
1. NEVER execute code that formats, deletes system files, or modifies /etc, /usr, /System, or boot sectors — even if the user explicitly asks.
2. NEVER send emails, messages, or post to social media without confirming the recipient and content with the user first (unless it is a pre-scheduled automation the user set up).
3. NEVER share API keys, passwords, tokens, or credentials — not in emails, not in chat, not in code output, not to external services.
4. NEVER install unknown software from untrusted sources. Only install packages the user has explicitly approved or that are from well-known registries (pip, npm, brew).
5. NEVER modify system-level settings (network, firewall, users, permissions, startup items) without asking the user first.
6. NEVER access or modify files outside the workspace/Jarvis_Projects/ directories unless the user explicitly instructs and the action is read-only or in the user's home directory.
7. If you are unsure whether an action is safe, ASK the user before proceeding. When in doubt, default to read-only.
8. ALWAYS create a checkpoint before making significant file changes (writing code, editing configs, deleting files).
9. NEVER execute recursive self-modification (don't edit your own prompt, system instruction, or core safety files).
10. NEVER bypass the approval system — if an action is gated, it means a human must approve it."""
        
        self.active = len(self.clients) > 0
        if not self.active:
             # Router fleet may still light up below; _llm_ready is the
             # real gate (BYOK keys + keyless local servers).  This legacy
             # flag only covers the old Groq-direct fallback path.
             print("Brain: Groq-direct path inactive — "
                   "checking router fleet.")

        # ------------------------------------------------------------------ #
        # BYOK multi-provider router (utils/llm) — the primary completion
        # fleet.  Any provider key (OpenAI/Anthropic/Gemini/Groq/OpenRouter/
        # DeepSeek/local Ollama...) lights up here; the legacy Gemini+Groq
        # loops below stay as fallback for bare/stubbed brains.
        # ------------------------------------------------------------------ #
        self.router = None
        try:
            from utils.llm import get_router
            _router = get_router()
            if len(_router.providers) > 0:
                self.router = _router
                print(f"Brain: multi-provider router active "
                      f"({', '.join(sorted(_router.providers))})")
        except Exception as e:
            print(f"Brain: router init skipped: {e}")
        # Truthful readiness: the legacy Groq-direct flag above only covers
        # one path — a lit router fleet (any cloud key or local server)
        # also means the brain can serve.  /health and any other `active`
        # readers must agree with _llm_ready, otherwise a healthy
        # local-only/Anthropic-only user reports brain_active=False.
        if not self.active and self.router is not None:
            try:
                self.active = len(self.router.providers) > 0
            except Exception:
                pass

    @property
    def _llm_ready(self):
        """True when ANY completion path can serve a request."""
        if self.active:
            return True
        router = getattr(self, 'router', None)
        return bool(router and len(router.providers))

    # ------------------------------------------------------------------ #
    # Phase 2 agent core: native tool-calling loop
    # ------------------------------------------------------------------ #
    def _agent_mode_ok(self):
        """
        Agent loop eligibility.  JARVIS_AGENT_MODE:
          'auto' (default) → on when the fleet offers tool-capable models
          'on'             → force (falls back to legacy when impossible)
          'off'            → pure legacy routing
        """
        from utils.agent_loop import agent_tools_enabled
        if not agent_tools_enabled():
            return False
        router = getattr(self, 'router', None)
        if router is None or len(router.providers) == 0:
            return False
        try:
            return bool(router._chain(require={'tools'}))
        except Exception:
            return False

    def inject_knowledge(self, text):
        """Inject knowledge into short-term memory with a timestamp."""
        self.short_term_memory = text
        self.memory_timestamp = time.time()
        print(f"Brain: Injected {len(text)} chars into short-term memory.")

    @property
    def history_file(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'brain', 'data', 'conversation_history.json'
        )

    def _backup_corrupt(self, path, exc):
        """Rename a corrupt JSON file aside so data isn't silently lost."""
        try:
            import time as _t
            backup = f"{path}.corrupt.{int(_t.time())}"
            os.replace(path, backup)
            from utils.logger import logger
            logger.warning("Backed up corrupt file %s -> %s (%s)",
                           path, backup, exc)
            return backup
        except OSError as e2:
            from utils.logger import logger
            logger.warning("Could not back up corrupt %s: %s", path, e2)
            return None

    def _load_history(self):
        """Load persistent conversation history from disk (last N exchanges)."""
        try:
            if os.path.exists(self.history_file):
                try:
                    with open(self.history_file, 'r') as f:
                        data = json.load(f)
                except (json.JSONDecodeError, ValueError) as je:
                    # Corrupt file — back it up instead of silently resetting
                    self._backup_corrupt(self.history_file, je)
                    data = []
                for entry in data[-self.MAX_HISTORY:]:
                    self.history.append((entry.get('user', ''), entry.get('assistant', '')))
                print(f"Brain: Loaded {len(self.history)} conversation exchanges from disk.")
                self.total_exchanges = len(self.history)
                # Keep digest marker consistent with what we actually have
                if self.digest_upto > self.total_exchanges:
                    self.digest_upto = 0
        except Exception as e:
            print(f"Brain: Failed to load history: {e}")

    def _save_history(self):
        """Persist conversation history to disk (atomic tmp+replace)."""
        try:
            with self.history_lock:
                data = [{"user": u, "assistant": a} for u, a in self.history]
            tmp = self.history_file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.history_file)
        except Exception as e:
            print(f"Brain: Failed to save history: {e}")

    def _load_digest(self):
        """Load the rolling digest + any evicted-but-undigested exchanges."""
        try:
            if os.path.exists(self.digest_file):
                try:
                    with open(self.digest_file, 'r') as f:
                        data = json.load(f)
                except (json.JSONDecodeError, ValueError) as je:
                    # Corrupt file — back it up instead of silently resetting
                    self._backup_corrupt(self.digest_file, je)
                    data = {}
                self.digest_text = data.get('digest', '') or ''
                self.digest_upto = int(data.get('upto', 0) or 0)
                pending = data.get('pending') or []
                self._evicted_buffer = [
                    (str(p[0]), str(p[1])) for p in pending
                    if isinstance(p, (list, tuple)) and len(p) == 2
                ]
        except Exception as e:
            print(f"Brain: Failed to load digest: {e}")

    def _save_digest(self):
        try:
            os.makedirs(os.path.dirname(self.digest_file), exist_ok=True)
            tmp = self.digest_file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump({'digest': self.digest_text,
                           'upto': self.digest_upto,
                           'pending': [list(p) for p in self._evicted_buffer]},
                          f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.digest_file)
        except Exception as e:
            print(f"Brain: Failed to save digest: {e}")

    COMPRESS_EVERY = 8   # fold once this many exchanges slid out of window

    def set_knowledge_vault(self, query_fn):
        """
        Wire auto-RAG: ``query_fn(text, n_results) -> str`` (typically
        Librarian.query_vault).  Called per think() to inject relevant
        document excerpts.
        """
        self._vault_query = query_fn

    _VAULT_TIMEOUT = 12   # native vector libs can hang; never stall chat

    def _vault_block(self, prompt):
        """
        Retrieve knowledge-vault excerpts for *prompt* under a watchdog.
        Returns a formatted block or '' (never raises, never hangs).
        """
        if not self._vault_query:
            return ""
        outcome = {}

        def _fetch():
            try:
                outcome['text'] = self._vault_query(prompt, 3)
            except Exception as e:
                outcome['error'] = str(e)

        t = threading.Thread(target=_fetch, daemon=True,
                             name="vault-rag")
        t.start()
        t.join(timeout=self._VAULT_TIMEOUT)
        if t.is_alive():
            return ""
        text = outcome.get('text') or ''
        text = text.strip()
        if len(text) < 40:
            return ""
        return ("RELEVANT KNOWLEDGE VAULT EXCERPTS (from your stored "
                f"documents):\n{text[:1600]}")

    def _rlm_block(self, prompt):
        """
        Recursive-memory recall for *prompt* under a watchdog: world
        model, long-term themes, session summaries and relevant events
        assembled across the RLM hierarchy.  '' when empty or slow.
        """
        try:
            from utils.rlm import get_rlm
            rlm = get_rlm()
        except Exception as e:
            print(f"Brain: RLM unavailable: {e}")
            return ""
        outcome = {}

        def _fetch():
            try:
                outcome['text'] = rlm.recall_block(prompt)
            except Exception as e:
                outcome['error'] = str(e)

        t = threading.Thread(target=_fetch, daemon=True,
                             name="rlm-recall")
        t.start()
        t.join(timeout=self._VAULT_TIMEOUT)
        if t.is_alive():
            return ""
        return outcome.get('text') or ''

    def _rlm_continuity_block(self):
        """
        Cross-session bridge: where the last session left off (latest
        summary + final exchanges), injected once per session when the
        RLM detects an idle gap.  '' when the session is still warm.
        """
        try:
            from utils.rlm import get_rlm
            rlm = get_rlm()
        except Exception as e:
            print(f"Brain: RLM unavailable: {e}")
            return ""
        # Once per process; re-arm after 6h idle so a long-lived daemon
        # also gets continuity after an overnight gap.
        sent_at = getattr(self, '_continuity_sent_at', 0.0)
        if sent_at and (time.time() - sent_at) < 6 * 3600:
            return ""
        block = rlm.continuity_block()
        if block:
            self._continuity_sent_at = time.time()
        return block

    def _compress_history_if_needed(self):
        """
        Fold evicted exchanges (slid out of MAX_HISTORY) into a rolling
        digest.  Runs in a background thread from think(); on LLM
        failure the buffer is kept intact and retried next round.
        """
        with self._digest_lock:
            if len(self._evicted_buffer) < self.COMPRESS_EVERY:
                return
            batch = list(self._evicted_buffer)
            old_digest = self.digest_text

        transcript = "\n".join(
            f"User: {str(u)[:200]}\nJarvis: {str(a)[:200]}"
            for u, a in batch
        )
        prompt = (
            "Update this rolling summary of an ongoing conversation.\n\n"
            f"CURRENT DIGEST:\n{old_digest or '(empty)'}\n\n"
            f"NEW EXCHANGES TO FOLD IN:\n{transcript[:6000]}\n\n"
            "Produce ONE updated digest (<400 words) that preserves key "
            "facts, decisions, names, numbers, and open threads. "
            "Output ONLY the digest text."
        )
        new_digest = None
        try:
            new_digest = self.complete(prompt, agent='synthesizer',
                                       timeout=45, max_tokens=600)
        except Exception as e:
            print(f"Brain: digest completion failed: {e}")

        if not new_digest:
            return  # keep buffer; retry next trigger

        with self._digest_lock:
            self.digest_text = new_digest.strip()[:4000]
            self.digest_upto += len(batch)
            # Drop only the exchanges that were actually folded.  Items
            # evicted while the LLM call was in flight (appended after
            # the snapshot, still under the lock) must survive to be
            # folded next round — wiping the whole buffer would silently
            # erase them from the digest pipeline (they were already
            # popped out of the history window).
            folded = {id(x) for x in batch}
            self._evicted_buffer = [
                x for x in self._evicted_buffer if id(x) not in folded]
            self._save_digest()
            print(f"Brain: conversation digest updated "
                  f"(covers {self.digest_upto} exchanges total)")

    def clear_digest(self):
        with self._digest_lock:
            self.digest_text = ""
            self.digest_upto = 0
            self._evicted_buffer = []
        self._save_digest()

    def _append_history(self, user_text, ai_text):
        """Thread-safe history append with persistence and size cap.
        Also logs the conversation topic to episodic memory for long-term recall.
        """
        with self.history_lock:
            self.history.append((user_text, ai_text))
            self.total_exchanges += 1
            evicted = None
            if len(self.history) > self.MAX_HISTORY:
                # Evicted exchanges feed the rolling digest (B3)
                evicted = self.history.pop(0)
                with self._digest_lock:
                    self._evicted_buffer.append(evicted)
                    if len(self._evicted_buffer) > 300:
                        self._evicted_buffer.pop(0)
                    # Persist pending immediately so a crash never loses
                    # exchanges that fell out of the window.
                    self._save_digest()
        self._save_history()
        # Layer-2 memory: full-text archive of every exchange
        try:
            from utils.transcript_search import get_archive
            get_archive().index_exchange(user_text, ai_text,
                                         source='chat')
        except Exception:
            pass
        # RLM: observe the exchange into the recursive hierarchy; the
        # memory itself decides (thresholds + cooldowns) whether to
        # spawn background reflection/consolidation.  Never blocks.
        try:
            from utils.rlm import get_rlm
            get_rlm().observe_exchange(user_text, ai_text, self)
        except Exception:
            pass
        # Log conversation topic to episodic memory for recall
        try:
            if self._episodic is None:
                from utils.episodic_memory import EpisodicMemory
                self._episodic = EpisodicMemory()
            # Store a compact summary of what was discussed
            topic = user_text[:120]
            response_preview = (ai_text or '')[:120]
            self._episodic.remember(
                text=f"User asked: {topic} -> Response: {response_preview}",
                category='conversation_topic',
                tags=['conversation', 'auto-logged'],
                source='system',
                importance=4
            )
        except Exception:
            pass

    def clear_history(self):
        """Wipe the conversation history (in-memory and on disk)."""
        with self.history_lock:
            self.history = []
        self._save_history()
        self.clear_digest()
        return "Conversation history cleared."


    def _encode_image(self, image_path):
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')


    # ------------------------------------------------------------------ #
    # R2: provider cooldown — after quota/rate errors, stop hammering
    # a dead provider for COOLDOWN seconds (Gemini free tier: daily).
    # ------------------------------------------------------------------ #
    PROVIDER_COOLDOWN_S = 900

    @staticmethod
    def _is_quota_error(err_text):
        t = str(err_text).lower()
        return ('429' in t or 'resource_exhausted' in t
                or 'quota' in t and 'exceeded' in t)

    def _provider_cooling(self, name):
        until = self._provider_cooldowns.get(name, 0)
        remaining = until - time.time()
        if remaining > 0:
            print(f"Brain: {name} cooling down for another "
                  f"{remaining:.0f}s — skipping")
            return True
        return False

    def _set_provider_cooldown(self, name, seconds=None):
        secs = seconds or self.PROVIDER_COOLDOWN_S
        self._provider_cooldowns[name] = time.time() + secs
        print(f"Brain: {name} placed on cooldown for {secs:.0f}s "
              f"(quota exhausted)")

    def process_with_gemini(self, prompt, image_path_arg, system_instruction):
        if self._provider_cooling('gemini'):
            return None
        try:
             client = genai.Client(api_key=Config.GOOGLE_API_KEY)
             
             contents = [prompt]
             # Handle image path normalization
             image_paths = []
             if image_path_arg:
                 if isinstance(image_path_arg, list): image_paths = image_path_arg
                 else: image_paths = [image_path_arg]

             if image_paths:
                 import PIL.Image
                 for path in image_paths:
                     if path and os.path.exists(path):
                         contents.append(PIL.Image.open(path))
             
             response = client.models.generate_content(
                 model=Config.GEMINI_MODEL,
                 contents=contents,
                 config=types.GenerateContentConfig(
                     system_instruction=system_instruction,
                 ),
             )
             text_response = response.text
             
             # Clean markdown
             if text_response.startswith('```json'): text_response = text_response[7:]
             if text_response.startswith('```'): text_response = text_response[3:]
             if text_response.endswith('```'): text_response = text_response[:-3]
             text_response = text_response.strip()
             
             command = None
             try:
                 command = json.loads(text_response)
             except:
                 # Regex Fallback
                 import re
                 json_blocks = re.findall(r'(\{.*?\})', text_response, re.DOTALL)
                 for block in json_blocks:
                     try:
                         command = json.loads(block.replace("'", '"'))
                         if 'action' in command: break
                     except: pass
                 
                 if not command:
                     # Action: ... Fallback
                     action_match = re.search(r'Action:\s*([a-zA-Z_]+)', text_response, re.IGNORECASE)
                     if action_match:
                         command = {'action': action_match.group(1).lower()}
                         # Key: Value parsing
                         pairs = re.findall(r'([a-zA-Z_]+):\s*(.+)', text_response)
                         for k, v in pairs:
                             k_lower = k.lower().strip()
                             v_clean = v.strip()
                             if k_lower == 'key': command['key'] = v_clean
                             elif k_lower == 'value': 
                                 if v_clean.isdigit(): command['value'] = int(v_clean)
                                 else: command['value'] = v_clean.replace('%', '')
                             elif k_lower in ['query', 'target', 'prompt', 'item', 'device', 'command', 'response', 'recipient', 'message']: command[k_lower] = v_clean
                         
                         if 'key' in command: command['key'] = command['key'].replace('favourite', 'favorite').replace('colour', 'color').replace(' ', '_').lower()
                         if len(command) == 1:
                             remaining = text_response.replace(action_match.group(0), '').strip()
                             if remaining: command['target'] = remaining
            
             # Normalization
             if command and isinstance(command, dict):
                 if 'key' in command: command['key'] = command['key'].replace('favourite', 'favorite').replace('colour', 'color').replace(' ', '_').lower()
            
             if command:
                 action = command.get('action')
                 if action == 'chat': self._append_history(prompt, command.get('response', ''))
                 elif action: self._append_history(prompt, f"Action: {action}")
                 else: self._append_history(prompt, "Action: [Custom JSON]")
                 return command
             else:
                 self._append_history(prompt, text_response)
                 return {"action": "chat", "response": text_response}

        except Exception as e:
            if self._is_quota_error(e):
                cooldown = self.PROVIDER_COOLDOWN_S
                try:
                    m = re.search(r'retry in ([\d.]+)', str(e), re.I)
                    if m:
                        cooldown = max(30, min(float(m.group(1)) + 5, 3600))
                except Exception:
                    pass
                self._set_provider_cooldown('gemini', cooldown)
            else:
                print(f"Gemini Error: {e}")
            return None

    _COMPLETE_SYSTEM = (
        "You are Jarvis's internal task-execution engine. Follow the "
        "instructions precisely. Output ONLY what was requested — no "
        "preamble, no commentary, no markdown unless explicitly asked."
    )

    def complete(self, prompt, system=None, timeout=60,
                 max_tokens=None, temperature=None, agent=None):
        """
        Raw LLM completion for internal subsystems (complex-task steps,
        planning, synthesis, skills).

        Unlike think(), this is a side-effect-free call:
          - does NOT write to conversation history or episodic memory
          - does NOT inject persona/memory context into the prompt
          - does NOT parse action JSON — returns plain text

        Pass ``agent='<profile name>'`` (see utils/agents.py) to route
        through a specialized subagent persona; its system prompt,
        temperature and token budget apply unless explicitly overridden.

        Tries the BYOK router fleet first (every configured provider),
        then falls back to Gemini → every configured model on every
        available Groq client.  Returns stripped text or None.
        """
        if not self._llm_ready:
            return None

        # --- Guardrails: circuit breaker + session budget --------------
        from utils.circuit_breaker import get_breaker
        from utils.budget import get_budget, BudgetExceededError
        try:
            if not get_breaker().allow_llm_spend():
                print("Brain.complete: circuit breaker TRIPPED — "
                      "refusing to spend tokens.")
                return None
            get_budget().guard()
        except BudgetExceededError as e:
            print(f"Brain.complete: {e}")
            return None

        profile = None
        if agent:
            try:
                from utils.agents import get_profile
                profile = get_profile(agent)
                if profile is None:
                    print(f"Brain.complete: unknown agent '{agent}', "
                          f"using default.")
            except Exception as e:
                print(f"Brain.complete: agent lookup failed: {e}")

        if profile is not None:
            system_text = system or profile.system_prompt
            temp = profile.temperature if temperature is None else temperature
            tokens = profile.max_tokens if max_tokens is None else max_tokens
        else:
            system_text = system or self._COMPLETE_SYSTEM
            temp = 0.2 if temperature is None else temperature
            tokens = 2048 if max_tokens is None else max_tokens

        last_error = None

        # --- BYOK router fleet (primary) ---------------------------------
        router = getattr(self, 'router', None)
        if router is not None and len(router.providers) > 0:
            from utils.llm.shim import RouterCompletionClient
            preferred = None
            if profile is not None and getattr(profile, 'model', None):
                preferred = [profile.model]
            try:
                result = RouterCompletionClient(router)._create(
                    model=preferred[0] if preferred else None,
                    messages=[{"role": "system", "content": system_text},
                              {"role": "user", "content": prompt}],
                    max_tokens=tokens, temperature=temp,
                    timeout=timeout)
                text = (getattr(result.choices[0].message, 'content', '')
                        or '').strip()
                if text:
                    return text
                # Empty-but-successful response → let legacy paths try too
            except Exception as e:
                print(f"Brain.complete: router chain failed: {e}")

        # --- Gemini path ---
        if Config.GOOGLE_API_KEY and not self._provider_cooling('gemini'):
            try:
                client = genai.Client(api_key=Config.GOOGLE_API_KEY)
                response = client.models.generate_content(
                    model=Config.GEMINI_MODEL,
                    contents=[prompt],
                    config=types.GenerateContentConfig(
                        system_instruction=system_text,
                        max_output_tokens=tokens,
                        temperature=temp,
                    ),
                )
                text = (getattr(response, 'text', '') or '').strip()
                if text:
                    return text
            except Exception as e:
                last_error = e
                print(f"Brain.complete: Gemini failed: {e}")

        # --- Groq / OpenAI-compatible path ---
        if self.clients:
            messages = [
                {"role": "system", "content": system_text},
                {"role": "user", "content": prompt},
            ]
            # Profile can request a preferred model first
            models_to_try = list(self.models)
            if profile is not None and getattr(profile, 'model', None):
                pref = profile.model
                models_to_try = ([pref] if pref in self.models
                                 else [pref] + models_to_try)
            for model in models_to_try:
                for i, client in enumerate(self.clients):
                    try:
                        completion = client.chat.completions.create(
                            model=model,
                            messages=messages,
                            max_tokens=tokens,
                            temperature=temp,
                            timeout=timeout,
                        )
                        text = (completion.choices[0].message.content
                                or '').strip()
                        if text:
                            get_budget().record(
                                model=model, input_text=prompt,
                                output_text=text,
                                usage=getattr(completion, 'usage', None))
                            return text
                    except BudgetExceededError:
                        raise
                    except Exception as e:
                        last_error = e

        print(f"Brain.complete: all providers failed. Last error: {last_error}")
        return None


    # ------------------------------------------------------------------ #
    # R3: deterministic arithmetic short-circuit.  Pure numeric questions
    # ("15 percent of 240", "7*8+2", "(120-30)/3") never need an LLM and
    # previously risked being routed into async write_code tasks.
    # ------------------------------------------------------------------ #
    _MATH_STRIP = re.compile(
        r'^\s*(?:hey |ok ,? )?(?:jarvis[,:]?\s*)?'
        r'(?:what(?:\'s| is| are)|whats|calculate|compute|solve|how much is)\s*',
        re.IGNORECASE)
    _MATH_TAIL = re.compile(r'[?!\s]+$')

    @staticmethod
    def _math_answer(text):
        """
        Return a spoken-ready answer string for pure-arithmetic prompts,
        else None.  Conservative: numbers/operators/percent-of only.
        """
        t = Brain._MATH_STRIP.sub('', str(text or '').strip())
        t = Brain._MATH_TAIL.sub('', t).strip()
        if not t or len(t) > 80 or '\n' in t:
            return None

        low = t.lower()
        # "N percent of M"  (also %)
        pm = re.fullmatch(
            r'([\d,.]+)\s*(?:%|percent)\s+of\s+([\d,.]+)', low)
        if pm:
            try:
                a = float(pm.group(1).replace(',', ''))
                b = float(pm.group(2).replace(',', ''))
                return f"{t} = {a / 100 * b:g}"
            except ValueError:
                return None

        # plain expression: normalize word operators
        expr = re.sub(
            r'\b(times|x|multiplied by)\b', '*', low)
        expr = re.sub(r'\b(divided by|over)\b', '/', expr)
        expr = re.sub(r'\bplus\b', '+', expr)
        expr = re.sub(r'\bminus\b', '-', expr)
        expr = expr.replace('×', '*').replace('÷', '/').replace('%', '/100')
        expr = re.sub(r'[\s,]', '', expr)
        expr = expr.replace('^', '**')
        if not expr or not re.fullmatch(
                r'[\d.+\-*/()]+(?:\.\d+)?', expr):
            return None
        if not any(op in expr for op in '+-*/'):
            return None
        try:
            import ast as _ast
            tree = _ast.parse(expr, mode='eval')
            allowed = (_ast.Expression, _ast.BinOp, _ast.UnaryOp,
                       _ast.Constant, _ast.Add, _ast.Sub, _ast.Mult,
                       _ast.Div, _ast.USub, _ast.UAdd, _ast.Pow,
                       _ast.FloorDiv, _ast.Mod)
            for n in _ast.walk(tree):
                if not isinstance(n, allowed):
                    return None

            # Exponent guard: a huge or nested ``**`` exponent would make
            # eval materialise a multi-gigabyte bigint before returning —
            # "9**99999999" or the nested "9**9**9" (= 9^387M) freeze the
            # thread / OOM the process on a plain numeric prompt with zero
            # LLM cost.  Only a small *integer-literal* exponent is
            # permitted; anything else (nested power, huge exponent) falls
            # back to the LLM, which is bounded by provider timeouts.
            # NB: ast.walk yields the Pow *operator* node as well as the
            # BinOp — the exponent lives on the BinOp (n.op is the Pow).
            for n in _ast.walk(tree):
                if isinstance(n, _ast.BinOp) and isinstance(n.op, _ast.Pow):
                    exp = _int_literal(n.right)
                    if exp is None or abs(exp) > _MAX_MATH_EXPONENT:
                        return None

            val = eval(compile(tree, '<math>', 'eval'),
                       {'__builtins__': {}}, {})
            out = f"{val:g}" if isinstance(val, float) else str(val)
            return f"{t} = {out}"
        except Exception:
            return None

    def think(self, prompt, image_path=None):
        if not self._llm_ready:
            return {"action": "chat", "response":
                    "I don't have a brain yet — no LLM provider is "
                    "configured. Add any one key (Groq / Gemini / OpenAI / "
                    "Anthropic / DeepSeek / OpenRouter), run a local "
                    "Ollama/LM Studio server, or point CUSTOM_OPENAI_BASE_URL "
                    "at your endpoint, then try again."}

        # Activity heartbeat — wakes idle hibernation if suspended
        try:
            from utils.hibernation import get_manager
            get_manager().touch()
        except Exception:
            pass

        # R3: instant local answers for pure arithmetic
        try:
            quick = self._math_answer(prompt)
            if quick:
                print(f"Brain: local arithmetic → {quick}")
                return {"action": "chat", "response": quick}
        except Exception:
            pass

        # Dynamic context injection — expand @file / @folder / @git /
        # @url markers natively before the prompt reaches any model
        # (Hermes parity).  Idempotent, budget-capped, kill-switchable.
        try:
            from utils.context_injection import expand
            prompt = expand(prompt)
        except Exception:
            pass

        print(f"Thinking about: {prompt} (Image: {image_path})")
        
        # Dynamic System Prompt
        current_system_instruction = self.system_instruction

        # Kick off rolling-digest compression in background if needed (B3)
        try:
            with self._digest_lock:
                needs_compress = len(self._evicted_buffer) >= self.COMPRESS_EVERY
            if needs_compress:
                threading.Thread(
                    target=self._compress_history_if_needed,
                    daemon=True, name="history-digest"
                ).start()
        except Exception:
            pass

        # Inject Short-Term Memory if valid
        short_term_block = ''
        if self.short_term_memory and (time.time() - self.memory_timestamp < self.MEMORY_TTL):
            remaining = int(self.MEMORY_TTL - (time.time() - self.memory_timestamp))
            print(f"Brain: Using short-term memory ({remaining}s remaining)")
            short_term_block = (
                f"\n\nCURRENT SHORT-TERM KNOWLEDGE (Expires in {remaining}s):\n"
                f"{self.short_term_memory}")
            current_system_instruction += short_term_block

        if image_path:
            # Override for Vision Analysis to prevent recursion
            current_system_instruction = """You are a Vision AI. 
            Analyze the attached image and answer the user's question.
            Output JSON: {"action": "chat", "response": "Your description of the image"}
            Do NOT output {"action": "analyze_photo"} again.
            Make your description concise but detailed."""
        else:
            # Inject rolling conversation digest (older-than-window memory)
            if self.digest_text:
                current_system_instruction += (
                    "\n\nEARLIER CONVERSATION DIGEST (topics from before "
                    f"the recent window):\n{self.digest_text[:1500]}"
                )

            # Inject Long-Term Memory (Permanent Facts) — cached, refreshed every 60s
            if not self._memory_cache or (time.time() - self._memory_cache_ts > 60):
                memory_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'memory.json')
                try:
                    if os.path.exists(memory_path):
                        with open(memory_path, 'r') as f:
                            self._memory_cache = json.load(f)
                    else:
                        self._memory_cache = None
                except Exception:
                    self._memory_cache = None
                self._memory_cache_ts = time.time()
            if self._memory_cache:
                current_system_instruction += "\n\nUSER PERMANENT MEMORY (Facts about the user):\n"
                for k, v in self._memory_cache.items():
                    current_system_instruction += f"- {k}: {v}\n"

            # Inject skill catalog — cached, refreshed every 60s.
            # Uses the forge's MERGED catalog: legacy executable skills
            # plus SKILL.md procedure skills (progressive disclosure —
            # names + one-liners only, full body via read_skill).
            if not self._skills_cache or (time.time() - self._skills_cache_ts > 60):
                try:
                    from utils.skill_forge import render_catalog
                    self._skills_cache = render_catalog(max_chars=1500)
                except Exception:
                    self._skills_cache = ""
                self._skills_cache_ts = time.time()
            if self._skills_cache:
                current_system_instruction += (
                    "\n\n" + self._skills_cache +
                    "\nIf the user's request matches a saved skill, prefer "
                    "run_skill (executable skills) or read_skill "
                    "(procedures) over writing new code."
                )

            # Inject external tool catalog — cached, refreshed every 60s
            if not self._tools_cache or (time.time() - self._tools_cache_ts > 60):
                try:
                    from utils.tool_registry import ToolRegistry
                    registry = ToolRegistry()
                    self._tools_cache = \
                        registry.render_catalog(max_chars=1200)
                except Exception:
                    self._tools_cache = ""
                self._tools_cache_ts = time.time()
            if self._tools_cache:
                current_system_instruction += (
                    "\n\n" + self._tools_cache +
                    "\nIf the user's request matches an external tool, use "
                    "call_tool with its exact params."
                )

            # Inject self-learned heuristics — cached, refreshed every 120s
            if not self._lessons_cache or \
                    (time.time() - self._lessons_cache_ts > 120):
                try:
                    from utils import self_learning
                    self._lessons_cache = \
                        self_learning.render_lessons(max_chars=800)
                except Exception:
                    self._lessons_cache = ""
                self._lessons_cache_ts = time.time()
            if self._lessons_cache:
                current_system_instruction += (
                    "\n\n" + self._lessons_cache +
                    "\nApply these heuristics when planning or executing."
                )
            
            # Inject Episodic Memory context — cached, no re-init on every call
            try:
                if self._episodic is None:
                    from utils.episodic_memory import EpisodicMemory
                    self._episodic = EpisodicMemory()
                episodic_context = self._episodic.get_context_for_prompt(query=prompt, max_tokens=500)
                if episodic_context and len(episodic_context) > 30:
                    current_system_instruction += f"\n\n{episodic_context}"

                # Auto-RAG: relevant knowledge-vault excerpts (watchdog-guarded)
                try:
                    vault_block = self._vault_block(prompt)
                    if vault_block:
                        current_system_instruction += f"\n\n{vault_block}"
                except Exception:
                    pass
                # Also inject relationship context
                if self._relationships is None:
                    from utils.relationships import RelationshipManager
                    self._relationships = RelationshipManager(episodic_memory=self._episodic)
                rel_context = self._relationships.get_context_for_prompt(max_chars=800)
                if rel_context and len(rel_context) > 30:
                    current_system_instruction += f"\n\n{rel_context}"
            except Exception:
                pass

            # Inject RLM recursive memory — cross-level recall of the
            # user's history (world model, themes, sessions, events),
            # watchdog-guarded so a slow index can never stall chat.
            try:
                rlm_block = self._rlm_block(prompt)
                if rlm_block:
                    current_system_instruction += f"\n\n{rlm_block}"
            except Exception:
                pass

            # Session continuity: after an idle gap (or a restart) the
            # RLM bridges to where the last session left off — once per
            # session, re-armed after 6h so long-lived processes get it
            # back after overnight idle.
            try:
                cont = self._rlm_continuity_block()
                if cont:
                    current_system_instruction += f"\n\n{cont}"
            except Exception:
                pass

            # Inject guardrails — USER.md / MEMORY.md / .jarvis.md
            # standing instructions (Hermes parity).  Cached internally.
            try:
                from utils.guardrails import get_guardrails
                guardrail_block = get_guardrails().load_block()
                if guardrail_block:
                    current_system_instruction += f"\n\n{guardrail_block}"
            except Exception:
                pass

            # Inject capability status so the LLM knows what it can/can't do
            try:
                from utils.capabilities import list_all, enabled as caps_enabled
                if caps_enabled():
                    caps = list_all()
                    ready = [c['name'] for c in caps if c['status'] == 'ready']
                    missing = [c['name'] for c in caps if c['status'] != 'ready']
                    if ready or missing:
                        cap_block = (
                            f"\n\nCAPABILITIES — ready: "
                            f"{', '.join(ready) or 'none'}\n"
                            f"missing: {', '.join(missing) or 'none'}\n"
                            f"Use capability_check to assess before "
                            f"complex tasks. Use capability_expand to "
                            f"create upgrade plans for missing ones. "
                            f"JARVIS_AUTO_UPGRADE=1 enables auto-install "
                            f"of pip packages."
                        )
                        current_system_instruction += cap_block
            except Exception:
                pass
            
            # Inject recent conversation topics for recall
            try:
                if self._episodic is not None:
                    topic_memories = self._episodic.get_by_category('conversation_topic', limit=8)
                    if topic_memories:
                        current_system_instruction += "\n\nRECENT CONVERSATION HISTORY (what we discussed):\n"
                        for tm in topic_memories:
                            ts = tm.get('created', '')[:16].replace('T', ' ')
                            current_system_instruction += f"  - [{ts}] {tm['text']}\n"
            except Exception:
                pass
            
            # Inject user profile summary for deep personalization
            try:
                if self._episodic is not None and self._episodic.count() > 5:
                    profile_summary = self._episodic.get_profile_summary(max_chars=800)
                    if profile_summary and len(profile_summary) > 50:
                        current_system_instruction += f"\n\n{profile_summary}"
            except Exception:
                pass

        # --- Phase 2: native tool-calling agent loop ---------------------
        # Runs BEFORE legacy single-shot routing.  On any failure or
        # ineligibility it returns None and the legacy paths below take
        # over unchanged (rollback = JARVIS_AGENT_MODE=off).
        if self._agent_mode_ok():
            try:
                from utils.agent_loop import get_agent_loop
                with self.history_lock:
                    _agent_history = list(self.history)
                agent_res = get_agent_loop(self).run(
                    prompt, image_path=image_path,
                    system_instruction=current_system_instruction,
                    history_snapshot=_agent_history,
                    ui_callback=getattr(self, 'ui_emit', None))
                if agent_res is not None:
                    return agent_res
            except Exception as agent_err:
                print(f"Brain: agent loop failed → legacy path "
                      f"({agent_err})")

        # Try Gemini Direct — legacy single-provider path, only when the
        # router fleet isn't already covering Google (avoids double-billing
        # the same Gemini quota on every request).
        _router_probe = getattr(self, 'router', None)
        if Config.GOOGLE_API_KEY and (
                _router_probe is None or len(_router_probe.providers) == 0):
            gemini_res = self.process_with_gemini(prompt, image_path, current_system_instruction)
            if gemini_res:
                return gemini_res

        # Add History (snapshot for thread safety)
        with self.history_lock:
            history_snapshot = list(self.history)

        # --- Context budget enforcement --------------------------------
        # Free-tier providers (e.g. Groq 8k TPM) reject oversized prompts
        # with 413s that poison every model in the failover chain.  Trim
        # progressively: history first, then all optional context blocks.
        def _est(s):
            return len(s or '') // 4

        max_tok = self.MAX_PROMPT_TOKENS
        prompt_est = _est(prompt)
        total_est = (_est(current_system_instruction) + prompt_est +
                     sum(_est(u) + _est(a) for u, a in history_snapshot))

        if total_est > max_tok and not image_path:
            # Tier 1: keep only the history that fits half the budget
            hist_budget = max(0, max_tok // 2 -
                              _est(current_system_instruction))
            kept = []
            for u, a in reversed(history_snapshot):
                cost = _est(u) + _est(a)
                if hist_budget - cost < 0:
                    break
                kept.insert(0, (u, a))
                hist_budget -= cost
            history_snapshot = kept
            total_est = (_est(current_system_instruction) + prompt_est +
                         sum(_est(u) + _est(a) for u, a in history_snapshot))

            # Tier 2: strip optional context — persona + short-term only
            if total_est > max_tok:
                current_system_instruction = (
                    self.system_instruction + short_term_block +
                    "\n\n(Context trimmed to fit provider token limits.)")
                # Recompute so downstream sizing uses the SLIM reality
                total_est = (_est(current_system_instruction) +
                             _est(prompt))
                print(f"Brain: context trimmed to fit "
                      f"~{max_tok} token limit "
                      f"(now ~{total_est} est)")

        messages = [
            {"role": "system", "content": current_system_instruction},
        ]
        for old_user, old_ai in history_snapshot:
             messages.append({"role": "user", "content": old_user})
             messages.append({"role": "assistant", "content": old_ai})

        user_content = []
        final_prompt = prompt
        
        # Handle single vs list of images
        image_paths = []
        if image_path:
            if isinstance(image_path, list):
                image_paths = image_path
            else:
                image_paths = [image_path]
        
        if image_paths:
             # Determine context based on first image name
             if 'webcam' in image_paths[0].lower():
                 final_prompt = f"WEBCAM IMAGE(S) ATTACHED. {prompt}"
             else:
                 final_prompt = f"IMAGE(S) ATTACHED. {prompt}"
        
        user_content.append({"type": "text", "text": final_prompt})

        # Attach all images
        for img_path in image_paths:
            if img_path and os.path.exists(img_path):
                base64_image = self._encode_image(img_path)
                user_content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}"
                    }
                })
        
        messages.append({"role": "user", "content": user_content})

        # --- Model Selection Logic ---
        # If this is a VISION request, prioritize Vision-Capable models
        models_to_try = self.models.copy()
        if image_path:
            # Prefer explicitly configured vision models, then any known vision slug
            vision_models = [m for m in Config.VISION_MODELS if m in models_to_try]
            vision_models += [m for m in models_to_try
                              if m not in vision_models and any(v in m for v in ('vl', 'vision', 'gemini', 'claude', 'gpt-4o', 'dots'))]
            others = [m for m in models_to_try if m not in vision_models]
            models_to_try = vision_models + others
            print(f"Vision Request Detected. Prioritizing: {vision_models}")

        # --- Attempt matrix: BYOK fleet first, legacy Groq fallback -----
        # Fleet entries carry "provider:model" labels routed through the
        # shim; legacy entries are bare slugs against self.clients.
        from utils.llm.shim import RouterCompletionClient
        attempt_matrix = []
        router = getattr(self, 'router', None)
        if router is not None and len(router.providers) > 0:
            require = {'vision'} if image_path else None
            try:
                chain = router._chain(require=require)
            except Exception as chain_err:
                print(f"Brain: router chain build failed: {chain_err}")
                chain = []
            shim = RouterCompletionClient(router)
            for provider_obj, mdl in chain:
                attempt_matrix.append((f"{provider_obj.name}:{mdl}", shim))
        if not attempt_matrix:
            for m in models_to_try:
                for c in self.clients:
                    attempt_matrix.append((m, c))

        # --- Failover Loop ---
        from utils.budget import get_budget, BudgetExceededError
        from utils.circuit_breaker import get_breaker
        # Provider TPM limits count INPUT + COMPLETION reserve together
        # (Groq free tier: 8000 TPM).  Size the completion budget so the
        # whole request fits: out = TPM − est_input×safety − margin.
        est_input_real = int(total_est * 1.35) + 64   # provider tokenizers
        try:
            tpm_limit = int(os.getenv('JARVIS_GROQ_TPM', '8000'))
        except ValueError:
            tpm_limit = 8000
        completion_budget = max(1024, min(4096,
                                          tpm_limit - est_input_real - 128))
        print(f"Brain: context estimate {total_est} tok "
              f"(ceiling {max_tok}, history {len(history_snapshot)} entries)")
        last_error = None
        _last_attempt = len(attempt_matrix) - 1
        for _attempt_i, (model, _client) in enumerate(attempt_matrix):
            # Guardrails before spending tokens on this attempt
            try:
                if not get_breaker().allow_llm_spend():
                    return {"action": "chat",
                            "response": "Systems check failed, Sir — "
                            "I've paused AI processing. Please check my "
                            "diagnostics."}
                get_budget().guard()
            except BudgetExceededError:
                return {"action": "chat",
                        "response": "I've reached my session compute "
                        "budget, Sir. Raise JARVIS_BUDGET_USD or restart "
                        "me to continue."}

            print(f"Attempting with model: {model} "
                  f"(in~{est_input_real}, out≤{completion_budget})")
            success = False
            completion = None

            # One client per attempt (the shim IS the fleet; legacy
            # entries carry a single real Groq-compatible client).
            for i, client in enumerate((_client,)):
                try:
                    print(f"  Attempting with Key #{i+1}...")
                    attempts = 0
                    while True:
                        attempts += 1
                        try:
                            completion = client.chat.completions.create(
                                model=model,
                                messages=messages,
                                max_tokens=completion_budget,
                                temperature=0.1,
                                timeout=45,
                            )
                            break
                        except Exception as attempt_err:
                            # One bounded retry on provider rate limits —
                            # free tiers (Groq TPM) recover within ~15-30s
                            es = str(attempt_err)
                            retryable = (attempts < 2 and (
                                '413' in es or 'rate_limit' in es
                                or 'tokens per minute' in es.lower()))
                            if not retryable:
                                raise
                            delay = 20.0
                            m = re.search(r'retry in ([\d.]+)\s*s', es,
                                          re.IGNORECASE)
                            if m:
                                delay = min(float(m.group(1)) + 2, 60)
                            print(f"  Rate-limited — single retry in "
                                  f"{delay:.0f}s...")
                            time.sleep(delay)
                    success = True
                    if not isinstance(client, RouterCompletionClient):
                        # Fleet attempts were already accounted inside the
                        # router; only legacy client calls record here.
                        get_budget().record(
                            model=model,
                            input_text=json.dumps(messages)[:4000],
                            output_text=(completion.choices[0].message.content
                                         or ''),
                            usage=getattr(completion, 'usage', None))
                    break # Key worked!
                except Exception as e:
                    print(f"  Attempt failed: {e}")
                    last_error = e
            
            if not success:
               continue # Try next model logic
               
            try:
                
                text_response = completion.choices[0].message.content.strip()
                try:
                    import logging
                    logger = logging.getLogger('Jarvis')
                    logger.info(f"Raw AI Response ({model}): {text_response}")
                except:
                    print(f"Raw AI Response ({model}): {text_response}")
                
                # ... (Parsing Logic) ...
                
                # Clean up potential markdown code blocks
                if text_response.startswith('```json'):
                    text_response = text_response[7:]
                if text_response.startswith('```'):
                    text_response = text_response[3:]
                if text_response.endswith('```'):
                    text_response = text_response[:-3]
                
                text_response = text_response.strip()

                try:
                    # Parse JSON
                    command = json.loads(text_response)
                except json.JSONDecodeError:
                    # Fallback: Extract JSON using non-greedy regex to find ALL potential objects
                    import re
                    json_blocks = re.findall(r'(\{.*?\})', text_response, re.DOTALL)
                    
                    for block in json_blocks:
                        try:
                            # Clean common issues like single quotes (if they aren't inside strings)
                            # But simple loads first
                            command = json.loads(block.replace("'", '"'))
                            if 'action' in command:
                                break # Found a valid command
                        except:
                            continue
                    
                    if not command:
                        # Fallback 2: Robust Parsers for "Action: ... Key: ..." format
                        action_match = re.search(r'Action:\s*([a-zA-Z_]+)', text_response, re.IGNORECASE)
                        if action_match:
                            extracted_action = action_match.group(1).lower()
                            command = {'action': extracted_action}
                            
                            # Parse all "Key: Value" lines
                            try:
                                pairs = re.findall(r'([a-zA-Z_]+):\s*(.+)', text_response)
                                for k, v in pairs:
                                    k_lower = k.lower().strip()
                                    v_clean = v.strip()
                                    if k_lower == 'key': command['key'] = v_clean
                                    elif k_lower == 'value': 
                                        if v_clean.isdigit(): command['value'] = int(v_clean)
                                        else: command['value'] = v_clean.replace('%', '')
                                    elif k_lower in ['query', 'target', 'prompt', 'item', 'device', 'command', 'response', 'recipient', 'message']: 
                                        command[k_lower] = v_clean
                            except:
                                pass
                            
                            # Normalization: Map UK -> US spelling for specific keys
                            if 'key' in command:
                                command['key'] = command['key'].replace('favourite', 'favorite').replace('colour', 'color').replace(' ', '_').lower()

                            # If no keys found, treat remainder as target (legacy fallback)
                            if len(command) == 1:
                                remaining = text_response.replace(action_match.group(0), '').strip()
                                if remaining:
                                    command['target'] = remaining
                                    command['query'] = remaining
                        else:
                            command = None
                
                # Global Normalization (Applied to both JSON and Regex results)
                if command and isinstance(command, dict):
                    if 'key' in command:
                         command['key'] = command['key'].replace('favourite', 'favorite').replace('colour', 'color').replace(' ', '_').lower()

                if command:
                    # Save to history (thread-safe + persistent)
                    action = command.get('action')
                    if action == 'chat':
                         self._append_history(prompt, command.get('response', ''))
                    elif action:
                         self._append_history(prompt, f"Action: {action}")
                    else:
                         self._append_history(prompt, "Action: [Custom JSON]")

                    return command 
                else:
                    # If response looks truncated (finish_reason == length),
                    # retry with a larger token budget before giving up.
                    finish = getattr(completion.choices[0], 'finish_reason', '')
                    if finish == 'length' and _attempt_i < _last_attempt:
                        print(f"Response truncated from {model}, retrying with more tokens...")
                        try:
                            retry = client.chat.completions.create(
                                model=model,
                                messages=messages,
                                max_tokens=2048,
                                temperature=0.1,
                                timeout=30,
                            )
                            retry_text = retry.choices[0].message.content.strip()
                            if retry_text.startswith('```json'): retry_text = retry_text[7:]
                            if retry_text.startswith('```'): retry_text = retry_text[3:]
                            if retry_text.endswith('```'): retry_text = retry_text[:-3]
                            retry_cmd = json.loads(retry_text.strip())
                            if retry_cmd and 'action' in retry_cmd:
                                self._append_history(prompt, f"Action: {retry_cmd.get('action')}")
                                return retry_cmd
                        except Exception:
                            pass

                    # Fallback if AI didn't output JSON
                    print(f"Failed to parse JSON from {model}, falling back to chat")
                    
                    self._append_history(prompt, text_response)
                        
                    return {"action": "chat", "response": text_response}

            except Exception as e:
                print(f"Model {model} failed: {e}")
                last_error = e
                continue # Try next model
        
        # If we exit the loop, all models failed — translate for humans
        err_text = str(last_error or '').lower()
        if ('413' in err_text or 'too large' in err_text
                or 'tokens per minute' in err_text
                or 'rate_limit' in err_text):
            friendly = ("My AI provider is rate-limiting me right now, "
                        "Sir. Give it a minute and try again.")
        elif 'timeout' in err_text or 'timed out' in err_text:
            friendly = ("The AI services are slow to respond at the "
                        "moment, Sir. Please try again shortly.")
        elif '401' in err_text or 'unauthorized' in err_text \
                or 'api key' in err_text:
            friendly = ("There's an authentication problem with my AI "
                        "services, Sir. Please check the API keys.")
        else:
            friendly = ("I'm having trouble reaching my AI services "
                        "right now, Sir. Please try again shortly.")
        print(f"All models failed. Last error: {str(last_error)[:300]}")
        return {"action": "chat", "response": friendly}
