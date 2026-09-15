"""
J.A.R.V.I.S. — RLM: the reasoning layer (the "Reasoning Language Model"
half).

Frontier agents reason *before* they act and correct course when tools
fail.  This module gives the agent loop those two reflexes without
taxing simple chat:

    should_plan(prompt)        microsecond heuristic complexity gate
    make_plan(brain, prompt)   one planner call → short working plan
    critique_failures(...)     one critic call → strategy correction

Everything degrades to None (and the loop continues unchanged) on any
failure — the reasoning layer augments the agent, never gates it.
"""

import os
import re
import json
import logging

logger = logging.getLogger("Jarvis.RLM.Reasoner")

MAX_PLAN_CHARS = 900

# Multi-step / research / composition signals — a hit means the request
# probably deserves an explicit plan before tools start firing.
_PLAN_SIGNALS = (
    'research', 'compare', 'plan', 'schedule', 'organize', 'organise',
    'analyze', 'analyse', 'summarize', 'summarise', 'build', 'create',
    'prepare', 'draft', 'book', 'arrange', 'put together', 'find out',
    'figure out', 'work out', 'itinerary', 'roadmap', 'workflow',
    'step by step', 'and then', 'after that', 'once that', 'while you',
    'budget for', 'check if', 'make sure',
)


def plan_enabled():
    """JARVIS_AGENT_PLAN=0 turns plan-injection off (reasoning tools
    stay live)."""
    return os.getenv('JARVIS_AGENT_PLAN', '1') != '0'


def verify_enabled():
    """JARVIS_AGENT_VERIFY=0 disables the critic's final-answer check."""
    return os.getenv('JARVIS_AGENT_VERIFY', '1') != '0'


# Reasoning-effort escalation cues — the user explicitly asking the
# agent to slow down and reason harder (Hermes-style depth control).
_DEEP_CUES = (
    'think harder', 'think deeply', 'think carefully', 'think step',
    'reason carefully', 'reason through', 'reason step by step',
    'deeply analyze', 'think this through', 'really think',
    'take your time', 'be thorough', 'ultrathink', 'meditate on',
    'work through it carefully', 'carefully consider',
)


def base_effort():
    """Global reasoning effort from env: normal | deep."""
    return 'deep' if os.getenv('JARVIS_REASON_EFFORT', '').strip().lower() \
        in ('deep', 'high', 'xhigh', 'max') else 'normal'


def detect_effort(prompt):
    """
    'deep' when the user asks for deeper reasoning (or the env default
    says so), else 'normal'.  No LLM call.
    """
    if base_effort() == 'deep':
        return 'deep'
    t = str(prompt or '').lower()
    return 'deep' if any(cue in t for cue in _DEEP_CUES) else 'normal'


def should_plan(prompt):
    """
    Heuristic complexity gate — no LLM call, microseconds.

    Long requests, chained steps ("and then…"), or two-plus planning
    verbs → True.  Small talk, single questions and one-shot commands
    → False.
    """
    t = str(prompt or '').strip().lower()
    if len(t) < 24:
        return False
    hits = sum(1 for s in _PLAN_SIGNALS if s in t)
    if hits >= 2:
        return True
    if hits >= 1 and len(t) > 120:
        return True
    return len(t) > 400


def _clean(raw, max_chars=MAX_PLAN_CHARS):
    text = str(raw or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```(json|markdown)?|```$', '', text).strip()
    if not text:
        return None
    # Planner personas may answer with {"steps": [...]} — flatten it.
    try:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match and '"steps"' in match.group(0):
            data = json.loads(match.group(0))
            steps = data.get('steps') or []
            flat = []
            for s in steps:
                if isinstance(s, dict):
                    s = s.get('text', '')
                if str(s).strip():
                    flat.append(f"- {str(s).strip()}")
            if flat:
                text = "\n".join(flat)
    except (json.JSONDecodeError, AttributeError, ValueError):
        pass
    text = re.sub(r'\n{3,}', '\n\n', text)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…"
    return text or None


def make_plan(brain, prompt):
    """
    Ask the planner persona for a short working plan for *prompt*.
    Returns plan text or None (never raises).
    """
    if brain is None:
        return None
    try:
        raw = brain.complete(
            f"Draft a short working plan for fulfilling this request.\n\n"
            f"REQUEST: {str(prompt)[:600]}\n\n"
            "3-6 numbered one-line steps, concrete and tool-oriented "
            "(name the tool/action each step needs). Keep it tight — "
            "this primes an agent that executes immediately.",
            agent='planner', timeout=35, max_tokens=350)
    except Exception as e:
        logger.debug("plan generation failed: %s", e)
        return None
    return _clean(raw)


def critique_failures(brain, prompt, failures):
    """
    Ask the critic persona to correct strategy after repeated tool
    errors.  ``failures`` is a list of ``(tool_name, error_text)``.
    Returns a one-or-two-line correction or None (never raises).
    """
    if brain is None or not failures:
        return None
    lines = "\n".join(
        f"- Tool '{name}' failed: {str(err)[:200]}"
        for name, err in failures[-4:])
    try:
        raw = brain.complete(
            f"An agent working for the user keeps failing tools.\n\n"
            f"USER REQUEST: {str(prompt)[:300]}\n"
            f"FAILURES:\n{lines}\n\n"
            "In ONE or TWO sentences: diagnose the likely cause and "
            "state the corrected next action. Output only those "
            "sentences — no JSON, no preamble.",
            agent='critic', timeout=30, max_tokens=160)
    except Exception as e:
        logger.debug("critique failed: %s", e)
        return None
    text = str(raw or '').strip()
    if not text:
        return None
    # Some critics still emit their task-shaped JSON — flatten it.
    try:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                bits = [str(data.get('assessment', '')).strip()]
                steps = data.get('revised_steps') or []
                if steps and isinstance(steps[0], dict):
                    bits.append("Next: " + str(
                        steps[0].get('text', '')).strip())
                text = " — ".join(b for b in bits if b)
    except (json.JSONDecodeError, AttributeError, ValueError):
        pass
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:400] or None


def verify_answer(brain, prompt, answer):
    """
    Hermes-style self-check: after a tool-using run, the critic decides
    whether the draft answer actually satisfies the request.

    Returns ``(ok, revised)``:
      (True, None)     — approved (or verification unavailable)
      (False, text)    — replaced by the critic's revised answer
    Never raises; one bounded LLM call.
    """
    if brain is None or not answer:
        return True, None
    try:
        raw = brain.complete(
            f"USER REQUEST: {str(prompt)[:400]}\n\n"
            f"DRAFT ANSWER:\n{str(answer)[:2000]}\n\n"
            "Does the draft answer FULLY satisfy the request — correct, "
            "complete, nothing promised but missing?\n"
            "If yes, output exactly: APPROVED\n"
            "If no, output: REVISED: <the corrected complete answer>",
            agent='critic', timeout=40, max_tokens=900)
    except Exception as e:
        logger.debug("verify call failed: %s", e)
        return True, None
    text = str(raw or '').strip()
    if not text:
        return True, None
    if text.startswith('APPROVED'):
        return True, None
    if text.upper().startswith('REVISED'):
        revised = text.split(':', 1)[-1].strip() if ':' in text \
            else text[7:].strip()
        if len(revised) > 20:
            return False, revised
    # Unparsable verdict — trust the draft rather than loop on noise.
    return True, None
