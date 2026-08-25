"""
Provider factory — builds every provider the user's keys unlock.
"""

import os
import logging

from utils.llm.keystore import get_keystore
from utils.llm.providers.openai_compat import OpenAICompatProvider
from utils.llm.providers.anthropic_api import AnthropicProvider
from utils.llm.providers.gemini_api import GeminiProvider

logger = logging.getLogger("Jarvis.LLM.Factory")


def _env_models(env_name, fallback):
    raw = [m.strip() for m in os.getenv(env_name, '').split(',')
           if m.strip()]
    return raw or list(fallback)


def build_providers(keystore=None):
    """
    Return ``{name: provider}`` for every provider whose credentials are
    present (or that needs none).  Never raises.
    """
    ks = keystore or get_keystore()
    providers = {}

    def _add(provider):
        try:
            if provider.available():
                providers[provider.name] = provider
                logger.info("Provider '%s' online (%d model(s))",
                            provider.name, len(provider.models))
            else:
                logger.info("Provider '%s' not configured — skipped",
                            provider.name)
        except Exception as e:
            logger.warning("Provider '%s' probe failed: %s",
                           provider.name, e)

    # --- Groq (existing key) ------------------------------------------ #
    groq_key = ks.get('groq')
    if groq_key:
        try:
            from config import Config
            legacy = list(Config.MODELS or [])
        except Exception:
            legacy = []
        _add(OpenAICompatProvider(
            "groq", "https://api.groq.com/openai/v1", key=groq_key,
            models=legacy or _env_models("GROQ_MODELS", [
                "openai/gpt-oss-120b",
                "openai/gpt-oss-20b",
                "groq/compound-mini",
            ])))

    # --- Google Gemini (existing key) --------------------------------- #
    google_key = ks.get('google')
    if google_key:
        try:
            from config import Config
            gem_first = Config.GEMINI_MODEL
        except Exception:
            gem_first = "gemini-2.5-flash"
        gem_models = [gem_first] + [m for m in
                                    ("gemini-2.5-flash", "gemini-2.5-pro")
                                    if m != gem_first]
        _add(GeminiProvider(key=google_key, models=gem_models))

    # --- Anthropic ----------------------------------------------------- #
    anthropic_key = ks.get('anthropic')
    if anthropic_key:
        _add(AnthropicProvider(key=anthropic_key, models=_env_models(
            "ANTHROPIC_MODELS",
            ["claude-sonnet-4-5", "claude-3-5-haiku-latest"])))

    # --- OpenAI -------------------------------------------------------- #
    openai_key = ks.get('openai')
    if openai_key:
        _add(OpenAICompatProvider(
            "openai", "https://api.openai.com/v1", key=openai_key,
            models=_env_models("OPENAI_MODELS", [
                "gpt-4.1-mini", "gpt-4o-mini", "gpt-4o",
            ])))

    # --- DeepSeek ------------------------------------------------------- #
    deepseek_key = ks.get('deepseek')
    if deepseek_key:
        _add(OpenAICompatProvider(
            "deepseek", "https://api.deepseek.com/v1", key=deepseek_key,
            models=_env_models("DEEPSEEK_MODELS",
                               ["deepseek-chat", "deepseek-reasoner"])))

    # --- OpenRouter ------------------------------------------------------ #
    openrouter_key = ks.get('openrouter')
    if openrouter_key:
        _add(OpenAICompatProvider(
            "openrouter", "https://openrouter.ai/api/v1",
            key=openrouter_key,
            models=_env_models("OPENROUTER_MODELS", ["openrouter/auto"])))

    # --- Custom OpenAI-compatible endpoint ------------------------------- #
    custom_url = os.getenv('CUSTOM_OPENAI_BASE_URL', '').strip()
    custom_key = ks.get('custom')
    if custom_url and (custom_key or os.getenv('CUSTOM_OPENAI_KEYLESS')
                       == '1'):
        _add(OpenAICompatProvider(
            "custom", custom_url, key=custom_key,
            models=_env_models("CUSTOM_OPENAI_MODELS", [])))

    # --- Local servers (no key needed; probed for reachability) ---------- #
    ollama_url = os.getenv('OLLAMA_BASE_URL',
                           'http://localhost:11434/v1').strip()
    _add(OpenAICompatProvider("ollama", ollama_url, key=None,
                              key_optional=True, dynamic_models=True))

    lmstudio_url = os.getenv('LMSTUDIO_BASE_URL',
                             'http://localhost:1234/v1').strip()
    _add(OpenAICompatProvider("lmstudio", lmstudio_url, key=None,
                              key_optional=True, dynamic_models=True))

    return providers
