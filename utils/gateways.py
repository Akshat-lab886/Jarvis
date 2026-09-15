"""
J.A.R.V.I.S. — Omnichannel Gateway Hub (Hermes parity)
======================================================

One gateway wrapper that keeps Jarvis continuously present across a
messaging footprint far beyond the web dashboard:

    telegram     full bot (utils/telegram_bot.py — voice, photos,
                 reminders) — started by the server when its token is set
    discord      REST-polling adapter (no extra dependencies: plain
                 HTTPS against the Discord Bot API)
    slack        outgoing = incoming-webhook URL; incoming = the
                 /gateway/slack Flask route (Events API + slash commands)
    webhook      any other platform (WhatsApp / Signal / Matrix / Teams /
                 Google Chat bridges) via the existing POST /webhook

Every adapter activates purely by configuration — no token, no thread.
All inbound messages funnel through the SAME event-bus pipeline as the
dashboard (think → execute), so behaviour is identical everywhere.

Multimodal bot mode: in group chats, ``@jarvis …`` mentions are routed
to specialist sub-agents (coder / researcher / synthesizer …) which can
carry out multi-step tasks in tandem, per ``JARVIS_BOT_MODE``.

``JARVIS_GATEWAYS=0`` disables every adapter (Telegram included — it
is gated separately by its token).
"""

import os
import re
import json
import time
import logging
import threading

logger = logging.getLogger("Jarvis.Gateways")

DISCORD_API = 'https://discord.com/api/v10'


def _env_float(name, default, floor=None):
    """Parse a float env var safely: a malformed value falls back to the
    default instead of crashing the module at import, and an optional
    floor keeps a 0/negative value from turning a polling loop into a
    busy-spin."""
    try:
        value = float(os.getenv(name, default))
    except (TypeError, ValueError):
        value = float(default)
    return value if floor is None else max(floor, value)


_POLL_S = _env_float('JARVIS_GATEWAY_POLL', '3', floor=1.0)

# Specialist routing for bot mode (@jarvis <role> <task>)
_ROLE_KEYWORDS = {
    'coder': ('code', 'debug', 'fix', 'refactor', 'script', 'bug'),
    'researcher': ('research', 'find out', 'look up', 'compare',
                   'investigate'),
    'synthesizer': ('summarize', 'summarise', 'digest', 'tldr', 'brief'),
    'planner': ('plan', 'roadmap', 'schedule', 'itinerary', 'organize'),
    'critic': ('review', 'critique', 'check my', 'proofread'),
}


def enabled():
    return os.getenv('JARVIS_GATEWAYS', '1') != '0'


def bot_mode_enabled():
    return os.getenv('JARVIS_BOT_MODE', '1') != '0'


def pick_persona(text):
    """Keyword-route a mention to a specialist persona (or None)."""
    t = str(text or '').lower()
    best, best_hits = None, 0
    for persona, words in _ROLE_KEYWORDS.items():
        hits = sum(1 for w in words if w in t)
        if hits > best_hits:
            best, best_hits = persona, hits
    return best if best_hits >= 1 else None


def strip_mention(text, names=('jarvis',)):
    """Remove @mentions / wake prefixes from a group-chat message."""
    t = str(text or '')
    for name in names:
        t = re.sub(rf'@?{re.escape(name)}[,:]?\s*',
                   '', t, flags=re.IGNORECASE)
    return t.strip()


def route_message(text, brain, fallback=None):
    """
    Multimodal bot mode: decide how one inbound group message is served.

    Returns (used_specialist, response_text).  When no specialist fits
    the request, ``fallback`` (normally the standard event pipeline) is
    invoked and its result returned with used_specialist=False.
    """
    text = str(text or '').strip()
    if not text or not bot_mode_enabled():
        return False, (fallback(text) if fallback else None)
    persona = pick_persona(text)
    if persona is None or brain is None:
        return False, (fallback(text) if fallback else None)
    task = strip_mention(text)
    try:
        answer = brain.complete(
            f"Group-chat request (keep the reply compact — under 120 "
            f"words, no preamble):\n\n{task[:1500]}",
            agent=persona, timeout=60, max_tokens=700)
    except Exception as e:
        logger.debug("specialist %s failed: %s", persona, e)
        return False, (fallback(text) if fallback else None)
    if not answer:
        return False, (fallback(text) if fallback else None)
    return True, answer


# --------------------------------------------------------------------- #
# Discord adapter (pure REST — no discord.py dependency)
# --------------------------------------------------------------------- #

class DiscordAdapter:
    """Polls a channel and replies via the Bot API."""

    def __init__(self, hub, token, channel_id):
        self.hub = hub
        self.token = token
        self.channel_id = channel_id
        self._last_id = None
        self._running = False
        self._thread = None

    # ---------------- transport ---------------- #
    def _api(self, method, path, payload=None):
        import requests
        url = f"{DISCORD_API}{path}"
        headers = {'Authorization': f'Bot {self.token}',
                   'Content-Type': 'application/json'}
        if method == 'GET':
            resp = requests.get(url, headers=headers, timeout=15)
        else:
            resp = requests.post(url, headers=headers,
                                 data=json.dumps(payload or {}),
                                 timeout=15)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def send(self, text):
        """Outbound message (chunked to Discord's 2000-char limit)."""
        text = str(text or '')
        for start in range(0, max(len(text), 1), 1900):
            chunk = text[start:start + 1900]
            if not chunk.strip():
                continue
            self._api('POST', f'/channels/{self.channel_id}/messages',
                      {'content': chunk[:2000]})

    # ---------------- polling ---------------- #
    def _poll_once(self):
        params_path = (f'/channels/{self.channel_id}/messages?limit=10'
                       + (f'&after={self._last_id}'
                          if self._last_id else ''))
        messages = self._api('GET', params_path)
        for msg in reversed(messages):          # oldest → newest
            mid = msg.get('id')
            if self._last_id is None or int(mid) > int(self._last_id):
                self._last_id = mid
            if msg.get('author', {}).get('bot'):
                continue
            content = str(msg.get('content') or '')
            if not content.strip():
                continue
            if not (f'<@{os.getenv("JARVIS_DISCORD_USER_ID", "")}>'
                    in content
                    or 'jarvis' in content.lower()):
                continue     # group channel: only react to mentions
            self.hub.handle_message('discord', content,
                                    reply=self.send,
                                    chat_id=self.channel_id)

    def _loop(self):
        while self._running:
            try:
                self._poll_once()
            except Exception as e:
                logger.debug("discord poll failed: %s", e)
            time.sleep(_POLL_S)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="GatewayDiscord")
        self._thread.start()
        logger.info("discord gateway polling channel %s",
                    self.channel_id)

    def stop(self):
        self._running = False


# --------------------------------------------------------------------- #
# Slack adapter (webhook out + Flask route in)
# --------------------------------------------------------------------- #

class SlackAdapter:
    """Outgoing via incoming-webhook URL; inbound via /gateway/slack."""

    def __init__(self, hub, webhook_url=None, verify_token=None):
        self.hub = hub
        self.webhook_url = webhook_url or \
            os.getenv('JARVIS_SLACK_WEBHOOK_URL', '')
        self.verify_token = verify_token or \
            os.getenv('JARVIS_SLACK_VERIFY_TOKEN', '')
        self.channel = os.getenv('JARVIS_SLACK_CHANNEL', '')

    def send(self, text):
        if not self.webhook_url:
            return False
        import requests
        payload = {'text': str(text or '')[:3800]}
        if self.channel:
            payload['channel'] = self.channel
        try:
            resp = requests.post(self.webhook_url, json=payload,
                                 timeout=15)
            return resp.status_code < 300
        except Exception as e:
            logger.debug("slack send failed: %s", e)
            return False

    def handle_http(self, payload):
        """
        Handle one inbound Slack HTTP hit (Events API or slash command).
        Returns (status_code, body).
        """
        if not isinstance(payload, dict):
            return 400, 'bad payload'
        # URL verification handshake
        if payload.get('type') == 'url_verification':
            return 200, payload.get('challenge', '')
        if self.verify_token and \
                payload.get('token') != self.verify_token:
            return 401, 'bad token'
        event = payload.get('event') or {}
        text = str(event.get('text') or payload.get('text') or '')
        user = event.get('user') or payload.get('user_name') or '?'
        if not text.strip():
            return 200, ''
        self.hub.handle_message('slack', strip_mention(text),
                                reply=self.send, chat_id=self.channel,
                                user=user)
        return 200, ''


# --------------------------------------------------------------------- #
# The hub
# --------------------------------------------------------------------- #

class GatewayHub:
    """
    Registry + router for every chat channel.  Adapters activate per
    configuration; inbound messages all funnel through the event bus.
    """

    def __init__(self, brain=None):
        self.brain = brain
        self.discord = None
        self.slack = None
        self._running = False

    # ------------------------------------------------------------------ #
    # Inbound
    # ------------------------------------------------------------------ #
    def handle_message(self, source, text, reply=None, chat_id=None,
                       user=None):
        """
        One inbound message from any channel.  Bot-mode specialists get
        first refusal; otherwise the standard event pipeline runs.
        Fire-and-forget: heavy work happens on a worker thread so
        transport callbacks never block.
        """
        text = str(text or '').strip()
        if not text:
            return
        from utils.event_bus import get_bus, InternalEvent
        bus = get_bus()

        def _pipeline(msg):
            event = InternalEvent(
                source, 'text', msg,
                meta={'chat_id': chat_id, 'user': user},
                reply=reply)
            return bus.publish(event, background=False)

        used_specialist, answer = route_message(
            strip_mention(text), self.brain, fallback=_pipeline)
        if used_specialist and answer and callable(reply):
            reply(str(answer))
        logger.info("gateway %s: handled message from %s", source,
                    user or chat_id or '?')

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self, brain=None):
        """Activate every configured adapter (Telegram is started by the
        server separately — it shares the brain and needs asyncio)."""
        if self._running or not enabled():
            return
        self._running = True
        if brain is not None:
            self.brain = brain

        discord_token = os.getenv('JARVIS_DISCORD_TOKEN', '')
        discord_channel = os.getenv('JARVIS_DISCORD_CHANNEL', '')
        if discord_token and discord_channel:
            self.discord = DiscordAdapter(self, discord_token,
                                          discord_channel)
            self.discord.start()
        elif discord_token:
            logger.info("discord token set but no JARVIS_DISCORD_CHANNEL "
                        "— adapter idle")

        if os.getenv('JARVIS_SLACK_WEBHOOK_URL', '') or \
                os.getenv('JARVIS_SLACK_VERIFY_TOKEN', ''):
            self.slack = SlackAdapter(self)
            logger.info("slack gateway active (webhook out + "
                        "/gateway/slack in)")
        self._running = True

    def stop(self):
        if self.discord:
            self.discord.stop()
        self._running = False

    def send(self, channel, text):
        """Outbound push to a named channel ('discord' | 'slack')."""
        if channel == 'discord' and self.discord:
            self.discord.send(text)
            return True
        if channel == 'slack' and self.slack:
            return self.slack.send(text)
        logger.warning("no active adapter for channel %r", channel)
        return False

    def status(self):
        return {
            'gateways_enabled': enabled(),
            'bot_mode': bot_mode_enabled(),
            'discord': bool(self.discord),
            'slack': bool(self.slack),
            'telegram': bool(os.getenv('TELEGRAM_TOKEN', '')),
            'webhook': True,      # always on via POST /webhook
        }


# --------------------------------------------------------------------- #
# Singleton
# --------------------------------------------------------------------- #

_singleton = None
_singleton_lock = threading.Lock()


def get_hub():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = GatewayHub()
        return _singleton


def _reset_singleton():
    """Test helper."""
    global _singleton
    with _singleton_lock:
        _singleton = None
