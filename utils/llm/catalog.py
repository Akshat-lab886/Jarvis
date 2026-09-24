"""
Model catalog — capabilities + context windows for routing decisions.

The catalog only needs to be right about the models Jarvis actually
ships defaults for; everything else falls through to name heuristics
(``caps_for``) which are deliberately optimistic: a false "yes" for
tools/vision just means the model may error and the router fails over.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    model: str
    context: int = 32768
    vision: bool = False
    tools: bool = True
    json_mode: bool = True


# provider:model → overrides.  Contexts are conservative published maxima.
CATALOG = {
    # --- Groq --------------------------------------------------------- #
    "groq:openai/gpt-oss-120b":        dict(context=131072),
    "groq:openai/gpt-oss-20b":         dict(context=131072),
    "groq:groq/compound-mini":         dict(context=131072),
    "groq:qwen/qwen3.6-27b":           dict(context=131072),
    "groq:llama-3.3-70b-versatile":    dict(context=131072, vision=False),
    "groq:meta-llama/llama-4-scout-17b-16e-instruct":
        dict(context=131072, vision=True),
    "groq:meta-llama/llama-4-maverick-17b-128e-instruct":
        dict(context=131072, vision=True),

    # --- Google Gemini ------------------------------------------------ #
    # gemini-3.6-flash mirrors Config.GEMINI_MODEL (the shipped default):
    # without an entry it falls through to the 32k heuristic and fails
    # the long_context capability check, so long-context requests would
    # wrongly skip the user's primary Gemini model.
    "google:gemini-3.6-flash":         dict(context=1048576, vision=True),
    "google:gemini-2.5-flash":         dict(context=1048576, vision=True),
    "google:gemini-2.5-pro":           dict(context=1048576, vision=True),
    "google:gemini-2.0-flash":         dict(context=1048576, vision=True),

    # --- Anthropic ---------------------------------------------------- #
    "anthropic:claude-sonnet-4-5":     dict(context=200000, vision=True),
    "anthropic:claude-sonnet-4-20250514": dict(context=200000, vision=True),
    "anthropic:claude-3-7-sonnet-latest": dict(context=200000, vision=True),
    "anthropic:claude-3-5-haiku-latest":  dict(context=200000, vision=True),

    # --- OpenAI ------------------------------------------------------- #
    "openai:gpt-4o":                   dict(context=128000, vision=True),
    "openai:gpt-4o-mini":              dict(context=128000, vision=True),
    "openai:gpt-4.1":                  dict(context=1047576, vision=True),
    "openai:gpt-4.1-mini":             dict(context=1047576, vision=True),
    "openai:gpt-4.1-nano":             dict(context=1047576, vision=True),

    # --- DeepSeek ----------------------------------------------------- #
    "deepseek:deepseek-chat":          dict(context=65536, vision=False),
    "deepseek:deepseek-reasoner":      dict(context=65536, vision=False),

    # --- OpenRouter --------------------------------------------------- #
    "openrouter:openrouter/auto":      dict(context=200000, vision=True),

    # --- Custom OpenAI-compatible endpoint ------------------------------ #
    # Local omni gateway (CUSTOM_OPENAI_BASE_URL). Omni = multimodal, so
    # vision is on; context is a conservative published-style maximum.
    "custom:oc/mimo-v2.5-free":        dict(context=128000, vision=True),

    # --- Vyce (DeepSeek v4.1) ------------------------------------------- #
    # DeepSeek v4.1: 128K context, tool use + structured outputs.
    # NOTE: Vision is *not* supported on the vyce/deepseek-v4.1 chat
    # endpoint — a controlled image probe returned a refusal ("I can't
    # analyze images..."), not a real description. So vision=False here;
    # the router will fail vision requests over to another provider
    # (Gemini/Groq-vision) instead of letting deepseek hallucinate.
    "vyce:deepseek-v4.1":              dict(context=128000, vision=False),
    # (deepseek-v4-flash / -flash-lr also accept images HTTP-wise but
    # return 200 refusals — treat the whole vyce line as text-only.)

    # --- Local SigLIP (on-device ONNX vision) ---------------------------- #
    # Zero-shot image↔text ranking, not a decoder.  vision=True so the
    # router fails vision requests here when no cloud vision model is
    # configured; tools=False (it only returns a caption string).
    "siglip:siglip-base-patch16-224":  dict(context=2048, vision=True,
                                          tools=False, json_mode=False),
}

# Name fragments that imply vision support when a model is unknown.
_VISION_HINTS = ('vl', 'vision', 'gpt-4o', 'gpt-4.1', 'gpt-5', 'gemini',
                 'claude', 'pixtral', 'llama-4', 'llava', 'qwen-vl',
                 'dots.ocr', 'multimodal')


def caps_for(provider, model):
    """ModelInfo for any (provider, model), catalog first, heuristics second."""
    key = f"{provider}:{model}"
    if key in CATALOG:
        return ModelInfo(provider=provider, model=model,
                         **CATALOG[key])
    low = (model or '').lower()
    vision = any(h in low for h in _VISION_HINTS)
    return ModelInfo(provider=provider, model=model,
                     context=32768, vision=vision)


def has_caps(provider, model, require):
    """True when the model satisfies every required capability string."""
    if not require:
        return True
    info = caps_for(provider, model)
    checks = {
        'vision': info.vision,
        'tools': info.tools,
        'json': info.json_mode,
        'long_context': info.context >= 100000,
    }
    return all(checks.get(cap, True) for cap in require)
