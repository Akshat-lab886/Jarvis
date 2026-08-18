import json
import os
from utils.brain import Brain
from utils.logger import web_log

class MissionPlanner:
    def __init__(self, executor=None, brain=None):
        # Reuse the shared Brain when available to avoid extra API clients
        self.brain = brain or Brain()
        self.executor = executor # Reference to JarvisExecutor
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.mission_file = os.path.join(self.base_dir, 'workspace', 'current_mission.json')

    def create_plan(self, user_goal):
        """
        Sends a prompt to the Brain (LLM) to break a complex task into sub-tasks.
        """
        web_log(f"Mission Control: Planning for goal: '{user_goal}'")
        
        prompt = f"""You are an Autonomous Planner. The user wants: '{user_goal}'. 
Break this complex task into a numbered list of sub-tasks. 
Each sub-task must be solvable by one of your tools: [Search Google, Browse Website, Write Code, Send Email, Calendar].
Output STRICT JSON format: 
{{'mission': 'name', 'steps': [{{'id': 1, 'task': 'Search for...'}}, {{'id': 2, 'task': 'Analyze...'}}]}}"""

        # Using Brain to get the plan. Brain.think returns a dict (parsed JSON).
        # We need to ensure Brain handles this prompt correctly.
        # However, Brain.think is designed to return actions like {"action": "chat", "response": "..."}
        # or other specific actions. 
        # For Mission Control, we want the RAW JSON defined in the prompt.
        
        # Let's call brain.think and see if we can get the steps.
        # NOTE: Brain.think internally appends its own system instructions.
        # We might need a more direct way to prompt the LLM or handle the response if it wraps it.
        
        response = self.brain.think(prompt)
        
        # Brain.think parses the response into a dictionary.
        # If it returned a "chat" action with the JSON in the response string, we might need to parse it.
        # But if the LLM followed instructions, 'response' might be the dict we want if we're lucky,
        # or it might be in response['response'].
        
        steps_data = None
        if isinstance(response, dict):
            if 'mission' in response and 'steps' in response:
                steps_data = response
            elif 'action' in response and response['action'] == 'chat':
                text = response.get('response', '')
                try:
                    # Try to extract JSON from the chat response
                    import re
                    match = re.search(r'(\{.*\})', text, re.DOTALL)
                    if match:
                        json_str = match.group(1).replace("'", '"')
                        steps_data = json.loads(json_str)
                except:
                    pass

        if not steps_data:
            web_log("Mission Control: Failed to generate valid plan JSON.")
            return 0

        # Create workspace directory if it doesn't exist
        os.makedirs(os.path.dirname(self.mission_file), exist_ok=True)

        with open(self.mission_file, 'w') as f:
            json.dump(steps_data, f, indent=4)
            
        web_log(f"Mission Control: Plan saved to {self.mission_file}")
        return len(steps_data.get('steps', []))
            
    def execute_step(self, step_dict, context):
        """
        Takes a single step, sends it to the Brain with context, and returned the result.
        """
        task = step_dict.get('task', 'No task specified')
        web_log(f"Mission Control: Executing Step - {task}")
        
        prompt = f"""You are executing a step in a mission.
MISSION CONTEXT (What happened before):
{context}

CURRENT TASK: 
{task}

Execute this task using your tools. If you need to search, do it. If you need to browse, do it.
Provide a concise summary of your ACTION and the RESULT for this specific step."""

        response = self.brain.think(prompt)
        
        # We expect the Brain to return a 'chat' response with the summary, 
        # or perform an action (which we should ideally track).
        # For simple engine logic, let's assume 'chat' or extract the response.
        
        if isinstance(response, dict):
            if response.get('action') == 'chat':
                return response.get('response', 'Step completed.')
            else:
                # Actual Execution via JarvisExecutor
                if self.executor:
                    web_log(f"Mission Control: Interfacing with Executor for action: {response.get('action')}")
                    result = self.executor.execute_command(response, self.brain)
                    return f"Action Result: {result}"
                else:
                    return f"Performed action: {response.get('action')} target: {response.get('target', response.get('query', ''))}"
        
        return str(response)

    def start_mission(self):
        """
        Loads the plan and loops through steps to execute them.
        """
        if not os.path.exists(self.mission_file):
            web_log("Mission Control: No current mission found.")
            return "No mission to start."

        with open(self.mission_file, 'r') as f:
            mission_data = json.load(f)

        mission_name = mission_data.get('mission', 'Unnamed Mission')
        steps = mission_data.get('steps', [])
        
        web_log(f"Mission Control: Starting mission: {mission_name}")
        mission_context = ""
        
        for step in steps:
            web_log(f"--- Step {step['id']}: {step['task']} ---")
            # In a real scenario, we'd need to actually EXECUTE the tool actions.
            # This requires passing the command to JarvisExecutor.
            # Let's assume for this basic version we just gather summaries.
            result = self.execute_step(step, mission_context)
            
            mission_context += f"\nStep {step['id']} ({step['task']}):\nResult: {result}\n"
            
            # Save context progress
            context_file = os.path.join(self.base_dir, 'workspace', 'mission_context.txt')
            with open(context_file, 'w') as f:
                f.write(mission_context)

        web_log("Mission Control: Mission Complete.")
        
        # Generate Final Summary
        final_prompt = f"The mission '{mission_name}' is complete. Here is the context:\n{mission_context}\n\nProvide a final elegant summary for the user as Jarvis."
        summary_resp = self.brain.think(final_prompt)
        
        final_summary = summary_resp.get('response', 'Mission completed, Sir.')
        return final_summary

if __name__ == "__main__":
    planner = MissionPlanner()
    # count = planner.create_plan("Research the best gaming mouse under $50")
    # print(f"Generated {count} steps.")
    summary = planner.start_mission()
    print(f"FINAL SUMMARY: {summary}")
