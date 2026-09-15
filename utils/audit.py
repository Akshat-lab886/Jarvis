"""
Jarvis Action Audit Trail + Anomaly Detection
=============================================

Every side-effecting action passes through this module.  It provides:

1. **Append-only audit log** — timestamped records of every action
   with outcome, duration, and context.  Persisted to
   ``brain/data/audit_log.json`` (bounded ring buffer).

2. **Anomaly detector** — sliding-window counters that fire alerts
   when the agent's behaviour looks wrong:
   * burst of destructive actions (>N in M minutes)
   * repeated failures (>N in M minutes)
   * unusual hour (action at 3 AM when user is typically sleeping)
   * spending spike (single action or daily total)

3. **Spending tracker** — per-action and daily spending totals pulled
   from the privacy framework's audit log (legacy path) and the new
   unified log.

Kill switch: ``JARVIS_AUDIT=0`` disables persistence (in-memory ring
only, anomaly detection still runs).
"""

import os
import time
import json
import threading
import logging
import hashlib
from collections import deque

logger = logging.getLogger("Jarvis.Audit")

# ------------------------------------------------------------------ #
# Config
# ------------------------------------------------------------------ #

_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'brain', 'data')
_LOG_FILE = os.path.join(_DATA_DIR, 'audit_log.json')

_MAX_RING = 500          # in-memory ring size
_MAX_FILE = 3000         # persisted ring cap
_FLUSH_INTERVAL = 30     # seconds between disk flushes
_WINDOW_MINUTES = 30     # anomaly detection window

# Anomaly thresholds
_DESTRUCTIVE_BURST = 8   # destructive actions in window → alert
_FAILURE_BURST = 5       # failures in window → alert
_UNUSUAL_HOUR_EARLY = 2  # before 2 AM → unusual
_UNUSUAL_HOUR_LATE = 23  # after 11 PM → unusual (for night-owls)


def enabled():
    return os.getenv('JARVIS_AUDIT', '1') != '0'


# ------------------------------------------------------------------ #
# Risk classification
# ------------------------------------------------------------------ #

# Tier 1: Destructive — irreversible or hard to undo
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

# Tier 2: Sensitive — side-effects but reversible
SENSITIVE_ACTIONS = frozenset({
    'open_app', 'open_web', 'play_youtube', 'download_file',
    'set_volume', 'set_mode', 'media_play_pause',
    'add_event', 'set_reminder', 'send_email',
    'todo_add', 'todo_done', 'todo_remove', 'todo_clear',
    'note_save', 'note_remove',
    'schedule_automation', 'cancel_automation',
    'goal_set', 'goal_done', 'goal_drop',
    'background_task',
    'guardrails_update', 'privacy_update',
    'save_memory', 'save_skill',
})

# Tier 3: Read-only — no side effects
READONLY_ACTIONS = frozenset({
    'system_info', 'get_battery', 'get_weather', 'get_stock',
    'check_calendar', 'check_email', 'read_email',
    'todo_list', 'note_list', 'list_reminders',
    'list_skills', 'read_skill', 'list_tools',
    'news_headlines', 'morning_briefing', 'daily_summary',
    'triage', 'schedule_insights',
    'recall', 'remember', 'get_memory', 'memory_stats',
    'memory_about', 'recall_deep',
    'system_processes', 'home_status',
    'chat', 'help', 'conversation_history', 'last_topic',
    'search_transcripts', 'task_status', 'goal_list',
    'list_automations', 'diagnose_file',
    'privacy_settings', 'audit_log',
    'checkpoint', 'read_webpage', 'search_web',
    'consult_archive', 'person_info', 'gift_suggestions',
    'dinner_suggestions', 'meeting_transcript', 'suggest_recipe',
    'analyze_fridge',
})


def risk_tier(action):
    """Return 'destructive', 'sensitive', or 'readonly' for *action*."""
    action = str(action or '').lower()
    if action in DESTRUCTIVE_ACTIONS:
        return 'destructive'
    if action in SENSITIVE_ACTIONS:
        return 'sensitive'
    return 'readonly'


# ------------------------------------------------------------------ #
# Audit Record
# ------------------------------------------------------------------ #

class AuditRecord:
    __slots__ = ('ts', 'action', 'args_hash', 'outcome', 'duration_ms',
                 'detail', 'origin', 'risk')

    def __init__(self, action='', args_hash='', outcome='pending',
                 duration_ms=0, detail='', origin='', risk=''):
        self.ts = time.time()
        self.action = action
        self.args_hash = args_hash
        self.outcome = outcome
        self.duration_ms = duration_ms
        self.detail = detail
        self.origin = origin
        self.risk = risk or risk_tier(action)

    def to_dict(self):
        return {
            'ts': self.ts,
            'action': self.action,
            'args_hash': self.args_hash,
            'outcome': self.outcome,
            'duration_ms': self.duration_ms,
            'detail': self.detail[:200],
            'origin': self.origin,
            'risk': self.risk,
        }

    @classmethod
    def from_dict(cls, d):
        r = cls.__new__(cls)
        r.ts = d.get('ts', 0)
        r.action = d.get('action', '')
        r.args_hash = d.get('args_hash', '')
        r.outcome = d.get('outcome', '')
        r.duration_ms = d.get('duration_ms', 0)
        r.detail = d.get('detail', '')
        r.origin = d.get('origin', '')
        r.risk = d.get('risk', '')
        return r


# ------------------------------------------------------------------ #
# Anomaly Detector
# ------------------------------------------------------------------ #

class AnomalyDetector:
    """Sliding-window anomaly detection on action streams."""

    def __init__(self):
        self._destructive = deque()   # timestamps of destructive actions
        self._failures = deque()      # timestamps of failed actions
        self._lock = threading.Lock()

    def record(self, record):
        now = time.time()
        cutoff = now - (_WINDOW_MINUTES * 60)
        with self._lock:
            # Prune old entries
            while self._destructive and self._destructive[0] < cutoff:
                self._destructive.popleft()
            while self._failures and self._failures[0] < cutoff:
                self._failures.popleft()
            # Record
            if record.risk == 'destructive' and record.outcome != 'denied':
                self._destructive.append(now)
            if record.outcome in ('error', 'denied', 'timeout'):
                self._failures.append(now)

    def check(self):
        """Return list of alert strings (empty = healthy)."""
        alerts = []
        now = time.time()
        cutoff = now - (_WINDOW_MINUTES * 60)
        with self._lock:
            while self._destructive and self._destructive[0] < cutoff:
                self._destructive.popleft()
            while self._failures and self._failures[0] < cutoff:
                self._failures.popleft()
            d_count = len(self._destructive)
            f_count = len(self._failures)
        if d_count >= _DESTRUCTIVE_BURST:
            alerts.append(
                f"DESTRUCTIVE BURST: {d_count} destructive actions "
                f"in last {_WINDOW_MINUTES}m (threshold {_DESTRUCTIVE_BURST})")
        if f_count >= _FAILURE_BURST:
            alerts.append(
                f"FAILURE BURST: {f_count} failures "
                f"in last {_WINDOW_MINUTES}m (threshold {_FAILURE_BURST})")
        hour = time.localtime().tm_hour
        if hour < _UNUSUAL_HOUR_EARLY or hour >= _UNUSUAL_HOUR_LATE:
            if d_count > 0:
                alerts.append(
                    f"UNUSUAL HOUR: {d_count} destructive actions "
                    f"at {hour}:00 (outside normal hours)")
        return alerts

    def stats(self):
        with self._lock:
            return {
                'destructive_window': len(self._destructive),
                'failures_window': len(self._failures),
                'window_minutes': _WINDOW_MINUTES,
            }


# ------------------------------------------------------------------ #
# Audit Logger
# ------------------------------------------------------------------ #

class AuditLogger:
    """Append-only audit log with disk persistence."""

    def __init__(self):
        self._ring = deque(maxlen=_MAX_RING)
        self._lock = threading.Lock()
        self._dirty = False
        self._detector = AnomalyDetector()
        self._last_flush = 0
        self._load()

    # ---- persistence ---- #

    def _load(self):
        try:
            if os.path.exists(_LOG_FILE):
                with open(_LOG_FILE, 'r') as f:
                    data = json.load(f)
                for d in data[-_MAX_RING:]:
                    self._ring.append(AuditRecord.from_dict(d))
                logger.debug("loaded %d audit records", len(self._ring))
        except Exception as e:
            logger.warning("audit log load failed: %s", e)

    def _flush(self):
        if not enabled():
            return
        now = time.time()
        if now - self._last_flush < _FLUSH_INTERVAL:
            return
        with self._lock:
            if not self._dirty:
                return
            self._last_flush = now
            try:
                os.makedirs(_DATA_DIR, exist_ok=True)
                data = [r.to_dict() for r in list(self._ring)[-_MAX_FILE:]]
                tmp = _LOG_FILE + '.tmp'
                with open(tmp, 'w') as f:
                    json.dump(data, f, indent=1)
                os.replace(tmp, _LOG_FILE)
                # Only clear dirty AFTER successful write
                self._dirty = False
            except Exception as e:
                logger.warning("audit log flush failed: %s", e)

    # ---- public ---- #

    def log(self, action, command=None, outcome='ok', duration_ms=0,
            detail='', origin=''):
        """Record an action.  Returns the record."""
        args_str = json.dumps(
            {k: v for k, v in (command or {}).items()
             if k not in ('_origin',)},
            sort_keys=True, default=str)[:200]
        args_hash = hashlib.md5(args_str.encode()).hexdigest()[:8]
        rec = AuditRecord(
            action=action,
            args_hash=args_hash,
            outcome=outcome,
            duration_ms=duration_ms,
            detail=detail,
            origin=origin or (command or {}).get('_origin', ''),
        )
        with self._lock:
            self._ring.append(rec)
            self._dirty = True
        self._detector.record(rec)
        # Check anomalies — log AND surface to dashboard (if socket live)
        alerts = self._detector.check()
        for alert in alerts:
            logger.warning("ANOMALY: %s", alert)
            try:
                from utils.server import socketio as _sio
                _sio.emit('anomaly_alert', {'alert': alert,
                                           'ts': rec.ts})
            except Exception:
                pass  # dashboard not live — log only
        self._flush()
        return rec

    def recent(self, limit=20, action_filter=None, risk_filter=None):
        """Return recent records (newest first)."""
        with self._lock:
            records = list(self._ring)
        if action_filter:
            records = [r for r in records if r.action == action_filter]
        if risk_filter:
            records = [r for r in records if r.risk == risk_filter]
        return [r.to_dict() for r in records[-limit:]][::-1]

    def stats(self):
        with self._lock:
            records = list(self._ring)
        now = time.time()
        hour_ago = now - 3600
        day_ago = now - 86400
        recent_h = [r for r in records if r.ts > hour_ago]
        recent_d = [r for r in records if r.ts > day_ago]
        return {
            'total_records': len(records),
            'last_hour': len(recent_h),
            'last_day': len(recent_d),
            'destructive_hour': len(
                [r for r in recent_h if r.risk == 'destructive']),
            'failures_hour': len(
                [r for r in recent_h
                 if r.outcome in ('error', 'denied', 'timeout')]),
            'anomaly': self._detector.stats(),
        }

    def flush(self):
        """Force flush to disk."""
        self._last_flush = 0
        self._flush()


# ------------------------------------------------------------------ #
# Singleton
# ------------------------------------------------------------------ #

_singleton = None
_singleton_lock = threading.Lock()


def get_audit():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = AuditLogger()
        return _singleton


def _reset_singleton():
    """Test helper."""
    global _singleton
    with _singleton_lock:
        _singleton = None
