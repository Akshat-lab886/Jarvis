"""
Jarvis Quick Actions

Centralizes the keyword-based command triggers so the local voice loop
(main.py), the web dashboard (server.py) and any future channel behave
identically. Previously this logic was copy-pasted in two places and drifted.

`handle_quick_actions(text, executor, brain, ui_callback)` returns True when
the text was handled here (the caller should NOT send it to the Brain), or
False when it should fall through to normal Brain processing.

Long-running work (missions) is executed on a background thread so the caller
never blocks.
"""

import os
import threading
import logging

logger = logging.getLogger("Jarvis.QuickActions")


def _start_mission(goal, executor, brain, ui_callback=None):
    """Run a mission on a background thread so the caller never blocks."""
    def _run():
        try:
            # Wire up task manager with current brain and callback
            executor.task_manager.brain = brain
            executor.task_manager.ui_callback = ui_callback or (lambda e, d: None)

            from utils.mission_control import MissionPlanner
            planner = MissionPlanner(executor=executor, brain=brain)
            executor.mouth.speak("Analyzing mission parameters, Sir.")
            count = planner.create_plan(goal)
            if count == 0:
                executor.mouth.speak("I couldn't break that into steps, Sir.")
                if ui_callback:
                    ui_callback('ai_text', {'text': 'Failed to generate a plan.'})
                return
            msg = f"Plan generated with {count} steps. Executing now."
            executor.mouth.speak(msg)
            if ui_callback:
                ui_callback('ai_text', {'text': f"Mission: {goal}\nSteps: {count}"})
            # start_mission now waits for the ComplexTaskManager to finish
            summary = planner.start_mission()
            executor.mouth.speak(summary)
            if ui_callback:
                ui_callback('ai_text', {'text': summary})
        except Exception as e:
            logger.error(f"Mission failed: {e}", exc_info=True)
            executor.mouth.speak("I hit a snag running that mission, Sir.")

    threading.Thread(target=_run, daemon=True, name="MissionThread").start()


def _speak(executor, text, ui_callback=None):
    if ui_callback:
        ui_callback('ai_text', {'text': text})
    executor.mouth.speak(text)


def handle_quick_actions(text, executor, brain, ui_callback=None):
    """
    Returns True if `text` was handled by a quick action (caller should not
    process it further), False if it should go to the Brain.
    """
    if not text:
        return False
    text_lower = text.lower()

    # ------------------------------------------------------------------ #
    # Missions
    # ------------------------------------------------------------------ #
    if "start mission" in text_lower or "auto-agent" in text_lower:
        goal = text_lower.replace("start mission", "").replace("auto-agent", "").replace(":", "").strip()
        if goal:
            _start_mission(goal, executor, brain, ui_callback)
            return True

    # ------------------------------------------------------------------ #
    # Auto-Build App (Mobile Studio CTO loop)
    # ------------------------------------------------------------------ #
    if "auto-build app" in text_lower:
        idea = text_lower.split("auto-build app")[-1].replace(":", "").strip()
        if not idea:
            idea = "a generic app"
        executor.mouth.speak("I am becoming the CTO. Designing architecture...")
        try:
            from utils.mobile_studio import MobileManager
            from utils.dev_studio import ProjectManager
            mobile = MobileManager()
            pm = ProjectManager()
            blueprint = mobile.generate_app_blueprint(idea)
            if 'project_name' in blueprint:
                executor.mouth.speak(f"Blueprint approved for {blueprint['project_name']}. Starting construction loop. This may take a moment.")
                result = mobile.build_from_blueprint(blueprint)
                logger.info("Blueprint build result: %s", result)
                executor.mouth.speak("Verifying code integrity...")
                diag_res = mobile.run_diagnostics(blueprint['project_name'])
                if "No errors found" in diag_res:
                    executor.mouth.speak("Build successful. Opening project.")
                    pm.open_in_vscode(blueprint['project_name'])
                else:
                    executor.mouth.speak("Errors detected. Initiating repair protocol.")
                    logger.warning("Diagnostics report:\n%s", diag_res)
                    max_retries = 3
                    for i in range(max_retries):
                        executor.mouth.speak(f"Attempting fix {i+1}...")
                        if mobile.attempt_repair(blueprint['project_name'], diag_res):
                            executor.mouth.speak("Repairs complete. System stable.")
                            break
                        executor.mouth.speak("Attempting another fix...")
                    else:
                        executor.mouth.speak("Repair protocol failed. Manual intervention required.")
                    pm.open_in_vscode(blueprint['project_name'])
            else:
                executor.mouth.speak("I couldn't generate a valid blueprint, Sir.")
        except Exception as e:
            logger.error(f"Auto-build error: {e}", exc_info=True)
            executor.mouth.speak("Auto-build failed, Sir.")
        return True

    # ------------------------------------------------------------------ #
    # Architect (blueprint only)
    # ------------------------------------------------------------------ #
    if "architect" in text_lower and "app" in text_lower:
        idea = text_lower.split("for")[-1].strip()
        if not idea:
            idea = "a generic app"
        executor.mouth.speak(f"Architecting a solution for {idea}...")
        try:
            from utils.mobile_studio import MobileManager
            mobile = MobileManager()
            blueprint = mobile.generate_app_blueprint(idea)
            import json
            logger.info("--- APP BLUEPRINT ---")
            logger.info("Blueprint: %s", json.dumps(blueprint, indent=2))
            logger.info("--- END BLUEPRINT ---")
            if 'project_name' in blueprint:
                files_count = len(blueprint.get('files_to_create', []))
                executor.mouth.speak(f"Blueprint generated for {blueprint['project_name']}. It requires {files_count} files.")
            else:
                executor.mouth.speak("I couldn't generate a valid blueprint, Sir.")
        except Exception as e:
            logger.error(f"Architect error: {e}", exc_info=True)
            executor.mouth.speak("Architect failed, Sir.")
        return True

    # ------------------------------------------------------------------ #
    # Mobile Studio init (Flutter / React Native)
    # ------------------------------------------------------------------ #
    if "initialize" in text_lower and ("flutter" in text_lower or "react" in text_lower) and "app" in text_lower:
        try:
            from utils.mobile_studio import MobileManager
            mobile = MobileManager()
            if "named" in text_lower:
                name = text_lower.split("named")[-1].strip().split()[0]
            else:
                name = text_lower.split()[-1]
            name = name.replace(".", "")
            if "flutter" in text_lower:
                executor.mouth.speak(f"Initializing Flutter architecture for {name}...")
                result = mobile.init_flutter_app(name)
            elif "react" in text_lower:
                executor.mouth.speak(f"Initializing React Native scaffolding for {name}...")
                result = mobile.init_react_native(name)
            else:
                result = "I couldn't determine the framework, Sir."
            _speak(executor, result, ui_callback)
        except Exception as e:
            logger.error(f"Mobile Studio error: {e}", exc_info=True)
            executor.mouth.speak("Failed to initialize mobile infrastructure.")
        return True

    # ------------------------------------------------------------------ #
    # Dev Studio: create project
    # ------------------------------------------------------------------ #
    if ("create" in text_lower or "make" in text_lower or "start" in text_lower) and ("project" in text_lower or "app" in text_lower):
        try:
            from utils.dev_studio import ProjectManager
            pm = ProjectManager()
            proj_type = "python"
            if "web" in text_lower or "html" in text_lower:
                proj_type = "web"
            if "named" in text_lower:
                name = text_lower.split("named")[-1].strip().split()[0]
            else:
                name = text_lower.split()[-1]
            name = name.replace(".", "").capitalize()
            executor.mouth.speak(f"Initializing scaffold for {name}...")
            result = pm.create_project(name, proj_type)
            _speak(executor, result, ui_callback)
            if "open" in text_lower and ("code" in text_lower or "editor" in text_lower):
                executor.mouth.speak(f"Opening {name} in VS Code.")
                pm.open_in_vscode(name)
        except Exception as e:
            logger.error(f"Dev Studio error: {e}", exc_info=True)
            executor.mouth.speak("I failed to create the project, Sir.")
        return True

    # ------------------------------------------------------------------ #
    # Dev Studio: open project in VS Code
    # ------------------------------------------------------------------ #
    if "open project" in text_lower and ("code" in text_lower or "editor" in text_lower):
        try:
            from utils.dev_studio import ProjectManager
            pm = ProjectManager()
            parts = text_lower.split("project")
            if len(parts) > 1:
                target = parts[1].strip()
                if "in" in target:
                    name = target.split("in")[0].strip()
                else:
                    name = target.split()[0].strip()
                name = name.replace(".", "").capitalize()
                executor.mouth.speak(f"Opening Visual Studio Code for {name}...")
                result = pm.open_in_vscode(name)
                logger.info("Dev Studio: %s", result)
        except Exception as e:
            logger.error(f"VS Code Launch error: {e}", exc_info=True)
            executor.mouth.speak("I didn't catch the project name, Sir.")
        return True

    # ------------------------------------------------------------------ #
    # Git init
    # ------------------------------------------------------------------ #
    if "initialize git" in text_lower:
        try:
            idea = text_lower.split("for")[-1].strip()
            if not idea:
                executor.mouth.speak("Which project, Sir?")
                return True
            name = idea.split()[0].replace(".", "").strip()
            if not name:
                executor.mouth.speak("I didn't catch the project name.")
                return True
            executor.mouth.speak(f"Initializing Git repository for {name}...")
            mobile_path = os.path.join("mobile_projects", name)
            dev_path = os.path.join("Jarvis_Projects", name)
            dev_path_underscore = os.path.join("Jarvis_Projects", name.replace(" ", "_").capitalize())
            target_path = None
            if os.path.exists(mobile_path):
                target_path = mobile_path
            elif os.path.exists(dev_path):
                target_path = dev_path
            elif os.path.exists(dev_path_underscore):
                target_path = dev_path_underscore
            if target_path:
                from utils.git_manager import GitManager
                result = GitManager().init_local_git(target_path)
                _speak(executor, result, ui_callback)
            else:
                executor.mouth.speak(f"I couldn't find a project named {name}.")
        except Exception as e:
            logger.error(f"Git init error: {e}", exc_info=True)
            executor.mouth.speak("Failed to initialize Git protocols.")
        return True

    # ------------------------------------------------------------------ #
    # Publish to GitHub
    # ------------------------------------------------------------------ #
    if "publish" in text_lower and "github" in text_lower:
        try:
            part1 = text_lower.split("publish")[-1]
            project_name = part1.split("to")[0].strip().replace(".", "").strip()
            if not project_name:
                executor.mouth.speak("Which project should I publish?")
                return True
            mobile_path = os.path.join("mobile_projects", project_name)
            dev_path = os.path.join("Jarvis_Projects", project_name)
            dev_path_underscore = os.path.join("Jarvis_Projects", project_name.replace(" ", "_").capitalize())
            target_path = None
            if os.path.exists(mobile_path):
                target_path = mobile_path
            elif os.path.exists(dev_path):
                target_path = dev_path
            elif os.path.exists(dev_path_underscore):
                target_path = dev_path_underscore
            if not target_path:
                executor.mouth.speak(f"I couldn't find the project {project_name}.")
                return True
            from utils.git_manager import GitManager
            git_mgr = GitManager()
            executor.mouth.speak("Initializing local Git repository...")
            git_mgr.init_local_git(target_path)
            executor.mouth.speak("Creating remote repository on GitHub...")
            repo_name = project_name.replace(" ", "-")
            repo_url = git_mgr.create_remote_repo(repo_name)
            if "Error" in repo_url:
                executor.mouth.speak(f"Failed to create remote repository. {repo_url}")
                return True
            executor.mouth.speak("Pushing code to the cloud...")
            push_res = git_mgr.push_to_remote(target_path, repo_url)
            if "successfully" in push_res:
                executor.mouth.speak(f"Project is live at {repo_url}")
            else:
                executor.mouth.speak(f"Push failed: {push_res}")
        except Exception as e:
            logger.error(f"Publish error: {e}", exc_info=True)
            executor.mouth.speak("Failed to publish to GitHub.")
        return True

    # ------------------------------------------------------------------ #
    # Email: send
    # ------------------------------------------------------------------ #
    if "send email to" in text_lower and "saying" in text_lower:
        try:
            parts = text_lower.split("send email to")
            if len(parts) > 1 and "saying" in parts[1]:
                recipient_raw = parts[1].split("saying")[0].strip()
                message_body = parts[1].split("saying")[1].strip()
                to_email = recipient_raw
                if " " in to_email and "@" not in to_email:
                    executor.mouth.speak(f"I don't have an email address for {recipient_raw}. Please say the full email.")
                    return True
                from utils.secretary import Secretary
                executor.mouth.speak(f"Sending email to {to_email}...")
                res = Secretary().send_email(to_email, "Message from Jarvis", message_body)
                _speak(executor, res, ui_callback)
        except Exception as e:
            logger.error(f"Email send error: {e}", exc_info=True)
            executor.mouth.speak("Failed to send email.")
        return True

    # ------------------------------------------------------------------ #
    # Email: reply
    # ------------------------------------------------------------------ #
    if "reply to" in text_lower and "email about" in text_lower and "saying" in text_lower:
        try:
            topic_part = text_lower.split("email about")[1]
            query = topic_part.split("saying")[0].strip()
            message_body = topic_part.split("saying")[1].strip()
            if not query or not message_body:
                executor.mouth.speak("I didn't catch the topic or the message.")
                return True
            from utils.secretary import Secretary
            executor.mouth.speak(f"Replying to email about {query}...")
            res = Secretary().reply_to_email(query, message_body)
            _speak(executor, res, ui_callback)
        except Exception as e:
            logger.error(f"Email reply error: {e}", exc_info=True)
            executor.mouth.speak("Failed to reply to email.")
        return True

    # ------------------------------------------------------------------ #
    # Operator (GUI): type / press
    # Note: "open <x>" is intentionally NOT handled here — the Brain routes
    # it to open_app / open_web which handles both apps and websites better.
    # ------------------------------------------------------------------ #
    if text_lower.startswith("type "):
        text_to_type = text[5:]
        if text_to_type:
            from utils.gui_automator import Operator
            res = Operator().write_text(text_to_type)
            _speak(executor, res, ui_callback)
        return True

    if text_lower.startswith("press "):
        key_name = text_lower.replace("press ", "").strip()
        if key_name:
            from utils.gui_automator import Operator
            res = Operator().press_key(key_name)
            _speak(executor, res, ui_callback)
        return True

    # ------------------------------------------------------------------ #
    # Meeting Mode
    # ------------------------------------------------------------------ #
    if "start meeting" in text_lower or "meeting mode" in text_lower:
        result = executor.meeting.start_recording()
        _speak(executor, result, ui_callback)
        return True

    if "stop meeting" in text_lower or "end meeting" in text_lower:
        result = executor.meeting.stop_recording()
        _speak(executor, result, ui_callback)
        return True

    if "meeting transcript" in text_lower or "what was said" in text_lower:
        transcript = executor.meeting.get_live_transcript()
        _speak(executor, transcript or "No transcript available yet.", ui_callback)
        return True

    # ------------------------------------------------------------------ #
    # Fridge Vision
    # ------------------------------------------------------------------ #
    if "look at my fridge" in text_lower or "what can i cook" in text_lower or "analyze fridge" in text_lower:
        executor.mouth.speak("Analyzing your fridge, Sir.")
        result = executor.fridge.analyze_fridge()
        if result.get('error'):
            _speak(executor, result['error'], ui_callback)
        else:
            ingredients = result.get('ingredients', [])
            recipes = result.get('recipes', [])
            shopping = result.get('shopping_list', [])
            msg = f"Found {len(ingredients)} ingredients."
            if recipes:
                msg += f" Suggested {len(recipes)} recipes."
            if shopping:
                msg += f" Shopping list ready."
            _speak(executor, msg, ui_callback)
        return True

    # ------------------------------------------------------------------ #
    # Relationship commands
    # ------------------------------------------------------------------ #
    if text_lower.startswith("my friend ") or text_lower.startswith("my mom") or \
       text_lower.startswith("my dad") or text_lower.startswith("my brother") or \
       text_lower.startswith("my sister") or text_lower.startswith("my wife") or \
       text_lower.startswith("my husband") or text_lower.startswith("my partner"):
        # Store relationship info
        try:
            executor.episodic.auto_capture(text)
            _speak(executor, "Noted, Sir. I'll remember that.", ui_callback)
        except Exception as e:
            logger.error(f"Relationship capture error: {e}", exc_info=True)
        return True

    if "gift idea" in text_lower or "what should i get" in text_lower:
        # Extract the person name
        for prefix in ["gift idea for ", "what should i get ", "gift for "]:
            if prefix in text_lower:
                name = text_lower.split(prefix)[-1].strip().title()
                suggestions = executor.relationships.get_gift_suggestions(name)
                msg = f"Gift ideas for {name}: {', '.join(suggestions[:3])}"
                _speak(executor, msg, ui_callback)
                return True

    # ------------------------------------------------------------------ #
    # Triage & Privacy
    # ------------------------------------------------------------------ #
    if "what needs my attention" in text_lower or "message triage" in text_lower or "critical messages" in text_lower:
        from utils.proactive import CrossAppTriage
        triage = CrossAppTriage(secretary=executor.tools.secretary)
        result = triage.get_triage_summary()
        _speak(executor, result, ui_callback)
        return True

    if "privacy settings" in text_lower or "trust controls" in text_lower:
        result = executor.privacy.get_trust_summary()
        _speak(executor, result, ui_callback)
        return True

    if "schedule insights" in text_lower or "time blocks" in text_lower:
        from utils.proactive import TimeBlockScheduler
        blocker = TimeBlockScheduler(secretary=executor.tools.secretary)
        suggestions = blocker.scan_and_adjust()
        if suggestions:
            lines = [s['message'] for s in suggestions[:5]]
            _speak(executor, "\n".join(lines), ui_callback)
        else:
            _speak(executor, "Your schedule looks clear, Sir.", ui_callback)
        return True

    # ------------------------------------------------------------------ #
    # Memory commands
    # ------------------------------------------------------------------ #
    if "recall" in text_lower and ("what do you know" in text_lower or "remember" in text_lower):
        query = text_lower.replace("recall", "").replace("what do you know about", "").strip()
        results = executor.episodic.search(query, limit=5)
        if results:
            msg = f"I found {len(results)} related memories. " + "; ".join(m['text'][:60] for m in results[:3])
        else:
            msg = f"I don't have specific memories about '{query}' yet."
        _speak(executor, msg, ui_callback)
        return True

    if "memory stats" in text_lower or "how much do you remember" in text_lower:
        stats = executor.episodic.stats()
        msg = f"I have {stats['total']} memories across {len(stats['categories'])} categories."
        _speak(executor, msg, ui_callback)
        return True

    return False
