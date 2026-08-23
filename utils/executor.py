import os
import time
import datetime
import logging
from utils.brain import Brain
from utils.mouth import Mouth
from utils.tools import Tools
from utils.memory import Memory
from utils.episodic_memory import EpisodicMemory
from utils.organizer import clean_downloads, delete_file, delete_screenshots
from utils.downloader import search_and_download
from utils.web_reader import get_answer_from_web
from utils.smarthome import SmartHome
from utils.security import Security
from utils.coder import Coder
from utils.history import CommandLog
from utils.tasks import TodoList, NotePad
from utils.relationships import RelationshipManager
from utils.privacy import PrivacyFramework
from utils.fridge_vision import FridgeVision
from utils.meeting_audio import MeetingTranscriber
from utils.complex_task import ComplexTaskManager, normalize_step_specs
from utils.skills import SkillRegistry, SkillError
from utils.tool_registry import ToolRegistry, ToolError

logger = logging.getLogger("Jarvis.Executor")

class JarvisExecutor:
    def __init__(self):
        self.mouth = Mouth()
        self.tools = Tools()
        self.memory = Memory()  # Legacy flat memory (wraps episodic)
        self.episodic = EpisodicMemory()  # New structured memory
        self.smarthome = SmartHome()
        self.security = Security()
        self.coder = Coder()
        self.skills = SkillRegistry()
        self.tool_registry = ToolRegistry()
        self.history = CommandLog()
        self.tasks = TodoList()
        self.notes = NotePad()
        self.relationships = RelationshipManager(self.episodic)
        self.privacy = PrivacyFramework()
        self.fridge = FridgeVision(episodic_memory=self.episodic)
        self.meeting = MeetingTranscriber(
            episodic_memory=self.episodic, tasks=self.tasks, mouth=self.mouth
        )
        self.active_project = None  # Dev Context State
        self._complex_task_manager = None  # Lazy init
        logger.info("JarvisExecutor modules initialized (Lazy Loaded).")

    @property
    def automation(self):
        if not hasattr(self, '_automation'):
            from utils.automation import Automation
            self._automation = Automation()
        return self._automation

    @property
    def agent(self):
        if not hasattr(self, '_agent'):
            from utils.agent import WebAgent
            self._agent = WebAgent()
        return self._agent

    @property
    def librarian(self):
        if not hasattr(self, '_librarian'):
            from utils.knowledge import Librarian
            self._librarian = Librarian()
        return self._librarian

    @property
    def courier(self):
        if not hasattr(self, '_courier'):
            from utils.courier import Courier
            self._courier = Courier()
        return self._courier

    @property
    def scheduler(self):
        if not hasattr(self, '_scheduler'):
            from utils.scheduler import ReminderScheduler
            self._scheduler = ReminderScheduler(mouth=self.mouth)
        return self._scheduler

    @property
    def recurring(self):
        if not hasattr(self, '_recurring'):
            from utils.recurring import RecurringAutomations
            self._recurring = RecurringAutomations(mouth=self.mouth)
        return self._recurring

    @property
    def approvals(self):
        if not hasattr(self, '_approvals'):
            from utils.approvals import get_manager
            self._approvals = get_manager()
        return self._approvals

    @property
    def desktop_agent(self):
        if not hasattr(self, '_desktop_agent'):
            from utils.desktop_agent import DesktopAgent
            self._desktop_agent = DesktopAgent()
        return self._desktop_agent

    @property
    def task_manager(self):
        """Lazy-init the ComplexTaskManager."""
        if self._complex_task_manager is None:
            self._complex_task_manager = ComplexTaskManager(
                executor=self, brain=None, ui_callback=None
            )
        return self._complex_task_manager

    def execute_command(self, command, brain, original_text=None, ui_callback=None):
        """
        Executes a JSON command from the brain.
        ui_callback: function(event, data) - optional callback to update UI/Dashboard
        Returns: str - summary of the action taken
        """
        action = command.get('action', '').lower()
        target = command.get('target', '')
        response_text = command.get('response', '')

        result_msg = ""

        # Human-in-the-loop checkpoint: critical actions wait for a
        # deterministic human "yes" before executing (G3 guardrail).
        if action and self.approvals.requires(action):
            approved, note = self.approvals.request(command)
            if not approved:
                result_msg = (f"⛔ Action '{action}' was cancelled — "
                              f"{note}.")
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)
                return result_msg

        try:
            if action == 'system_info':
                result_msg = self.tools.get_system_info()
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'get_weather':
                result_msg = self.tools.get_weather()
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'read_webpage':
                url = command.get('url') or target or original_text or ''
                if not url:
                    result_msg = "Which page should I read, Sir?"
                else:
                    self.mouth.speak("Reading the page, Sir.")
                    if ui_callback: ui_callback('status', {'message': f'Reading {url}...'})
                    from utils.web_reader import read_url
                    content = read_url(url)
                    if content and "couldn't read" not in content:
                        prompt = f"Here is the content of {url}:\n{content[:4000]}\n\nSummarize the key points concisely."
                        brain_res = brain.think(prompt)
                        result_msg = brain_res.get('response', content[:500])
                    else:
                        result_msg = content or "I couldn't read that page."
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'daily_summary':
                entries = self.history.today()
                if not entries:
                    result_msg = "You haven't asked me to do anything yet today, Sir."
                else:
                    lines = "\n".join(f"- {e['ts'][11:16]} {e['user_text']}" for e in entries)
                    prompt = f"Here is a log of what the user asked me today:\n{lines}\n\nSummarize the day's activity briefly and elegantly as Jarvis."
                    brain_res = brain.think(prompt)
                    result_msg = brain_res.get('response', lines)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'todo_add':
                text = command.get('text') or target or original_text or ''
                result_msg = self.tasks.add(text)
                if ui_callback: ui_callback('todo_update', {'items': self.tasks.items_json()})
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'todo_list':
                result_msg = self.tasks.list()
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'todo_done':
                text = command.get('text') or target or original_text or ''
                result_msg = self.tasks.done(text)
                if ui_callback: ui_callback('todo_update', {'items': self.tasks.items_json()})
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'todo_remove':
                text = command.get('text') or target or original_text or ''
                result_msg = self.tasks.remove(text)
                if ui_callback: ui_callback('todo_update', {'items': self.tasks.items_json()})
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'todo_clear':
                result_msg = self.tasks.clear()
                if ui_callback: ui_callback('todo_update', {'items': self.tasks.items_json()})
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'note_save':
                text = command.get('text') or target or original_text or ''
                result_msg = self.notes.save(text)
                if ui_callback: ui_callback('notes_update', {'items': self.notes.items_json()})
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'note_list':
                result_msg = self.notes.list()
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'note_remove':
                text = command.get('text') or target or original_text or ''
                result_msg = self.notes.remove(text)
                if ui_callback: ui_callback('notes_update', {'items': self.notes.items_json()})
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'system_processes':
                result_msg = self.tools.get_top_processes()
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'kill_process':
                name = command.get('name') or target or original_text or ''
                result_msg = self.tools.kill_process(name)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'news_headlines':
                self.mouth.speak("Fetching today's headlines, Sir.")
                if ui_callback: ui_callback('status', {'message': 'Fetching news...'})
                from utils.web_reader import get_top_headlines
                result_msg = get_top_headlines()
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'open_app':
                logger.info(f"Executor attempting to open: {target}")
                if self.tools.open_app(target):
                    result_msg = f"Opening {target}"
                else:
                    result_msg = f"I couldn't find an app named {target}"
                
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)
                
            elif action == 'open_web':
                domain = target.replace('https://', '').replace('http://', '').replace('www.', '').split('/')[0]
                result_msg = f"Opening {domain}"
                if ui_callback: ui_callback('ai_text', {'text': f"Opening {target}"})
                self.mouth.speak(result_msg)
                self.tools.open_website(target)

            elif action == 'save_memory':
                key = command.get('key')
                value = command.get('value')
                self.memory.save_memory(key, value)
                # Auto-capture into episodic memory
                self.episodic.remember(
                    text=f"{key}: {value}", category='fact',
                    tags=[key.lower().replace(' ', '_')],
                    source='conversation', importance=7
                )
                result_msg = f"I'll remember that your {key} is {value}."
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'get_memory':
                key = command.get('key')
                value = self.memory.get_memory(key)
                if value:
                    result_msg = f"Your {key} is {value}."
                else:
                    result_msg = f"I don't recall information about {key}."
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'consult_archive':
                query = command.get('query')
                if ui_callback: ui_callback('status', {'message': f'Searching archives for: {query}'})
                self.mouth.speak("Searching my knowledge vault...")
                
                # Use the shared librarian instance from executor
                # Wait, executor needs its own librarian instance or pass it in?
                # We initialized it in __init__
                context = self.librarian.query_vault(query)
                
                if context:
                    prompt = f"Context from Knowledge Vault:\n{context}\n\nUser Question: {query}\n\nAnswer the question based strictly on the provided context."
                    brain_res = brain.think(prompt)
                    result_msg = brain_res.get('response', "I found the info but couldn't articulate it.")
                else:
                    result_msg = "I couldn't find any relevant records in my archives."
                    
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)
                
            elif action == 'set_volume':
                val = command.get('value')
                self.tools.set_volume(val)
                result_msg = f"Volume set to {val} percent."
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'media_play_pause':
                res = self.tools.media_play_pause()
                if res == "PermissionError":
                    result_msg = "I need Accessibility permissions to control media."
                else:
                    result_msg = "Media toggled."
                if ui_callback: ui_callback('status', {'message': result_msg})
                if res == "PermissionError": self.mouth.speak(result_msg)

            elif action == 'verify_identity':
                self.mouth.speak("Verifying identity. Please look at the camera, Sir.")
                time.sleep(1)
                res = self.security.verify_admin()
                if res is True:
                    result_msg = "Identity Verified: Admin Access Granted."
                    self.mouth.speak("Welcome back, Sir. Admin access granted.")
                else:
                    result_msg = "Identity not recognized, but Developer Override is active. Admin access granted."
                    self.mouth.speak(result_msg)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
            
            elif action == 'lock_system':
                result_msg = "Lockdown initiated. Goodbye, Sir."
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)
                time.sleep(2)
                self.tools.lock_system()
                 
            elif action == 'start_browser':
                result_msg = self.automation.launch_chrome()
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'close_browser':
                result_msg = self.automation.close_chrome()
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)
                
            elif action == 'agent_browse':
                url = command.get('url')
                if not url:
                    result_msg = "Where should I navigate, Sir?"
                else:
                    result_msg = self.agent.go_to(url)
                    if "error" in result_msg.lower():
                        self.mouth.speak(f"I failed to navigate. {result_msg}")
                    else:
                        self.mouth.speak("I am on the site, Sir.")
                if ui_callback: ui_callback('ai_text', {'text': result_msg})

            elif action == 'agent_read_title':
                title = self.agent.read_page_title()
                result_msg = f"Page Title: {title}"
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(f"The current page is {title}")
                
            elif action == 'agent_google':
                query = command.get('query')
                if ui_callback: ui_callback('ai_text', {'text': f"Googling: {query}"})
                self.mouth.speak("Searching Google now, Sir.")
                result_msg = self.agent.google_search(query)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})

            elif action == 'agent_amazon':
                item = command.get('item')
                if ui_callback: ui_callback('ai_text', {'text': f"Checking Amazon for: {item}"})
                self.mouth.speak("Checking Amazon...")
                result_msg = self.agent.search_amazon(item)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)
                
            elif action == 'play_youtube':
                target = target or command.get('query', '')
                result_msg = f"Playing {target} on YouTube."
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)
                self.tools.play_on_youtube(target)

            elif action == 'get_battery':
                result_msg = self.tools.get_battery_status()
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'chat':
                result_msg = response_text
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'check_calendar':
                raw_data = self.tools.get_calendar()
                prompt = f"Here is my calendar data: '{raw_data}'. Summarize my schedule naturally for me as a helpful butler. Be concise."
                brain_res = brain.think(prompt)
                result_msg = brain_res.get('response', raw_data)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'add_event':
                text = command.get('text')
                result_msg = self.tools.add_calendar_event(text)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'check_email':
                raw_data = self.tools.get_emails()
                prompt = f"Here is a list of unread emails: '{raw_data}'. Summarize who they are from and the subjects naturally. Be concise."
                brain_res = brain.think(prompt)
                result_msg = brain_res.get('response', raw_data)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'send_email':
                recipient = command.get('recipient')
                body = command.get('message')
                
                # Check memory for default email if not provided or set to 'me'
                if not recipient or recipient.lower() == 'me' or recipient == 'email@example.com' or recipient == 'user@example.com':
                    saved_email = self.memory.get_memory('email')
                    if saved_email:
                        recipient = saved_email
                        logger.info(f"Using saved email from memory: {recipient}")
                
                if not recipient:
                    result_msg = "I don't have a recipient email address, Sir."
                else:
                    result_msg = self.tools.send_email(recipient, "Message from Jarvis", body)
                
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'read_email':
                query = command.get('query')
                raw = self.tools.read_email(query)
                prompt = f"I have fetched this email for '{query}':\n{raw}\n\nRead/Summarize this naturally."
                brain_res = brain.think(prompt)
                result_msg = brain_res.get('response', raw)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'morning_briefing':
                try:
                    if ui_callback: ui_callback('status', {'message': 'Preparing morning briefing...'})
                    cur_time = datetime.datetime.now().strftime("%I:%M %p")
                    date_str = datetime.datetime.now().strftime("%A, %B %d")
                    weather = self.tools.get_weather()
                    calendar = self.tools.get_calendar()
                    emails = self.tools.get_emails()
                    headlines = ""
                    try:
                        from utils.web_reader import get_top_headlines
                        headlines = get_top_headlines(5)
                    except Exception:
                        headlines = "(headlines unavailable)"
                    
                    prompt = (f"Time: {cur_time}. Date: {date_str}. Weather: {weather}. "
                              f"Calendar: {calendar}. Emails: {emails}. "
                              f"Today's headlines: {headlines}. Generate a briefing.")
                    brain_res = brain.think(prompt)
                    result_msg = brain_res.get('response', f"Good morning Sir. {cur_time}, {weather}.")
                    if ui_callback: ui_callback('ai_text', {'text': result_msg})
                    self.mouth.speak(result_msg)
                except Exception as e:
                    result_msg = "Morning briefing failed."
                    self.mouth.speak(result_msg)

            elif action == 'clean_downloads':
                result_msg = clean_downloads()
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'delete_file':
                result_msg = delete_file(target)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'delete_screenshot':
                count = command.get('count', 1)
                result_msg = delete_screenshots(count)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'download_file':
                q = command.get('query', '')
                ft = command.get('file_type', 'pdf')
                if ui_callback: ui_callback('status', {'message': f'Searching for {ft}: {q}...'})
                self.mouth.speak(f"Searching for {q}. One moment.")
                result_msg = search_and_download(q, ft)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'search_web':
                query = command.get('query', target or original_text)
                self.mouth.speak("Searching the internet, Sir.")
                web_content = get_answer_from_web(query)
                prompt = f"Search content for '{query}':\n{web_content[:4000]}\nProvide a clear answer."
                brain_res = brain.think(prompt)
                result_msg = brain_res.get('response', "I found some info but couldn't summarize.")
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'get_stock':
                sym = command.get('symbol', target)
                result_msg = self.tools.get_stock_price(sym)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'set_mode':
                mode = command.get('value')
                result_msg = self.tools.set_mode(mode)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)
            
            elif action == 'smarthome':
                d = command.get('device', '')
                c = command.get('command', 'turn_on')
                v = command.get('value')
                result_msg = self.smarthome.control_device(d, c, v)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                # Push fresh device state so the dashboard reflects the change
                if ui_callback: ui_callback('home_update', self.smarthome.get_devices_json())
                self.mouth.speak(result_msg)
            
            elif action == 'home_status':
                result_msg = self.smarthome.get_status()
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg.replace("🟢", "").replace("⚫", ""))
            
            elif action == 'capture_photo':
                self.mouth.speak("Say cheese, Sir!")
                time.sleep(0.5)
                photo_path = self.tools.capture_photo()
                if photo_path and os.path.exists(photo_path):
                    result_msg = "Photo captured successfully."
                    # Refresh the photo frame on the dashboard
                    if ui_callback: ui_callback('photo_taken', {'photo_url': '/static/webcam_capture.jpg', 'timestamp': time.time()})
                else:
                    result_msg = "Failed to capture photo."
                if ui_callback: ui_callback('ai_text', {'text': result_msg})

            elif action == 'analyze_photo':
                p = command.get('prompt', 'Describe what you see.')
                self.mouth.speak("Analyzing visual data, Sir.")
                # Basic logic for photo_path here... (simplified for executor)
                photo_path = os.path.join('static', 'webcam_capture.jpg')
                if not os.path.exists(photo_path):
                     photo_path = self.tools.capture_photo()
                
                if photo_path and os.path.exists(photo_path):
                    brain_res = brain.think(p, image_path=photo_path)
                    result_msg = brain_res.get('response', "Analysis failed.")
                else:
                    result_msg = "No image found to analyze."
                
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'run_study_mode':
                self.mouth.speak("Starting analysis of new files in the knowledge input folder.")
                if ui_callback: ui_callback('status', {'message': 'Studying documents...'})
                
                # Run study folder
                result_msg = self.librarian.study_folder()
                
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'study_memory':
                if brain.short_term_memory:
                    self.mouth.speak("Committing current knowledge to my permanent vault.")
                    if ui_callback: ui_callback('status', {'message': 'Memorizing...'})
                    
                    # Store manually
                    res = self.librarian.memorize_text(brain.short_term_memory, "User_Session_Upload")
                    result_msg = f"I have studied the documents. {res}"
                else:
                    result_msg = " I don't have any temporary documents in my short-term memory to study."
                
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'write_code':
                description = command.get('description', 'do something complex')
                self.mouth.speak(f"I am writing a script to {description}, Sir.")
                if ui_callback: ui_callback('status', {'message': f'Coding: {description}...'})

                # Use ComplexTaskManager for single-step code tasks too
                # — it handles auto-fix, retries, and progress reporting
                self.task_manager.brain = brain
                self.task_manager.ui_callback = ui_callback or (lambda e, d: None)

                task_obj = self.task_manager.create_task(
                    f"Write and run code: {description}",
                    [f"Write and execute Python code to: {description}"]
                )
                ok, msg = self.task_manager.start_task(task_obj.id)
                result_msg = msg
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                if not ok:
                    self.mouth.speak(result_msg)

            elif action == 'complex_task':
                task_text = command.get('task', original_text or '')
                raw_steps = command.get('steps') or []

                # Normalize whatever the LLM produced: plain strings,
                # dicts without depends_on, mixed/string IDs, etc.
                mode, specs = normalize_step_specs(raw_steps)
                if not specs:
                    specs = [{"id": 1, "text": task_text, "depends_on": []}]

                # Wire up the task manager with the current brain and callback
                self.task_manager.brain = brain
                self.task_manager.ui_callback = ui_callback or (lambda e, d: None)

                if mode == "deps":
                    task_obj = self.task_manager.create_task_with_deps(
                        task_text, specs
                    )
                else:
                    # No real dependencies — clean sequential run
                    task_obj = self.task_manager.create_task(
                        task_text, [s['text'] for s in specs]
                    )

                ok, msg = self.task_manager.start_task(task_obj.id)
                result_msg = msg
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                if not ok:
                    self.mouth.speak(result_msg)

            elif action == 'run_skill':
                name = command.get('name', '')
                params = command.get('params') or {}
                self.mouth.speak(f"Running skill {name}, Sir.")
                if ui_callback:
                    ui_callback('status', {'message': f'Skill: {name}...'})
                try:
                    res = self.skills.run(
                        name, params=params, coder=self.coder
                    )
                    result_msg = res['output'] if res['success'] else (
                        f"Skill '{name}' failed: {res['output'][:300]}"
                    )
                except SkillError as e:
                    result_msg = str(e)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg[:200])

            elif action == 'save_skill':
                name = command.get('name')
                description = command.get('description', '')
                code = command.get('code')
                params = command.get('params') or {}
                try:
                    result_msg = self.skills.save_skill(
                        name, description, code, params=params
                    )
                except SkillError as e:
                    result_msg = str(e)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'list_skills':
                skills = self.skills.list_skills()
                if not skills:
                    result_msg = "No skills saved yet, Sir."
                else:
                    lines = [f"{len(skills)} skills available:"]
                    for s in skills:
                        p = f" (params: {', '.join(s['params'])})" \
                            if s['params'] else ""
                        d = s.get('description') or ''
                        lines.append(f"- {s['name']}: {d[:60]}{p}")
                    result_msg = "\n".join(lines)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(f"You have {len(skills)} skills, Sir.")

            elif action == 'delete_skill':
                result_msg = self.skills.delete_skill(command.get('name', ''))
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'call_tool':
                name = command.get('name', '')
                args = command.get('args') or command.get('params') or {}
                try:
                    ok, text = self.tool_registry.call(name, args=args)
                    result_msg = text
                except ToolError as e:
                    result_msg = str(e)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg.split('\n')[0][:180])

            elif action == 'list_tools':
                tools = self.tool_registry.list_tools()
                if not tools:
                    result_msg = "No external tools registered, Sir."
                else:
                    lines = [f"{len(tools)} external tools available:"]
                    for t in tools:
                        p = ", ".join(t['params']) or 'none'
                        lines.append(f"- {t['name']}: "
                                     f"{t['description'][:60]} (params: {p})")
                    result_msg = "\n".join(lines)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(f"You have {len(tools)} external tools, Sir.")

            elif action == 'schedule_automation':
                text = command.get('text', original_text or '')
                result_msg = self.recurring.add(text)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'list_automations':
                result_msg = self.recurring.list_jobs()
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(
                    f"You have {self.recurring.count()} recurring "
                    f"automations, Sir.")

            elif action == 'cancel_automation':
                result_msg = self.recurring.remove(
                    command.get('keyword', ''))
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'dev_set_context':
                project_name = command.get('project')
                if project_name:
                    self.active_project = project_name
                    self.mouth.speak(f"Focusing on {project_name}. Dev context active.")
                    if ui_callback: ui_callback('status', {'message': f'Active Context: {project_name}'})
                else:
                    self.active_project = None
                    self.mouth.speak("Dev context cleared.")

            elif action == 'dev_write':
                project_name = command.get('project') or self.active_project
                file_path = command.get('file')
                code_content = command.get('code')
                
                if not project_name:
                    result_msg = "No project specified and no active context. Which project?"
                elif not file_path or not code_content:
                    result_msg = "Invalid parameters for Developer Write. File and Code are required."
                else:
                    self.mouth.speak(f"Writing code to {file_path} in {project_name}.")
                    
                    from utils.dev_studio import ProjectManager
                    pm = ProjectManager()
                    result_msg = pm.write_to_file(project_name, file_path, code_content)
                
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'dev_command':
                project_name = command.get('project') or self.active_project
                cmd = command.get('command')
                
                if not project_name:
                    result_msg = "No project specified and no active context. Which project?"
                elif not cmd:
                    result_msg = "Invalid parameters for Developer Command. Command is required."
                else:
                    self.mouth.speak(f"Executing command in {project_name}...")
                    if ui_callback: ui_callback('status', {'message': f'Running: {cmd}'})
                    
                    from utils.dev_studio import ProjectManager
                    pm = ProjectManager()
                    result_msg = pm.run_project_command(project_name, cmd)
                
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak("Command execution complete.")

            elif action == 'dev_create':
                project_name = command.get('project')
                proj_type = command.get('type', 'python')
                
                if not project_name:
                    result_msg = "I need a project name to get started, Sir."
                else:
                    self.mouth.speak(f"Initializing {proj_type} project: {project_name}...")
                    from utils.dev_studio import ProjectManager
                    pm = ProjectManager()
                    result_msg = pm.create_project(project_name, proj_type)
                    
                    # Auto-set context
                    self.active_project = project_name
                    
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'dev_open':
                project_name = command.get('project') or self.active_project
                
                if not project_name:
                    result_msg = "Which project should I open?"
                else:
                    self.mouth.speak(f"Opening project {project_name} in VS Code...")
                    from utils.dev_studio import ProjectManager
                    pm = ProjectManager()
                    result_msg = pm.open_in_vscode(project_name)
                
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'dev_build_full':
                name = command.get('project_name')
                if not name:
                    name = "Jarvis_Project"

                if ui_callback: ui_callback('status', {'message': f'Factory Building: {name}...'})
                
                from utils.dev_studio import ProjectManager
                pm = ProjectManager()
                result_msg = pm.build_full_project(command)
                
                self.mouth.speak("Coding complete. Packaging application...")
                
                # Zip and Ship
                zip_path = pm.zip_project(name.replace(" ", "_"))
                if zip_path:
                    self.mouth.speak("Project compressed. Initiating delivery protocol.")
                    
                    # Send to User (Sender = Receiver for self-delivery)
                    recipient = os.environ.get("GMAIL_USER")
                    if recipient:
                        email_res = self.courier.send_file(
                            to_email=recipient,
                            subject=f"Project Delivery: {name}",
                            body=f"Jarvis Software Factory has completed the build for {name}.\n\nSource code attached.",
                            attachment_path=zip_path
                        )
                        self.mouth.speak("Source code emailed to you, Sir.")
                        result_msg += f"\n[Delivery] {email_res}"
                    else:
                        result_msg += "\n[Delivery] Skipped (No Email Configured)."
                
                # Auto open in VS Code
                pm.open_in_vscode(name.replace(" ", "_"))
                
                # Auto-set context
                self.active_project = name.replace(" ", "_")
                
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'mobile_code':
                project_name = command.get('project')
                steps = command.get('steps', [])
                
                if not project_name:
                    result_msg = "No project specified for mobile code generation."
                elif not steps:
                    result_msg = "No steps provided for mobile code generation."
                else:
                    self.mouth.speak(f"Applying {len(steps)} changes to {project_name}...")
                    if ui_callback: ui_callback('status', {'message': f'Coding in {project_name} ({len(steps)} files)...'})
                    
                    from utils.mobile_studio import MobileManager
                    mobile = MobileManager()
                    
                    success_count = 0
                    for step in steps:
                        file_path = step.get('file')
                        content = step.get('content')
                        if file_path and content:
                            mobile.write_feature(project_name, file_path, content)
                            success_count += 1
                            
                    result_msg = f"Implemented feature across {success_count} files in {project_name}."
                
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'architect_app':
                idea = command.get('idea')
                if not idea:
                    idea = "a generic app"

                self.mouth.speak(f"Architecting a solution for {idea}...")
                if ui_callback: ui_callback('status', {'message': f'Architecting {idea}...'})
                
                from utils.mobile_studio import MobileManager
                mobile = MobileManager()
                
                blueprint = mobile.generate_app_blueprint(idea)
                
                import json
                print("\n--- APP BLUEPRINT ---")
                print(json.dumps(blueprint, indent=2))
                print("---------------------")
                
                result_msg = "I couldn't generate a blueprint."
                if 'project_name' in blueprint:
                    files_count = len(blueprint.get('files_to_create', []))
                    result_msg = f"Blueprint generated for {blueprint['project_name']}. It requires {files_count} files."
                
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'auto_build_app':
                idea = command.get('idea')
                if not idea: idea = "a generic app"
                
                self.mouth.speak(f"I am becoming the CTO. Designing architecture for {idea}...")
                if ui_callback: ui_callback('status', {'message': f'Architecting {idea}...'})
                
                from utils.mobile_studio import MobileManager
                mobile = MobileManager()
                
                # Step 1: Design
                blueprint = mobile.generate_app_blueprint(idea)
                
                if 'project_name' in blueprint:
                    self.mouth.speak(f"Blueprint approved for {blueprint['project_name']}. Starting construction loop. This may take a moment.")
                    if ui_callback: ui_callback('status', {'message': f'Building {blueprint["project_name"]}...'})
                    
                    # Step 2: Build
                    build_result = mobile.build_from_blueprint(blueprint)
                    print(build_result)
                    
                    if "Error" in build_result or "failed" in build_result.lower():
                        self.mouth.speak(f"Construction encountered an error: {build_result}")
                        result_msg = build_result
                    else:
                        self.mouth.speak(f"The application is ready. Opening in VS Code.")
                        
                        # Step 3: Open
                        from utils.dev_studio import ProjectManager
                        pm = ProjectManager()
                        pm.open_in_vscode(blueprint['project_name'])
                        
                        result_msg = build_result
                else:
                    result_msg = "Blueprint generation failed."
                    self.mouth.speak(result_msg)

                if ui_callback: ui_callback('ai_text', {'text': result_msg})

            elif action == 'set_reminder':
                text = command.get('text') or original_text or ''
                self.mouth.speak("Setting a reminder, Sir.")
                result_msg = self.scheduler.schedule(text)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'list_reminders':
                result_msg = self.scheduler.list_reminders()
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'cancel_reminder':
                text = command.get('text') or ''
                result_msg = self.scheduler.cancel(text)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'desktop_task':
                task = command.get('task') or original_text or ''
                if not task:
                    result_msg = "What should I do on your computer, Sir?"
                else:
                    self.mouth.speak("Looking at your screen, Sir.")
                    if ui_callback: ui_callback('status', {'message': f'Desktop task: {task}'})
                    result_msg = self.desktop_agent.run_task(task)
                    if "complete" in result_msg.lower() or "task complete" in result_msg.lower():
                        self.mouth.speak("Task complete, Sir.")
                    else:
                        self.mouth.speak("I hit a snag with that task, Sir.")
                if ui_callback: ui_callback('ai_text', {'text': result_msg})

            elif action == 'clear_history':
                result_msg = brain.clear_history()
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            # === EPISODIC MEMORY ===
            elif action == 'remember':
                text = command.get('text') or target or original_text or ''
                category = command.get('category', 'fact')
                mem = self.episodic.remember(text, category=category, source='conversation')
                result_msg = f"I'll remember that: {text[:80]}" if mem else "I couldn't store that."
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'recall':
                query = command.get('query') or target or original_text or ''
                results = self.episodic.search(query, limit=5)
                if results:
                    lines = [f"I found {len(results)} related memories:"]
                    for m in results:
                        lines.append(f"  • [{m['category']}] {m['text'][:80]}")
                    result_msg = "\n".join(lines)
                else:
                    result_msg = f"I don't have any memories about '{query}'."
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'forget_memory':
                text = command.get('text') or target or ''
                count = self.episodic.forget(keyword=text)
                result_msg = f"Forgot {count} memories matching '{text}'." if count else f"No memories found for '{text}'."
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'memory_stats':
                stats = self.episodic.stats()
                result_msg = f"Memory: {stats['total']} entries across {len(stats['categories'])} categories. "
                result_msg += "Categories: " + ", ".join(f"{k}({v})" for k, v in stats['categories'].items())
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            # === RELATIONSHIPS ===
            elif action == 'add_person':
                name = command.get('name', '')
                rel = command.get('relationship', '')
                details = command.get('details', '')
                birthday = command.get('birthday', '')
                person = self.relationships.add_person(
                    name, relationship_type=rel, details=details, birthday=birthday
                )
                result_msg = f"Got it. {name} is your {rel}."
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'person_info':
                name = command.get('name') or target or ''
                person = self.relationships.get_person(name)
                if person:
                    lines = [f"{person['name']} ({person.get('relationship', '?')}):"]
                    if person.get('hobbies'): lines.append(f"  Hobbies: {', '.join(person['hobbies'])}")
                    if person.get('allergies'): lines.append(f"  Allergies: {', '.join(person['allergies'])}")
                    if person.get('birthday'): lines.append(f"  Birthday: {person['birthday']}")
                    if person.get('preferences'): lines.append(f"  Likes: {', '.join(person['preferences'][:5])}")
                    result_msg = "\n".join(lines)
                else:
                    result_msg = f"I don't have info about {name}. Tell me about them!"
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'gift_suggestions':
                name = command.get('name') or target or ''
                suggestions = self.relationships.get_gift_suggestions(name)
                result_msg = f"Gift ideas for {name}:\n" + "\n".join(f"  • {s}" for s in suggestions)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'dinner_suggestions':
                name = command.get('name') or target or ''
                suggestions = self.relationships.get_dinner_suggestions(name)
                result_msg = f"Dinner ideas with {name}:\n" + "\n".join(f"  • {s}" for s in suggestions)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            # === MEETING MODE ===
            elif action == 'start_meeting':
                result_msg = self.meeting.start_recording()
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'stop_meeting':
                result_msg = self.meeting.stop_recording()
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'meeting_transcript':
                transcript = self.meeting.get_live_transcript()
                result_msg = transcript if transcript else "No transcript available yet."
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak("Here's what I've captured so far, Sir.")

            # === FRIDGE VISION ===
            elif action == 'analyze_fridge':
                self.mouth.speak("Analyzing your fridge, Sir.")
                if ui_callback: ui_callback('status', {'message': 'Analyzing fridge contents...'})
                result = self.fridge.analyze_fridge()
                if result.get('error'):
                    result_msg = result['error']
                else:
                    ingredients = result.get('ingredients', [])
                    recipes = result.get('recipes', [])
                    shopping = result.get('shopping_list', [])
                    lines = [f"Found {len(ingredients)} ingredients: {', '.join(ingredients[:10])}."]
                    if recipes:
                        lines.append(f"\nSuggested {len(recipes)} recipes:")
                        for r in recipes[:3]:
                            name = r.get('name', 'Recipe') if isinstance(r, dict) else str(r)[:60]
                            lines.append(f"  🍳 {name}")
                    if shopping:
                        lines.append(f"\nShopping list: {', '.join(shopping[:8])}")
                    result_msg = "\n".join(lines)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'suggest_recipe':
                prompt = command.get('query') or target or original_text or ''
                result_msg = self.fridge.quick_recipe(prompt)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            # === TRIAGE & PROACTIVE ===
            elif action == 'triage':
                if not hasattr(self, '_proactive'):
                    from utils.proactive import ProactiveEngine
                    self._proactive = ProactiveEngine(
                        secretary=self.tools.secretary,
                        episodic_memory=self.episodic
                    )
                result_msg = self._proactive.triage.get_triage_summary()
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak("Here's your message triage, Sir.")

            elif action == 'schedule_insights':
                if not hasattr(self, '_proactive'):
                    from utils.proactive import ProactiveEngine
                    self._proactive = ProactiveEngine(
                        secretary=self.tools.secretary,
                        episodic_memory=self.episodic
                    )
                suggestions = self._proactive.time_blocker.scan_and_adjust()
                if suggestions:
                    lines = ["Schedule insights:"]
                    for s in suggestions[:5]:
                        lines.append(f"  • {s['message']}")
                    result_msg = "\n".join(lines)
                else:
                    result_msg = "Your schedule looks good, Sir."
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            # === PRIVACY ===
            elif action == 'privacy_settings':
                result_msg = self.privacy.get_trust_summary()
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak("Here are your privacy settings, Sir.")

            elif action == 'privacy_update':
                key = command.get('key', '')
                value = command.get('value', '')
                result_msg = self.privacy.update_setting(key, value)
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'audit_log':
                entries = self.privacy.get_audit_log(limit=10)
                if entries:
                    lines = ["Recent activity:"]
                    for e in entries:
                        lines.append(f"  [{e['date'][:16]}] {e['action']} — {e.get('status', '')}")
                    result_msg = "\n".join(lines)
                else:
                    result_msg = "No audit entries yet."
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            # === ENHANCED MORNING BRIEFING ===
            elif action == 'morning_briefing':
                try:
                    if ui_callback: ui_callback('status', {'message': 'Preparing morning briefing...'})
                    from utils.briefing import MorningBriefing
                    from utils.proactive import ProactiveEngine
                    proactive = ProactiveEngine(
                        secretary=self.tools.secretary,
                        episodic_memory=self.episodic
                    )
                    briefing = MorningBriefing(
                        tools=self.tools,
                        secretary=self.tools.secretary,
                        episodic_memory=self.episodic,
                        relationships=self.relationships,
                        proactive_engine=proactive,
                        tasks=self.tasks,
                        history=self.history,
                        brain=brain
                    )
                    result_msg = briefing.generate()
                    if ui_callback: ui_callback('ai_text', {'text': result_msg})
                    self.mouth.speak(result_msg)
                except Exception as e:
                    result_msg = "Morning briefing failed."
                    self.mouth.speak(result_msg)

            elif action == 'help':
                result_msg = (
                    "Here's what I can do, Sir:\n"
                    "• General chat & questions\n"
                    "• Web search & real-time info (news, weather, stocks)\n"
                    "• Open apps, websites, play YouTube, control media\n"
                    "• Email & calendar (Gmail / Google Calendar)\n"
                    "• Reminders — 'remind me in 20 minutes to X'\n"
                    "• Smart home control (lights, fan, AC)\n"
                    "• System info: battery, CPU/RAM, screenshots, camera\n"
                    "• Knowledge vault: 'study new files', recall documents\n"
                    "• Missions — 'start mission: <goal>'\n"
                    "• Coding: write & run code, scaffold projects, build mobile apps\n"
                    "• Desktop control — 'click <thing> on my computer'\n"
                    "• Clean up downloads, download files from the web\n"
                    "• System info & weather — 'system status', 'what's the weather'\n"
                    "• System insights — 'what's using my RAM', 'kill <app>'\n"
                    "• Read a webpage — 'read this page: <url>'\n"
                    "• News — 'today's headlines'\n"
                    "• To-do list — 'add <task> to my todo list', 'mark X as done'\n"
                    "• Notes — 'take a note: <text>', 'show my notes'\n"
                    "• Daily recap — 'what did I do today'\n"
                    "🧠 NEW FEATURES:\n"
                    "• Episodic Memory — I remember your preferences, goals, and context across chats\n"
                    "• Relationship Intelligence — I track people you care about, birthdays, gift ideas\n"
                    "• Fridge Vision — 'look at my fridge' for recipe suggestions\n"
                    "• Meeting Mode — 'start meeting mode' for live transcription\n"
                    "• Privacy Controls — 'privacy settings' to manage trust levels\n"
                    "• Cross-App Triage — 'what needs my attention' for priority messages\n"
                    "Say 'morning briefing' for a daily summary."
                )
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak("I can do a lot, Sir. Check the dashboard for the full list.")

            # === CONVERSATION MEMORY ===
            elif action == 'last_topic':
                topics = self.episodic.get_by_category('conversation_topic', limit=5)
                if topics:
                    latest = topics[0]
                    ts = latest.get('created', '')[:16].replace('T', ' ')
                    result_msg = f"Last time we talked ({ts}): {latest['text']}"
                    # Also show a few more recent topics
                    if len(topics) > 1:
                        result_msg += f"\n\nBefore that:\n"
                        for t in topics[1:3]:
                            ts2 = t.get('created', '')[:16].replace('T', ' ')
                            result_msg += f"  • [{ts2}] {t['text']}\n"
                else:
                    result_msg = "We haven't had any conversations yet, Sir. This is our first!"
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg)

            elif action == 'conversation_history':
                topics = self.episodic.get_by_category('conversation_topic', limit=10)
                if topics:
                    lines = ["Here's our recent conversation history:"]
                    for t in topics:
                        ts = t.get('created', '')[:16].replace('T', ' ')
                        lines.append(f"  • [{ts}] {t['text']}")
                    result_msg = "\n".join(lines)
                else:
                    result_msg = "No conversation history found, Sir."
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak("Here's our recent conversation history, Sir.")

            elif action == 'search_transcripts':
                query = command.get('query', original_text or '')
                try:
                    from utils.transcript_search import get_archive
                    hits = get_archive().search(query, limit=5)
                    if not hits:
                        result_msg = f"No past conversations matched '{query}', Sir."
                    else:
                        lines = [f"Found {len(hits)} matching exchanges for '{query}':"]
                        for h in hits:
                            lines.append(f"• {h['snippet']}  [{h['created'][:16]}]")
                        result_msg = "\n".join(lines)
                except Exception as e:
                    result_msg = f"Transcript search failed: {e}"
                if ui_callback: ui_callback('ai_text', {'text': result_msg})
                self.mouth.speak(result_msg[:220])

            else:
                result_msg = f"Unknown action: {action}"
                if ui_callback: ui_callback('ai_text', {'text': result_msg})

        except Exception as e:
            logger.error(f"Executor Error: {e}", exc_info=True)
            result_msg = f"System error during execution: {e}"            # Record the command for the daily-summary feature
        if original_text:
            try:
                self.history.log(original_text, action, result_msg or response_text)
            except Exception:
                pass

        # Auto-capture memories from all conversations
        if original_text and action in ('chat', None, ''):
            try:
                self.episodic.auto_capture(original_text, result_msg or response_text)
            except Exception:
                pass

        # Periodically consolidate memories (every ~50 interactions)
        try:
            if self.episodic.count() > 100 and self.episodic.count() % 50 == 0:
                self.episodic.consolidate()
        except Exception:
            pass

        return result_msg
