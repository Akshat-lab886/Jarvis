"""
Jarvis Privacy-First Framework

Provides granular trust controls for all AI actions:
- Permission model: every action type has an approval level
- Spending limits: configurable caps for autonomous purchases
- Data scope: what data the AI can access and analyze
- Action preview: preview before execute for sensitive operations
- Audit log: tracks every action taken with timestamps
- Local processing preference: option to keep data on-device
"""

import os
import json
import datetime
import threading
import logging

logger = logging.getLogger("Jarvis.Privacy")


# ---- Action categories and their sensitivity levels ----
ACTION_CATEGORIES = {
    # Level 1: Always allowed (read-only, no side effects)
    'read': {
        'description': 'Read-only operations (weather, time, system info)',
        'default_trust': 'always',
        'actions': ['system_info', 'get_weather', 'get_battery', 'check_calendar',
                     'check_email', 'get_stock', 'news_headlines', 'system_processes',
                     'home_status', 'todo_list', 'note_list', 'list_reminders',
                     'daily_summary', 'help']
    },
    # Level 2: Low risk (minor actions, easily reversible)
    'minor': {
        'description': 'Minor actions (set volume, open apps, add todos)',
        'default_trust': 'always',
        'actions': ['set_volume', 'open_app', 'open_web', 'play_youtube',
                     'media_play_pause', 'todo_add', 'todo_done', 'todo_remove',
                     'note_save', 'note_remove', 'smarthome', 'set_mode']
    },
    # Level 3: Medium risk (sending messages, creating events)
    'medium': {
        'description': 'Medium risk (send email, add calendar event, reminders)',
        'default_trust': 'ask',
        'actions': ['send_email', 'add_event', 'set_reminder', 'capture_photo',
                     'read_webpage', 'search_web', 'read_email']
    },
    # Level 4: High risk (deleting files, killing processes, system commands)
    'high': {
        'description': 'High risk (delete files, kill processes, run commands)',
        'default_trust': 'ask',
        'actions': ['delete_file', 'delete_screenshot', 'kill_process',
                     'lock_system', 'dev_command', 'desktop_task', 'clean_downloads']
    },
    # Level 5: Critical (spending money, autonomous actions)
    'critical': {
        'description': 'Critical (financial transactions, autonomous web actions)',
        'default_trust': 'deny',
        'actions': ['agent_amazon', 'agent_browse', 'send_money',
                     'purchase', 'book_flight', 'order_food']
    }
}

# Trust levels
TRUST_LEVELS = {
    'always': 'Always allow without asking',
    'ask': 'Ask for approval before executing',
    'deny': 'Block entirely unless explicitly overridden',
    'preview': 'Show preview of what will happen, then ask'
}


class PrivacyFramework:
    """
    Manages trust controls, permissions, and action audit logging.

    All settings are persisted to privacy_settings.json.
    """

    def __init__(self):
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.settings_file = os.path.join(self.base_dir, 'privacy_settings.json')
        self.audit_file = os.path.join(self.base_dir, 'audit_log.json')
        self._lock = threading.RLock()

        # Default settings
        self.settings = {
            # Trust levels per action category
            'trust_levels': {
                cat: info['default_trust']
                for cat, info in ACTION_CATEGORIES.items()
            },

            # Spending limits
            'spending_limit_per_transaction': 1500,  # in user's currency
            'spending_limit_daily': 5000,
            'spending_currency': 'INR',

            # Data scope
            'allow_analyze_calendar': True,
            'allow_analyze_email': True,
            'allow_analyze_files': True,
            'allow_web_search': True,
            'allow_store_conversations': True,

            # Processing preferences
            'prefer_local_processing': False,  # Use local LLM when available
            'auto_approve_minor': True,         # Auto-approve minor actions
            'voice_confirmation': False,         # Require voice confirmation

            # Notification preferences
            'notify_on_critical_action': True,
            'notify_on_email_send': True,
            'notify_on_reminder_fire': True,

            # Session
            'session_trust_boost': False,  # Temporary trust increase for current session
        }

        # Approved action templates (user pre-approves certain actions)
        self.approved_actions = []

        # Pending approvals queue
        self.pending_approvals = {}

        self._load_settings()
        self._load_audit_log()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _backup_corrupt(self, path, exc):
        """Rename a corrupt JSON file aside instead of silently resetting."""
        try:
            import time as _t
            backup = f"{path}.corrupt.{int(_t.time())}"
            os.replace(path, backup)
            logger.warning("Backed up corrupt %s -> %s (%s)",
                           path, backup, exc)
        except OSError as e2:
            logger.warning("Could not back up corrupt %s: %s", path, e2)

    def _load_settings(self):
        try:
            if os.path.exists(self.settings_file):
                try:
                    with open(self.settings_file, 'r') as f:
                        saved = json.load(f)
                except (json.JSONDecodeError, ValueError) as je:
                    self._backup_corrupt(self.settings_file, je)
                    saved = {}
                # Merge with defaults (new keys get default values)
                for key, value in saved.items():
                    if key in self.settings:
                        self.settings[key] = value
        except Exception as e:
            logger.warning(f"Failed to load privacy settings: {e}")

    def _save_settings(self):
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(self.settings, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save privacy settings: {e}")

    def _load_audit_log(self):
        """Load recent audit log entries."""
        self._audit_log = []
        try:
            if os.path.exists(self.audit_file):
                try:
                    with open(self.audit_file, 'r') as f:
                        data = json.load(f)
                except (json.JSONDecodeError, ValueError) as je:
                    self._backup_corrupt(self.audit_file, je)
                    data = []
                if isinstance(data, list):
                    # Keep last 1000 entries
                    self._audit_log = data[-1000:]
        except Exception as e:
            logger.warning(f"Failed to load audit log: {e}")

    def _save_audit_log(self):
        try:
            with open(self.audit_file, 'w') as f:
                json.dump(self._audit_log[-1000:], f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save audit log: {e}")

    # ------------------------------------------------------------------ #
    # Permission Checking
    # ------------------------------------------------------------------ #
    def get_trust_level(self, action):
        """Get the trust level for a specific action."""
        action_lower = (action or '').lower()

        for cat_name, cat_info in ACTION_CATEGORIES.items():
            if action_lower in cat_info['actions']:
                return self.settings['trust_levels'].get(cat_name, cat_info['default_trust'])

        # Unknown action — default to 'ask'
        return 'ask'

    def can_execute(self, action, context=None):
        """
        Check if an action is allowed.
        Returns: ('allow', None) or ('ask', approval_id) or ('deny', reason)
        """
        trust = self.get_trust_level(action)

        if trust == 'always':
            return ('allow', None)

        if trust == 'deny':
            return ('deny', f"Action '{action}' is denied by privacy settings.")

        if trust in ('ask', 'preview'):
            # Check if this action has been pre-approved
            if self._is_pre_approved(action, context):
                return ('allow', None)

            # Create a pending approval
            approval_id = self._create_approval(action, context)
            return ('ask', approval_id)

        return ('allow', None)

    def _prune_templates(self):
        """Drop expired pre-approval templates and cap list growth."""
        now = datetime.datetime.now()
        kept = []
        for t in self.approved_actions:
            exp = t.get('expires_at')
            if exp:
                try:
                    if datetime.datetime.fromisoformat(exp) <= now:
                        continue  # expired — drop
                except (ValueError, TypeError):
                    continue  # malformed expiry — drop
            kept.append(t)
        # Cap at 100 newest to bound growth
        self.approved_actions = kept[-100:]

    def _prune_pending(self):
        """Drop settled/stale pending approvals and cap dict growth."""
        now = datetime.datetime.now()
        for aid in [k for k, v in self.pending_approvals.items()
                    if not isinstance(v, dict)
                    or v.get('status') in ('approved', 'denied')]:
            del self.pending_approvals[aid]
        # Cap at 100 newest pending
        if len(self.pending_approvals) > 100:
            by_created = sorted(
                self.pending_approvals.items(),
                key=lambda kv: kv[1].get('created', ''))
            for aid, _ in by_created[:-100]:
                del self.pending_approvals[aid]

    def _is_pre_approved(self, action, context=None):
        """Check if an action matches a non-expired pre-approved template."""
        self._prune_templates()
        now = datetime.datetime.now()
        for template in self.approved_actions:
            exp = template.get('expires_at')
            if exp:
                try:
                    if datetime.datetime.fromisoformat(exp) <= now:
                        continue  # expired
                except (ValueError, TypeError):
                    continue  # malformed — treat as expired
            if template.get('action') == action:
                # Check any context constraints
                constraints = template.get('constraints', {})
                if constraints:
                    # For spending actions, check amount
                    if 'max_amount' in constraints and context:
                        amount = context.get('amount', 0)
                        if amount > constraints['max_amount']:
                            return False
                    # For email, check recipient domain
                    if 'allowed_recipients' in constraints and context:
                        recipient = context.get('recipient', '')
                        if recipient not in constraints['allowed_recipients']:
                            return False
                return True
        return False

    def _create_approval(self, action, context=None):
        """Create a pending approval request."""
        import uuid
        self._prune_pending()
        approval_id = str(uuid.uuid4())[:8]

        self.pending_approvals[approval_id] = {
            'id': approval_id,
            'action': action,
            'context': context or {},
            'created': datetime.datetime.now().isoformat(),
            'status': 'pending'
        }

        # Log it
        self.log_action(action, context, 'pending_approval',
                        details=f"Awaiting user approval")

        return approval_id

    def approve(self, approval_id):
        """Approve a pending action."""
        with self._lock:
            if approval_id in self.pending_approvals:
                self.pending_approvals[approval_id]['status'] = 'approved'
                self.pending_approvals[approval_id]['approved_at'] = \
                    datetime.datetime.now().isoformat()
                return True
        return False

    def deny(self, approval_id):
        """Deny a pending action."""
        with self._lock:
            if approval_id in self.pending_approvals:
                self.pending_approvals[approval_id]['status'] = 'denied'
                self.pending_approvals[approval_id]['denied_at'] = \
                    datetime.datetime.now().isoformat()
                return True
        return False

    def get_pending_approvals(self):
        """Get all pending approval requests."""
        with self._lock:
            return {
                k: dict(v) for k, v in self.pending_approvals.items()
                if v.get('status') == 'pending'
            }

    # ------------------------------------------------------------------ #
    # Spending Controls
    # ------------------------------------------------------------------ #
    def check_spending(self, amount, description=""):
        """
        Check if a spending action is within limits.
        Returns: (allowed: bool, message: str)
        """
        limit = self.settings.get('spending_limit_per_transaction', 1500)
        daily_limit = self.settings.get('spending_limit_daily', 5000)
        currency = self.settings.get('spending_currency', 'INR')

        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return True, "Amount not parseable — proceeding."

        if amount > limit:
            return False, (
                f"Spending of {currency} {amount:.0f} exceeds per-transaction "
                f"limit of {currency} {limit}. Approval required."
            )

        # Check daily total
        daily_total = self._get_daily_spending()
        if daily_total + amount > daily_limit:
            return False, (
                f"Daily spending limit of {currency} {daily_limit} would be exceeded "
                f"(current: {currency} {daily_total:.0f}). Approval required."
            )

        return True, "Within spending limits."

    def _get_daily_spending(self):
        """Calculate today's total spending from audit log."""
        today = datetime.datetime.now().strftime('%Y-%m-%d')
        total = 0
        for entry in self._audit_log:
            if (entry.get('date', '').startswith(today) and
                    entry.get('action') in ('purchase', 'agent_amazon', 'send_money')):
                total += entry.get('amount', 0)
        return total

    # ------------------------------------------------------------------ #
    # Audit Logging
    # ------------------------------------------------------------------ #
    def log_action(self, action, context=None, status='completed', details=""):
        """Log an action to the audit trail."""
        entry = {
            'date': datetime.datetime.now().isoformat(timespec='seconds'),
            'action': action,
            'context': context or {},
            'status': status,
            'details': details
        }

        with self._lock:
            self._audit_log.append(entry)
            if len(self._audit_log) > 2000:
                self._audit_log = self._audit_log[-1000:]

        # Save periodically (every 10 entries)
        if len(self._audit_log) % 10 == 0:
            self._save_audit_log()

    def get_audit_log(self, limit=50, action_filter=None):
        """Get recent audit log entries."""
        with self._lock:
            entries = list(self._audit_log)

        if action_filter:
            entries = [e for e in entries if e.get('action') == action_filter]

        return entries[-limit:]

    # ------------------------------------------------------------------ #
    # Settings Management
    # ------------------------------------------------------------------ #
    def update_setting(self, key, value):
        """Update a privacy setting."""
        if key in self.settings:
            old_value = self.settings[key]
            self.settings[key] = value
            self._save_settings()
            self.log_action('setting_changed', {'key': key, 'old': old_value, 'new': value})
            return f"Updated {key}: {old_value} → {value}"
        return f"Unknown setting: {key}"

    def update_trust(self, category, level):
        """Set the trust level for one action category (validated)."""
        if category not in self.settings.get('trust_levels', {}):
            return f"Unknown action category: {category}"
        if level not in TRUST_LEVELS:
            return f"Unknown trust level: {level}"
        old = self.settings['trust_levels'][category]
        self.settings['trust_levels'][category] = level
        self._save_settings()
        self.log_action('trust_changed', {'category': category,
                                          'old': old, 'new': level})
        return (f"{category.upper()} trust → {level} "
                f"({TRUST_LEVELS[level]})")

    def get_setting(self, key):
        """Get a privacy setting."""
        return self.settings.get(key)

    def get_all_settings(self):
        """Get all settings (snapshot)."""
        with self._lock:
            return dict(self.settings)

    def approve_action_template(self, action, constraints=None, expires_hours=24):
        """Pre-approve an action for a limited time."""
        template = {
            'action': action,
            'constraints': constraints or {},
            'approved_at': datetime.datetime.now().isoformat(),
            'expires_at': (
                datetime.datetime.now() + datetime.timedelta(hours=expires_hours)
            ).isoformat()
        }
        self.approved_actions.append(template)
        self.log_action('template_approved', {'action': action, 'expires': expires_hours})
        return f"Action '{action}' pre-approved for {expires_hours} hours."

    def get_trust_summary(self):
        """Get a human-readable summary of current trust settings."""
        lines = ["Privacy Trust Summary:"]
        for cat_name, info in ACTION_CATEGORIES.items():
            trust = self.settings['trust_levels'].get(cat_name, 'unknown')
            trust_label = TRUST_LEVELS.get(trust, trust)
            lines.append(f"  [{cat_name.upper()}] ({info['description']})")
            lines.append(f"    Trust: {trust} — {trust_label}")

        lines.append(f"\nSpending Limits:")
        lines.append(f"  Per transaction: {self.settings.get('spending_currency', 'INR')} "
                      f"{self.settings.get('spending_limit_per_transaction', 0)}")
        lines.append(f"  Daily total: {self.settings.get('spending_currency', 'INR')} "
                      f"{self.settings.get('spending_limit_daily', 0)}")

        lines.append(f"\nData Scope:")
        lines.append(f"  Calendar analysis: {'✅' if self.settings.get('allow_analyze_calendar') else '❌'}")
        lines.append(f"  Email analysis: {'✅' if self.settings.get('allow_analyze_email') else '❌'}")
        lines.append(f"  File analysis: {'✅' if self.settings.get('allow_analyze_files') else '❌'}")
        lines.append(f"  Web search: {'✅' if self.settings.get('allow_web_search') else '❌'}")
        lines.append(f"  Store conversations: {'✅' if self.settings.get('allow_store_conversations') else '❌'}")

        return "\n".join(lines)

    def settings_json(self):
        """Dashboard-friendly snapshot."""
        return self.get_all_settings()
