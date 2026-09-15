"""
J.A.R.V.I.S. — Full TUI Environment (Hermes parity)
===================================================

A native-terminal chat client with the works:

    * multiline editing      — end a line with '\\' to continue it
    * slash commands         — /skills /plan /checkpoint /rollback …
      with TAB autocompletion
    * instant interrupts     — Ctrl+C aborts the current turn (Ctrl+C
      twice quickly, or /quit, exits)
    * inline streaming tool feed — every [RUN]/[ OK ]/[FAIL] event from
      the agent loop prints live while tools execute
    * colour                 — amber-on-dark, matching the house style

Start it with:

    python main.py --tui        (headless-safe: no mic, no Flask)

The TUI talks to the same shared Brain/Executor instances, so memory,
skills and history are identical to the dashboard.
"""

import os
import sys
import time
import datetime

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE_DIR)

AMBER = '\033[38;5;214m'
DIM = '\033[38;5;244m'
BOLD = '\033[1m'
RESET = '\033[0m'
RED = '\033[38;5;203m'
GREEN = '\033[38;5;114m'

BANNER = r"""
     ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗
     ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝
     ██║███████║██████╔╝██║   ██║██║███████╗
██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║
╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║
 ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝
"""


# --------------------------------------------------------------------- #
# Slash commands
# --------------------------------------------------------------------- #

def _slash_commands():
    return [
        '/help', '/skills', '/read_skill', '/plan', '/memory',
        '/reminders', '/automations', '/todos', '/notes',
        '/checkpoint', '/rollback', '/runtimes', '/providers',
        '/rlm', '/goals', '/tasks', '/clear', '/quit',
    ]


def _run_slash(cmd_line, brain, executor, ui):
    """Execute one slash command.  Returns True when handled."""
    parts = cmd_line.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ''

    if cmd == '/help':
        ui.say(f"{BOLD}Slash commands{RESET}\n"
               f"  /skills            list reusable skills\n"
               f"  /read_skill NAME   load full skill instructions\n"
               f"  /plan              show the active agent task plan\n"
               f"  /memory            episodic memory stats\n"
               f"  /reminders         pending reminders\n"
               f"  /automations       recurring cron-style jobs\n"
               f"  /todos /notes      tasks and notes\n"
               f"  /checkpoint        snapshot the workspace now\n"
               f"  /rollback [ID]     restore a checkpoint\n"
               f"  /runtimes          available execution backends\n"
               f"  /providers         LLM provider fleet status\n"
               f"  /rlm               recursive memory stats\n"
               f"  /goals             tracked goals (open + countdowns)\n"
               f"  /tasks [ID]        background tasks (or one task's "
               f"detail)\n"
               f"  /clear             clear conversation history\n"
               f"  /quit              exit (also: Ctrl+C twice)")
    elif cmd == '/skills':
        from utils.skill_forge import render_catalog
        ui.say(render_catalog() or "No skills saved yet.")
    elif cmd == '/read_skill':
        from utils.skill_forge import read_skill
        ui.say(read_skill(arg) if arg else "Usage: /read_skill NAME")
    elif cmd == '/plan':
        from utils.rlm import get_plan_state
        ui.say(get_plan_state().render() or "No active task plan.")
    elif cmd == '/memory':
        stats = executor.episodic.stats()
        ui.say(f"Memory: {stats.get('total', 0)} entries.")
    elif cmd == '/reminders':
        ui.say(executor.scheduler.list_reminders())
    elif cmd == '/automations':
        ui.say(executor.recurring.list_jobs())
    elif cmd == '/todos':
        ui.say(executor.tasks.list_todos()
               if hasattr(executor.tasks, 'list_todos')
               else str(executor.tasks.get_todos()))
    elif cmd == '/notes':
        ui.say(str(executor.tasks.get_notes())
               if hasattr(executor.tasks, 'get_notes') else '(notes)')
    elif cmd == '/checkpoint':
        from utils.checkpoints import get_checkpoints
        ui.say(get_checkpoints().create(label='tui-manual'))
    elif cmd == '/rollback':
        from utils.checkpoints import get_checkpoints
        cp = get_checkpoints()
        ui.say(cp.rollback(arg) if arg else cp.list())
    elif cmd == '/runtimes':
        from utils.runtimes import describe
        ui.say(describe())
    elif cmd == '/providers':
        router = getattr(brain, 'router', None)
        names = sorted(getattr(router, 'providers', {}) or {})
        ui.say(f"Provider fleet: {', '.join(names) or 'none'}")
    elif cmd == '/rlm':
        from utils.rlm import get_rlm
        ui.say(str(get_rlm().stats()))
    elif cmd == '/goals':
        from utils.goals import get_goals
        ui.say(get_goals().render())
    elif cmd == '/tasks':
        tm = executor.task_manager
        if arg:
            t = tm.get_task(arg)
            if t is None:
                for cand in tm.get_recent_tasks(limit=30):
                    if cand.id.startswith(arg):
                        t = cand
                        break
            if t is None:
                ui.say(f"No task '{arg}'.")
            else:
                st = t.status.value if hasattr(t.status, 'value') \
                    else t.status
                lines = [f"Task {t.id}: {t.description}",
                         f"status: {st} ({t.progress_pct()}%)"]
                for s in t.steps:
                    sst = s.status.value if hasattr(
                        s.status, 'value') else s.status
                    lines.append(f"  - [{sst}] {s.text[:80]}")
                ui.say("\n".join(lines))
        else:
            active = tm.get_active_tasks()
            if not active:
                ui.say("No active background tasks.")
            else:
                ui.say("Active tasks:\n" + "\n".join(
                    f"- {t.id}: {t.description[:60]} "
                    f"({t.progress_pct()}%)" for t in active))
    elif cmd == '/clear':
        brain.clear_history()
        ui.say("History cleared.")
    elif cmd == '/quit':
        raise SystemExit(0)
    else:
        return False
    return True


# --------------------------------------------------------------------- #
# Terminal UI
# --------------------------------------------------------------------- #

class TerminalUI:
    """Prints pipeline events live — the inline streaming tool feed."""

    def __init__(self):
        self.last_status = ''

    def say(self, text):
        print(f"{RESET}{text}{RESET}")

    def callback(self, event, data):
        """ui_callback wired into brain/executor paths."""
        if event == 'status':
            msg = (data or {}).get('message', '')
            if msg and msg != self.last_status:
                print(f"{DIM}  │ {msg}{RESET}")
                self.last_status = msg
        elif event == 'ai_text':
            pass      # the final reply prints once, below
        elif event == 'user_text':
            pass


def _read_multiline(prompt):
    """Read a (possibly multi-line) input; '\\' continues the line."""
    try:
        line = input(prompt)
    except EOFError:
        raise SystemExit(0)
    while line.endswith('\\'):
        try:
            line = line[:-1] + input(f"{DIM}… {RESET}")
        except EOFError:
            # Ctrl+D on a continuation line must exit as cleanly as on
            # the main prompt (EOFError is not KeyboardInterrupt, so an
            # uncaught one would escape run_tui as an ugly traceback).
            raise SystemExit(0)
    return line


def _completer_setup():
    """TAB completion for slash commands (readline on unix terminals)."""
    try:
        import readline
    except ImportError:
        return
    commands = _slash_commands()

    def complete(text, index):
        matches = [c for c in commands if c.startswith(text)]
        return matches[index] if index < len(matches) else None

    readline.parse_and_bind('tab: complete')
    readline.set_completer(complete)


def run_tui():
    """Main TUI loop."""
    from utils.brain import Brain
    from utils.executor import JarvisExecutor

    brain = Brain()
    executor = JarvisExecutor()
    ui = TerminalUI()
    # brain.think() surfaces agent-loop progress through the instance
    # attribute ``ui_emit`` (it takes NO ui_callback keyword); the
    # executor phase below gets ui_callback directly.
    brain.ui_emit = ui.callback

    print(f"{AMBER}{BANNER}{RESET}")
    print(f"{DIM}JARVIS TUI — {datetime.datetime.now():%Y-%m-%d %H:%M}"
          f"  ·  /help for commands  ·  Ctrl+C twice to exit{RESET}\n")

    _completer_setup()
    last_ctrlc = 0.0

    while True:
        try:
            text = _read_multiline(f"{AMBER}{BOLD}you ▸ {RESET}")
        except KeyboardInterrupt:
            now = time.time()
            if now - last_ctrlc < 2.0:
                print(f"\n{DIM}Goodnight, Sir.{RESET}")
                return
            last_ctrlc = now
            print(f"\n{DIM}(interrupted — Ctrl+C again to exit){RESET}")
            continue

        if not text.strip():
            continue

        if text.startswith('/'):
            try:
                if _run_slash(text, brain, executor, ui):
                    continue
            except SystemExit:
                print(f"{DIM}Goodnight, Sir.{RESET}")
                return
            ui.say(f"Unknown command: {text.split()[0]}  (/help)")
            continue

        started = time.time()
        try:
            print(f"{DIM}  │ thinking…{RESET}")
            command = brain.think(text)
            result = executor.execute_command(
                command, brain, original_text=text,
                ui_callback=ui.callback)
            reply = _extract_reply(command, result)
            print(f"\n{GREEN}{BOLD}jarvis ▸{RESET} {reply}\n")
        except KeyboardInterrupt:
            print(f"\n{RED}  ✗ turn interrupted{RESET}\n")
        except Exception as e:
            print(f"\n{RED}  ✗ {e}{RESET}\n")
            continue
        _ = started


def _extract_reply(command, result):
    """Best-effort reply text from a think/execute round trip."""
    if isinstance(command, dict) and command.get('response'):
        return str(command['response'])
    if result is not None:
        return str(result)
    return "(done)"


if __name__ == '__main__':
    run_tui()
