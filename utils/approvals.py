"""
Jarvis Human-in-the-Loop Checkpoints
====================================

Critical actions pause the execution engine until a human approves them
through the dashboard (or any channel wired to ``resolve()``).  Denial
or timeout cancels the action deterministically — nothing destructive
fires without a human "yes".

Gated actions (CRITICAL_ACTIONS): outbound email, file deletion,
process killing, developer shell/file operations, downloads cleanup.

Policy (env):
    JARVIS_APPROVALS      off | critical | strict   (default critical)
    APPROVAL_TIMEOUT_S    seconds to wait    (default 120)
    DESTRUCTIVE_COOLDOWN_S  min seconds between destructive approvals (default 30)

Modes:
    off       — nothing gates (dangerous; background autonomy needs this
                OFF only when you fully trust the loop)
    critical  — destructive actions hold for a human "yes" (default).
                Trusted-origin background work (goals watchdog, recurring
                automations) may auto-approve LOW-RISK reads/writes
                (see AUTO_APPROVABLE) but NEVER destructive ones.
                Paired phones arrive as 'mobile:<device>' — an UNTRUSTED
                origin that can never claim the trusted path, so a phone
                can chat/read freely but destructive phone actions always
                hold on the dashboard.
    strict    — everything in CRITICAL_ACTIONS holds, no exceptions,
                regardless of origin.  Use when you want full oversight.
"""

import os
import time
import uuid
import logging
import threading

logger = logging.getLogger("Jarvis.Approvals")

CRITICAL_ACTIONS = frozenset({
    'send_email',
    'delete_file',
    'delete_screenshot',
    'kill_process',
    'clean_downloads',
    'dev_command',
    'dev_write',
    'dev_create',
    # Full autonomy still holds a human "yes" for: real-world effectors
    # (apps/browser/media/desktop, smart home, code execution, skills,
    # external tools, meetings, outgoing files) and irreversible memory
    # loss (forget).  Everything NOT listed here is read-only or
    # personal-organization and never gates — including the AUTO_APPROVABLE
    # set below, which additionally documents the trusted-origin path.
    'open_app', 'open_web', 'play_youtube', 'download_file',
    'computer_use', 'browser_use', 'desktop_task',
    'start_browser', 'agent_browse', 'agent_google', 'agent_amazon',
    'smarthome', 'media_play_pause', 'set_volume', 'set_mode',
    'capture_photo', 'analyze_photo',
    'sandbox_run', 'python_rpc', 'write_code', 'dev_build_full',
    'mobile_code', 'architect_app', 'auto_build_app',
    'run_skill', 'save_skill', 'delete_skill', 'call_tool',
    'forget_memory',
    'send_file', 'reply_to_email',
    'start_meeting', 'stop_meeting',
})

_SUMMARY_FIELDS = (
    'recipient', 'target', 'command', 'project', 'file', 'query', 'name'
)

# Actions a TRUSTED background origin (goals watchdog autostart,
# recurring automations, proactive follow-ups) may self-approve WITHOUT
# a human click.  Deliberately narrow: read-only queries, personal
# organization (todos/notes/reminders/calendar reads + event creation),
# memory writes, checkpoints, diagnostics, and non-destructive task
# scaffolding (complex_task/background_task/goal_* only CREATE and START
# tracked work — every step inside still passes through this same gate,
# so a task can never smuggle a destructive action past a human).
# Destructive actions (send_email, delete_*, kill_process,
# dev_command/dev_write/dev_create, clean_downloads) are NEVER here —
# they always hold for a human, even from trusted origins, even when
# JARVIS_APPROVALS=critical.
AUTO_APPROVABLE = frozenset({
    'search_web', 'read_webpage', 'check_email', 'read_email',
    'check_calendar', 'add_event', 'morning_briefing', 'news_headlines',
    'daily_summary', 'triage', 'schedule_insights',
    'todo_add', 'todo_done', 'todo_remove', 'todo_list', 'todo_clear',
    'note_save', 'note_list', 'note_remove',
    'set_reminder', 'list_reminders', 'cancel_reminder',
    'schedule_automation', 'list_automations',
    'recall', 'remember', 'save_memory', 'get_memory', 'memory_stats',
    'consult_archive', 'memory_about',
    'system_info', 'get_battery', 'get_weather', 'get_stock',
    'checkpoint', 'diagnose_file', 'list_skills', 'read_skill',
    'complex_task', 'background_task', 'task_status',
    'goal_set', 'goal_list', 'goal_done', 'goal_drop',
    'chat',
})

# Origin markers a command may carry (command['_origin']) to claim the
# trusted-background path.  Anything else → untrusted → full gating.
TRUSTED_ORIGINS = frozenset({
    'goals-watchdog', 'recurring', 'proactive',
})

# Destructive actions — NEVER auto-approvable, always require human
# confirmation regardless of mode or origin.  Subset of CRITICAL_ACTIONS.
DESTRUCTIVE_ACTIONS = frozenset({
    'delete_file', 'delete_screenshot', 'clean_downloads',
    'kill_process', 'lock_system',
    'send_email', 'reply_to_email', 'send_file',
    'forget_memory',
    'dev_command', 'dev_write', 'dev_create', 'dev_build_full',
    'sandbox_run', 'python_rpc', 'write_code',
    'computer_use', 'browser_use', 'desktop_task',
    'agent_browse', 'agent_google', 'agent_amazon',
    'smarthome', 'start_browser',
    'run_skill', 'save_skill', 'delete_skill', 'call_tool',
    'mobile_code', 'architect_app', 'auto_build_app',
    'start_meeting', 'stop_meeting',
    'capture_photo', 'analyze_photo',
})


def _summarize(command):
    action = command.get('action', '?')
    bits = [f"[{action}]"]
    for field in _SUMMARY_FIELDS:
        val = command.get(field)
        if val:
            text = str(val)
            bits.append(f"{field}={text[:80]}")
    if action == 'send_email':
        msg = str(command.get('message', ''))[:120]
        bits.append(f"message=\"{msg}\"")
    return " ".join(bits)


class ApprovalManager:
    def __init__(self, timeout=None, emit_fn=None):
        self.timeout = int(os.getenv('APPROVAL_TIMEOUT_S', '120') or 120) \
            if timeout is None else timeout
        self.emit_fn = emit_fn          # fn(event, payload) -> None
        self._pending = {}              # id -> state dict
        self._lock = threading.Lock()
        self._last_destructive = 0.0    # timestamp of last destructive approval
        try:
            self._destructive_cooldown = max(
                5, int(os.getenv('DESTRUCTIVE_COOLDOWN_S', '30') or 30))
        except (TypeError, ValueError):
            self._destructive_cooldown = 30

    # ------------------------------------------------------------------ #
    @staticmethod
    def enabled():
        return os.getenv('JARVIS_APPROVALS', 'critical').lower() != 'off'

    def requires(self, action, command=None):
        """
        True when *action* must hold for a human "yes".

        Trusted-origin background work (command['_origin'] in
        TRUSTED_ORIGINS) self-approves AUTO_APPROVABLE actions — but
        only in 'critical' mode.  'strict' mode gates everything;
        destructive actions always gate regardless of origin.
        """
        if not self.enabled():
            return False
        if action not in CRITICAL_ACTIONS:
            return False
        # Destructive actions ALWAYS require human approval
        if action in DESTRUCTIVE_ACTIONS:
            return True
        try:
            mode = os.getenv('JARVIS_APPROVALS', 'critical').lower()
            if mode == 'strict':
                return True
            origin = ''
            if isinstance(command, dict):
                origin = str(command.get('_origin', '') or '').lower()
            if origin in TRUSTED_ORIGINS:
                logger.info(f"AUTO-APPROVED [{action}] from trusted "
                            f"origin '{origin}' (critical mode)")
                return False
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------ #
    def request(self, command):
        """
        Block until a human resolves the request.

        Returns (approved: bool, note: str).
        Non-critical mode or unknown action → (True, '').
        """
        action = str(command.get('action', ''))
        if not self.requires(action, command):
            return True, ''

        # --- Jev pre-check (utils/jev.py) -----------------------------
        # Fast-deny an adversarial payload BEFORE it reaches the queue
        # (saves the full APPROVAL_TIMEOUT_S hold), and attach a
        # calibrated risk signal to the approval so the operator decides
        # in a glance.  Jev NEVER auto-approves — destructive actions
        # still hold for a human exactly as before.  Fail-open: any
        # error / missing key / timeout leaves today's behaviour intact.
        jev_info = None
        try:
            from utils import jev as _jev
            if action in _jev.GATED_ACTIONS and _jev.enabled():
                jev_info = _jev.precheck(command)
                if jev_info is not None:
                    jev_info['label'] = _jev.risk_label(jev_info)
                if _jev.inject_deny(jev_info):
                    p = jev_info.get('injection')
                    logger.warning(f"JEV fast-deny [{action}] "
                                   f"injection={p:.2f}")
                    return False, (
                        f"blocked by Jev safety gate (prompt-injection "
                        f"signal, confidence {p:.2f})")
        except Exception as e:
            logger.debug(f"jev precheck skipped: {e}")
            jev_info = None

        # Destructive cooldown — don't stack rapid-fire destructive requests
        if action in DESTRUCTIVE_ACTIONS:
            now = time.time()
            elapsed = now - self._last_destructive
            remaining = self._destructive_cooldown - elapsed
            if remaining > 0:
                logger.warning(
                    f"DESTRUCTIVE COOLDOWN: {action} waiting "
                    f"{remaining:.0f}s since last destructive approval")
                time.sleep(remaining)

        approval_id = uuid.uuid4().hex[:10]
        summary = _summarize(command)
        event = threading.Event()
        state = {'decision': None, 'event': event,
                 'summary': summary, 'created': time.time(),
                 'action': action}

        with self._lock:
            # Bound pending set so a dead dashboard can't leak memory
            if len(self._pending) > 50:
                oldest = min(self._pending,
                             key=lambda k: self._pending[k]['created'])
                stale = self._pending.pop(oldest)
                stale['decision'] = False
                stale['event'].set()
            self._pending[approval_id] = state

        logger.info(f"HOLD ⏸  {summary} (approval {approval_id}, "
                    f"waiting ≤{self.timeout}s)")
        if self.emit_fn:
            try:
                self.emit_fn('approval_request', {
                    'id': approval_id,
                    'action': action,
                    'summary': summary,
                    'timeout': self.timeout,
                    'destructive': action in DESTRUCTIVE_ACTIONS,
                    'jev': jev_info,   # calibrated risk, or None (fail-open)
                })
            except Exception as e:
                logger.warning(f"approval emit failed: {e}")

        got = event.wait(self.timeout)

        with self._lock:
            self._pending.pop(approval_id, None)

        if not got:
            logger.warning(f"DENIED (timeout) {summary}")
            return False, "timed out waiting for approval"
        if state['decision'] is True:
            logger.info(f"APPROVED ✓ {summary}")
            if action in DESTRUCTIVE_ACTIONS:
                self._last_destructive = time.time()
            return True, ''
        logger.info(f"DENIED ✗ {summary}")
        return False, "denied by operator"

    # ------------------------------------------------------------------ #
    def resolve(self, approval_id, approved):
        """Resolve a pending request from a UI/channel callback."""
        with self._lock:
            state = self._pending.get(str(approval_id))
            if not state or state['decision'] is not None:
                return False
            state['decision'] = bool(approved)
        state['event'].set()
        return True

    def pending_count(self):
        with self._lock:
            return len([s for s in self._pending.values()
                        if s['decision'] is None])


# Shared manager (server wires emit_fn at startup)
_shared = None


def get_manager():
    global _shared
    if _shared is None:
        _shared = ApprovalManager()
    return _shared
