"""
Jarvis Mission Control (Legacy Wrapper)

This module now delegates to ComplexTaskManager. It is kept for backward
compatibility — the 'start mission' keyword in quick_actions.py calls
create_plan / start_mission through this class.

For new code, use utils.complex_task.ComplexTaskManager directly.
"""

import json
import os
import logging
import threading

logger = logging.getLogger("Jarvis.MissionControl")


class MissionPlanner:
    def __init__(self, executor=None, brain=None):
        self.brain = brain
        self.executor = executor
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.mission_file = os.path.join(self.base_dir, 'workspace', 'current_mission.json')

    def create_plan(self, user_goal):
        """
        Ask the planner subagent to break a goal into steps, then create
        a ComplexTask and start it. Returns the number of steps.
        """
        prompt = (
            f"The user wants: '{user_goal}'.\n"
            f"Break this into 2-5 concrete sub-tasks. Each step should be a\n"
            f"clear action: research, write code, analyze, summarize, etc.\n"
            f'Output ONLY raw JSON: {{"steps": ["step 1", "step 2", ...]}}'
        )

        # Route through the planner persona — side-effect-free call
        response_text = None
        try:
            response_text = self.brain.complete(prompt, agent='planner')
        except Exception as e:
            logger.warning(f"Planner complete() failed: {e}")

        steps_data = None
        if response_text:
            import re
            try:
                cleaned = re.sub(r'^```(?:json)?\s*', '',
                                 str(response_text).strip())
                cleaned = re.sub(r'```\s*$', '', cleaned).strip()
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict) and 'steps' in parsed:
                    steps_data = parsed
                else:
                    match = re.search(r'(\{.*\})', cleaned,
                                      re.DOTALL)
                    if match:
                        parsed = json.loads(match.group(1))
                        if isinstance(parsed, dict) and 'steps' in parsed:
                            steps_data = parsed
            except Exception:
                pass

        if not steps_data or 'steps' not in steps_data:
            logger.warning("Mission Control: Failed to generate valid plan.")
            return 0

        steps = steps_data['steps']

        # Create and start via ComplexTaskManager
        if self.executor:
            self.executor.task_manager.brain = self.brain
            self.executor.task_manager.ui_callback = getattr(
                self.executor, '_ui_callback', lambda e, d: None
            )
            task = self.executor.task_manager.create_task(user_goal, steps)
            self.executor.task_manager.start_task(task.id)
            self._task_id = task.id
            return len(steps)

        # Fallback: save to file for manual inspection
        os.makedirs(os.path.dirname(self.mission_file), exist_ok=True)
        with open(self.mission_file, 'w') as f:
            json.dump({'mission': user_goal, 'steps': steps}, f, indent=4)
        return len(steps)

    def start_mission(self):
        """
        If we have a running task, return its summary. Otherwise fall back
        to the old file-based approach.
        """
        # If we created a task via ComplexTaskManager, wait for it
        task_id = getattr(self, '_task_id', None)
        if task_id and self.executor:
            import time
            tm = self.executor.task_manager
            task = tm.get_task(task_id)
            if task:
                # Wait for completion (with timeout)
                for _ in range(300):  # 5 min max
                    task = tm.get_task(task_id)
                    if task and task.status.value in ('completed', 'failed', 'cancelled'):
                        break
                    time.sleep(1)
                task = tm.get_task(task_id)
                if task and task.final_summary:
                    return task.final_summary
                return "Mission completed, Sir."

        # Legacy fallback
        if not os.path.exists(self.mission_file):
            return "No mission to start."

        try:
            with open(self.mission_file, 'r') as f:
                mission_data = json.load(f)
            steps = mission_data.get('steps', [])
            return f"Mission '{mission_data.get('mission', 'Unknown')}' with {len(steps)} steps."
        except Exception:
            return "Mission data unavailable."
