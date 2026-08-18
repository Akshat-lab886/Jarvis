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
cp .env.example .env        # then fill in OPENROUTER_API_KEY (required)

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
  persists across restarts, repeats daily, notified via Telegram too
- **Smart home (simulated)** — lights, fan, AC with dashboard controls
- **Knowledge vault** — feed PDFs/TXT/MD, ask questions over them (ChromaDB RAG)
- **Missions** — "start mission: <goal>" breaks a big task into steps and
  executes them
- **Coding** — write & run Python, scaffold python/web projects, build Flutter
  & React Native apps ("auto-build app: <idea>"), manage Git/GitHub
- **To-do list** — "add buy milk to my todo list", "mark X as done", dashboard TASKS panel
- **Notes** — "take a note: <text>", "show my notes"
- **System insights** — "what's using my RAM?", "kill <app>"
- **News** — "today's headlines" (Google News RSS)
- **Daily recap** — "what did I do today?"
- **Smart home state persists** across restarts
- **Telegram remote** — control Jarvis from your phone (authorized IDs only)

## Architecture

| Module | Purpose |
| --- | --- |
| `main.py` | Entry point: local voice loop + server startup |
| `utils/server.py` | Flask + SocketIO dashboard, shared `brain`/`executor` singletons |
| `utils/brain.py` | LLM routing (OpenRouter with multi-key/multi-model failover, Gemini fallback), JSON command parsing, conversation history |
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
| `templates/`, `static/` | HUD-style web dashboard |

## Configuration

See `.env.example` for every setting. The most important:

- `OPENROUTER_API_KEY` — **required** for the brain (or `GOOGLE_API_KEY` as fallback)
- `TELEGRAM_TOKEN` + `TELEGRAM_ALLOWED_IDS` — remote control
- `OPENROUTER_MODELS` — override the default (currently verified) free model list

## Troubleshooting

- **"All models failed"** in the log — your OpenRouter key is invalid/expired,
  or the model slugs are stale. Check `jarvis.log`, update `OPENROUTER_API_KEY`,
  or override `OPENROUTER_MODELS` with slugs from
  https://openrouter.ai/api/v1/models.
- **No audio** — the assistant degrades to UI-only; check the `jarvis.log`
  `Mouth` warnings.
- **Screenshot/desktop control blocked on macOS** — grant Screen Recording
  permission to your terminal/IDE.
