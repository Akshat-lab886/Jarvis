"""
Jarvis Relationship Intelligence

Tracks the user's social circle with rich detail:
- People's names, relationships, birthdays, anniversaries
- Their preferences, allergies, hobbies
- Interaction history and sentiment
- Timely reminders for birthdays, anniversaries, gift ideas
- Proactive suggestions for dinners, catch-ups, gifts

All data is stored in the EpisodicMemory under the 'relationship' category.
"""

import os
import json
import datetime
import threading
import logging
import re

logger = logging.getLogger("Jarvis.Relationships")


class RelationshipManager:
    """
    Manages the user's social relationships.

    People are stored as structured entries in episodic memory with rich metadata.
    This module provides methods to:
    - Add/update people
    - Record interaction details
    - Get upcoming dates and reminders
    - Generate proactive suggestions
    """

    def __init__(self, episodic_memory=None):
        self.memory = episodic_memory
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.file_path = os.path.join(self.base_dir, 'relationships.json')
        self._lock = threading.RLock()
        self._people = {}
        self._load()
        logger.info("RelationshipManager initialized")

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _load(self):
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._people = data
        except Exception as e:
            logger.warning(f"Failed to load relationships: {e}")

    def _save(self):
        try:
            with open(self.file_path, 'w') as f:
                json.dump(self._people, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed to save relationships: {e}")

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #
    def add_person(self, name, relationship_type=None, details=None,
                   birthday=None, anniversary=None, preferences=None,
                   allergies=None, hobbies=None):
        """
        Add or update a person in the relationship graph.

        Returns the person entry dict.
        """
        name_key = name.lower().strip()

        with self._lock:
            if name_key in self._people:
                person = self._people[name_key]
                # Update fields if provided
                if relationship_type:
                    person['relationship'] = relationship_type
                if details:
                    existing = person.get('details', [])
                    if isinstance(existing, str):
                        existing = [existing]
                    existing.append(details)
                    person['details'] = existing[-20:]  # keep last 20 details
                if birthday:
                    person['birthday'] = birthday
                if anniversary:
                    person['anniversary'] = anniversary
                if preferences:
                    old_prefs = person.get('preferences', [])
                    old_prefs.extend(preferences if isinstance(preferences, list) else [preferences])
                    person['preferences'] = list(set(old_prefs))[-30:]
                if allergies:
                    old_allergies = person.get('allergies', [])
                    old_allergies.extend(allergies if isinstance(allergies, list) else [allergies])
                    person['allergies'] = list(set(old_allergies))
                if hobbies:
                    old_hobbies = person.get('hobbies', [])
                    old_hobbies.extend(hobbies if isinstance(hobbies, list) else [hobbies])
                    person['hobbies'] = list(set(old_hobbies))
                person['updated'] = datetime.datetime.now().isoformat()
            else:
                person = {
                    "name": name,
                    "relationship": relationship_type or "acquaintance",
                    "details": [details] if details else [],
                    "birthday": birthday,
                    "anniversary": anniversary,
                    "preferences": preferences if isinstance(preferences, list) else ([preferences] if preferences else []),
                    "allergies": allergies if isinstance(allergies, list) else ([allergies] if allergies else []),
                    "hobbies": hobbies if isinstance(hobbies, list) else ([hobbies] if hobbies else []),
                    "interactions": [],
                    "sentiment": "neutral",  # positive, neutral, negative
                    "created": datetime.datetime.now().isoformat(),
                    "updated": datetime.datetime.now().isoformat()
                }
                self._people[name_key] = person

        self._save()

        # Also store in episodic memory for cross-referencing
        if self.memory:
            self.memory.remember(
                text=f"{name} — {person.get('relationship', 'acquaintance')}",
                category='relationship',
                tags=['relationship', name.lower(), (relationship_type or '').lower()],
                source='conversation',
                importance=8,
                metadata={'person_key': name_key}
            )

        return person

    def get_person(self, name):
        """Get a person by name (case-insensitive)."""
        name_key = name.lower().strip()
        with self._lock:
            return dict(self._people.get(name_key, {})) or None

    def remove_person(self, name):
        """Remove a person from the graph."""
        name_key = name.lower().strip()
        with self._lock:
            if name_key in self._people:
                del self._people[name_key]
                self._save()
                return f"Removed {name} from your contacts."
        return f"I couldn't find {name} in your contacts."

    def list_all(self):
        """Get all people (snapshot)."""
        with self._lock:
            return {k: dict(v) for k, v in self._people.items()}

    def search(self, query):
        """Search people by name, relationship, or details."""
        query_lower = query.lower().strip()
        results = []
        with self._lock:
            for key, person in self._people.items():
                score = 0
                # Name match
                if query_lower in key:
                    score = 3
                # Relationship match
                if query_lower in person.get('relationship', '').lower():
                    score = max(score, 2)
                # Detail match
                for detail in person.get('details', []):
                    if query_lower in detail.lower():
                        score = max(score, 1)
                # Hobby/preference match
                for h in person.get('hobbies', []) + person.get('preferences', []):
                    if query_lower in h.lower():
                        score = max(score, 1)

                if score > 0:
                    results.append((score, key, person))

        results.sort(key=lambda x: x[0], reverse=True)
        return [(key, person) for _, key, person in results]

    # ------------------------------------------------------------------ #
    # Interactions
    # ------------------------------------------------------------------ #
    def record_interaction(self, name, summary, sentiment='neutral'):
        """Record that the user interacted with someone."""
        name_key = name.lower().strip()
        with self._lock:
            if name_key in self._people:
                person = self._people[name_key]
                interactions = person.get('interactions', [])
                interactions.append({
                    'date': datetime.datetime.now().isoformat(),
                    'summary': summary,
                    'sentiment': sentiment
                })
                # Keep last 50 interactions
                person['interactions'] = interactions[-50:]
                person['updated'] = datetime.datetime.now().isoformat()
                self._save()
                return f"Recorded interaction with {name}."
        return f"I don't have {name} in my records yet."

    # ------------------------------------------------------------------ #
    # Upcoming Dates & Reminders
    # ------------------------------------------------------------------ #
    def get_upcoming_dates(self, days_ahead=30):
        """Get people with birthdays/anniversaries in the next N days."""
        now = datetime.datetime.now()
        upcoming = []

        with self._lock:
            for key, person in self._people.items():
                # Check birthday
                bday_str = person.get('birthday')
                if bday_str:
                    try:
                        bday = self._parse_date_str(bday_str, now.year)
                        if bday < now:
                            bday = bday.replace(year=now.year + 1)
                        delta = (bday - now).days
                        if 0 <= delta <= days_ahead:
                            upcoming.append({
                                'name': person['name'],
                                'type': 'birthday',
                                'date': bday.strftime('%B %d'),
                                'days_until': delta,
                                'person': person
                            })
                    except Exception:
                        pass

                # Check anniversary
                ann_str = person.get('anniversary')
                if ann_str:
                    try:
                        ann = self._parse_date_str(ann_str, now.year)
                        if ann < now:
                            ann = ann.replace(year=now.year + 1)
                        delta = (ann - now).days
                        if 0 <= delta <= days_ahead:
                            upcoming.append({
                                'name': person['name'],
                                'type': 'anniversary',
                                'date': ann.strftime('%B %d'),
                                'days_until': delta,
                                'person': person
                            })
                    except Exception:
                        pass

        upcoming.sort(key=lambda x: x['days_until'])
        return upcoming

    def get_gift_suggestions(self, name):
        """Generate gift suggestions based on a person's interests."""
        person = self.get_person(name)
        if not person:
            return [f"I don't have enough info about {name} to suggest gifts."]

        suggestions = []
        hobbies = person.get('hobbies', [])
        preferences = person.get('preferences', [])
        relationship = person.get('relationship', '')
        details = person.get('details', [])

        # Build a context string
        context_parts = []
        if relationship:
            context_parts.append(f"Relationship: {relationship}")
        if hobbies:
            context_parts.append(f"Hobbies: {', '.join(hobbies)}")
        if preferences:
            context_parts.append(f"Preferences: {', '.join(preferences)}")
        if details:
            context_parts.append(f"Details: {'; '.join(details[-5:])}")

        context = "\n".join(context_parts) if context_parts else f"Name: {name}"

        # Rule-based suggestions (no LLM needed for common cases)
        hobby_gifts = {
            'music': ['concert tickets', 'vinyl record', 'premium headphones', 'music lessons'],
            'reading': ['book by their favorite author', 'Kindle', 'bookstore gift card', 'reading lamp'],
            'cooking': ['cookbook', 'kitchen gadget', 'cooking class', 'spice set'],
            'gaming': ['game gift card', 'gaming accessories', 'new game release', 'gaming headset'],
            'fitness': ['gym membership', 'workout gear', 'fitness tracker', 'yoga mat'],
            'travel': ['travel pillow', 'luggage tag', 'travel guidebook', 'airline gift card'],
            'art': ['art supplies', 'museum membership', 'art book', 'easel'],
            'photography': ['camera accessories', 'photo frame', 'photography course', 'memory card'],
            'tech': ['smart home gadget', 'USB hub', 'tech book', 'wireless charger'],
            'gardening': ['gardening tools', 'plant pot', 'seed collection', 'gardening book'],
            'movies': ['movie subscription', 'popcorn maker', 'movie poster', 'home theater accessory'],
            'sports': ['team merchandise', 'sports tickets', 'sports equipment', 'jersey'],
            'coffee': ['premium coffee beans', 'coffee maker', 'coffee subscription', 'travel mug'],
            'wine': ['wine subscription', 'wine glasses', 'wine opener', 'wine tasting experience'],
        }

        for hobby in hobbies:
            hobby_lower = hobby.lower()
            for key, gifts in hobby_gifts.items():
                if key in hobby_lower:
                    suggestions.extend(gifts[:3])

        # Fallback if no hobby matches
        if not suggestions:
            if 'mom' in relationship.lower() or 'mother' in relationship.lower():
                suggestions = ['flowers', 'spa day', 'photo album', 'cooking class together', 'personalized jewelry']
            elif 'dad' in relationship.lower() or 'father' in relationship.lower():
                suggestions = ['tool set', 'book', 'golf accessories', 'tech gadget', 'experience gift']
            elif 'friend' in relationship.lower():
                suggestions = ['experience gift', 'personalized item', 'food/drink gift', 'game or activity']
            elif 'partner' in relationship.lower() or 'wife' in relationship.lower() or 'husband' in relationship.lower():
                suggestions = ['romantic dinner', 'jewelry', 'experience together', 'weekend trip', 'personalized gift']
            else:
                suggestions = ['gift card', 'book', 'gourmet food', 'personalized item', 'experience gift']

        return suggestions[:5]

    def get_dinner_suggestions(self, name):
        """Suggest dinner ideas based on a person's preferences and allergies."""
        person = self.get_person(name)
        if not person:
            return [f"Tell me more about {name} so I can suggest dinner ideas."]

        allergies = [a.lower() for a in person.get('allergies', [])]
        preferences = [p.lower() for p in person.get('preferences', [])]
        hobbies = [h.lower() for h in person.get('hobbies', [])]

        suggestions = []

        # If allergic to something, avoid restaurants/foods with that
        allergy_note = ""
        if allergies:
            allergy_note = f" (avoid: {', '.join(allergies)})"

        # Build suggestions based on context
        if any('vegan' in p or 'vegetarian' in p for p in preferences + hobbies):
            suggestions = [
                f"Vegan restaurant{allergy_note}",
                f"Farm-to-table dining{allergy_note}",
                f"Mediterranean cuisine{allergy_note}"
            ]
        elif any('sushi' in p or 'japanese' in p or 'asian' in p for p in preferences):
            suggestions = [
                f"Sushi restaurant{allergy_note}",
                f"Japanese izakaya{allergy_note}",
                f"Thai restaurant{allergy_note}"
            ]
        elif any('italian' in p or 'pizza' in p or 'pasta' in p for p in preferences):
            suggestions = [
                f"Italian restaurant{allergy_note}",
                f"Wood-fired pizza place{allergy_note}",
                f"Cozy pasta spot{allergy_note}"
            ]
        else:
            suggestions = [
                f"Nice restaurant{allergy_note}",
                f"New place downtown{allergy_note}",
                f"Casual dinner spot{allergy_note}"
            ]

        # Add context-aware suggestions
        if 'coffee' in ' '.join(hobbies):
            suggestions.append("Dinner + coffee afterward")
        if any('wine' in h for h in hobbies):
            suggestions.append("Restaurant with good wine selection")

        return suggestions

    # ------------------------------------------------------------------ #
    # Proactive Suggestions (for morning briefing, etc.)
    # ------------------------------------------------------------------ #
    def get_proactive_suggestions(self):
        """
        Generate proactive suggestions based on upcoming dates and interaction gaps.
        Called during morning briefing.
        """
        suggestions = []
        now = datetime.datetime.now()

        # Check upcoming dates
        upcoming = self.get_upcoming_dates(days_ahead=7)
        for event in upcoming:
            if event['days_until'] == 0:
                suggestions.append(
                    f"🎂 Today is {event['name']}'s {event['type']}! "
                    f"Consider reaching out."
                )
            elif event['days_until'] <= 3:
                suggestions.append(
                    f"📅 {event['name']}'s {event['type']} is in {event['days_until']} days "
                    f"({event['date']}). Gift ideas: {', '.join(self.get_gift_suggestions(event['name'])[:2])}"
                )
            elif event['days_until'] <= 7:
                suggestions.append(
                    f"📅 {event['name']}'s {event['type']} is on {event['date']}. "
                    f"You might want to plan something."
                )

        # Check for people we haven't interacted with in a while
        with self._lock:
            for key, person in self._people.items():
                interactions = person.get('interactions', [])
                if not interactions:
                    # Never interacted — suggest catching up
                    suggestions.append(
                        f"💡 You haven't logged an interaction with {person['name']} "
                        f"({person.get('relationship', 'contact')}). Consider reaching out."
                    )
                else:
                    last = interactions[-1]
                    try:
                        last_date = datetime.datetime.fromisoformat(last['date'])
                        days_since = (now - last_date).days
                        if days_since > 30:
                            suggestions.append(
                                f"💡 It's been {days_since} days since you last interacted "
                                f"with {person['name']}. Maybe a catch-up?"
                            )
                    except Exception:
                        pass

        return suggestions[:10]

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _parse_date_str(self, date_str, year):
        """Parse a date string (various formats) into a datetime."""
        formats = [
            '%Y-%m-%d', '%m-%d', '%m/%d/%Y', '%m/%d',
            '%B %d', '%B %d, %Y', '%b %d', '%b %d, %Y',
            '%d %B', '%d %B %Y', '%d %b', '%d %b %Y',
        ]
        for fmt in formats:
            try:
                dt = datetime.datetime.strptime(date_str.strip(), fmt)
                # If year is not in the format, use the provided year
                if dt.year == 1900:
                    dt = dt.replace(year=year)
                return dt
            except ValueError:
                continue

        # Try dateparser as last resort
        try:
            import dateparser
            dt = dateparser.parse(date_str)
            if dt:
                if dt.year == 1900:
                    dt = dt.replace(year=year)
                return dt
        except Exception:
            pass

        raise ValueError(f"Cannot parse date: {date_str}")

    def get_context_for_prompt(self, max_chars=1500):
        """Build a context block for the Brain's system prompt."""
        lines = ["RELATIONSHIP MAP:"]
        with self._lock:
            for key, person in sorted(self._people.items()):
                line = f"  {person['name']} ({person.get('relationship', '?')})"
                if person.get('hobbies'):
                    line += f" — likes: {', '.join(person['hobbies'][:3])}"
                if person.get('allergies'):
                    line += f" — allergic to: {', '.join(person['allergies'])}"
                if person.get('birthday'):
                    line += f" — birthday: {person['birthday']}"
                lines.append(line)

        context = "\n".join(lines)
        if len(context) > max_chars:
            context = context[:max_chars] + "\n[...more contacts...]"
        return context

    def items_json(self):
        """Dashboard-friendly snapshot."""
        with self._lock:
            return {k: dict(v) for k, v in self._people.items()}
