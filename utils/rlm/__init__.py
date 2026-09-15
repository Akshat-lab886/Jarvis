"""
RLM — Jarvis's Recursive Language Model core.

Two halves, one goal: a personal assistant whose memory and reasoning
both scale past the context window.

    utils/rlm/memory.py     recursive memory hierarchy (L0 events →
                            L1 summaries → L2 abstractions → L3 world
                            model/playbook), background consolidation,
                            reflection, and query-time recursive recall
                            with temporal scoring, supersession and
                            cross-session continuity
    utils/rlm/entities.py   temporal entity graph — who/what memory is
                            about, with counts, last-seen and
                            co-occurrence edges (subject queries)
    utils/rlm/reasoner.py   the reasoning layer: plan-before-acting,
                            and critic-driven strategy correction for
                            the agent loop

Both integrate through Brain/AgentLoop with full rollback switches
(``JARVIS_RLM=0``, ``JARVIS_AGENT_PLAN=0``).
"""

from utils.rlm.entities import EntityGraph      # noqa: F401
from utils.rlm.memory import (          # noqa: F401
    RecursiveMemory,
    get_rlm,
    enabled,
    _reset_singleton,
)
from utils.rlm.plan_state import (      # noqa: F401
    PlanState,
    get_plan_state,
)
from utils.rlm import reasoner          # noqa: F401

__all__ = ['RecursiveMemory', 'get_rlm', 'enabled', 'PlanState',
           'get_plan_state', 'EntityGraph', 'reasoner']
