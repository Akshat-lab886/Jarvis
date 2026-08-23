"""
Jarvis Passive Context Engine

Runs as a background thread to silently analyze:
- Calendar events (upcoming meetings, deadlines)
- Recent emails (important messages, action items)
- Local files (new documents in knowledge_input)

Stores extracted insights in EpisodicMemory so the Brain can
reference them in conversations. The user never has to explicitly
tell the AI what's happening in their life.

Respects the Privacy Framework — only analyzes data the user has
opted into (defaults: calendar=yes, email=yes, files=yes).
"""

import os
import time
import json
import threading
import datetime
import logging

logger = logging.getLogger("Jarvis.ContextEngine")


class ContextEngine:
    """
    Background context gatherer.

    Runs every `interval` seconds and:
    1. Scans calendar for upcoming events (next 24h)
    2. Scans recent emails for action items and important messages
    3. Checks for new files in knowledge_input folder
    4. Stores insights in EpisodicMemory
    """

    def __init__(self, episodic_memory=None, secretary=None,
                 interval=600, privacy_settings=None):
        """
        Args:
            episodic_memory: EpisodicMemory instance
            secretary: Secretary instance (Google API)
            interval: Seconds between scans (default 600 = 10 min)
            privacy_settings: dict with keys 'analyze_calendar', 'analyze_email', 'analyze_files'
        """
        self.memory = episodic_memory
        self.secretary = secretary
        self.interval = interval
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # Privacy controls
        self.privacy = privacy_settings or {
            'analyze_calendar': True,
            'analyze_email': True,
            'analyze_files': True,
        }

        # Track what we've already processed to avoid duplicates
        self._processed_events = set()
        self._processed_emails = set()
        self._processed_files = set()

        # State file for persistence
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.state_file = os.path.join(self.base_dir, 'context_engine_state.json')
        self._load_state()

        logger.info("ContextEngine initialized")

    # ------------------------------------------------------------------ #
    # State persistence
    # ------------------------------------------------------------------ #
    def _load_state(self):
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                self._processed_events = set(state.get('processed_events', []))
                self._processed_emails = set(state.get('processed_emails', []))
                self._processed_files = set(state.get('processed_files', []))
        except Exception as e:
            logger.warning(f"Failed to load context engine state: {e}")

    def _save_state(self):
        try:
            state = {
                'processed_events': list(self._processed_events)[-500:],
                'processed_emails': list(self._processed_emails)[-500:],
                'processed_files': list(self._processed_files)[-200:],
                'last_scan': datetime.datetime.now().isoformat()
            }
            with open(self.state_file, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save context engine state: {e}")

    # ------------------------------------------------------------------ #
    # Background loop
    # ------------------------------------------------------------------ #
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ContextEngine")
        self._thread.start()
        logger.info("ContextEngine started")

    def stop(self):
        self._running = False

    def _loop(self):
        # Initial scan after a short delay (let other modules init)
        time.sleep(30)
        while self._running:
            try:
                self.scan()
            except Exception as e:
                logger.error(f"ContextEngine scan error: {e}", exc_info=True)
            time.sleep(self.interval)

    def scan(self):
        """Run a single scan cycle across all enabled sources."""
        logger.info("ContextEngine: Starting scan cycle")
        new_insights = []

        if self.privacy.get('analyze_calendar', True):
            try:
                insights = self._scan_calendar()
                new_insights.extend(insights)
            except Exception as e:
                logger.error(f"Calendar scan error: {e}")

        if self.privacy.get('analyze_email', True):
            try:
                insights = self._scan_emails()
                new_insights.extend(insights)
            except Exception as e:
                logger.error(f"Email scan error: {e}")

        if self.privacy.get('analyze_files', True):
            try:
                insights = self._scan_files()
                new_insights.extend(insights)
            except Exception as e:
                logger.error(f"File scan error: {e}")

        if new_insights:
            logger.info(f"ContextEngine: Captured {len(new_insights)} new insights")
            self._save_state()

        return new_insights

    # ------------------------------------------------------------------ #
    # Calendar Analysis
    # ------------------------------------------------------------------ #
    def _scan_calendar(self):
        """Analyze upcoming calendar events and extract context."""
        if not self.secretary or not self.secretary.calendar_service:
            return []

        insights = []
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            tomorrow = now + datetime.timedelta(hours=24)

            events_result = self.secretary.calendar_service.events().list(
                calendarId='primary',
                timeMin=now.isoformat(),
                timeMax=tomorrow.isoformat(),
                maxResults=20,
                singleEvents=True,
                orderBy='startTime'
            ).execute()

            events = events_result.get('items', [])

            for event in events:
                event_id = event.get('id', '')
                if event_id in self._processed_events:
                    continue

                summary = event.get('summary', 'Untitled Event')
                start = event.get('start', {}).get('dateTime', event.get('start', {}).get('date', ''))
                location = event.get('location', '')
                description = event.get('description', '')

                # Parse the start time
                start_dt = None
                hours_until = 0
                try:
                    start_dt = datetime.datetime.fromisoformat(start.replace('Z', '+00:00'))
                    hours_until = (start_dt - now).total_seconds() / 3600
                except Exception:
                    pass

                # Build context text
                time_str = start_dt.strftime("%I:%M %p") if start_dt else start
                context_text = f"Upcoming event: '{summary}' at {time_str}"
                if location:
                    context_text += f" at {location}"
                if hours_until > 0:
                    context_text += f" (in {int(hours_until)} hours)"

                # Determine importance based on proximity and type
                importance = 6
                if hours_until < 1:
                    importance = 9  # happening very soon
                elif hours_until < 3:
                    importance = 8
                elif hours_until < 6:
                    importance = 7

                # Check for meeting patterns
                tags = ['calendar', 'upcoming-event']
                if any(kw in summary.lower() for kw in ['meeting', 'call', 'interview', 'demo']):
                    tags.append('meeting')
                if any(kw in summary.lower() for kw in ['deadline', 'due', 'submit']):
                    tags.append('deadline')
                    importance = min(importance + 1, 10)

                mem = self.memory.remember(
                    text=context_text,
                    category='context',
                    tags=tags,
                    source='passive',
                    importance=importance,
                    metadata={
                        'event_id': event_id,
                        'start': start,
                        'location': location,
                        'description': (description or '')[:200]
                    }
                )
                if mem:
                    insights.append(mem)
                    self._processed_events.add(event_id)

        except Exception as e:
            logger.error(f"Calendar analysis error: {e}")

        return insights

    # ------------------------------------------------------------------ #
    # Email Analysis
    # ------------------------------------------------------------------ #
    def _scan_emails(self):
        """Analyze recent emails for important messages and action items."""
        if not self.secretary or not self.secretary.gmail_service:
            return []

        insights = []
        try:
            results = self.secretary.gmail_service.users().messages().list(
                userId='me',
                q='is:unread newer_than:1d',
                maxResults=10
            ).execute()

            messages = results.get('messages', [])

            for msg_ref in messages:
                msg_id = msg_ref.get('id', '')
                if msg_id in self._processed_emails:
                    continue

                msg = self.secretary.gmail_service.users().messages().get(
                    userId='me', id=msg_id, format='metadata',
                    metadataHeaders=['Subject', 'From', 'Date']
                ).execute()

                headers = {
                    h['name']: h['value']
                    for h in msg.get('payload', {}).get('headers', [])
                }

                subject = headers.get('Subject', 'No Subject')
                sender = headers.get('From', 'Unknown')
                snippet = msg.get('snippet', '')

                # Determine if this email is important
                importance = 5
                tags = ['email', 'unread']

                subject_lower = subject.lower()
                if any(kw in subject_lower for kw in ['urgent', 'action required', 'deadline', 'important']):
                    importance = 9
                    tags.append('urgent')
                elif any(kw in subject_lower for kw in ['meeting', 'invite', 'calendar']):
                    importance = 7
                    tags.append('meeting-invite')
                elif any(kw in subject_lower for kw in ['invoice', 'payment', 'receipt']):
                    importance = 7
                    tags.append('financial')
                elif 're:' in subject_lower:
                    importance = 6
                    tags.append('reply')
                elif any(kw in subject_lower for kw in ['newsletter', 'digest', 'unsubscribe']):
                    importance = 3
                    tags.append('low-priority')

                context_text = f"New email from {sender}: {subject}"
                if snippet:
                    context_text += f" — {snippet[:120]}"

                mem = self.memory.remember(
                    text=context_text,
                    category='context',
                    tags=tags,
                    source='passive',
                    importance=importance,
                    metadata={
                        'email_id': msg_id,
                        'sender': sender,
                        'subject': subject,
                        'snippet': snippet[:200]
                    }
                )
                if mem:
                    insights.append(mem)
                    self._processed_emails.add(msg_id)

        except Exception as e:
            logger.error(f"Email analysis error: {e}")

        return insights

    # ------------------------------------------------------------------ #
    # File Analysis
    # ------------------------------------------------------------------ #
    def _scan_files(self):
        """Check for new files in knowledge_input and extract metadata."""
        input_dir = os.path.join(self.base_dir, 'knowledge_input')
        if not os.path.exists(input_dir):
            return []

        insights = []
        try:
            for filename in os.listdir(input_dir):
                filepath = os.path.join(input_dir, filename)
                if not os.path.isfile(filepath):
                    continue
                if filename in self._processed_files:
                    continue

                # Get file metadata
                stat = os.stat(filepath)
                size_kb = stat.st_size / 1024
                mod_time = datetime.datetime.fromtimestamp(stat.st_mtime)

                ext = os.path.splitext(filename)[1].lower()

                context_text = f"New file available: '{filename}' ({ext}, {size_kb:.1f} KB)"
                tags = ['file', 'knowledge-input']
                if ext == '.pdf':
                    tags.append('pdf')
                elif ext in ['.txt', '.md']:
                    tags.append('text')

                mem = self.memory.remember(
                    text=context_text,
                    category='context',
                    tags=tags,
                    source='passive',
                    importance=4,
                    metadata={
                        'filename': filename,
                        'path': filepath,
                        'size_kb': round(size_kb, 1),
                        'extension': ext
                    }
                )
                if mem:
                    insights.append(mem)
                    self._processed_files.add(filename)

        except Exception as e:
            logger.error(f"File scan error: {e}")

        return insights

    # ------------------------------------------------------------------ #
    # Manual trigger
    # ------------------------------------------------------------------ #
    def force_scan(self):
        """Force an immediate scan (e.g., from a command)."""
        return self.scan()

    def get_summary(self):
        """Get a human-readable summary of current context."""
        now = datetime.datetime.now()
        lines = [f"Context Summary ({now.strftime('%I:%M %p')}):"]

        # Upcoming events
        upcoming = self.memory.search(
            "upcoming event", category='context',
            tags=['calendar'], limit=5
        )
        if upcoming:
            lines.append("\nUpcoming:")
            for m in upcoming:
                lines.append(f"  • {m['text']}")

        # Important emails
        emails = self.memory.search(
            "email", category='context',
            tags=['email'], limit=5
        )
        if emails:
            lines.append("\nRecent Emails:")
            for m in emails:
                lines.append(f"  • {m['text']}")

        # Active goals
        goals = self.memory.get_by_category('goal', limit=5)
        if goals:
            lines.append("\nActive Goals:")
            for g in goals:
                lines.append(f"  • {g['text']}")

        return "\n".join(lines)
