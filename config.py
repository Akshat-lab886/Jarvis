import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.getenv('SECRET_KEY') or os.urandom(32).hex()
    PORT = int(os.getenv('PORT', 5001))
    DEBUG = os.getenv('DEBUG', 'False') == 'True'

    # Serve HTTPS with a persistent self-signed cert (browser warning once, then trusted)
    HTTPS = os.getenv('HTTPS', 'False') == 'True'

    # Groq API key
    GROQ_API_KEY = os.getenv('GROQ_API_KEY')

    # Primary provider: Vyce (DeepSeek v4.1), OpenAI-compatible endpoint.
    # NOTE: base_url is the API ROOT (https://vyceai.com/v1); the openai
    # SDK appends "/chat/completions" itself, so a full endpoint URL here
    # would produce a doubled ".../chat/completions/chat/completions" 404.
    VYCE_API_KEY = os.getenv('VYCE_API_KEY', 'sk-3584dfb613f16ff98b2915ee1b7330a19c4f9367e25ab452')
    VYCE_BASE_URL = os.getenv('VYCE_BASE_URL', 'https://vyceai.com/v1')
    VYCE_MODEL = os.getenv('VYCE_MODEL', 'deepseek-v4.1')
    VYCE_TIMEOUT_S = float(os.getenv('VYCE_TIMEOUT_S', '120'))

    # Models tried in order until one responds.
    # Groq models: https://console.groq.com/docs/models
    # Deprecated (Aug 2026): llama-3.3-70b-versatile, llama-3.1-8b-instant, gemma2-9b-it
    # Recommended replacements: openai/gpt-oss-120b, openai/gpt-oss-20b, groq/compound-mini, qwen/qwen3.6-27b
    # Balanced: capable model first, fast fallback, powerful last resort
    DEFAULT_MODELS = [
        "openai/gpt-oss-20b",
        "groq/compound-mini",
        "qwen/qwen3.6-27b",
        "openai/gpt-oss-120b",
    ]
    _env_models = [m.strip() for m in os.getenv('GROQ_MODELS', '').split(',') if m.strip()]
    MODELS = _env_models or DEFAULT_MODELS

    # Models that can accept images (used to prioritize vision-capable models
    # when the user attaches/screenshots an image).
    # NOTE: No vision models currently available on Groq; vision falls back to Gemini.
    VISION_MODELS = []

    # Gemini direct-connection fallback (primary when GOOGLE_API_KEY is set)
    GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
    GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.6-flash')

    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    # Comma-separated Telegram user IDs allowed to control Jarvis. Empty = locked down.
    TELEGRAM_ALLOWED_IDS = [int(i.strip()) for i in os.getenv('TELEGRAM_ALLOWED_IDS', '').split(',') if i.strip().isdigit()]

    # Reminder polling interval in seconds
    REMINDER_POLL_INTERVAL = int(os.getenv('REMINDER_POLL_INTERVAL', '10'))

    # Max steps for the desktop agent loop
    DESKTOP_AGENT_MAX_STEPS = int(os.getenv('DESKTOP_AGENT_MAX_STEPS', '8'))

    # How long a user-uploaded photo is protected from webcam overwrites (seconds)
    UPLOAD_PROTECTION_SECONDS = int(os.getenv('UPLOAD_PROTECTION_SECONDS', '300'))

    # Generated-code execution engine: 'auto' (docker if available),
    # 'docker' (require container), or 'local' (never containerize)
    CODE_SANDBOX = os.getenv('CODE_SANDBOX', 'auto')
