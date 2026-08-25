"""
Jarvis LLM layer — provider-agnostic brain plumbing (BYOK).

Anyone can bring ANY provider key and everything works::

    from utils.llm import get_router

    result = get_router().chat(messages, require={'vision'})
    print(result.text)          # unified across Groq/Gemini/Claude/GPT/...

Public surface:
    get_router()            — process-wide Router singleton
    ChatResult / ToolCall   — unified completion objects
    AllProvidersError       — raised when the whole chain fails
    get_keystore()          — runtime API-key store (overlay + env)
"""

from utils.llm.router import (Router, get_router, AllProvidersError)
from utils.llm.providers.base import ChatResult, ToolCall, Usage
from utils.llm.keystore import Keystore, get_keystore

__all__ = [
    "Router", "get_router", "AllProvidersError",
    "ChatResult", "ToolCall", "Usage",
    "Keystore", "get_keystore",
]
