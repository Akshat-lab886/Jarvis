import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'mysecret')
    PORT = int(os.getenv('PORT', 5001))
    DEBUG = os.getenv('DEBUG', 'True') == 'True'

    # Serve HTTPS with a persistent self-signed cert (browser warning once, then trusted)
    HTTPS = os.getenv('HTTPS', 'False') == 'True'

    # Support multiple keys (comma separated) for redundancy
    _keys = os.getenv('OPENROUTER_API_KEY', '')
    OPENROUTER_API_KEYS = [k.strip() for k in _keys.split(',') if k.strip()]
    OPENROUTER_API_KEY = OPENROUTER_API_KEYS[0] if OPENROUTER_API_KEYS else None

    # Models tried in order until one responds.
    # NOTE: OpenRouter free endpoints change frequently. The list below was
    # verified against https://openrouter.ai/api/v1/models (free variants) and
    # can be overridden entirely with the OPENROUTER_MODELS env var
    # (comma-separated slugs, tried in order).
    DEFAULT_MODELS = [
        "z-ai/glm-5.2:free",
        "openai/gpt-oss-20b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "google/gemma-4-31b-it:free",
        "nvidia/nemotron-nano-12b-v2-vl:free",
    ]
    _env_models = [m.strip() for m in os.getenv('OPENROUTER_MODELS', '').split(',') if m.strip()]
    MODELS = _env_models or DEFAULT_MODELS

    # Models that can accept images (used to prioritize vision-capable models
    # when the user attaches/screenshots an image).
    VISION_MODELS = [
        "nvidia/nemotron-nano-12b-v2-vl:free",
        "dots-studio/dots-3-note-preview:free",
        "openai/gpt-oss-20b:free",
    ]

    # Gemini direct-connection fallback (primary when GOOGLE_API_KEY is set)
    GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
    GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')

    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    # Comma-separated Telegram user IDs allowed to control Jarvis. Empty = locked down.
    TELEGRAM_ALLOWED_IDS = [int(i.strip()) for i in os.getenv('TELEGRAM_ALLOWED_IDS', '').split(',') if i.strip().isdigit()]

    # Reminder polling interval in seconds
    REMINDER_POLL_INTERVAL = int(os.getenv('REMINDER_POLL_INTERVAL', '10'))

    # Max steps for the desktop agent loop
    DESKTOP_AGENT_MAX_STEPS = int(os.getenv('DESKTOP_AGENT_MAX_STEPS', '8'))

    # How long a user-uploaded photo is protected from webcam overwrites (seconds)
    UPLOAD_PROTECTION_SECONDS = int(os.getenv('UPLOAD_PROTECTION_SECONDS', '300'))
