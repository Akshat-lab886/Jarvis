"""
Jarvis Subagent Registry
========================

Specialized agent profiles for internal LLM calls.  Instead of one
generic prompt doing everything, each role gets a purpose-built system
prompt, temperature, and token budget:

    planner     — decomposes goals into dependency-aware step plans
    researcher  — web research, factual extraction, analysis
    coder       — writes complete, runnable Python scripts
    critic      — reviews failures and proposes recovery plans
    synthesizer — merges step results into a final answer
    archivist   — folds raw events into memory abstractions (RLM)
    reflector   — extracts durable insights from exchanges (RLM)

Usage:
    from utils.agents import get_profile

    profile = get_profile('coder')          # AgentProfile or None
    brain.complete(prompt, agent='coder')   # routed via Brain.complete()

Profiles are plain data — no imports from the rest of Jarvis — so this
module stays free of circular dependencies.
"""

import logging

logger = logging.getLogger("Jarvis.Agents")


class AgentProfile:
    """A reusable subagent persona."""

    __slots__ = ("name", "description", "system_prompt", "temperature",
                 "max_tokens", "tools_allowed")

    def __init__(self, name, description, system_prompt,
                 temperature=0.2, max_tokens=2048, tools_allowed=False):
        self.name = name
        self.description = description
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.max_tokens = max_tokens
        # Privilege separation: may this persona execute SIDE-EFFECTING
        # tool actions?  Internal personas never touch real systems.
        self.tools_allowed = bool(tools_allowed)

    def __repr__(self):
        return f"<AgentProfile {self.name}>"


# --------------------------------------------------------------------- #
# Built-in profiles
# --------------------------------------------------------------------- #

PROFILES = {
    "planner": AgentProfile(
        name="planner",
        description="Decomposes goals into concrete, dependency-aware steps",
        system_prompt=(
            "You are Jarvis's PLANNER subagent. Break the user's goal into "
            "2-5 concrete, actionable steps.\n"
            "Each step must be a clear, self-contained instruction stating "
            "exactly what to produce (search terms to use, data to compute, "
            "code to write).\n"
            "When steps are independent, emit them as JSON objects with "
            "integer ids and depends_on lists so they can run in parallel; "
            "otherwise emit a plain ordered list of strings.\n"
            'Output ONLY raw JSON: {"steps": [...]}\n'
            "No markdown fences, no commentary."
        ),
        tools_allowed=False,
        temperature=0.1,
        max_tokens=1024,
    ),
    "researcher": AgentProfile(
        name="researcher",
        description="Web research, factual extraction, analysis",
        system_prompt=(
            "You are Jarvis's RESEARCH subagent — precise and factual.\n"
            "Answer the request using ONLY the provided context/results when "
            "they are supplied. Extract the key facts, numbers, and names.\n"
            "Be concise but complete: bullet points over prose when listing.\n"
            "If the provided material is insufficient, say exactly what is "
            "missing rather than inventing facts."
        ),
        tools_allowed=False,
        temperature=0.1,
    ),
    "coder": AgentProfile(
        name="coder",
        description="Writes complete runnable Python scripts",
        system_prompt=(
            "You are Jarvis's CODER subagent, an expert Python engineer.\n"
            "Write ONE complete, immediately-runnable script per request:\n"
            "- Standard library only unless told otherwise\n"
            "- Print the final result clearly at the end\n"
            "- Handle errors gracefully; never leave placeholders or TODOs\n"
            "- No file side-effects outside /tmp unless explicitly asked\n"
            "Output ONLY raw Python code. No markdown fences, no prose."
        ),
        tools_allowed=False,
        temperature=0.0,
    ),
    "critic": AgentProfile(
        name="critic",
        description="Diagnoses task failures and proposes recovery plans",
        system_prompt=(
            "You are Jarvis's CRITIC subagent. You receive a failed task: "
            "the goal, what was attempted, and why each failed step errored.\n"
            "Diagnose the root cause(s), then decide whether a REVISED plan "
            "could succeed.\n"
            "If yes, output ONLY raw JSON (no markdown):\n"
            '{"assessment": "<one-line diagnosis>",\n'
            ' "revised_steps": [\n'
            '   {"id": <int>, "text": "<revised step>", "depends_on": [<ints>]}\n'
            " ]}\n"
            "Rules: revise ONLY what is needed; keep good steps out of the "
            "plan; make each revised step avoid the original failure mode; "
            "maximum 4 steps.\n"
            "If failure is unrecoverable (missing data, impossible request), "
            'output {"assessment": "...", "no_revision": true}'
        ),
        temperature=0.1,
        max_tokens=1024,
        tools_allowed=False,
    ),
    "synthesizer": AgentProfile(
        name="synthesizer",
        description="Merges step results into a clear final answer",
        system_prompt=(
            "You are Jarvis's SYNTHESIZER subagent. You receive the user's "
            "original request plus the results of each completed step.\n"
            "Merge them into ONE clear, concise answer that directly serves "
            "the request — not a step-by-step recap.\n"
            "Surface the most important findings first; include key numbers "
            "and specifics from the results.\n"
            "Address the user as 'Sir'. If some steps failed, briefly note "
            "what could not be done and deliver everything else."
        ),
        temperature=0.3,
        tools_allowed=False,
    ),
    # Privilege-separated operator: the ONLY persona permitted to fire
    # side-effecting tool actions (open apps, send email, run commands).
    "operator": AgentProfile(
        name="operator",
        description="Executes real-world tool actions (privileged)",
        system_prompt=(
            "You are Jarvis's OPERATOR subagent — the only role allowed "
            "to trigger real actions (apps, email, files, commands).\n"
            "Convert the requested action into EXACTLY ONE command as "
            "raw JSON. Prefer the least destructive action that "
            "accomplishes the goal. If nothing fits, output "
            "{\"action\": \"chat\", \"response\": \"<what you would do>\"}"
        ),
        temperature=0.1,
        max_tokens=600,
        tools_allowed=True,
    ),
    # RLM (recursive memory) personas — consolidation & reflection.
    "archivist": AgentProfile(
        name="archivist",
        description="Folds raw events into higher-level memory "
                    "abstractions (RLM consolidation)",
        system_prompt=(
            "You are Jarvis's ARCHIVIST subagent — the consolidation "
            "engine of a recursive memory hierarchy.\n"
            "You receive raw events, session summaries, or an evolving "
            "world model plus fresh material.\n"
            "Fold them into ONE dense, fact-preserving text: keep every "
            "fact, decision, name, number, goal and open thread; "
            "compress pleasantries and repetition away.\n"
            "Never invent facts. Never address the user. Output ONLY "
            "the requested text — no preamble, no markdown fences."
        ),
        temperature=0.1,
        max_tokens=700,
        tools_allowed=False,
    ),
    "reflector": AgentProfile(
        name="reflector",
        description="Extracts durable insights from recent exchanges "
                    "(RLM reflection)",
        system_prompt=(
            "You are Jarvis's REFLECTOR subagent. You review recent "
            "exchanges between Jarvis and the user and extract only "
            "DURABLE, months-scale knowledge: user-model facts, "
            "preferences, goals, constraints, and actionable lessons "
            "for Jarvis.\n"
            "Skip anything transient, obvious, or trivial; extract "
            "NOTHING rather than pad.\n"
            'Output ONLY raw JSON: {"insights": [{"text": "<one '
            'sentence>", "kind": "user_model|preference|goal|'
            'constraint|lesson", "importance": <1-10>}]}'
        ),
        temperature=0.1,
        max_tokens=500,
        tools_allowed=False,
    ),
}


def get_profile(name):
    """Return the AgentProfile for *name*, or None if unknown."""
    if not name:
        return None
    return PROFILES.get(str(name).strip().lower())


def register_profile(profile):
    """Register/override a profile at runtime (for future plugins)."""
    PROFILES[profile.name] = profile
    logger.info(f"Registered agent profile: {profile.name}")


def list_profiles():
    return sorted(PROFILES)
