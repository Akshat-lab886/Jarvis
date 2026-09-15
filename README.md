# J.A.R.V.I.S. — Personal AI Assistant

A voice + web + Telegram personal assistant that can control your computer,
manage email/calendar, set reminders, run missions, scaffold apps, and answer
questions.

## Quick start

```bash
# 1. Create a venv and install dependencies
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 2. Configure
cp .env.example .env        # then fill in any ONE provider key (see below)

# 3. (Optional) Google email/calendar
#    Put your OAuth client JSON at client_secret.json, then:
./venv/bin/python auth.py   # logs in once, creates token.json

# 4. (Optional) Browser agent
./venv/bin/python setup_agent.py   # installs Playwright browsers

# 5. Run
./start.sh                  # or: ./venv/bin/python main.py
```

Open the dashboard at `http://localhost:5001` (or your machine's LAN IP).
Say **"Jarvis"** in the browser to wake him, or just type commands.

## What Jarvis can do

- **Chat & web search** — general knowledge, live news, weather, stocks
- **System control** — open apps/websites, system info, battery, volume,
  media keys, screenshots, webcam photos, desktop automation ("click X")
- **Email & calendar** — read/send/reply email, check & add calendar events,
  morning briefing
- **Reminders** — natural language ("remind me in 20 minutes to drink water"),
  persists across restarts, repeats daily, notified via Telegram and a
  dashboard toast with **Snooze 10m** / Dismiss
- **Smart home (simulated)** — lights, fan, AC with dashboard controls
- **Knowledge vault** — feed PDFs/TXT/MD, ask questions over them (ChromaDB RAG)
- **Missions** — "start mission: <goal>" breaks a big task into steps and
  executes them
- **Coding** — write & run Python, scaffold python/web projects, build Flutter
  & React Native apps ("auto-build app: <idea>"), manage Git/GitHub
- **To-do list** — "add buy milk to my todo list", "mark X as done", dashboard TASKS panel
- **Notes** — "take a note: <text>", "show my notes", dashboard NOTES panel
- **System insights** — "what's using my RAM?", "kill <app>"
- **News** — "today's headlines" (Google News RSS)
- **Daily recap** — "what did I do today?"
- **Smart home state persists** across restarts
- **Telegram remote** — control Jarvis from your phone (authorized IDs only)

### New: Episodic & Adaptive Memory
- **Structured Memory** — remember preferences, goals, health info, context across months of chats
- **Auto-capture** — automatically extracts and stores personal info from conversations
- **Searchable** — recall by keyword, category, or relevance with temporal decay
- **Dashboard MEMORY panel** — browse, add, and forget memories from the UI

### New: Relationship Intelligence
- **Track people** — names, relationships, birthdays, allergies, hobbies, preferences
- **Gift suggestions** — "gift ideas for Sarah" uses their interests to suggest gifts
- **Dinner suggestions** — "dinner ideas with Mom" considers allergies and preferences
- **Proactive reminders** — birthday/anniversary alerts in morning briefing
- **Dashboard PEOPLE panel** — manage your social graph from the UI

### New: Proactive Automation
- **Dynamic Time Blocking** — detects schedule conflicts and suggests focus blocks
- **Cross-App Triage** — priority-scores emails, flags only critical items
- **"What needs my attention"** — instant triage summary of urgent messages
- **"Schedule insights"** — calendar conflict detection and buffer recommendations

### New: Always-On Audio Processing
- **Meeting Mode** — background transcription during meetings or calls
- **Auto-extract tasks** — action items automatically added to todo list
- **Meeting summaries** — AI-generated summaries with key decisions
- **"Start meeting mode"** / **"Stop meeting mode"** — voice or dashboard

### New: Computer Vision Recipes
- **Fridge Vision** — take a photo of your fridge for recipe suggestions
- **Ingredient detection** — vision model identifies food items
- **Shopping lists** — generates shopping list for missing ingredients
- **Dietary awareness** — considers allergies and preferences from memory
- **"Look at my fridge"** / **"What can I cook?"**

### New: Privacy-First Framework
- **Granular trust controls** — 5-level permission model for all actions
- **Spending limits** — per-transaction and daily caps for purchases
- **Audit log** — tracks every action with timestamps
- **Data scope controls** — choose what data the AI can analyze
- **"Privacy settings"** — view and modify trust levels

### Enhanced: Morning Briefing V2
- **Context-aware** — includes schedule intelligence, relationship reminders
- **Health goals** — tracks and reminds about health commitments
- **Cross-app triage** — highlights messages needing attention
- **Proactive suggestions** — people to contact, gifts to plan

## Architecture

| Module | Purpose |
| --- | --- |
| `main.py` | Entry point: local voice loop + server startup |
| `utils/server.py` | Flask + SocketIO dashboard, shared `brain`/`executor` singletons |
| `utils/brain.py` | Provider-agnostic LLM routing (9-provider fleet + router failover), JSON command parsing, conversation history, RLM recall/continuity injection, agent-loop dispatch |
| `utils/executor.py` | Executes parsed commands, wires all tools together |
| `utils/quick_actions.py` | Shared keyword triggers (missions, dev studio, git, email, GUI) for voice + dashboard |
| `utils/history.py` | Command log powering the daily summary |
| `utils/ear.py` / `utils/mouth.py` | Speech recognition / TTS playback (non-blocking worker) |
| `utils/secretary.py` | Gmail + Google Calendar |
| `utils/scheduler.py` | Reminders (dateparser, persisted) |
| `utils/knowledge.py` | ChromaDB knowledge vault ("Librarian") |
| `utils/desktop_agent.py` | Vision-driven desktop automation loop |
| `utils/mission_control.py` | Multi-step mission planner |
| `utils/tasks.py` | Persistent to-do list + notes stores |
| `utils/quick_actions.py` | Shared keyword triggers (missions, dev studio, git, email, GUI) |
| `utils/mobile_studio.py`, `utils/dev_studio.py`, `utils/git_manager.py` | App scaffolding / project factory / GitHub |
| `utils/telegram_bot.py` | Remote control via Telegram |
| `utils/episodic_memory.py` | Structured, searchable memory engine (preferences, goals, health, context) |
| `utils/relationships.py` | Relationship intelligence (people, birthdays, gift ideas, dinner suggestions) |
| `utils/context_engine.py` | Passive background context gathering (calendar, email, files) |
| `utils/proactive.py` | Dynamic time blocking + cross-app message triage |
| `utils/meeting_audio.py` | Always-on meeting transcription + auto task extraction |
| `utils/fridge_vision.py` | Computer vision recipe generation + shopping lists |
| `utils/briefing.py` | Enhanced morning briefing V2 (context-aware, relationships, goals) |
| `utils/privacy.py` | Privacy-first framework (trust controls, spending limits, audit log) |
| `templates/`, `static/` | HUD-style web dashboard |
| `utils/llm/` | **Provider-agnostic brain fleet** — router, 9 providers (Groq/Gemini/Claude/OpenAI/DeepSeek/OpenRouter/Ollama/LM Studio/custom), capability-aware failover, cooldowns, runtime keystore |
| `utils/agent_loop.py` | **Native tool-calling agent core** — 41 curated tools (think/recall_deep/memory_about/task_plan/delegate + fleet) + executor bridge, streamed answers, step budget (`JARVIS_AGENT_MODE=off` reverts to legacy routing) |
| `utils/mcp_client.py` | **MCP client** — any Model-Context-Protocol server's tools join the fleet as `mcp__<server>__<tool>` |
| `utils/rlm/` | **Recursive memory core** — L0→L3 hierarchy, consolidation, reflection, entity graph, temporal recall, plan state, reasoner |
| `utils/guardrails.py` | Self-updating USER.md / MEMORY.md / .jarvis.md standing instructions |
| `utils/context_injection.py` | `@file` / `@git` / `@url` marker expansion in any interface |
| `utils/skill_forge.py` | Bounded learning loop: workflow telemetry → skill creation → self-patching |
| `utils/runtimes.py`, `utils/python_rpc.py` | Multi-backend execution (local/docker/ssh/daytona/singularity/vercel/modal) + persistent sandboxed Python worker |
| `utils/checkpoints.py`, `utils/lsp_diagnostics.py` | Pre-edit snapshots + `/rollback`; post-edit syntax/LSP diagnostics |
| `utils/computer_use.py` | Background desktop driving (AX tier — cursor never moves) |
| `utils/gateways.py` | Omnichannel hub: Discord, Slack, webhook + bot-mode specialist routing |
| `utils/mcp_server.py` | Jarvis **as** an MCP server (memory export to other agents) |
| `utils/moa.py` | Mixture-of-agents panel + synthesizer merge |
| `utils/wake_word.py`, `utils/hud.py`, `utils/tui.py` | Wake-word gating, floating HUD, full terminal client (`--tui`) |
| `utils/hyperframe.py` | Instructions → storyboard → self-playing HTML mockup (+ video) |
| `skills/`, `tools_registry/` | User-extensible skills (templated Python) + REST tool manifests — dropped-in JSON, hot-reloaded |

## First-run setup

Open **`/onboarding`** — pick a provider, paste a key, Jarvis goes live.
Any ONE of Groq / Gemini / OpenAI / Anthropic / DeepSeek / OpenRouter /
a local Ollama works; keys stack into an automatic failover chain and can
be added anytime from the dashboard's **LLM** panel (or
`config/providers.json`, chmod 600).

## Agent mode

With any tool-capable provider configured, requests run through the
iterative agent loop: the model calls real tools (memory, todos,
calendar, email, system control, skills, MCP servers) until the job is
done, then streams the answer. Tuning:

- `JARVIS_AGENT_MODEL=groq:openai/gpt-oss-120b` — pin the strongest
  model for agent turns while chat stays on cheap defaults
- `JARVIS_AGENT_MAX_STEPS=10` — tool-iteration budget per request
- `JARVIS_AGENT_STREAM=0` — disable token streaming
- `JARVIS_AGENT_MODE=off` — revert to single-shot legacy routing

## MCP tool servers

Copy `config/mcp_servers.example.json` → `mcp_servers.json`:

```json
[{"name": "files", "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}]
```

Restart or hit **RELOAD** in the LLM panel — those tools appear in the
fleet with `[ OK ]` status and become callable by the agent.

## RLM — recursive memory (PrimeAgent level)

Jarvis doesn't just chat — he remembers, abstracts and re-derives. The
RLM (`utils/rlm/`) recursively compresses experience into levels:

| Level | Content | Built by |
| --- | --- | --- |
| L0 events | one row per exchange | every turn |
| L1 summaries | session-sized folds | background "sleep" pass |
| L2 abstractions | themes & arcs across sessions | consolidation |
| L3 world model | ONE model of you + a playbook | periodic re-distill |

Retrieval is recursive too: a query fans out to every level, an event hit
summons its whole session, and the world model always anchors the block.

On top of the hierarchy sit the PrimeAgent-level memory features:

- **Temporal scoring** — retrieval ranks relevance × recency decay ×
  importance × reuse (tunable: `JARVIS_RLM_W_REL/W_REC/W_IMP`,
  `JARVIS_RLM_DECAY_DAYS`), with a recency floor so durable knowledge
  never vanishes from ranking.
- **Supersession** — a new insight that contradicts an old one *replaces*
  it (Mem0-style ADD/UPDATE) instead of piling up stale duplicates;
  superseded notes are excluded from recall and pruned first.
- **Entity graph** — who/what memory is about: counts, last-seen,
  co-occurrence edges, subject queries (`memory_about` tool,
  `jarvis_recall` over MCP). `JARVIS_RLM_ENTITIES=0` off.
- **Session continuity** — after an idle gap (default 30 min), the next
  turn re-injects where the last session left off.

The agent loop gets in-loop memory tools: `think` (zero-side-effect
scratchpad), `recall_deep` (every abstraction level), `task_plan`
(persistent multi-step plan surviving restarts), `delegate`
(privilege-separated subagents — researcher/coder/synthesizer only;
archivist/reflector/operator are unreachable through it),
`memory_about` (subject query). Tests: `tests/test_rlm.py`.

Offline-first notes: the whole RLM stack is local JSON + optional
ChromaDB — `JARVIS_RLM=0` disables it, and the breaker reports a
`memory_backends` line covering the RLM store (`brain/data/rlm_memory.json`,
overridable via `JARVIS_RLM_DATA_FILE`; plan via `JARVIS_AGENT_PLAN_FILE`),
so a read-only store shows as degraded instead of silently losing memory
on restart. The reflector → guardrails loop (`USER.md`/`MEMORY.md`) and
reflector → `brain/data/lessons.md` loop are both fail-soft with their own
kill switches (`JARVIS_GUARDRAILS=0`, `JARVIS_AUTO_LEARN=0`).

## Hermes-parity capability stack

The 26-feature Hermes capability list, implemented:

**Bounded learning loop** — `guardrails.py` (self-updating
USER.md / MEMORY.md / .jarvis.md), `context_injection.py` (`@file`,
`@dir`, `@git`, `@https://…` markers expand in any interface),
`skill_forge.py` (workflow telemetry → autonomous SKILL.md/code skill
creation → failure-triggered self-patching), FTS5 transcript search.

**Execution environments** — `runtimes.py` (one API, seven backends:
local / docker / ssh / daytona / singularity / vercel / modal, each
purely config-activated), `python_rpc.py` (persistent sandboxed worker
with a surviving namespace), `checkpoints.py` (pre-edit snapshots +
`/rollback`), `lsp_diagnostics.py` (post-edit syntax/LSP feedback),
`computer_use.py` (background AX clicks/typing — the cursor never moves).

**Omnichannel gateways** — `gateways.py` (Discord REST polling, Slack
Events API, webhook, Telegram; bot-mode `@jarvis` routing to specialist
subagents), `tui.py` (`python main.py --tui` — slash commands, streaming
tool feed, Ctrl+C interrupts), `hud.py` (floating hotkey HUD),
`wake_word.py` (opt-in "Hey Jarvis" gating).

**Collaborative automations & web/media** — `recurring.py` (natural
language cron, now with interval schedules "every 20 minutes"),
`mcp_server.py` (Jarvis *operates as* an MCP server exporting memory to
Claude Desktop / IDEs; `jarvis_ask` is opt-in), `moa.py`
(mixture-of-agents panel + synthesis), `browser_use.py` +
`tool_gateway.py` (browser automation tiers), `hyperframe.py`
(instructions → storyboard → self-playing HTML mockup → optional video).

Every module has an env kill switch (`JARVIS_<MODULE>=0`) and degrades
to a no-op — see `.env.example`. Tests: `tests/test_hermes_parity.py`.

## Configuration

See `.env.example` for every setting. The most important:

- **Any ONE of** `GROQ_API_KEY`, `GOOGLE_API_KEY`, `OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`,
  a local Ollama/LM Studio server, or any OpenAI-compatible
  `CUSTOM_OPENAI_BASE_URL` — Jarvis routes across every configured
  provider with automatic failover, quota cooldowns and capability-aware
  model selection (vision requests go to vision models; tools to
  tool-capable ones).
- Keys can also be added at runtime (no restart) via the dashboard —
  they persist in `config/providers.json` (chmod 600, never logged).
- Provider priority: `JARVIS_PROVIDER_ORDER=google,groq,anthropic,...`
  or `JARVIS_PREFER_LOCAL=1` to try Ollama/LM Studio first.
- `TELEGRAM_TOKEN` + `TELEGRAM_ALLOWED_IDS` — remote control
- `GROQ_MODELS` — override the default Groq model list
- `JARVIS_HOST=0.0.0.0` + **`JARVIS_WEBHOOK_KEY`** — expose the dashboard
  to your LAN (required before any phone can pair)

### Jarvis Lite — paired phone PWA (no App Store, no fees)

Your phone becomes a remote for this Mac hub — same brain, same memory,
same approvals. Open **`/mobile`** on the phone (or MOBILE LINK → OPEN
PHONE UI on the dashboard) → **Add to Home Screen** → pair once with the
8-char code from the dashboard's MOBILE LINK → PAIR button:

- **Chat** — same think→execute pipeline as every other channel. The host
  stays silent, and the request is stamped `_origin='mobile:<device>'` —
  an untrusted origin — so destructive actions ALWAYS hold for a human
  on the dashboard. The phone can never bypass HITL.
- **NOTE** — works fully offline: notes queue in the phone's localStorage
  outbox and push on SYNC. Sync is idempotent (client UUIDs) and
  cursor-pulled, so retries never double-apply.
- **Devices** — each phone gets its own per-device Bearer token (only the
  SHA-256 hash is stored, `brain/data/mobile_devices.json`, chmod 600).
  Revoke anytime from the dashboard — the phone goes dark immediately.
- **Pair link + QR** — MOBILE LINK → PAIR now shows the 8-char code plus a
  QR and a copyable `jarvis://pair?hub=..&code=..` link. The link carries
  no secret (same single-use code, 10-min TTL, same POST `/redeem`) —
  scan it with the Lite app, or paste it into the PWA's OR PASTE PAIR
  LINK field. QR renders via CDN with a graceful "use the code" fallback
  when offline.
- **Off-LAN** — prefer Tailscale (free ≤100 devices) over a raw
  port-forward; the PWA, pairing and sync all work unchanged over it.
  Open the dashboard via the LAN IP / Tailscale name so the QR encodes a
  phone-reachable hub origin.

### Jarvis Lite — Flutter app (same hub, no new trust)

`mobile_lite/` is a lightweight native client over the SAME
`/api/mobile/*` surface as the PWA — no new protocol, no new trust.
Chat runs the same host-silent, HITL-gated pipeline; notes queue offline
in SQLite (200 rows / 2000 chars, batch 100) and sync idempotently.

- **Pair** — SCAN QR (dashboard code), PASTE LINK, or manual hub + code.
  Token lives in the platform keychain (`flutter_secure_storage`), never
  in prefs/logs/DB.
- **Build (100% free)** — `flutter pub get && flutter analyze &&
  flutter test`, then `flutter create --platforms=android . &&
  flutter build apk --debug` and sideload. CI does the same on every
  `mobile_lite/` push (`.github/workflows/lite-apk.yml`) and uploads the
  debug APK as an artifact — no App Store, no fees.
- **Offline rule** — `/note …` / `note:` stays on-device; everything else
  goes to hub chat. The phone never guesses at actions.

## Troubleshooting

- **"All models failed" / "all providers failed"** in the log — no fleet
  member could serve: a key is invalid/expired, model slugs are stale,
  or local servers are down. Check `jarvis.log`, verify keys in the
  dashboard **LLM** panel (or `GROQ_MODELS` / `*_MODELS` overrides), and
  confirm Ollama/LM Studio is reachable if you rely on local models
  (Groq slug list: https://console.groq.com/docs/models).
- **No audio** — the assistant degrades to UI-only; check the `jarvis.log`
  `Mouth` warnings.
- **Screenshot/desktop control blocked on macOS** — grant Screen Recording
  permission to your terminal/IDE.
