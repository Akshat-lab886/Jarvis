import os
import subprocess
import shutil
import logging

logger = logging.getLogger("Jarvis.MobileStudio")

class MobileManager:
    def __init__(self):
        self.workspace_dir = "mobile_projects"
        if not os.path.exists(self.workspace_dir):
            os.makedirs(self.workspace_dir)

        # Try to find flutter in typical locations if not in PATH
        self.flutter_cmd = "flutter"
        possible_paths = [
            "/Users/akshatpratap/flutter/flutter/bin/flutter",
            "/Users/akshatpratap/flutter/bin/flutter",
            os.path.expanduser("~/flutter/bin/flutter"),
            os.path.expanduser("~/development/flutter/bin/flutter")
        ]
        
        # Check if 'flutter' is in PATH
        if not shutil.which("flutter"):
            for p in possible_paths:
                if os.path.exists(p) and os.access(p, os.X_OK):
                    self.flutter_cmd = p
                    break

    def _resolve(self, *parts):
        """Resolve *parts* under the workspace and return the absolute
        path ONLY if it stays inside ``mobile_projects/``.  Returns None
        when a ``..`` component, an absolute path, or a drive/prefix
        would escape the workspace.  Every path built from an
        agent/LLM-supplied name goes through here."""
        workspace = os.path.abspath(self.workspace_dir)
        candidate = os.path.abspath(os.path.join(workspace, *parts))
        if candidate == workspace or \
                candidate.startswith(workspace + os.sep):
            return candidate
        return None

    @staticmethod
    def _safe_component(name):
        """True only when *name* is one plain (possibly dotted) directory
        component with no path separators — so it can never address a
        sibling or parent directory regardless of who supplied it."""
        name = str(name or '')
        return bool(name) and not os.path.isabs(name) and \
            '/' not in name and '\\' not in name and \
            name not in ('.', '..')

    def init_flutter_app(self, app_name):
        """
        Scaffolds a new Flutter application.
        """
        try:
            # app_name = app_name.replace(" ", "_").lower() # Original line, removed as per instruction
            name = str(app_name or '').strip()
            if not MobileManager._safe_component(name):
                return (f"Error: Invalid project name {app_name!r} — "
                        "must be a single folder name.")
            full_path = self._resolve(name)
            if full_path is None:
                return (f"Error: Project {app_name} resolves outside "
                        "mobile_projects/.")

            if os.path.exists(full_path):
                return f"Error: Project {app_name} already exists."

            # Check if flutter is installed - This check is now handled in __init__
            # if not shutil.which("flutter"):
            #      return "Error: 'flutter' command not found. Please install the Flutter SDK."

            # flutter create [name] creates the directory, so run it INSIDE
            # the workspace to keep projects in mobile_projects/
            os.makedirs(self.workspace_dir, exist_ok=True)
            result = subprocess.run(
                [self.flutter_cmd, "create", name],
                cwd=self.workspace_dir,
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                return f"Flutter infrastructure built for {app_name}. Dependency tree established."
            else:
                return f"Flutter init failed: {result.stderr}"

        except Exception as e:
            return f"Mobile Studio Error: {e}"

    def init_react_native(self, app_name):
        """
        Initializes a new React Native (Expo) application.
        """
        try:
            name = str(app_name or '').strip().replace(" ", "-").lower()
            if not MobileManager._safe_component(name):
                return (f"Error: Invalid project name {app_name!r} — "
                        "must be a single folder name.")
            project_path = self._resolve(name)
            if project_path is None:
                return (f"Error: Project {app_name} resolves outside "
                        "mobile_projects/.")

            if os.path.exists(project_path):
                return f"Error: Mobile Project '{app_name}' already exists."

            # Check for npx/node
            if not shutil.which("npx"):
                return "Error: 'npx' not found. Please install Node.js."

            # using create-expo-app for easier setup
            cmd = ["npx", "create-expo-app", name, "--yes"] # --yes to skip prompts
            
            result = subprocess.run(
                cmd,
                cwd=self.workspace_dir,
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                return f"React Native infrastructure built for {app_name}."
            else:
                return f"React Native init failed: {result.stderr}"

        except Exception as e:
            return f"Mobile Studio Error: {e}"

    def read_file(self, project_name, file_path):
        """
        Reads content of a file in a mobile project.
        """
        try:
            full_path = self._resolve(project_name, file_path)
            if full_path is None:
                return (f"Error: Refusing to read outside mobile_projects/ "
                        f"({file_path!r}).")
            if not os.path.exists(full_path):
                return f"Error: File {file_path} not found in {project_name}."

            with open(full_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            return f"Read Error: {e}"

    def write_feature(self, project_name, file_path, code):
        """
        Writes/Overwrites a file in the mobile project.
        Automatically creates directories if they don't exist.
        """
        try:
            full_path = self._resolve(project_name, file_path)
            if full_path is None:
                return (f"Error: Refusing to write outside mobile_projects/ "
                        f"({file_path!r}).")

            # Ensure directory exists
            dir_name = os.path.dirname(full_path)
            if dir_name and not os.path.exists(dir_name):
                os.makedirs(dir_name)
                
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(code)
                
            return f"Updated {file_path}"
        except Exception as e:
            return f"Write Error: {e}"

    def generate_app_blueprint(self, app_idea):
        """
        Consults the Brain to generate a detailed file structure for a Flutter app.
        """
        try:
            # We need to import Brain here or pass it in. passed in is better but
            # for now we'll import it to be self-contained or use the one from main.
            # Best is to initialize a temporary brain instance or reuse.
            # Let's import to keep it simple as this is a "Studio" tool.
            from utils.brain import Brain
            brain = Brain()
            
            prompt = f"""You are a CTO. The user wants this app: '{app_idea}'.
            Analyze the requirements. List EVERY single file needed for a Minimum Viable Product (MVP) in Flutter.
            Include:
            1. Folder structure (lib/screens, lib/widgets, lib/models).
            2. A list of at least 5-10 critical files (e.g., login.dart, home.dart, user_model.dart).
            3. A brief description of what goes in each file.
            Output strictly JSON:
            {{
              'project_name': 'ValidFlutterName',
              'dependencies': ['google_fonts', 'provider', 'http'],
              'files_to_create': [
                {{'path': 'lib/models/user.dart', 'description': 'User class with name and photo'}},
                {{'path': 'lib/screens/login.dart', 'description': 'Login UI with email/pass'}},
                {{'path': 'lib/screens/home.dart', 'description': 'List view of posts'}}
              ]
            }}"""
            
            response = brain.think(prompt)
            # The brain returns a dict if it parsed JSON, or a chat dict.
            # We want the raw dict if it's the action format, OR we need to trust the brain to return the JSON structure we asked for.
            # However, brain.think() wraps things in its own structure usually.
            # Let's see. logic in brain.py parses JSON.
            # If the brain returns the JSON we asked for, brain.think -> returns that dict.
            
            if response and 'project_name' in response:
                return response
            elif response and 'response' in response:
                 # It might have been wrapped in a chat response key if parsing failed partially
                 # Or we can just return the raw text if needed but we want the dict.
                 return response
            else:
                return {"error": "Failed to generate blueprint."}

        except Exception as e:
            return {"error": f"Blueprint Generation Error: {e}"}

    def build_from_blueprint(self, blueprint):
        """
        Executes the blueprint to build the full application autonomously.
        """
        try:
            project_name = blueprint.get('project_name')
            if not project_name: return "Error: Invalid Blueprint (No Name)"
            
            # Step 1: Initialize Project
            logger.info("--- Step 1: Initialize %s ---", project_name)
            init_res = self.init_flutter_app(project_name)
            if "Error" in init_res: return init_res
            
            # Step 2: Install Dependencies
            dependencies = blueprint.get('dependencies', [])
            if dependencies:
                logger.info("--- Step 2: Install Dependencies (%s) ---", len(dependencies))
                project_path = self._resolve(project_name)
                if project_path is None:
                    return (f"Error: Refusing to install into "
                            f"{project_name!r} (outside mobile_projects/).")
                # argv, no shell — package names are agent-supplied and a
                # name like ``;rm -rf /`` must never reach ``sh -c``.
                subprocess.run(["flutter", "pub", "add", *dependencies],
                               cwd=project_path, check=False)
            
            # Step 3: The Coding Loop
            from utils.brain import Brain
            brain = Brain()
            files = blueprint.get('files_to_create', [])
            
            created_files = []
            
            logger.info("--- Step 3: Coding Loop (%s files) ---", len(files))
            for file_task in files:
                path = file_task.get('path')
                desc = file_task.get('description')
                
                logger.info("Autonomously coding: %s...", path)
                
                prompt = f"""We are building a Flutter app: {project_name}.
                Current Goal: Write the full code for '{path}'.
                Description: {desc}.
                Previously created files: {', '.join(created_files)}.
                Dependencies installed: {', '.join(dependencies)}.
                
                IMPORTANT:
                - Output ONLY the raw code for the file.
                - Do NOT use markdown backticks.
                - Ensure all imports are correct (package:{project_name}/...).
                - This must be a complete, working file."""
                
                # We need a way to get RAW code from brain, not JSON action.
                # Brain.think usually returns JSON action.
                # We will parse the 'chat' response or assume text.
                
                # Let's trust the "write_code" action format or just ask for code.
                response = brain.think(prompt)
                
                code = ""
                if isinstance(response, dict):
                    if 'code' in response: code = response['code']
                    elif 'response' in response: code = response['response']
                elif isinstance(response, str):
                    code = response
                
                # Clean code
                if "```dart" in code:
                    code = code.split("```dart")[1].split("```")[0]
                elif "```" in code:
                    code = code.split("```")[1].split("```")[0]
                    
                self.write_feature(project_name, path, code)
                created_files.append(path)
                
            # Step 4: Final Link
            logger.info("--- Step 4: Final Link (main.dart) ---")
            link_prompt = f"""We are building {project_name}.
            We have created these files: {', '.join(created_files)}.
            
            Goal: Write the content for 'lib/main.dart'.
            Requirements:
            1. Import 'package:flutter/material.dart'.
            2. Import all the screen files we created correctly (package:{project_name}/...).
            3. Create a main() function that runs MyApp.
            4. Create MyApp class (StatelessWidget) with MaterialApp.
            5. Define routes or home to point to the main screens.
            6. Ensure the app runs without errors.
            
            Output ONLY the raw code for lib/main.dart."""
            
            # Reuse logic to get code from brain
            response = brain.think(link_prompt)
            code = ""
            if isinstance(response, dict):
                 if 'code' in response: code = response['code']
                 elif 'response' in response: code = response['response']
            elif isinstance(response, str):
                 code = response
            
            if "```dart" in code:
                 code = code.split("```dart")[1].split("```")[0]
            elif "```" in code:
                 code = code.split("```")[1].split("```")[0]
                 
            self.write_feature(project_name, "lib/main.dart", code)
                
            return f"App construction complete for {project_name}. {len(created_files)} files created + main.dart."
        except Exception as e:
            return f"Build Error: {e}"

    def run_diagnostics(self, project_name):
        """
        Runs diagnostics (flutter analyze) on the constructed project.
        Returns "No errors found." if clean, else the error log.
        """
        try:
             project_path = self._resolve(project_name)
             if project_path is None:
                 return (f"Error: Refusing to analyze outside "
                         f"mobile_projects/ ({project_name!r}).")
             if not os.path.exists(project_path):
                 return f"Error: Project {project_name} not found."
             
             logger.info("--- verifing code integrity for %s ---", project_name)
             
             # flutter analyze
             result = subprocess.run(
                 [self.flutter_cmd, "analyze"],
                 cwd=project_path,
                 capture_output=True,
                 text=True
             )
             
             # flutter analyze returns 0 if no issues, or small issues (warnings) depending on config.
             # usually if it finds errors it might return non-zero or just print them.
             # let's check output.
             
             output = result.stdout + result.stderr
             if "No issues found" in output:
                 return "No errors found."
             elif result.returncode == 0:
                  # Some versions return 0 even with infos
                  return "No errors found."
             else:
                 # It failed
                 return output
                 
        except Exception as e:
            return f"Diagnostics Error: {e}"

    def attempt_repair(self, project_name, error_log):
        """
        Attempts to fix syntax errors by asking the Brain.
        Returns True if fixed, False if failed.
        """
        try:
            logger.info("--- Diagnosing and Repairing %s ---", project_name)
            
            # Step 1: parse error log to find file
            # Sample: "lib/main.dart:20:5: Error: ..."
            import re
            match = re.search(r"lib/[\w/]+\.dart", error_log)
            if not match:
                logger.warning("Could not identify broken file from logs.")
                return False
                
            broken_file = match.group(0)
            logger.info("identified broken file: %s", broken_file)
            
            # Step 2: Read content
            current_code = self.read_file(project_name, broken_file)
            if "Error" in current_code:
                logger.warning("Could not read broken file.")
                return False
                
            # Step 3: Ask Brain for fix
            from utils.brain import Brain
            brain = Brain()
            
            prompt = f"""I am compiling a Flutter app.
            File: {broken_file}
            
            Current Code:
            {current_code}
            
            Compiler Error detected:
            {error_log}
            
            Mission: Fix the code to resolve the error.
            - Keep the rest of the file intact.
            - Import any missing packages if needed.
            - Output ONLY the full corrected code for {broken_file}.
            """
            
            response = brain.think(prompt)
            
            code = ""
            if isinstance(response, dict):
                 if 'code' in response: code = response['code']
                 elif 'response' in response: code = response['response']
            elif isinstance(response, str):
                 code = response
            
            if "```dart" in code:
                 code = code.split("```dart")[1].split("```")[0]
            elif "```" in code:
                 code = code.split("```")[1].split("```")[0]
                 
            if not code.strip():
                logger.warning("Brain returned empty code.")
                return False
                
            # Step 4: Overwrite
            self.write_feature(project_name, broken_file, code)
            logger.info("Applied fix to %s.", broken_file)
            
            # Step 5: Verify
            logger.info("Re-verifying...")
            new_diag = self.run_diagnostics(project_name)
            if "No errors found" in new_diag:
                return True
            else:
                logger.warning("Fix failed. Errors persist.")
                return False

        except Exception as e:
            logger.warning("Repair Exception: %s", e)
            return False

