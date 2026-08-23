"""
Jarvis Complex Task Manager

Replaces the inline `complex_task` handler with a proper async task engine:

- Background thread execution (never blocks the voice loop / server)
- Structured steps with typed execution (code, search, chat, tool_call)
- **Dependency-based step ordering** — independent steps run in parallel
- Real-time progress events via SocketIO (step started, completed, failed)
- Task persistence — active and completed tasks saved to disk
- Pause / Resume / Cancel support
- Per-step retry with fallback strategy
- Error isolation — a failed step doesn't corrupt context for others

DAG Execution Model:
    Each step can declare ``depends_on`` — a list of step IDs that must
    complete before it can start.  Steps with all dependencies satisfied
    execute concurrently up to ``MAX_PARALLEL`` at a time.  Steps without
    dependencies run immediately in parallel.  If no step declares any
    dependency the task falls back to sequential execution (fully backward
    compatible).
"""

import os
import re
import json
import time
import uuid
import threading
import datetime
import logging
import traceback
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
from enum import Enum

logger = logging.getLogger("Jarvis.ComplexTask")


# ====================================================================== #
# Enums
# ====================================================================== #

class StepType(str, Enum):
    CODE = "code"
    SEARCH = "search"
    CHAT = "chat"
    TOOL = "tool"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepTimeoutError(RuntimeError):
    """Raised when a step exceeds its execution budget."""
    pass


# ====================================================================== #
# Step classification keywords
# ====================================================================== #

_CODE_KEYWORDS = frozenset([
    'write', 'code', 'script', 'program', 'calculate', 'compute',
    'process', 'analyze data', 'build', 'generate', 'parse', 'transform',
    'algorithm', 'function', 'class', 'module', 'regex', 'json',
    'csv', 'dataframe', 'matrix', 'fft', 'sort', 'filter', 'pipeline',
])
_SEARCH_KEYWORDS = frozenset([
    'research', 'search', 'find', 'look up', 'what is', 'latest',
    'current', 'compare', 'review', 'price', 'score', 'news',
    'statistics', 'survey', 'benchmark', 'alternative', 'recommend',
])
_TOOL_KEYWORDS = frozenset([
    'open', 'send email', 'create file', 'delete', 'rename',
    'move', 'copy', 'install', 'run', 'deploy', 'push',
    'screenshot', 'capture', 'download',
])


def _classify_step(step_text):
    """Determine the execution type for a step based on its text."""
    lower = step_text.lower()
    code_score = sum(1 for kw in _CODE_KEYWORDS if kw in lower)
    search_score = sum(1 for kw in _SEARCH_KEYWORDS if kw in lower)
    tool_score = sum(1 for kw in _TOOL_KEYWORDS if kw in lower)
    best = max(code_score, search_score, tool_score)
    if best == 0:
        return StepType.CHAT
    if code_score == best:
        return StepType.CODE
    if search_score == best:
        return StepType.SEARCH
    return StepType.TOOL


# ====================================================================== #
# ID normalization + DAG helpers
# ====================================================================== #

def _norm_id(value):
    """
    Canonicalize a step ID so comparisons never fail on type mismatches.

    LLMs emit IDs inconsistently ("1" vs 1 vs 1.0).  Numeric values are
    coerced to int; everything else passes through unchanged, so both
    sides of any comparison go through the same transform.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _normalize_step_map(step_map):
    """
    Build a type-agnostic view of the dependency graph.

    Returns (norm_map, key_back):
      norm_map: {norm_key -> [norm_dep_keys]}  (deps filtered to known
                steps, self-deps and duplicates removed)
      key_back: {norm_key -> original step id} (so layers can carry the
                original-typed IDs callers expect)
    """
    norm_map = {}
    key_back = {}
    for sid, info in step_map.items():
        nid = _norm_id(sid)
        if nid in key_back:
            # Duplicate normalized key — keep the first occurrence
            continue
        key_back[nid] = sid

    for nid, orig_sid in key_back.items():
        info = step_map[orig_sid]
        deps = []
        seen = set()
        for d in (info.get('depends_on') or []):
            nd = _norm_id(d)
            if nd not in key_back or nd == nid or nd in seen:
                if nd not in key_back:
                    logger.warning(f"DAG: unknown dependency {d!r} ignored")
                continue
            seen.add(nd)
            deps.append(nd)
        norm_map[nid] = deps

    return norm_map, key_back


def _detect_cycle(step_map):
    """
    Detect cycles in the dependency graph.

    Returns (has_cycle, cycle_description_or_None).
    Uses iterative DFS on the directed graph. Type-agnostic: works with
    mixed int/str IDs and deps.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    norm_map, _key_back = _normalize_step_map(step_map)
    color = {nid: WHITE for nid in norm_map}
    parent = {}

    def find_cycle(node, dep):
        cycle = [dep, node]
        cur = node
        while cur != dep:
            cur = parent.get(cur)
            if cur is None:
                break
            cycle.append(cur)
        cycle.reverse()
        return f"Cycle: {' -> '.join(str(c) for c in cycle)}"

    for start in list(norm_map):
        if color[start] != WHITE:
            continue
        stack = [start]
        while stack:
            node = stack[-1]
            if color[node] == WHITE:
                color[node] = GRAY
                for dep in norm_map[node]:
                    if color.get(dep) == GRAY:
                        return True, find_cycle(node, dep)
                    if color.get(dep) == WHITE:
                        parent[dep] = node
                        stack.append(dep)
                continue
            if color[node] == GRAY:
                color[node] = BLACK
                stack.pop()

    return False, None


def _topological_layers(step_map):
    """
    Compute execution layers for the DAG.

    Layer 0 contains all steps with no dependencies.
    Layer 1 contains steps whose dependencies are all in layer 0, etc.
    Returns a list of lists of ORIGINAL step IDs (types preserved).
    Type-agnostic: works with mixed int/str IDs and deps.
    """
    norm_map, key_back = _normalize_step_map(step_map)

    in_degree = {nid: len(deps) for nid, deps in norm_map.items()}
    dependents = defaultdict(list)
    for nid, deps in norm_map.items():
        for d in deps:
            dependents[d].append(nid)

    layers_norm = []
    ready = deque(nid for nid, deg in in_degree.items() if deg == 0)

    while ready:
        layer = list(ready)
        layers_norm.append(layer)
        next_ready = deque()
        for nid in layer:
            for dep_sid in dependents[nid]:
                in_degree[dep_sid] -= 1
                if in_degree[dep_sid] == 0:
                    next_ready.append(dep_sid)
        ready = next_ready

    # Steps that couldn't be scheduled (cycle) — surface them as a final
    # layer so the caller can skip them explicitly.
    scheduled = {nid for layer in layers_norm for nid in layer}
    unscheduled = [nid for nid in norm_map if nid not in scheduled]
    if unscheduled:
        logger.warning(f"DAG: {len(unscheduled)} steps could not be "
                       f"scheduled (cycle?) — they will be skipped")
        layers_norm.append(unscheduled)

    return [[key_back[nid] for nid in layer] for layer in layers_norm]


# ====================================================================== #
# Step-spec normalization (public — used by manager and executor)
# ====================================================================== #

def normalize_step_specs(steps):
    """
    Normalize LLM-generated step specs into clean, validated form.

    Accepts a messy mix of:
      - plain strings:                       "Research X"
      - dicts with text only:                {"text": "Research X"}
      - full dependency specs:               {"id": 1, "text": "...",
                                              "depends_on": [1, 2],
                                              "type": "search"}

    Returns ``(mode, specs)`` where ``mode`` is either ``"deps"`` (at
    least one step declares a real dependency) or ``"sequential"``, and
    ``specs`` is a list of dicts with guaranteed keys::

        {"id": <canonical id>, "text": <str>, "depends_on": [<ids>]}
        plus optional "type"

    IDs are canonicalized via :func:`_norm_id`; missing IDs are assigned
    sequentially.  Dependencies referencing unknown steps are dropped
    with a warning rather than silently corrupting the graph.
    """
    if not isinstance(steps, (list, tuple)):
        steps = [steps]

    raw = []
    for item in steps:
        if isinstance(item, dict):
            text = item.get('text') or item.get('description') \
                or item.get('step') or ''
            if not isinstance(text, str):
                text = str(text)
            spec = {"text": text.strip()}
            sid = item.get('id', item.get('step_id'))
            if sid is not None:
                spec['id'] = _norm_id(sid)
            deps = item.get('depends_on') or []
            if not isinstance(deps, (list, tuple)):
                deps = [deps]
            spec['depends_on'] = [_norm_id(d) for d in deps]
            if item.get('type'):
                spec['type'] = item['type']
            raw.append(spec)
        elif isinstance(item, str):
            raw.append({"text": item.strip(), "depends_on": []})
        else:
            raw.append({"text": str(item), "depends_on": []})

    # Drop empty-text specs; assign sequential IDs where missing
    specs = [s for s in raw if s['text']]
    used = {s.get('id') for s in specs if 'id' in s}
    auto = 1
    for spec in specs:
        if 'id' not in spec:
            while auto in used:
                auto += 1
            spec['id'] = auto
            used.add(auto)

    # Filter deps to known steps, remove self-deps and dupes
    known = {_norm_id(s['id']) for s in specs}
    has_real_deps = False
    for spec in specs:
        my_id = _norm_id(spec['id'])
        cleaned, seen = [], set()
        for d in spec['depends_on']:
            nd = _norm_id(d)
            if nd not in known:
                logger.warning(f"normalize_steps: unknown dependency "
                               f"{d!r} on step {my_id!r} ignored")
                continue
            if nd == my_id or nd in seen:
                continue
            seen.add(nd)
            cleaned.append(nd)
        spec['depends_on'] = cleaned
        if cleaned:
            has_real_deps = True

    mode = "deps" if has_real_deps else "sequential"
    return mode, specs


# ====================================================================== #
# TaskStep
# ====================================================================== #

class TaskStep:
    """A single step in a complex task."""

    def __init__(self, step_id, text, step_type=None, depends_on=None):
        self.id = step_id
        self.text = text
        self.type = step_type or _classify_step(text)
        self.depends_on = depends_on or []  # list of step IDs
        self.status = StepStatus.PENDING
        self.result = None
        self.error = None
        self.retries = 0
        self.started_at = None
        self.completed_at = None
        self.thread_id = None  # which thread executed this

    def to_dict(self):
        d = {
            "id": self.id,
            "text": self.text,
            "type": self.type,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "retries": self.retries,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }
        if self.depends_on:
            d["depends_on"] = self.depends_on
        return d

    @classmethod
    def from_dict(cls, data):
        step = cls(
            data["id"],
            data["text"],
            data.get("type"),
            depends_on=data.get("depends_on", []),
        )
        step.status = StepStatus(data.get("status", "pending"))
        step.result = data.get("result")
        step.error = data.get("error")
        step.retries = data.get("retries", 0)
        step.started_at = data.get("started_at")
        step.completed_at = data.get("completed_at")
        return step


# ====================================================================== #
# ComplexTask
# ====================================================================== #

class ComplexTask:
    """A multi-step task executed asynchronously."""

    def __init__(self, task_id, description, steps, created_at=None):
        self.id = task_id
        self.description = description
        self.steps = steps  # list of TaskStep
        self.status = TaskStatus.PENDING
        self.created_at = created_at or datetime.datetime.now().isoformat()
        self.started_at = None
        self.completed_at = None
        self.final_summary = None
        self.context = f"User request: {description}\n"
        # Reflection bookkeeping
        self.reflected = False   # one reflection cycle max per task
        self.parent_id = None    # set on recovery tasks spawned by a critic
        # Cost attribution (R8)
        self.cost_usd = 0.0
        self.cost_tokens = 0

    # --- helpers ------------------------------------------------------- #

    def step_by_id(self, step_id):
        """Look up a step by ID — tolerant of int/str type mismatches."""
        target = str(_norm_id(step_id))
        for s in self.steps:
            if str(_norm_id(s.id)) == target:
                return s
        return None

    def progress_pct(self):
        done = sum(
            1 for s in self.steps
            if s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED)
        )
        return int(done / max(len(self.steps), 1) * 100)

    def has_dependencies(self):
        """True if any step declares a dependency."""
        return any(s.depends_on for s in self.steps)

    def dependency_layers(self):
        """
        Compute execution layers based on the dependency DAG.
        Returns list of lists of step IDs.
        """
        step_map = {str(s.id): s.to_dict() for s in self.steps}
        return _topological_layers(step_map)

    # --- serialization ------------------------------------------------- #

    def to_dict(self):
        d = {
            "id": self.id,
            "description": self.description,
            "steps": [s.to_dict() for s in self.steps],
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "final_summary": self.final_summary,
            "progress": self.progress_pct(),
            "has_dependencies": self.has_dependencies(),
            "reflected": self.reflected,
            "cost_usd": round(self.cost_usd, 4),
            "cost_tokens": self.cost_tokens,
        }
        if self.parent_id:
            d["parent_id"] = self.parent_id
        if self.has_dependencies():
            d["dependency_layers"] = self.dependency_layers()
        return d

    @classmethod
    def from_dict(cls, data):
        task = cls(
            data["id"],
            data["description"],
            [TaskStep.from_dict(s) for s in data.get("steps", [])],
            data.get("created_at"),
        )
        task.status = TaskStatus(data.get("status", "pending"))
        task.started_at = data.get("started_at")
        task.completed_at = data.get("completed_at")
        task.final_summary = data.get("final_summary")
        task.reflected = bool(data.get("reflected", False))
        task.parent_id = data.get("parent_id")
        task.cost_usd = float(data.get("cost_usd", 0) or 0)
        task.cost_tokens = int(data.get("cost_tokens", 0) or 0)
        return task


# ====================================================================== #
# ComplexTaskManager
# ====================================================================== #

class ComplexTaskManager:
    """
    Manages background execution of complex tasks with progress tracking
    and dependency-based parallel execution.

    Usage::

        manager = ComplexTaskManager(executor, brain, ui_callback)

        # Simple (sequential, backward compatible)
        task = manager.create_task("Research AI trends", [
            "Search for latest AI trends in 2026",
            "Summarize the top 3 trends",
            "Write a Python script to visualize the data"
        ])

        # With dependencies (parallel where possible)
        task = manager.create_task_with_deps("Build a web scraper", [
            {"id": 1, "text": "Research best Python scraping libraries",
             "depends_on": []},
            {"id": 2, "text": "Check which sites need JS rendering",
             "depends_on": []},
            {"id": 3, "text": "Write scraping script using findings from steps 1 & 2",
             "depends_on": [1, 2]},
            {"id": 4, "text": "Test the scraper on example.com",
             "depends_on": [3]},
        ])

        manager.start_task(task.id)
    """

    MAX_RETRIES_PER_STEP = 2
    STEP_TIMEOUT = 180      # hard watchdog per step (any type)
    CODE_EXEC_TIMEOUT = 45  # subprocess limit for generated code
    MAX_CONCURRENT_TASKS = 3  # max complex tasks running at once
    MAX_PARALLEL = 4        # max steps executing simultaneously within one task
    PRUNE_DAYS = 7          # drop terminal tasks older than this on startup
    MAX_LOADED_TASKS = 100  # cap on tasks held in memory
    REFLECT_ENABLED = True  # critic-driven retry of failed work
    MAX_REFLECTION_STEPS = 4  # cap on steps in a recovery plan

    def __init__(self, executor, brain, ui_callback=None, data_dir=None):
        self.executor = executor
        self.brain = brain
        self.ui_callback = ui_callback or (lambda e, d: None)

        self._tasks = {}           # task_id -> ComplexTask
        self._active_threads = {}  # task_id -> threading.Thread
        self._lock = threading.Lock()
        self._pause_events = {}    # task_id -> threading.Event (set = running)
        self._context_locks = {}   # task_id -> threading.Lock (guards task.context)

        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # data_dir is overridable so tests can isolate persistence
        self.data_dir = data_dir or os.path.join(self.base_dir,
                                                 'workspace', 'tasks')
        os.makedirs(self.data_dir, exist_ok=True)

        self._load_tasks()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _task_path(self, task_id):
        return os.path.join(self.data_dir, f"{task_id}.json")

    def _save_task(self, task):
        try:
            with open(self._task_path(task.id), 'w') as f:
                json.dump(task.to_dict(), f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save task {task.id}: {e}")

    def _load_tasks(self):
        """
        Load persisted tasks from disk with hygiene rules:

        - Tasks that were RUNNING/PENDING when the process died are
          parked as PAUSED (marked interrupted) so they can be resumed
          manually instead of haunting the dashboard as ghost "running"
          tasks forever.
        - Terminal tasks (completed/failed/cancelled) older than
          PRUNE_DAYS are deleted from disk and not loaded.
        - At most MAX_LOADED_TASKS tasks are held in memory.
        """
        try:
            entries = []
            for fname in os.listdir(self.data_dir):
                if not fname.endswith('.json'):
                    continue
                path = os.path.join(self.data_dir, fname)
                try:
                    with open(path, 'r') as f:
                        data = json.load(f)
                    entries.append((fname, path, data))
                except Exception as e:
                    logger.warning(f"Skipping corrupt task file {fname}: {e}")

            # Newest first so the cap keeps recent work
            entries.sort(
                key=lambda e: e[2].get('created_at') or '', reverse=True
            )
            cutoff = (datetime.datetime.now()
                      - datetime.timedelta(days=self.PRUNE_DAYS))

            for fname, path, data in entries:
                try:
                    status_raw = data.get('status', 'pending')

                    # Prune stale terminal tasks
                    if status_raw in ('completed', 'failed', 'cancelled'):
                        ended = data.get('completed_at') \
                            or data.get('created_at') or ''
                        try:
                            if datetime.datetime.fromisoformat(ended) < cutoff:
                                os.remove(path)
                                continue
                        except ValueError:
                            pass  # unparseable timestamp — keep it

                    if len(self._tasks) >= self.MAX_LOADED_TASKS:
                        continue

                    task = ComplexTask.from_dict(data)

                    if task.status in (TaskStatus.RUNNING, TaskStatus.PENDING):
                        # Process died mid-task — park for manual resume
                        task.status = TaskStatus.PAUSED
                        for s in task.steps:
                            if s.status == StepStatus.RUNNING:
                                s.status = StepStatus.PENDING
                                s.error = "Interrupted by restart."
                        self._save_task(task)
                        logger.info(
                            f"Task {task.id} was interrupted by a restart "
                            f"— parked as PAUSED (resumable)."
                        )

                    self._tasks[task.id] = task
                except Exception as e:
                    logger.warning(f"Failed to load task from {fname}: {e}")
        except Exception as e:
            logger.error(f"Failed to load tasks: {e}")

    def _emit(self, event, data):
        """Emit a progress event to the dashboard."""
        try:
            self.ui_callback(event, data)
        except Exception as e:
            logger.error(f"UI emit failed ({event}): {e}")

    def _emit_task_update(self, task):
        """Emit a full task state update."""
        self._emit('task_update', task.to_dict())

    # ------------------------------------------------------------------ #
    # Task Lifecycle
    # ------------------------------------------------------------------ #
    def create_task(self, description, steps_texts):
        """
        Create a new sequential task (backward compatible).
        ``steps_texts`` is a list of plain strings (dicts tolerated —
        they are normalized and run sequentially).
        """
        _mode, specs = normalize_step_specs(steps_texts)
        if not specs:
            specs = [{"id": 1, "text": description, "depends_on": []}]
        steps = [TaskStep(s['id'], s['text'],
                          s.get('type'), s['depends_on'])
                 for s in specs]
        # Force sequential: no deps even if specs contained them
        for s in steps:
            s.depends_on = []
        task_id = str(uuid.uuid4())[:8]
        task = ComplexTask(task_id, description, steps)
        self._register_task(task)
        return task

    def create_task_with_deps(self, description, steps_specs):
        """
        Create a new task with explicit dependency information.

        ``steps_specs`` is a list of dicts, each with::

            {"id": <int>, "text": "<description>",
             "depends_on": [<id>, ...],       # optional
             "type": "<code|search|chat|tool>"}  # optional override

        IDs and dependency references are canonicalized (int-when-
        numeric) so mixed "1" / 1 input from LLMs cannot corrupt the
        graph. Unknown deps, self-deps, and cycles fall back safely.
        """
        mode, specs = normalize_step_specs(steps_specs)
        if not specs:
            raise ValueError("create_task_with_deps requires at least "
                             "one non-empty step.")
        if mode != "deps":
            # Nothing actually depends on anything — plain sequential
            return self.create_task(description,
                                    [s['text'] for s in specs])

        steps = [TaskStep(s['id'], s['text'], s.get('type'),
                          list(s['depends_on']))
                 for s in specs]

        # Validate the DAG
        step_map = {str(s.id): {'depends_on': s.depends_on} for s in steps}
        has_cycle, cycle_info = _detect_cycle(step_map)
        if has_cycle:
            logger.warning(f"Dependency cycle detected: {cycle_info}. "
                           "Falling back to sequential execution.")
            for s in steps:
                s.depends_on = []

        task = ComplexTask(str(uuid.uuid4())[:8], description, steps)
        self._register_task(task)
        return task

    def _register_task(self, task):
        """Common registration logic for both create methods."""
        with self._lock:
            self._tasks[task.id] = task
            self._pause_events[task.id] = threading.Event()
            self._pause_events[task.id].set()
            self._context_locks[task.id] = threading.Lock()
        self._save_task(task)
        self._emit_task_update(task)

    def get_task(self, task_id):
        with self._lock:
            return self._tasks.get(task_id)

    def get_recent_tasks(self, limit=10):
        """Return the most recent tasks (completed or active)."""
        with self._lock:
            all_tasks = sorted(self._tasks.values(),
                               key=lambda t: t.created_at, reverse=True)
        return all_tasks[:limit]

    def get_active_tasks(self):
        """Return only tasks that are pending, running, or paused."""
        with self._lock:
            return sorted(
                [t for t in self._tasks.values()
                 if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING,
                                 TaskStatus.PAUSED)],
                key=lambda t: t.created_at, reverse=True
            )

    def get_history(self, limit=30):
        """Return completed, failed, or cancelled tasks (newest first)."""
        with self._lock:
            history = sorted(
                [t for t in self._tasks.values()
                 if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                                 TaskStatus.CANCELLED)],
                key=lambda t: t.completed_at or t.created_at, reverse=True
            )
        return history[:limit]

    def get_history_stats(self):
        """Aggregate stats for the history view."""
        with self._lock:
            tasks = list(self._tasks.values())
        total = len(tasks)
        completed = sum(1 for t in tasks if t.status == TaskStatus.COMPLETED)
        failed = sum(1 for t in tasks if t.status == TaskStatus.FAILED)
        cancelled = sum(1 for t in tasks if t.status == TaskStatus.CANCELLED)
        total_steps = sum(len(t.steps) for t in tasks)
        completed_steps = sum(
            sum(1 for s in t.steps if s.status == StepStatus.COMPLETED)
            for t in tasks
        )
        return {
            'total': total,
            'completed': completed,
            'failed': failed,
            'cancelled': cancelled,
            'total_steps': total_steps,
            'completed_steps': completed_steps,
        }

    def start_task(self, task_id):
        """Start executing a task in a background thread."""
        thread = None
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False, "Task not found."
            if task.status in (TaskStatus.RUNNING,):
                return False, "Task is already running."
            if not task.steps:
                return False, "Task has no steps to execute."
            if len(self._active_threads) >= self.MAX_CONCURRENT_TASKS:
                return False, "Too many concurrent tasks. Wait for one to finish."

            task.status = TaskStatus.RUNNING
            task.started_at = datetime.datetime.now().isoformat()
            if task_id not in self._pause_events:
                self._pause_events[task_id] = threading.Event()
            self._pause_events[task_id].set()
            if task_id not in self._context_locks:
                self._context_locks[task_id] = threading.Lock()

            # Register the thread inside the lock so the concurrency cap
            # can never be raced past.
            mode = "parallel" if task.has_dependencies() else "sequential"
            thread = threading.Thread(
                target=self._run_task, args=(task_id,), daemon=True,
                name=f"Task-{task_id}"
            )
            self._active_threads[task_id] = thread

        self._save_task(task)
        self._emit_task_update(task)

        self.executor.mouth.speak(
            f"Executing your task with {len(task.steps)} steps ({mode}), Sir."
        )

        thread.start()
        return True, f"Task started ({len(task.steps)} steps, {mode})."

    def pause_task(self, task_id):
        """Pause a running task (waits at current step boundary)."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status != TaskStatus.RUNNING:
                return "Task is not running."
            event = self._pause_events.get(task_id)
            if event:
                event.clear()
            task.status = TaskStatus.PAUSED
        self._save_task(task)
        self._emit_task_update(task)
        return "Task paused."

    def resume_task(self, task_id):
        """Resume a paused task."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status != TaskStatus.PAUSED:
                return "Task is not paused."
            event = self._pause_events.get(task_id)
            if event:
                event.set()
            task.status = TaskStatus.RUNNING
        self._save_task(task)
        self._emit_task_update(task)
        return "Task resumed."

    def cancel_task(self, task_id):
        """Cancel a task (stops at next checkpoint)."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return "Task not found."
            task.status = TaskStatus.CANCELLED
            event = self._pause_events.get(task_id)
            if event:
                event.set()  # unblock so thread can exit
        self._save_task(task)
        self._emit_task_update(task)
        return "Task cancelled."

    # ------------------------------------------------------------------ #
    # Background Execution
    # ------------------------------------------------------------------ #
    def _run_task(self, task_id):
        """Execute all steps — sequential or parallel depending on DAG."""
        task = self._tasks.get(task_id)
        if not task:
            return

        # Snapshot budget for cost attribution (R8)
        try:
            from utils.budget import get_budget
            _budget_start = get_budget().status()
        except Exception:
            _budget_start = None

        recovery = None
        try:
            if task.has_dependencies():
                self._run_task_dag(task)
            else:
                self._run_task_sequential(task)

            # Synthesize final result if not cancelled
            if task.status == TaskStatus.RUNNING:
                # One bounded reflection cycle: the critic reviews the
                # failures and may run a revised recovery plan first.
                recovery = self._reflect(task) if not task.parent_id else None

                task.status = TaskStatus.COMPLETED
                task.completed_at = datetime.datetime.now().isoformat()
                task.final_summary = self._synthesize(task, recovery=recovery)
                # Cost attribution (R8)
                try:
                    if _budget_start:
                        from utils.budget import get_budget
                        cur = get_budget().status()
                        task.cost_usd = max(0, cur['spent_usd'] - _budget_start['spent_usd'])
                        task.cost_tokens = max(0, cur['spent_tokens'] - _budget_start['spent_tokens'])
                except Exception:
                    pass
                self._save_task(task)
                self._emit_task_update(task)

                summary = task.final_summary or "Task complete, Sir."
                self.executor.mouth.speak(summary)
                self._emit('ai_text', {'text': summary})

            # Autonomous procedural learning: distill lessons + skills
            try:
                from utils import self_learning
                self_learning.review_task(
                    self.brain, task, recovery=recovery if task.status == TaskStatus.COMPLETED else None,
                    registry=getattr(self.executor, 'skills', None))
            except Exception as e:
                logger.debug(f"self-review skipped: {e}")

        except Exception as e:
            logger.error(f"Task {task_id} failed: {e}\n{traceback.format_exc()}")
            task.status = TaskStatus.FAILED
            task.completed_at = datetime.datetime.now().isoformat()
            self._save_task(task)
            self._emit_task_update(task)
            self.executor.mouth.speak("Task encountered a critical error, Sir.")

        finally:
            with self._lock:
                self._active_threads.pop(task_id, None)

    # ------------------------------------------------------------------ #
    # Reflection (critic-driven recovery)
    # ------------------------------------------------------------------ #
    def _reflect(self, task):
        """
        Ask the critic subagent to diagnose failed/skipped steps and,
        if it proposes a viable revision, run that recovery plan as an
        inline child task.

        Bounded by design: at most one cycle per task, and children
        never spawn their own reflections.  Returns a dict describing
        the outcome, or None when reflection is not applicable.
        """
        failed = [s for s in task.steps if s.status == StepStatus.FAILED]
        skipped = [s for s in task.steps if s.status == StepStatus.SKIPPED]

        if not failed and not skipped:
            return None
        if task.reflected or not self.REFLECT_ENABLED:
            return None
        if self._should_stop(task):
            return None
        task.reflected = True

        failure_lines = []
        for s in failed:
            failure_lines.append(
                f"- [FAILED] Step {s.id}: {s.text}\n"
                f"    Error: {(s.error or 'unknown')[:200]}"
            )
        for s in skipped:
            failure_lines.append(
                f"- [SKIPPED] Step {s.id}: {s.text}\n"
                f"    Reason: {(s.error or '')[:160]}"
            )
        done_preview = "\n".join(
            f"- Step {s.id} ({s.text[:60]}): {str(s.result)[:120]}"
            for s in task.steps if s.status == StepStatus.COMPLETED
        )

        critic_prompt = (
            f"Original request: {task.description}\n\n"
            f"Steps that COMPLETED (do NOT redo these):\n"
            f"{done_preview or '(none completed)'}\n\n"
            f"Steps that FAILED or were SKIPPED:\n"
            + "\n".join(failure_lines)
            + f"\n\nAccumulated context so far:\n{task.context[:1500]}\n\n"
              "Assess the situation and output your JSON decision."
        )

        try:
            raw = self.brain.complete(critic_prompt, agent='critic',
                                      timeout=self.STEP_TIMEOUT)
        except Exception as e:
            logger.warning(f"Task {task.id}: critic call failed: {e}")
            return None

        decision = self._extract_json(raw or '')
        if not isinstance(decision, dict):
            logger.info(f"Task {task.id}: critic produced no parsable "
                        f"decision — skipping recovery")
            return None

        assessment = str(decision.get('assessment', ''))[:200]
        if decision.get('no_revision'):
            logger.info(f"Task {task.id}: critic declined revision — "
                        f"{assessment}")
            return {"assessment": assessment, "recovered": False}

        _mode, specs = normalize_step_specs(decision.get('revised_steps') or [])
        if not specs:
            return {"assessment": assessment, "recovered": False}
        specs = specs[:self.MAX_REFLECTION_STEPS]

        try:
            child = self.create_task_with_deps(
                f"[Recovery] {task.description}", specs
            )
        except ValueError:
            return None
        child.parent_id = task.id

        self._emit('task_reflection', {
            "task_id": task.id,
            "child_id": child.id,
            "assessment": assessment,
            "steps": len(child.steps),
        })
        logger.info(f"Task {task.id}: reflecting ({len(failed)} failed / "
                    f"{len(skipped)} skipped) → recovery task {child.id} "
                    f"with {len(child.steps)} step(s)")

        # Run the child inline on this thread.  No nested reflection.
        child.status = TaskStatus.RUNNING
        child.started_at = datetime.datetime.now().isoformat()
        try:
            if child.has_dependencies():
                self._run_task_dag(child)
            else:
                self._run_task_sequential(child)
            child.status = TaskStatus.COMPLETED
            child.completed_at = datetime.datetime.now().isoformat()
        except Exception as e:
            logger.error(f"Recovery task {child.id} crashed: {e}")
            child.status = TaskStatus.FAILED
            child.completed_at = datetime.datetime.now().isoformat()
        finally:
            self._save_task(child)
            self._emit_task_update(child)

        done_steps = [s for s in child.steps
                      if s.status == StepStatus.COMPLETED]
        recovered = bool(done_steps)
        child_summary = "; ".join(
            f"{s.text[:60]} → {str(s.result)[:100]}" for s in done_steps
        ) or "(recovery steps also failed)"

        # Fold the child's results into the parent's context so the
        # synthesizer can use them.
        ctx_lock = self._context_locks.get(task.id)
        entry = (f"\n[Recovery attempt '{child.id}' — "
                 f"{assessment or 'no assessment'}]\n"
                 f"{child_summary}\n")
        if ctx_lock:
            with ctx_lock:
                task.context += entry
        else:
            task.context += entry

        return {
            "assessment": assessment,
            "recovered": recovered,
            "child": {"id": child.id, "summary": child_summary},
        }

    # ------------------------------------------------------------------ #
    # Sequential execution (no dependencies)
    # ------------------------------------------------------------------ #
    def _run_task_sequential(self, task):
        """Run steps one after another — the original behavior."""
        for step in task.steps:
            if self._should_stop(task):
                break
            self._wait_if_paused(task)
            if self._should_stop(task):
                break

            self._execute_step(task, step)
            self._save_task(task)
            self._emit_task_update(task)

    # ------------------------------------------------------------------ #
    # DAG execution (with dependencies)
    # ------------------------------------------------------------------ #
    def _run_task_dag(self, task):
        """
        Execute steps according to their dependency DAG.

        Steps in the same layer run in parallel (up to MAX_PARALLEL).
        A layer's steps must all complete (or fail/skip) before the next
        layer starts.  A step whose dependencies failed or were skipped
        is itself SKIPPED so downstream work never runs on missing data.
        """
        layers = task.dependency_layers()
        total = len(task.steps)
        logger.info(f"Task {task.id}: DAG has {len(layers)} layers, "
                     f"{total} steps total")

        for layer_idx, layer_ids in enumerate(layers):
            if self._should_stop(task):
                break

            # Skip any pending steps whose dependencies did not complete.
            # Loop because skipping a step can block its dependents too.
            skipped_here = self._skip_blocked_steps(task)
            if skipped_here:
                self._save_task(task)
                self._emit_task_update(task)

            # Filter to steps that are still pending
            pending_steps = []
            for sid in layer_ids:
                step = task.step_by_id(sid)
                if step and step.status == StepStatus.PENDING:
                    pending_steps.append(step)

            if not pending_steps:
                continue

            logger.info(
                f"Task {task.id}: Layer {layer_idx + 1}/{len(layers)} — "
                f"{len(pending_steps)} step(s): "
                f"{[s.id for s in pending_steps]}"
            )

            if len(pending_steps) == 1:
                # Single step — run directly, no thread overhead
                self._wait_if_paused(task)
                if self._should_stop(task):
                    break
                self._execute_step(task, pending_steps[0])
                self._save_task(task)
                self._emit_task_update(task)
            else:
                # Multiple independent steps — run in parallel
                self._run_layer_parallel(task, pending_steps, layer_idx)

    def _skip_blocked_steps(self, task):
        """
        Mark PENDING steps as SKIPPED when any of their dependencies
        FAILED or was SKIPPED.  Cascades transitively.  Returns True if
        anything was marked.
        """
        bad = {StepStatus.FAILED, StepStatus.SKIPPED}
        changed = False
        status_by_id = {_norm_id(s.id): s.status for s in task.steps}
        for _pass in range(len(task.steps)):
            pass_changed = False
            for step in task.steps:
                if step.status != StepStatus.PENDING:
                    continue
                my_id = _norm_id(step.id)
                blocked = [d for d in step.depends_on
                           if status_by_id.get(_norm_id(d)) in bad]
                if blocked:
                    step.status = StepStatus.SKIPPED
                    step.error = (
                        "Skipped: dependency step(s) "
                        f"{blocked} failed or were skipped."
                    )
                    step.completed_at = datetime.datetime.now().isoformat()
                    status_by_id[my_id] = StepStatus.SKIPPED
                    pass_changed = True
                    changed = True
                    self._emit('task_step_skipped', {
                        "task_id": task.id,
                        "step": step.to_dict(),
                        "progress": task.progress_pct(),
                    })
            if not pass_changed:
                break
        return changed

    def _run_layer_parallel(self, task, steps, layer_idx):
        """Execute a set of independent steps in parallel via ThreadPool."""
        max_workers = min(len(steps), self.MAX_PARALLEL)

        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"Task-{task.id}-L{layer_idx}"
        ) as pool:
            futures = {}
            for step in steps:
                # Check stop condition before submitting
                if self._should_stop(task):
                    break
                self._wait_if_paused(task)
                if self._should_stop(task):
                    break

                future = pool.submit(self._execute_step, task, step)
                futures[future] = step

            # Wait for all steps in this layer to finish
            for future in as_completed(futures):
                step = futures[future]
                try:
                    future.result()  # raises if _execute_step raised
                except Exception as e:
                    logger.error(
                        f"Task {task.id} step {step.id} thread failed: {e}"
                    )

            self._save_task(task)
            self._emit_task_update(task)

    # ------------------------------------------------------------------ #
    # Checkpoints (pause / cancel)
    # ------------------------------------------------------------------ #
    def _should_stop(self, task):
        """Return True if the task should stop (cancelled or failed)."""
        return task.status in (TaskStatus.CANCELLED, TaskStatus.FAILED)

    def _wait_if_paused(self, task):
        """Block until the task is resumed (or cancelled — event is set)."""
        pause_event = self._pause_events.get(task.id)
        if pause_event:
            pause_event.wait()

    # ------------------------------------------------------------------ #
    # Step execution
    # ------------------------------------------------------------------ #
    def _run_with_timeout(self, fn, timeout, *args):
        """
        Run ``fn`` under a hard watchdog.

        Python threads cannot be killed, so a timed-out worker is
        abandoned (its underlying call will eventually error out on its
        own client-side timeouts) while the task moves on immediately.
        """
        if not timeout or timeout <= 0:
            return fn(*args)
        pool = ThreadPoolExecutor(max_workers=1,
                                  thread_name_prefix="step-watchdog")
        try:
            future = pool.submit(fn, *args)
            try:
                return future.result(timeout=timeout)
            except FutureTimeoutError:
                future.cancel()
                raise StepTimeoutError(
                    f"Step exceeded its {timeout}s execution budget."
                )
        finally:
            pool.shutdown(wait=False)

    def _execute_step(self, task, step):
        """Execute a single step with retries and error isolation."""
        step.status = StepStatus.RUNNING
        step.started_at = datetime.datetime.now().isoformat()
        step.thread_id = threading.current_thread().name
        self._emit('task_step_started', {
            "task_id": task.id,
            "step": step.to_dict(),
            "progress": task.progress_pct(),
        })

        max_retries = self.MAX_RETRIES_PER_STEP
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                result = self._run_with_timeout(
                    self._dispatch_step, self.STEP_TIMEOUT, task, step
                )
                step.status = StepStatus.COMPLETED
                step.result = result
                step.completed_at = datetime.datetime.now().isoformat()
                step.retries = attempt

                # Thread-safe context accumulation
                ctx_lock = self._context_locks.get(task.id)
                ctx_entry = (
                    f"\nStep {step.id} ({step.text[:80]}):\n"
                    f"Result: {str(result)[:500]}\n"
                )
                if ctx_lock:
                    with ctx_lock:
                        task.context += ctx_entry
                else:
                    task.context += ctx_entry

                self._emit('task_step_completed', {
                    "task_id": task.id,
                    "step": step.to_dict(),
                    "progress": task.progress_pct(),
                })
                return  # success

            except StepTimeoutError as e:
                # A hung step will hang again — don't burn retries on it.
                logger.warning(
                    f"Task {task.id} step {step.id} timed out: {e}"
                )
                last_error = e
                step.retries = attempt + 1
                break

            except Exception as e:
                last_error = e
                step.retries = attempt + 1
                logger.warning(
                    f"Task {task.id} step {step.id} attempt "
                    f"{attempt + 1} failed: {e}"
                )
                if attempt < max_retries:
                    time.sleep(1)

        # All retries exhausted (or timed out)
        timed_out = isinstance(last_error, StepTimeoutError)
        step.status = StepStatus.FAILED
        step.error = str(last_error)[:300]
        step.completed_at = datetime.datetime.now().isoformat()

        # Add failure to context so later steps can adapt
        ctx_lock = self._context_locks.get(task.id)
        fail_entry = (
            f"\nStep {step.id} ({step.text[:80]}): FAILED"
            f"{' (timed out)' if timed_out else ''} — {step.error}\n"
            f"(Continuing with remaining steps despite this failure.)\n"
        )
        if ctx_lock:
            with ctx_lock:
                task.context += fail_entry
        else:
            task.context += fail_entry

        self._emit('task_step_failed', {
            "task_id": task.id,
            "step": step.to_dict(),
            "progress": task.progress_pct(),
        })

    # ------------------------------------------------------------------ #
    # Step dispatch
    # ------------------------------------------------------------------ #
    def _dispatch_step(self, task, step):
        """Route a step to the appropriate executor based on its type."""
        if step.type == StepType.CODE:
            return self._exec_code_step(task, step)
        if step.type == StepType.SEARCH:
            return self._exec_search_step(task, step)
        if step.type == StepType.TOOL:
            return self._exec_tool_step(task, step)
        return self._exec_chat_step(task, step)

    def _exec_code_step(self, task, step):
        """Generate and execute Python code for a step."""
        code_prompt = (
            f"Task: {step.text}\n"
            f"Context from previous steps:\n{task.context}\n\n"
            f"Write a Python script to accomplish this step.\n"
            f"- Use only standard library unless the task clearly needs an external package.\n"
            f"- Print the final result clearly.\n"
            f"- Handle errors gracefully.\n"
            f"Return ONLY raw Python code, no markdown, no explanations."
        )
        raw_code = self.brain.complete(code_prompt, agent='coder',
                                       timeout=self.STEP_TIMEOUT)
        clean_code = self._clean_code(raw_code)

        if not clean_code:
            raise RuntimeError("Brain did not generate valid code.")

        result = self.executor.coder.execute_with_retry(
            clean_code, max_retries=0, timeout=self.CODE_EXEC_TIMEOUT
        )

        if result['success']:
            return result['output']

        # Auto-fix
        fix_prompt = (
            f"The following Python script failed.\n"
            f"Error:\n{result['output'][:400]}\n\n"
            f"Script:\n{clean_code[:1200]}\n\n"
            f"Original task: {step.text}\n"
            f"Return ONLY the corrected full Python script."
        )
        fixed_code = self._clean_code(
            self.brain.complete(fix_prompt, agent='coder',
                                timeout=self.STEP_TIMEOUT) or ''
        )

        if fixed_code and fixed_code != clean_code:
            result2 = self.executor.coder.execute_with_retry(
                fixed_code, max_retries=0, timeout=self.CODE_EXEC_TIMEOUT
            )
            if result2['success']:
                return f"(Fixed on retry) {result2['output']}"

        raise RuntimeError(f"Code execution failed: {result['output'][:200]}")

    def _exec_search_step(self, task, step):
        """Search the web for a step's information needs."""
        from utils.web_reader import get_answer_from_web

        self._emit('status', {'message': f"Searching: {step.text[:50]}..."})
        web_content = get_answer_from_web(step.text)

        if not web_content or len(web_content.strip()) < 20:
            return self._exec_chat_step(task, step)

        summary_prompt = (
            f"Search results for '{step.text}':\n"
            f"{web_content[:3000]}\n\n"
            f"Provide a concise, useful summary of the findings."
        )
        summary = self.brain.complete(summary_prompt, agent='researcher',
                                      timeout=self.STEP_TIMEOUT)
        return summary or web_content[:1000]

    def _exec_chat_step(self, task, step):
        """Use the brain for reasoning, analysis, or general knowledge."""
        prompt = (
            f"Task: {step.text}\n"
            f"Context:\n{task.context}\n\n"
            f"Provide a clear, concise result for this step."
        )
        result = self.brain.complete(prompt, agent='researcher',
                                     timeout=self.STEP_TIMEOUT)
        if not result:
            raise RuntimeError("Brain returned empty response.")
        return result

    _TOOL_SYSTEM = (
        "You are Jarvis's task-execution module. Convert the user's "
        "requested action into EXACTLY ONE command as raw JSON "
        "(no markdown, no prose). Available actions include: open_app, "
        "open_web, download_file (query + file_type), send_email "
        "(recipient + message), create_file/write_file via dev_write, "
        "run shell via dev_command, capture_photo, clean_downloads. "
        "If no tool fits, output: {\"action\": \"chat\", "
        "\"response\": \"<what you would do>\"}"
    )

    def _exec_tool_step(self, task, step):
        """Route tool-type steps through the executor.

        Privilege separation: only a tools-allowed persona ('operator')
        may fire side effects.  Any other assignment degrades to pure
        reasoning with no real-world action.
        """
        from utils.agents import get_profile
        profile = get_profile('operator')
        if profile is None or not profile.tools_allowed:
            raise RuntimeError("Tool execution blocked: no privileged "
                               "operator profile available.")

        compose_prompt = (
            f"Requested action: {step.text}\n"
            f"Task context:\n{task.context[:600]}\n\n"
            f"Return ONLY the JSON command to execute."
        )
        resp_text = self.brain.complete(
            compose_prompt, agent='operator',
            timeout=self.STEP_TIMEOUT
        ) or ''
        cmd = self._extract_json(resp_text)

        if isinstance(cmd, dict) and cmd.get('action') \
                and cmd.get('action') != 'chat':
            return self.executor.execute_command(cmd, self.brain)

        # No executable action identified — fall back to reasoning
        response = (cmd or {}).get('response') if isinstance(cmd, dict) else None
        if response:
            return response
        raise RuntimeError("Tool step could not be resolved to an action.")

    @staticmethod
    def _extract_json(text):
        """Best-effort extraction of a JSON object from raw LLM text."""
        if not text:
            return None
        cleaned = text.strip()
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'```\s*$', '', cleaned).strip()
        try:
            data = json.loads(cleaned)
            return data if isinstance(data, dict) else None
        except (ValueError, TypeError):
            pass
        match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
                return data if isinstance(data, dict) else None
            except (ValueError, TypeError):
                pass
        return None

    # ------------------------------------------------------------------ #
    # Synthesis
    # ------------------------------------------------------------------ #
    def _synthesize(self, task, recovery=None):
        """Generate a final summary of the completed task."""
        completed_steps = [
            s for s in task.steps
            if s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED)
        ]
        failed_steps = [s for s in task.steps if s.status == StepStatus.FAILED]
        skipped_steps = [s for s in task.steps if s.status == StepStatus.SKIPPED]

        recovery_child = (recovery or {}).get('child') or {}
        recovered = bool((recovery or {}).get('recovered'))
        assessment = str((recovery or {}).get('assessment', ''))

        # Nothing worked — don't dress it up as success.
        if not completed_steps and failed_steps:
            if recovered and recovery_child.get('summary'):
                return (
                    "The original approach ran into problems, Sir, but a "
                    "revised plan rescued the task. "
                    f"{recovery_child['summary']}"
                )
            reasons = "; ".join(
                f"Step {s.id}: {s.error}" for s in failed_steps[:3]
            )
            return (
                f"I'm sorry Sir, the task could not be completed. "
                f"All {len(failed_steps)} step(s) failed. "
                f"Details: {reasons}"
            )

        step_results = []
        for s in completed_steps:
            result_preview = (s.result or '')[:300]
            step_results.append(
                f"Step {s.id} ({s.text[:60]}): {result_preview}"
            )

        summary_prompt = (
            f"The user asked: {task.description}\n\n"
            f"Results of each step:\n\n"
            + "\n\n".join(step_results)
        )
        if failed_steps:
            summary_prompt += (
                f"\n\nNote: {len(failed_steps)} step(s) FAILED: "
                + ", ".join(f"S{s.id}: {(s.error or '')[:80]}"
                            for s in failed_steps)
            )
        if skipped_steps:
            summary_prompt += (
                f"\n\nNote: {len(skipped_steps)} step(s) were SKIPPED "
                f"because their dependencies failed: "
                + ", ".join(f"S{s.id}" for s in skipped_steps)
            )
        if recovery:
            summary_prompt += (
                f"\n\nA critic review afterwards ({assessment or 'no assessment'}) "
                f"attempted recovery of the failures."
                + (f" It produced: {recovery_child.get('summary', '')}"
                   if recovered else " The recovery also failed.")
            )
        if task.has_dependencies():
            summary_prompt += (
                "\n\nNote: Steps ran in parallel where dependencies allowed."
            )

        summary_prompt += (
            "\n\nProvide a clear, concise final answer summarizing everything. "
            "Address the user as 'Sir'."
        )

        resp = self.brain.complete(summary_prompt, agent='synthesizer',
                                   timeout=self.STEP_TIMEOUT)
        return resp or self._fallback_summary(task)

    def _fallback_summary(self, task):
        """Simple text summary when brain synthesis fails."""
        lines = [f"Task: {task.description}", ""]
        for s in task.steps:
            icon = "✅" if s.status == StepStatus.COMPLETED else "❌"
            result = (s.result or s.error or '')[:100]
            deps = f" (depends on {s.depends_on})" if s.depends_on else ""
            lines.append(f"{icon} Step {s.id}: {s.text[:60]}{deps}")
            if result:
                lines.append(f"   → {result}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Code cleanup utility
    # ------------------------------------------------------------------ #
    _FENCE_RE = re.compile(r'```(?:python|py)?\s*\n(.*?)```', re.DOTALL)

    @classmethod
    def _clean_code(cls, raw_code):
        """
        Extract executable Python from a raw LLM response.

        Prefers fenced ```python blocks; otherwise treats the whole
        response as code (minus stray fences).  The old line-heuristic
        silently dropped leading assignment-only scripts.
        """
        if not raw_code:
            return ""
        text = raw_code.strip()

        fences = cls._FENCE_RE.findall(text)
        if fences:
            return max(fences, key=len).strip()

        text = text.replace("```python", "").replace("```", "").strip()
        return text
