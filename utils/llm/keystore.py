"""
Runtime API-key storage for BYOK.

Resolution order: ``config/providers.json`` overlay (editable from the
dashboard at runtime, chmod 0600) → environment variables.  Keys are
never logged; ``status()`` returns masked previews only.
"""

import os
import json
import threading
import logging

# The router/keystore is also used standalone (probes, scripts) where
# config.py — the normal load_dotenv() caller — may never be imported.
# Load .env here too so CUSTOM_OPENAI_* / JARVIS_PROVIDER_ORDER from the
# file are visible. Idempotent; no-op when already loaded.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logger = logging.getLogger("Jarvis.LLM.Keystore")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

# provider -> env var consulted when the overlay has no entry
ENV_MAP = {
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "custom": "CUSTOM_OPENAI_API_KEY",
}

# Providers that never need a key (local servers).
KEYLESS = ("ollama", "lmstudio")


class Keystore:

    def __init__(self, path=None):
        self.path = path or os.path.join(ROOT, 'config', 'providers.json')
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- #

    def _read_overlay(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, 'r') as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.warning("providers.json unreadable: %s", e)
        return {}

    def get(self, provider):
        """API key for *provider* (overlay first, then env), else None."""
        with self._lock:
            overlay = self._read_overlay()
        val = overlay.get(provider) or ""
        if isinstance(val, dict):
            val = val.get('api_key', '')
        val = (val or "").strip()
        if val:
            return val
        env_name = ENV_MAP.get(provider)
        return os.getenv(env_name, '').strip() or None if env_name else None

    def set(self, provider, key):
        """Persist a key to the runtime overlay (survives restart)."""
        with self._lock:
            overlay = self._read_overlay()
            overlay[provider] = {"api_key": str(key).strip()}
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd = os.open(self.path,
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as fh:
                json.dump(overlay, fh, indent=2)
        logger.info("API key stored for provider '%s'", provider)

    def delete(self, provider):
        with self._lock:
            overlay = self._read_overlay()
            if provider in overlay:
                del overlay[provider]
                # Rewrite with 0600 like set(): a plain open() would
                # re-persist any REMAINING keys at the default (often
                # world-readable 0644) umask.
                fd = os.open(self.path,
                             os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w') as fh:
                    json.dump(overlay, fh, indent=2)

    # ---------------------------------------------------------------- #

    @staticmethod
    def mask(key):
        if not key:
            return ""
        if len(key) <= 10:
            return key[:3] + "…" + key[-2:]
        return f"{key[:6]}…{key[-4:]}"

    def status(self):
        """Masked per-provider configuration map (for the dashboard)."""
        out = {}
        for provider in ENV_MAP:
            key = self.get(provider)
            out[provider] = {"configured": bool(key),
                             "preview": self.mask(key) if key else ""}
        return out


_singleton = None
_singleton_lock = threading.Lock()


def get_keystore():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = Keystore()
        return _singleton
