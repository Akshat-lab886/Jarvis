"""
Provider abstraction for the Jarvis LLM layer.

Every provider speaks the SAME message format in (OpenAI chat style:
``[{"role", "content"}]`` where ``content`` is a string or a list of
``{"type": "text"}`` / ``{"type": "image_url", "image_url": {...}}``
parts) and returns the SAME ``ChatResult`` out — whatever native API it
talks to underneath.  The router and Brain never see provider quirks.
"""

import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger("Jarvis.LLM.Base")


# --------------------------------------------------------------------- #
# Data objects
# --------------------------------------------------------------------- #

@dataclass
class Usage:
    """Token accounting (OpenAI-style field names on purpose)."""
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class ToolCall:
    """One tool invocation requested by the model."""
    id: str = ""
    name: str = ""
    # JSON-encoded arguments string (OpenAI convention; parsed lazily)
    arguments: str = "{}"


@dataclass
class ChatResult:
    """Unified completion outcome across all providers."""
    text: str = ""
    tool_calls: list = field(default_factory=list)
    usage: Usage = None
    provider: str = ""       # e.g. 'groq'
    model: str = ""          # e.g. 'openai/gpt-oss-120b'
    finish_reason: str = ""  # 'stop' | 'length' | 'tool_calls' | ...
    elapsed_s: float = 0.0
    raw: object = None       # provider-native object (debugging)

    @property
    def ok(self):
        return bool(self.text or self.tool_calls)


class ProviderError(Exception):
    """A provider call failed.  ``kind`` drives router cooldown policy."""

    def __init__(self, message, kind="generic", status=None):
        super().__init__(message)
        self.kind = kind   # 'quota'|'rate_limit'|'auth'|'context'|'timeout'|'generic'
        self.status = status


def classify_error(err) -> ProviderError:
    """Map a raw provider exception onto a ProviderError with a kind."""
    text = str(err).lower()
    status = getattr(err, 'status_code', None)
    if status is None:
        code = getattr(err, 'code', None)
        status = code if isinstance(code, int) else None
    if status == 401 or status == 403 or 'unauthorized' in text \
            or 'invalid api key' in text or 'invalid_api_key' in text:
        return ProviderError(str(err), kind='auth', status=status)
    if status == 429 or '429' in text or 'resource_exhausted' in text \
            or 'quota' in text:
        return ProviderError(str(err), kind='quota', status=status)
    if 'rate limit' in text or 'rate_limit' in text \
            or 'tokens per minute' in text:
        return ProviderError(str(err), kind='rate_limit', status=status)
    if status == 413 or 'too large' in text or 'context length' in text \
            or 'maximum context' in text:
        return ProviderError(str(err), kind='context', status=status)
    if 'timeout' in text or 'timed out' in text:
        return ProviderError(str(err), kind='timeout', status=status)
    return ProviderError(str(err), kind='generic', status=status)


# --------------------------------------------------------------------- #
# Provider interface
# --------------------------------------------------------------------- #

class BaseProvider(ABC):
    """One LLM vendor/endpoint.  Instances are shared across threads."""

    name = "base"

    def __init__(self, models=None):
        # Ordered default model chain for this provider.
        self.models = list(models or [])

    @abstractmethod
    def available(self) -> bool:
        """True when credentials/endpoint are configured and reachable."""

    @abstractmethod
    def chat(self, messages, *, model=None, tools=None, max_tokens=None,
             temperature=0.2, timeout=45, stream=False):
        """
        Run one completion.  Returns ChatResult, or a chunk iterator when
        ``stream=True``.  Raises ProviderError subclass-typed failures.
        """

    def list_models(self):
        """Live model listing when the provider supports it."""
        return list(self.models)

    # -- helpers ------------------------------------------------------- #

    @staticmethod
    def _split_images(content):
        """
        Split an OpenAI-style message content into ``(text, images)``
        where images is a list of ``(media_type, b64_data)`` tuples.
        Accepts plain strings and multimodal part lists.
        """
        if isinstance(content, str):
            return content, []
        texts, images = [], []
        for part in content or []:
            ptype = part.get('type')
            if ptype == 'text':
                texts.append(part.get('text', ''))
            elif ptype == 'image_url':
                url = (part.get('image_url') or {}).get('url', '')
                if url.startswith('data:'):
                    try:
                        header, b64 = url.split(',', 1)
                        media = header.split(':', 1)[1].split(';', 1)[0]
                    except ValueError:
                        continue
                    images.append((media or 'image/jpeg', b64))
        return "\n".join(texts), images
