"""
Jarvis Idle Hibernation
=======================

Keeps long-lived availability with near-zero idle overhead: after a
configurable quiet period the monitor SUSPENDS registered background
services (polling loops), drops heavy caches, and GCs; the first input
event (any think/execute touch) WAKES everything back up.

Register services as (name, suspend_fn, wake_fn).  Suspend must be
idempotent and non-destructive — persistent state stays on disk.

Config:
    JARVIS_IDLE_MINUTES   minutes of silence before hibernating
                          (0 disables; default 30)
"""

import os
import gc
import time
import logging
import threading

logger = logging.getLogger("Jarvis.Hibernation")


class HibernationManager:
    def __init__(self, idle_minutes=None):
        try:
            minutes = float(os.getenv('JARVIS_IDLE_MINUTES',
                                      '30') if idle_minutes is None
                            else idle_minutes)
        except ValueError:
            minutes = 30
        self.idle_seconds = max(0.0, minutes * 60)
        self.last_activity = time.time()
        self.hibernating = False

        self._services = []          # (name, suspend_fn, wake_fn)
        self._cache_droppers = []    # callables clearing in-memory caches
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------ #
    def touch(self):
        """Any inbound interaction calls this → wakes from hibernation."""
        self.last_activity = time.time()
        if self.hibernating:
            self._wake()

    def register_service(self, name, suspend_fn, wake_fn):
        self._services.append((str(name), suspend_fn, wake_fn))

    def register_cache(self, dropper_fn):
        self._cache_droppers.append(dropper_fn)

    # ------------------------------------------------------------------ #
    def _suspend_all(self):
        for name, suspend, _wake in self._services:
            try:
                suspend()
                logger.info(f"suspended service: {name}")
            except Exception as e:
                logger.warning(f"suspend {name} failed: {e}")
        for drop in self._cache_droppers:
            try:
                drop()
            except Exception:
                pass
        collected = gc.collect()
        self.hibernating = True
        logger.info(f"😴 Hibernating (idle >{self.idle_seconds / 60:.0f}m) "
                    f"— gc freed {collected} objects")

    def _wake(self):
        self.hibernating = False
        logger.info("⚡ Activity detected — waking services")
        for name, _suspend, wake in self._services:
            try:
                wake()
                logger.info(f"resumed service: {name}")
            except Exception as e:
                logger.warning(f"wake {name} failed: {e}")

    # ------------------------------------------------------------------ #
    def _loop(self):
        # Adaptive cadence: check often enough to honor small idle
        # windows, but never tighter than 2s or looser than 30s.
        poll = max(2.0, min(30.0, self.idle_seconds / 4.0)) \
            if self.idle_seconds else 30.0
        while not self._stop.wait(timeout=poll):
            try:
                if self.idle_seconds <= 0 or self.hibernating:
                    continue
                if time.time() - self.last_activity >= self.idle_seconds:
                    self._suspend_all()
            except Exception as e:
                logger.error(f"monitor error: {e}")

    def start(self):
        if self._thread is None and self.idle_seconds > 0:
            self._thread = threading.Thread(target=self._loop,
                                            daemon=True,
                                            name="HibernationMonitor")
            self._thread.start()
            logger.info(f"Hibernation monitor armed "
                        f"(idle limit: {self.idle_seconds / 60:.0f}m)")

    def stop(self):
        self._stop.set()


# Shared manager
_shared = None


def get_manager():
    global _shared
    if _shared is None:
        _shared = HibernationManager()
    return _shared


def wire_default_services(executor=None):
    """
    Register the standard suspend/wake set on the shared manager:
      - recurring automations loop (start()/stop())
      - brain context caches (skills/tools/lessons/memory)
    Pass the JarvisExecutor instance explicitly when available;
    falls back to importing the server's singleton.
    Call once at server startup.
    """
    mgr = get_manager()

    if executor is None:
        try:
            from utils.server import executor  # type: ignore
        except Exception as e:
            logger.debug(f"recurring registration skipped: {e}")
            executor = None

    if executor is not None:
        try:
            mgr.register_service(
                'recurring_automations',
                lambda: executor.recurring.stop(),
                lambda: executor.recurring.start(),
            )
        except Exception as e:
            logger.debug(f"recurring registration failed: {e}")

    def _drop_brain_caches():
        try:
            from utils.brain import brain
            brain._memory_cache = None
            brain._skills_cache = None
            brain._tools_cache = None
            brain._lessons_cache = None
        except Exception:
            pass

    mgr.register_cache(_drop_brain_caches)
