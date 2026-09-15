"""
J.A.R.V.I.S. — Mixture of Agents (MoA) Engine (Hermes parity)
=============================================================

Aggregates feedback from multiple DISTINCT model perspectives before
generating the final answer: the same prompt is sent to N different
providers in the router fleet (e.g. Gemini + Groq + DeepSeek), and a
synthesizer persona merges the proposals into one answer that keeps
each model's strengths and discards its quirks.

    from utils.moa import ask_moa
    answer = ask_moa(brain, "design a rate limiter for my API")

Also exposed as the executor action ``ask_moa`` and callable from the
agent loop.  ``JARVIS_MOA=0`` disables it (callers fall back to a
single-model answer).  ``JARVIS_MOA_N`` sets the panel size (default 3).
"""

import os
import logging

logger = logging.getLogger("Jarvis.MoA")

_MAX_PROPOSAL_CHARS = 4000


def enabled():
    return os.getenv('JARVIS_MOA', '1') != '0'


def panel_size():
    try:
        return max(2, min(5, int(os.getenv('JARVIS_MOA_N', '3'))))
    except ValueError:
        return 3


def _panel_providers(router, n):
    """
    Up to *n* distinct configured providers, in the router's own
    preference order.

    The registry is keyed by vendor (``build_providers`` emits at most
    one object per vendor — groq, google, anthropic, ...), so distinct
    providers here *is* diverse vendors: the whole point of the mixture
    is different perspectives, not different models from one vendor.  We
    walk ``router._ordered_providers()`` (which honours
    ``JARVIS_PROVIDER_ORDER`` / local-first) so the panel reflects
    operator intent rather than registry insertion order, and we never
    seat the same provider object twice even if a registry ever aliases
    it under a second key.
    """
    if router is None:
        return []
    order = None
    if getattr(router, '_ordered_providers', None) is not None:
        try:
            order = router._ordered_providers()
        except Exception:
            order = None
    if not order:
        try:
            order = list(router.providers)
        except Exception:
            return []

    panel = []
    seen = set()
    for name in order:
        if len(panel) >= n:
            break
        try:
            provider = router.providers.get(name)
        except Exception:
            provider = None
        if provider is None or id(provider) in seen:
            continue
        try:
            if not provider.list_models():
                continue
        except Exception:
            pass
        seen.add(id(provider))
        panel.append(name)
    return panel


def ask_moa(brain, prompt, n=None):
    """
    Run the mixture.  Returns a dict:
        {'answer', 'models': [...], 'proposals': {provider: text}}
    or None when the panel can't be assembled (caller falls back to a
    single-model answer).  Never raises.
    """
    if not enabled() or brain is None:
        return None
    router = getattr(brain, 'router', None)
    n = n or panel_size()
    panel = _panel_providers(router, n)
    if len(panel) < 2:
        logger.debug("MoA skipped: fewer than 2 providers configured")
        return None

    prompt = str(prompt or '')
    messages = [
        {"role": "system",
         "content": "You are one independent expert in a mixture-of-"
                    "agents panel. Give your best, self-contained answer."},
        {"role": "user", "content": prompt[:6000]},
    ]

    proposals = {}
    for provider in panel:
        try:
            result = router.chat(messages, models=[provider],
                                 max_tokens=900, timeout=45)
            text = (getattr(result, 'text', '') or '').strip()
            if text:
                proposals[provider] = text[:_MAX_PROPOSAL_CHARS]
        except Exception as e:
            logger.debug("MoA proposal from %s failed: %s", provider, e)

    if len(proposals) < 2:
        # A one-model "panel" is just a normal answer — don't pretend.
        if len(proposals) == 1:
            only = next(iter(proposals.values()))
            return {'answer': only, 'models': list(proposals),
                    'proposals': proposals}
        return None

    # Aggregate: the synthesizer persona merges the perspectives.
    panel_text = "\n\n".join(
        f"--- PROPOSAL from {provider} ---\n{text}"
        for provider, text in proposals.items())
    synthesis_prompt = (
        "You are the aggregator of a mixture-of-agents panel. Several "
        "models answered the same request independently.\n\n"
        f"REQUEST:\n{prompt[:3000]}\n\n{panel_text[:9000]}\n\n"
        "Produce the single best final answer: correct where they "
        "agree, choose the most reliable view where they differ, add "
        "anything important only one model caught, and omit their "
        "meta-commentary. Output ONLY the final answer."
    )
    try:
        answer = brain.complete(synthesis_prompt, agent='synthesizer',
                                timeout=60, max_tokens=1200)
    except Exception as e:
        logger.debug("MoA synthesis failed: %s", e)
        answer = None
    answer = (answer or '').strip()
    if not answer:
        # Degraded: first proposal is still an answer
        answer = next(iter(proposals.values()))

    logger.info("MoA answer merged from %d model(s): %s",
                len(proposals), ", ".join(proposals))
    return {'answer': answer, 'models': list(proposals),
            'proposals': proposals}


def format(brain, prompt, n=None):
    """Executor-facing rendering of ask_moa."""
    result = ask_moa(brain, prompt, n=n)
    if result is None:
        return None
    header = (f"[mixture of {len(result['models'])} models: "
              f"{', '.join(result['models'])}]")
    return f"{header}\n\n{result['answer']}"
