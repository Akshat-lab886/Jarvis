"""
Jarvis Morning Briefing V2

An enhanced daily briefing that goes beyond weather + calendar:

1. Context Awareness — what's happening today based on passive analysis
2. Relationship Intelligence — upcoming birthdays, anniversaries, people to contact
3. Health Goals — reminders about health commitments
4. Proactive Suggestions — schedule optimization, focus blocks
5. Cross-App Triage — critical messages that need attention
6. Daily Digest — summary of yesterday's activity
7. Weather & Calendar — enhanced with travel time, prep reminders
"""

import datetime
import logging

logger = logging.getLogger("Jarvis.Briefing")


class MorningBriefing:
    """
    Generates a comprehensive morning briefing using all available context.
    """

    def __init__(self, tools=None, secretary=None, episodic_memory=None,
                 relationships=None, proactive_engine=None, tasks=None,
                 history=None, brain=None):
        self.tools = tools
        self.secretary = secretary
        self.memory = episodic_memory
        self.relationships = relationships
        self.proactive = proactive_engine
        self.tasks = tasks
        self.history = history
        self.brain = brain

    def generate(self, voice_output=True):
        """
        Generate a full morning briefing.
        Returns a formatted string with all sections.
        """
        now = datetime.datetime.now()
        sections = []

        # ---- Greeting ----
        hour = now.hour
        if hour < 12:
            greeting = "Good morning"
        elif hour < 17:
            greeting = "Good afternoon"
        else:
            greeting = "Good evening"

        sections.append(f"{greeting}, Sir. Here's your briefing for {now.strftime('%A, %B %d')}.")

        # ---- Weather ----
        weather = self._get_weather()
        if weather:
            sections.append(f"\n🌤️ Weather: {weather}")

        # ---- Calendar Overview ----
        calendar_section = self._get_calendar_overview()
        if calendar_section:
            sections.append(f"\n📅 Schedule:\n{calendar_section}")

        # ---- Proactive Schedule Insights ----
        if self.proactive:
            try:
                insights = self.proactive.get_morning_briefing_context()
                if insights and insights != "No proactive insights today.":
                    sections.append(f"\n⚡ Schedule Intelligence:\n{insights}")
            except Exception as e:
                logger.error(f"Proactive briefing error: {e}")

        # ---- Critical Messages ----
        triage_section = self._get_triage_summary()
        if triage_section:
            sections.append(f"\n📬 Messages:\n{triage_section}")

        # ---- Relationship Reminders ----
        if self.relationships:
            try:
                suggestions = self.relationships.get_proactive_suggestions()
                if suggestions:
                    sections.append(f"\n👥 People:\n" + "\n".join(f"  {s}" for s in suggestions[:5]))
            except Exception as e:
                logger.error(f"Relationship briefing error: {e}")

        # ---- Health & Goals ----
        health_section = self._get_health_goals()
        if health_section:
            sections.append(f"\n💪 Goals & Health:\n{health_section}")

        # ---- Pending Tasks ----
        tasks_section = self._get_pending_tasks()
        if tasks_section:
            sections.append(f"\n📝 To-Do:\n{tasks_section}")

        # ---- Yesterday's Summary ----
        if self.history:
            try:
                entries = self.history.today()
                if entries:
                    sections.append(f"\n📊 Yesterday: {len(entries)} commands processed.")
            except Exception:
                pass

        # ---- Episodic Memory Context ----
        if self.memory:
            try:
                context = self.memory.get_context_for_prompt(max_tokens=800)
                if context and len(context) > 50:
                    sections.append(f"\n🧠 Memory Context:\n{context[:500]}")
            except Exception:
                pass

        briefing_text = "\n".join(sections)

        # Optionally enhance with LLM polish
        if self.brain and len(briefing_text) > 100:
            try:
                prompt = (
                    f"Polish this morning briefing into an elegant, concise Jarvis-style "
                    f"message. Keep it under 400 words. Be warm but professional. "
                    f"Highlight the most important items.\n\n"
                    f"BRIEFING DATA:\n{briefing_text}"
                )
                result = self.brain.think(prompt)
                polished = result.get('response', briefing_text)
                if len(polished) > 50:
                    return polished
            except Exception as e:
                logger.error(f"Briefing polish error: {e}")

        return briefing_text

    def _get_weather(self):
        """Get current weather."""
        if not self.tools:
            return None
        try:
            return self.tools.get_weather()
        except Exception:
            return None

    def _get_calendar_overview(self):
        """Get today's calendar with enhanced formatting."""
        if not self.secretary or not self.secretary.calendar_service:
            return None

        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            end_of_day = now.replace(hour=23, minute=59, second=59)

            events_result = self.secretary.calendar_service.events().list(
                calendarId='primary',
                timeMin=now.isoformat(),
                timeMax=end_of_day.isoformat(),
                maxResults=10,
                singleEvents=True,
                orderBy='startTime'
            ).execute()

            events = events_result.get('items', [])
            if not events:
                return "No events today. Free day!"

            lines = []
            for event in events:
                summary = event.get('summary', 'Untitled')
                start = event.get('start', {}).get('dateTime', event.get('start', {}).get('date', ''))
                location = event.get('location', '')

                try:
                    start_dt = datetime.datetime.fromisoformat(start.replace('Z', '+00:00'))
                    time_str = start_dt.strftime('%I:%M %p')
                except Exception:
                    time_str = start

                line = f"  {time_str} — {summary}"
                if location:
                    line += f" @ {location}"
                lines.append(line)

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"Calendar overview error: {e}")
            return None

    def _get_triage_summary(self):
        """Get critical messages from triage."""
        if not self.proactive:
            return None

        try:
            critical = self.proactive.triage.get_critical_items(min_priority=7)
            if not critical:
                return "All clear — no urgent messages."

            lines = [f"{len(critical)} items need attention:"]
            for item in critical[:5]:
                lines.append(f"  • [{item['priority_label']}] {item['sender']}: {item['subject'][:60]}")
            return "\n".join(lines)
        except Exception:
            return None

    def _get_health_goals(self):
        """Get active health goals and reminders."""
        if not self.memory:
            return None

        try:
            health = self.memory.get_by_category('health', limit=5)
            goals = self.memory.get_by_category('goal', limit=5)

            lines = []
            for h in health:
                lines.append(f"  💊 {h['text'][:80]}")
            for g in goals:
                lines.append(f"  🎯 {g['text'][:80]}")

            return "\n".join(lines) if lines else None
        except Exception:
            return None

    def _get_pending_tasks(self):
        """Get pending todo items."""
        if not self.tasks:
            return None

        try:
            import json
            items = self.tasks.items_json()
            pending = [i for i in items if not i.get('done')]
            if not pending:
                return "All caught up! No pending tasks."

            lines = [f"{len(pending)} tasks remaining:"]
            for t in pending[:5]:
                lines.append(f"  ⬜ {t['text'][:60]}")
            if len(pending) > 5:
                lines.append(f"  ...and {len(pending) - 5} more")
            return "\n".join(lines)
        except Exception:
            return None
