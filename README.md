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
cp .env.example .env        # then fill in GROQ_API_KEY (required)

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
| `utils/brain.py` | LLM routing (Groq with multi-model failover, Gemini fallback), JSON command parsing, conversation history |
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

## Configuration

See `.env.example` for every setting. The most important:

- `GROQ_API_KEY` — **required** for the brain (or `GOOGLE_API_KEY` as fallback)
- `TELEGRAM_TOKEN` + `TELEGRAM_ALLOWED_IDS` — remote control
- `GROQ_MODELS` — override the default model list (current: `openai/gpt-oss-120b,openai/gpt-oss-20b,groq/compound-mini,qwen/qwen3.6-27b`)

## Troubleshooting

- **"All models failed"** in the log — your Groq key is invalid/expired,
  or the model slugs are stale. Check `jarvis.log`, update `GROQ_API_KEY`,
  or override `GROQ_MODELS` with slugs from
  https://console.groq.com/docs/models.
- **No audio** — the assistant degrades to UI-only; check the `jarvis.log`
  `Mouth` warnings.
- **Screenshot/desktop control blocked on macOS** — grant Screen Recording
  permission to your terminal/IDE.
