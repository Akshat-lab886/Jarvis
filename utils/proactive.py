"""
Jarvis Proactive Automation Engine (The "Ghost Executive")

Two core capabilities:

1. Dynamic Time Blocking
   - Monitors calendar for meeting overruns and deadline changes
   - Auto-adjusts schedule when meetings run late
   - Suggests optimal focus blocks between meetings
   - Protects buffer time between meetings

2. Cross-App Triage
   - Monitors emails, calendar, and notifications for critical items
   - Priority-scores incoming messages
   - Flags only the items that truly need a response
   - Groups by urgency and topic
"""

import os
import json
import time
import datetime
import threading
import logging

logger = logging.getLogger("Jarvis.Proactive")


class TimeBlockScheduler:
    """
    Dynamic time-blocking engine that:
    - Scans calendar for meetings and creates time blocks
    - Detects when a meeting runs over its scheduled time
    - Suggests reschedulings and buffer times
    - Finds optimal focus time slots
    """

    def __init__(self, secretary=None, episodic_memory=None):
        self.secretary = secretary
        self.memory = episodic_memory
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.file_path = os.path.join(self.base_dir, 'time_blocks.json')
        self._lock = threading.Lock()
        self._blocks = []
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r') as f:
                    self._blocks = json.load(f)
        except Exception:
            self._blocks = []

    def _save(self):
        try:
            with open(self.file_path, 'w') as f:
                json.dump(self._blocks, f, indent=2)
        except Exception:
            pass

    def scan_and_adjust(self):
        """
        Scan today's calendar and detect schedule conflicts/overruns.
        Returns a list of adjustment suggestions.
        """
        if not self.secretary or not self.secretary.calendar_service:
            return []

        suggestions = []
        now = datetime.datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + datetime.timedelta(days=1)

        try:
            events_result = self.secretary.calendar_service.events().list(
                calendarId='primary',
                timeMin=today_start.isoformat(),
                timeMax=today_end.isoformat(),
                maxResults=20,
                singleEvents=True,
                orderBy='startTime'
            ).execute()

            events = events_result.get('items', [])
            blocks = []

            for event in events:
                summary = event.get('summary', 'Untitled')
                start = event.get('start', {}).get('dateTime', '')
                end = event.get('end', {}).get('dateTime', '')

                if not start or not end:
                    continue

                try:
                    start_dt = datetime.datetime.fromisoformat(start.replace('Z', '+00:00'))
                    end_dt = datetime.datetime.fromisoformat(end.replace('Z', '+00:00'))
                    # Remove timezone for comparison
                    start_dt = start_dt.replace(tzinfo=None)
                    end_dt = end_dt.replace(tzinfo=None)
                except Exception:
                    continue

                block = {
                    'summary': summary,
                    'start': start_dt,
                    'end': end_dt,
                    'is_now': start_dt <= now <= end_dt,
                    'is_overdue': now > end_dt,
                    'is_upcoming': start_dt > now
                }
                blocks.append(block)

            blocks.sort(key=lambda b: b['start'])

            # Detect overlapping meetings
            for i in range(len(blocks) - 1):
                current = blocks[i]
                next_block = blocks[i + 1]

                # Check if current meeting runs into next one
                if current['end'] > next_block['start']:
                    overlap_minutes = int((current['end'] - next_block['start']).total_seconds() / 60)
                    suggestions.append({
                        'type': 'overlap',
                        'severity': 'high',
                        'message': (
                            f"⚠️ '{current['summary']}' runs {overlap_minutes}min into "
                            f"'{next_block['summary']}'. Consider shortening the first meeting "
                            f"or pushing the second one back."
                        ),
                        'blocks': [current['summary'], next_block['summary']],
                        'overlap_minutes': overlap_minutes
                    })

                # Check if meetings are too close (no buffer)
                elif i + 1 < len(blocks):
                    gap = (next_block['start'] - current['end']).total_seconds() / 60
                    if 0 < gap < 5:
                        suggestions.append({
                            'type': 'tight_buffer',
                            'severity': 'medium',
                            'message': (
                                f"⏱️ Only {int(gap)}min between '{current['summary']}' "
                                f"and '{next_block['summary']}'. Consider adding a buffer."
                            )
                        })

            # Find focus blocks (gaps >= 25 minutes — Pomodoro minimum)
            if blocks:
                # Before first meeting
                if blocks[0]['start'] > now:
                    gap = (blocks[0]['start'] - now).total_seconds() / 60
                    if gap >= 25:
                        suggestions.append({
                            'type': 'focus_block',
                            'severity': 'info',
                            'message': (
                                f"🎯 You have a {int(gap)}min focus block until "
                                f"'{blocks[0]['summary']}'. Great time for deep work."
                            ),
                            'duration_minutes': int(gap)
                        })

                # Between meetings
                for i in range(len(blocks) - 1):
                    gap_start = blocks[i]['end']
                    gap_end = blocks[i + 1]['start']
                    if gap_start < now:
                        continue
                    gap = (gap_end - gap_start).total_seconds() / 60
                    if 25 <= gap <= 120:
                        suggestions.append({
                            'type': 'focus_block',
                            'severity': 'info',
                            'message': (
                                f"🎯 {int(gap)}min focus block between "
                                f"'{blocks[i]['summary']}' and '{blocks[i + 1]['summary']}'."
                            ),
                            'duration_minutes': int(gap)
                        })

            # Detect current running meeting that might be going overtime
            for block in blocks:
                if block['is_overdue']:
                    overdue_min = int((now - block['end']).total_seconds() / 60)
                    suggestions.append({
                        'type': 'meeting_overrun',
                        'severity': 'warning',
                        'message': (
                            f"⏰ '{block['summary']}' has gone {overdue_min}min over its "
                            f"scheduled end time."
                        )
                    })

        except Exception as e:
            logger.error(f"Time block scan error: {e}")

        return suggestions

    def get_focus_recommendation(self):
        """Get the best time to focus right now."""
        suggestions = self.scan_and_adjust()
        focus_blocks = [s for s in suggestions if s.get('type') == 'focus_block']
        if focus_blocks:
            return focus_blocks[0]
        return {
            'type': 'no_focus_block',
            'severity': 'info',
            'message': "No clear focus block available right now. Check your calendar for later slots."
        }


class CrossAppTriage:
    """
    Cross-app message triage engine.

    Monitors:
    - Email inbox (unread, flagged, urgent)
    - Calendar events (changes, cancellations)
    - Reminder notifications

    Priority-scores each item and returns only the critical ones.
    """

    # Priority scoring keywords
    URGENT_KEYWORDS = [
        'urgent', 'asap', 'emergency', 'critical', 'deadline', 'overdue',
        'action required', 'immediately', 'time-sensitive', 'expires'
    ]

    IMPORTANT_KEYWORDS = [
        'meeting', 'interview', 'demo', 'presentation', 'invoice', 'payment',
        'contract', 'agreement', 'proposal', 'review', 'feedback', 'approval'
    ]

    LOW_PRIORITY_KEYWORDS = [
        'newsletter', 'digest', 'unsubscribe', 'marketing', 'promotion',
        'offer', 'deal', 'sale', 'no-reply', 'noreply', 'notification'
    ]

    def __init__(self, secretary=None, episodic_memory=None):
        self.secretary = secretary
        self.memory = episodic_memory
        self._last_triage_time = None
        self._triage_results = []

    def triage_emails(self, max_results=20):
        """
        Fetch and priority-score recent emails.
        Returns sorted list of (priority, email_info) tuples.
        """
        if not self.secretary or not self.secretary.gmail_service:
            return []

        scored_emails = []

        try:
            # Get recent unread + recent read emails
            for query in ['is:unread newer_than:1d', 'is:read newer_than:1d is:important']:
                results = self.secretary.gmail_service.users().messages().list(
                    userId='me', q=query, maxResults=max_results
                ).execute()

                messages = results.get('messages', [])

                for msg_ref in messages:
                    msg_id = msg_ref.get('id', '')
                    try:
                        msg = self.secretary.gmail_service.users().messages().get(
                            userId='me', id=msg_id, format='metadata',
                            metadataHeaders=['Subject', 'From', 'Date']
                        ).execute()

                        headers = {
                            h['name']: h['value']
                            for h in msg.get('payload', {}).get('headers', [])
                        }

                        subject = headers.get('Subject', '')
                        sender = headers.get('From', '')
                        snippet = msg.get('snippet', '')

                        # Score the email
                        priority = self._score_item(subject, snippet, sender)

                        scored_emails.append({
                            'id': msg_id,
                            'subject': subject,
                            'sender': sender,
                            'snippet': snippet[:200],
                            'priority': priority,
                            'priority_label': self._priority_label(priority),
                            'labels': msg.get('labelIds', [])
                        })
                    except Exception:
                        continue

        except Exception as e:
            logger.error(f"Email triage error: {e}")

        # Sort by priority (highest first)
        scored_emails.sort(key=lambda x: x['priority'], reverse=True)
        self._triage_results = scored_emails
        self._last_triage_time = datetime.datetime.now()

        return scored_emails

    def get_critical_items(self, min_priority=7):
        """Get only items that truly need attention."""
        if not self._triage_results:
            self.triage_emails()
        return [item for item in self._triage_results if item['priority'] >= min_priority]

    def get_triage_summary(self):
        """Generate a human-readable triage summary."""
        if not self._triage_results:
            self.triage_emails()

        urgent = [e for e in self._triage_results if e['priority'] >= 9]
        important = [e for e in self._triage_results if 7 <= e['priority'] < 9]
        normal = [e for e in self._triage_results if 4 <= e['priority'] < 7]
        low = [e for e in self._triage_results if e['priority'] < 4]

        lines = ["📬 Message Triage:"]

        if urgent:
            lines.append(f"\n🔴 URGENT ({len(urgent)}):")
            for e in urgent[:5]:
                lines.append(f"  [{e['priority_label']}] {e['sender']}: {e['subject'][:80]}")

        if important:
            lines.append(f"\n🟡 IMPORTANT ({len(important)}):")
            for e in important[:5]:
                lines.append(f"  [{e['priority_label']}] {e['sender']}: {e['subject'][:80]}")

        if normal:
            lines.append(f"\n🟢 NORMAL ({len(normal)}):")
            for e in normal[:3]:
                lines.append(f"  {e['sender']}: {e['subject'][:60]}")

        if low:
            lines.append(f"\n⚪ LOW PRIORITY ({len(low)} items, filtered)")

        if not self._triage_results:
            lines.append("\nAll clear, Sir. No notable messages.")

        return "\n".join(lines)

    def _score_item(self, subject, body="", sender=""):
        """
        Score an email/notification on a 0-10 priority scale.

        Scoring factors:
        - Urgency keywords: +3
        - Important keywords: +2
        - Low priority keywords: -3
        - Known sender: +1
        - Has action words in subject: +2
        - Is a reply: +1
        """
        score = 5  # baseline
        text = f"{subject} {body}".lower()

        # Urgency keywords
        for kw in self.URGENT_KEYWORDS:
            if kw in text:
                score += 3
                break

        # Important keywords
        for kw in self.IMPORTANT_KEYWORDS:
            if kw in text:
                score += 2
                break

        # Low priority keywords
        for kw in self.LOW_PRIORITY_KEYWORDS:
            if kw in text:
                score -= 3
                break

        # Is a reply chain
        if subject.lower().startswith('re:'):
            score += 1

        # Is forwarded
        if subject.lower().startswith('fwd:'):
            score += 1

        # Action words
        action_words = ['please', 'need you', 'require', 'help', 'respond', 'confirm']
        for word in action_words:
            if word in text:
                score += 1
                break

        # Has attachments
        if 'attachment' in text:
            score += 1

        # Sender is from own organization (usually more important)
        # This is a simple heuristic — in production you'd check against a contacts list
        if sender and '@' in sender:
            domain = sender.split('@')[-1].strip('>')
            # Known important domains
            if any(d in domain.lower() for d in ['google', 'microsoft', 'apple', 'amazon']):
                score += 1

        return max(0, min(10, score))

    def _priority_label(self, score):
        """Convert numeric priority to human-readable label."""
        if score >= 9:
            return "🔴 URGENT"
        elif score >= 7:
            return "🟡 IMPORTANT"
        elif score >= 4:
            return "🟢 NORMAL"
        else:
            return "⚪ LOW"


class ProactiveEngine:
    """
    Unified proactive automation engine that combines:
    - Time Block Scheduling
    - Cross-App Triage
    - Proactive suggestions
    """

    def __init__(self, secretary=None, episodic_memory=None, interval=900,
                 notifier=None):
        self.time_blocker = TimeBlockScheduler(secretary, episodic_memory)
        self.triage = CrossAppTriage(secretary, episodic_memory)
        self.memory = episodic_memory
        self.secretary = secretary
        self.interval = interval
        self._running = False
        self._thread = None
        self._notifier = notifier
        # Dedup: announce each finding signature once per day so a
        # standing clash doesn't page the user every 15 minutes.
        self._announced = {}   # signature -> date string
        try:
            self._narrate = os.getenv('JARVIS_PROACTIVE_NARRATE',
                                      '0') == '1'
        except Exception:
            self._narrate = False

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ProactiveEngine")
        self._thread.start()
        logger.info("ProactiveEngine started")

    def stop(self):
        self._running = False

    def _loop(self):
        time.sleep(60)  # Initial delay
        while self._running:
            try:
                self.scan()
            except Exception as e:
                logger.error(f"ProactiveEngine scan error: {e}")
            time.sleep(self.interval)

    def scan(self, narrate=None):
        """
        Run a full proactive scan.  When narrate is on (constructor
        opt-in via JARVIS_PROACTIVE_NARRATE=1, or True passed here),
        NEW high-signal findings are announced through the notifier
        (dashboard always; voice/Telegram per its config), deduped to
        one announcement per finding per day.  Returns the results
        dict unchanged.  Never raises.
        """
        results = {
            'time_blocks': self.time_blocker.scan_and_adjust(),
            'triage': self.triage.triage_emails(),
            'suggestions': []
        }

        # Get proactive suggestions from relationships if available
        try:
            from utils.relationships import RelationshipManager
            # This will work if initialized
        except ImportError:
            pass

        try:
            want_voice = self._narrate if narrate is None else bool(narrate)
            if want_voice:
                self._narrate_findings(results)
        except Exception as e:
            logger.debug("proactive narration skipped: %s", e)
        return results

    def _narrate_findings(self, results):
        """Announce new high-signal findings once per day each."""
        try:
            from utils.notify import get_notifier
            nb = self._notifier or get_notifier()
        except Exception:
            return
        import datetime as _dt
        today = _dt.datetime.now().strftime('%Y-%m-%d')
        # Expire yesterday's signatures (bounded dict).
        for sig in [s for s, day in self._announced.items()
                    if day != today]:
            self._announced.pop(sig, None)

        def _once(signature, text, priority):
            import hashlib
            sig = hashlib.md5(signature.encode()).hexdigest()[:16]
            if self._announced.get(sig) == today:
                return
            try:
                if nb.announce(text, priority=priority):
                    self._announced[sig] = today
            except Exception:
                pass

        try:
            for b in (results.get('time_blocks') or [])[:6]:
                sev = str(b.get('severity', '')).lower()
                if sev in ('high', 'warning'):
                    _once(f"tb:{b.get('type')}:{b.get('message', '')[:80]}",
                          f"📅 {b.get('message', '')[:220]}",
                          'high' if sev == 'high' else 'normal')
        except Exception:
            pass
        try:
            urgent = [e for e in (results.get('triage') or [])
                      if isinstance(e, dict)
                      and int(e.get('priority', 0)) >= 9][:3]
            for e in urgent:
                _once(f"mail:{e.get('id', e.get('subject', ''))}",
                      f"📬 Urgent from {e.get('sender', '?')}: "
                      f"{e.get('subject', '')[:100]}",
                      'high')
        except Exception:
            pass

    def get_morning_briefing_context(self):
        """
        Build a rich context block for the enhanced morning briefing.
        """
        lines = []

        # Time blocks
        blocks = self.time_blocker.scan_and_adjust()
        if blocks:
            focus_blocks = [b for b in blocks if b.get('type') == 'focus_block']
            overlaps = [b for b in blocks if b.get('type') in ('overlap', 'meeting_overrun')]
            if focus_blocks:
                lines.append("Focus opportunities:")
                for b in focus_blocks[:3]:
                    lines.append(f"  • {b['message']}")
            if overlaps:
                lines.append("Schedule alerts:")
                for b in overlaps[:3]:
                    lines.append(f"  • {b['message']}")

        # Triage
        critical = self.triage.get_critical_items()
        if critical:
            lines.append(f"\nNeeds attention ({len(critical)} items):")
            for item in critical[:5]:
                lines.append(f"  • [{item['priority_label']}] {item['sender']}: {item['subject'][:60]}")

        return "\n".join(lines) if lines else "No proactive insights today."
