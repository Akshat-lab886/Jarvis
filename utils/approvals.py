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
    JARVIS_APPROVALS      off | critical     (default critical)
    APPROVAL_TIMEOUT_S    seconds to wait    (default 120)
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
})

_SUMMARY_FIELDS = (
    'recipient', 'target', 'command', 'project', 'file', 'query', 'name'
)


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

    # ------------------------------------------------------------------ #
    @staticmethod
    def enabled():
        return os.getenv('JARVIS_APPROVALS', 'critical').lower() != 'off'

    def requires(self, action):
        return self.enabled() and action in CRITICAL_ACTIONS

    # ------------------------------------------------------------------ #
    def request(self, command):
        """
        Block until a human resolves the request.

        Returns (approved: bool, note: str).
        Non-critical mode or unknown action → (True, '').
        """
        action = str(command.get('action', ''))
        if not self.requires(action):
            return True, ''

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
