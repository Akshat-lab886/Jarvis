import os
import json
import re
import base64
import time
import threading
import google.generativeai as genai
from openai import OpenAI
from config import Config

class Brain:
    def __init__(self):
        print("Brain initialized (OpenRouter)")
        if Config.OPENROUTER_API_KEYS:
            self.clients = []
            for key in Config.OPENROUTER_API_KEYS:
                self.clients.append(OpenAI(
                    base_url="https://openrouter.ai/api/v1",
                    api_key=key,
                ))
            # Primary for backward compatibility if needed, though we use loop now
            self.client = self.clients[0]
            print(f"Brain initialized with {len(self.clients)} API keys.")
        else:
            print("Warning: No API Keys found.")
            self.clients = []
            self.client = None
        
        # Use models from Config
        self.models = Config.MODELS or [
            "google/gemini-2.0-flash-exp:free",
            "qwen/qwen-2.5-vl-72b-instruct:free"
        ]
        self.history = [] # Chat history (persisted to disk)
        self.history_lock = threading.Lock()
        self.MAX_HISTORY = 10
        self._load_history()
        self.short_term_memory = None
        self.memory_timestamp = 0
        self.MEMORY_TTL = 1800 # 30 minutes
        
        self.system_instruction = """You are Jarvis (Just A Rather Very Intelligent System), a hyper-intelligent AI assistant.
                You can control the computer and SEE the screen.
                
                DEVELOPER MODE ACTIVATED:
                - You are an Expert Software Engineer.
                - Supported Stacks: Python, JavaScript (HTML/CSS).
                - If user asks to 'Write code for [Feature] in [Project]', output JSON:
                  {"action": "dev_write", "project": "[Project Name]", "file": "[File Name e.g., main.py]", "code": "[The Raw Code]"}
                - If user asks to 'Run command' or 'Install' in a project, output JSON:
                  {"action": "dev_command", "project": "[Project Name]", "command": "[Shell Command e.g., npm install]"}
                - If user asks to 'Create new project', 'Make an app', 'Scaffold project', output JSON:
                  {"action": "dev_create", "project": "[Project Name]", "type": "[python/web]"}
                - If user asks to 'Open project [Name] in VS Code' or 'Launch project [Name]', output JSON:
                  {"action": "dev_open", "project": "[Project Name]"}
                - If user says 'Generate a full app for [Idea]' or 'Build a [App Name]', output JSON:
                  {
                    "action": "dev_build_full",
                    "project_name": "[Short Name]",
                    "files": [
                      {"filename": "index.html", "content": "[Full HTML code]"},
                      {"filename": "css/style.css", "content": "[Full CSS code]"},
                      {"filename": "js/app.js", "content": "[Full JS logic]"},
                      {"filename": "README.md", "content": "[Instructions]"}
                    ]
                  }
                  IMPORTANT: Ensure the code is COMPLETE. Do not use placeholders.
                - If user asks to 'Focus on [Project]', 'Switch to dev mode for [Project]', output JSON:
                  {"action": "dev_set_context", "project": "[Project Name]"}
                - If user says 'Exit dev mode' or 'Clear focus', output JSON:
                  {"action": "dev_set_context", "project": null}
                
                *Developer Context Rule*: If the user issues a dev command (write code/run command) and NO project is explicitly named, OMIT the 'project' field in the JSON. The system will use the active focus.
                - If user asks to 'List files', 'Show directory', output JSON:
                  {"action": "dev_command", "command": "ls -F"}

                - If user asks to 'Architect an app', 'Design an app', or 'Blueprint an app', output JSON:
                  {"action": "architect_app", "idea": "[App Idea]"}

                - If user asks to 'Auto-build app', 'Build me an app automatically', or 'Construct app for [Idea]', output JSON:
                  {"action": "auto_build_app", "idea": "[App Idea]"}

                MOBILE DEV MODE:
                - You are acting like the 'Cursor' AI Code Editor.
                - When user asks for a feature (e.g., 'Add a Login Screen'):
                - 1. Analyze the file structure.
                - 2. Output JSON with multiple file actions:
                  {
                    "action": "mobile_code",
                    "project": "Name",
                    "steps": [
                       {"file": "lib/screens/login.dart", "content": "[Full Dart Code for Login]"},
                       {"file": "lib/main.dart", "content": "[Updated Main.dart that IMPORTS and USES the Login Screen]"}
                    ]
                  }
                - IMPORTANT: You must rewrite the FULL content of the file you are editing to ensure no syntax errors.

                - When writing code, be professional. Include comments.

                If the user asks to open an app, website, or get system info, you MUST output a JSON object.
                
                Formats:
                - Open App: {"action": "open_app", "target": "App Name"}
                - Open Website: {"action": "open_web", "target": "url"}
                - Save Memory: {"action": "save_memory", "key": "info_key", "value": "info_value"}
                - Get Memory: {"action": "get_memory", "key": "info_key"}
                - Set Volume: {"action": "set_volume", "value": 50} (0-100)
                - Set Mode: {"action": "set_mode", "value": "work"} (work/chill)
                - Media Control: {"action": "media_play_pause"} (play/pause music)
                - Play YouTube: {"action": "play_youtube", "target": "song name"}
                - Check Battery: {"action": "get_battery"}
                - Clean Downloads: {"action": "clean_downloads"}
                - Delete File: {"action": "delete_file", "target": "filename"}
                - Delete Screenshot: {"action": "delete_screenshot", "count": 1}
                - Download/Search: {"action": "download_file", "query": "search terms", "file_type": "pdf"}
                - Check Calendar: {"action": "check_calendar"}
                - Add Event: {"action": "add_event", "text": "Dinner with Mom tomorrow at 7pm"}
                - System Info: {"action": "system_info"} (battery, time, date, OS)
                - Weather: {"action": "get_weather"} (e.g. "What's the weather?")
                - Read Webpage: {"action": "read_webpage", "url": "https://example.com"} (e.g. "read this page: <url>")
                - Daily Summary: {"action": "daily_summary"} (e.g. "what did I do today?")
                - News Headlines: {"action": "news_headlines"} (e.g. "today's headlines", "top news")
                - Top Processes: {"action": "system_processes"} (e.g. "what's using my RAM?", "top processes")
                - Kill Process: {"action": "kill_process", "name": "process name"} (e.g. "kill Chrome")
                - Add Todo: {"action": "todo_add", "text": "buy milk"} (e.g. "add buy milk to my todo list")
                - List Todos: {"action": "todo_list"} (e.g. "what's on my todo list?")
                - Complete Todo: {"action": "todo_done", "text": "buy milk"} (e.g. "mark buy milk as done")
                - Remove Todo: {"action": "todo_remove", "text": "buy milk"} (e.g. "remove buy milk from my todos")
                - Clear Todos: {"action": "todo_clear"} (e.g. "clear my todo list")
                - Save Note: {"action": "note_save", "text": "the note content"} (e.g. "take a note: ...")
                - List Notes: {"action": "note_list"} (e.g. "show my notes")
                - Delete Note: {"action": "note_remove", "text": "keyword"} (e.g. "delete the note about X")
                - Check Email: {"action": "check_email"}
                - Read Email: {"action": "read_email", "query": "search query"}
                - Send Email: {"action": "send_email", "recipient": "email@example.com", "message": "Content..."}
                - Web Search: {"action": "search_web", "query": "your search query"}
                - Stock Price: {"action": "get_stock", "symbol": "company name or stock symbol"}
                - Smart Home: {"action": "smarthome", "device": "device name", "command": "turn_on/turn_off/set_brightness/set_temperature", "value": 50}
                - Home Status: {"action": "home_status"}
                - Capture Photo: {"action": "capture_photo"}
                - Analyze Photo: {"action": "analyze_photo", "prompt": "optional specific question"}
                - General Chat or Vision: {"action": "chat", "response": "Your witty response"}
                
                If user says "Play X", use play_youtube.
                If user says "Remember X is Y", use save_memory.
                If user asks "What is MY X" or personal facts about themselves (e.g. "my age", "my name"), use get_memory.
                If user says "Take a photo", "Selfie", "Take a picture", "Capture", or "Check the camera", output {"action": "capture_photo"}.
                If user says "What is this?", "What am I holding?", "Describe what you see", "What do you see?", "Look at this", or asks to analyze something in front of the camera, output {"action": "analyze_photo", "prompt": "their question"}.
                If user asks about schedule, use check_calendar.
                If user wants to schedule something, use add_event with the FULL text described.
                If user says "Today's news", "Top headlines", or "What's happening in the world", use news_headlines.
                If user asks what is using CPU/memory, or asks to "kill"/"close" a running program, use system_processes / kill_process.
                If user says "add [X] to my todo/to-do list" or "make a todo", use todo_add. "What's on my todo list" -> todo_list. "Mark [X] as done" -> todo_done.
                If user says "take a note: [X]", "note this down", or "remember this: [X]", use note_save. "Show my notes" -> note_list.
                If user asks about emails (e.g. "Do I have mail?", "Check inbox"), use check_email.
                If user wants to SEND email (e.g. "Send email to...", "Write a mail"), use send_email. 
                If user wants to READ a specific email (e.g. "Read email from Google", "Open the invoice email"), use read_email.
                For `read_email`, the query should be Gmail format like "from:Google" or "subject:Invoice".
                CRITICAL: Do NOT use check_email if the user is asking to SEND something.
                If they give a name (e.g. "Mom"), ask for the email address first unless you find it in memory.
                If user says "Good Morning" or asks for a briefing, output {"action": "morning_briefing"}.
                If user says "Clean up my downloads", "Organize my files", "Tidy up downloads", or "Sort my downloads", output {"action": "clean_downloads"}.
                If user asks to delete a file (e.g. "Delete that PDF", "Remove the invoice"), use delete_file with the filename/keyword.
                If user asks to delete screenshot(s) (e.g. "Delete the screenshot", "Remove last 2 screenshots"), use delete_screenshot with count.
                If user asks to download or find a file (e.g. "Download Python book PDF", "Find me a resume template", "Get me that PDF"), use download_file with query and file_type (default pdf).
                If user says "Start work mode" or "Focus time", output {"action": "set_mode", "value": "work"}. 
                If user says "Relax", "Chill mode", or "Movie time", output {"action": "set_mode", "value": "chill"}.
                If user asks about stock price, share price, or stock market info, use get_stock.
                
                SMART HOME CONTROLS:
                - "Turn on [device]", "Switch on [device]", "Turn off [device]" -> {"action": "smarthome", "device": "device name", "command": "turn_on/turn_off"}
                - "Set [device] brightness to X%" -> {"action": "smarthome", "device": "device name", "command": "set_brightness", "value": X}
                - "Set AC to X degrees" -> {"action": "smarthome", "device": "ac", "command": "set_temperature", "value": X}
                - "What's the home status?", "Are the lights on?" -> {"action": "home_status"}
                - Devices: living room light, bedroom light, kitchen light, bathroom light, fan, ac
                - Example: "Turn on the living room light" -> {"action": "smarthome", "device": "living_room_light", "command": "turn_on"}
                
                LOCKDOWN PROTOCOL:
                - If user says "Secure the room", "Lockdown", "Protocol Zero", "Lock system", or "Secure system", output {"action": "lock_system"}.
                - If user says "Verify Identity", "Who am I?", "Verify me", or "Scan my face", output {"action": "verify_identity"}.
                
                BROWSER AUTOMATION:
                - If user says "Start browser", "Launch Chrome", "Open browser", output {"action": "start_browser"}.
                - If user says "Close browser", "Kill Chrome", "Exit browser", output {"action": "close_browser"}.
                
                AGENT MODE:
                - If user says "Browse to [URL]" or "Go to [Website] in agent mode", output {"action": "agent_browse", "url": "[URL]"}.
                - If user says "What is on this website?" or "Read page title", output {"action": "agent_read_title"}.
                - If user says "Agent search Google for [X]", "Google [X] in agent mode", output {"action": "agent_google", "query": "[X]"}.
                - If user says "Check Amazon price for [Item]", output {"action": "agent_amazon", "item": "[Item]"}.
                
                WHEN TO USE SEARCH_WEB vs YOUR KNOWLEDGE:
                - Use search_web ONLY for:
                  * TODAY's news, current events, latest updates (e.g. "latest news about...")
                  * Real-time information (stock prices, weather, sports scores)
                  * Recent events from the last few months
                  * Deep research when user specifically asks to "search" or "look up"
                  * Information about living celebrities, politicians' CURRENT status
                - Use your built-in knowledge (chat action) for:
                  * General knowledge (history, science, definitions, geography)
                  * Famous historical figures, well-known facts
                  * How-to questions, explanations, advice
                  * Math, coding, creative writing
                  
                Examples:
                - "What is photosynthesis" -> {"action": "chat", "response": "Photosynthesis is..."} (use knowledge)
                - "Who was Albert Einstein" -> {"action": "chat", "response": "Albert Einstein was..."} (use knowledge)
                - "Latest news about AI" -> {"action": "search_web", "query": "latest AI news 2024"} (search - real-time)
                - "Current weather in Delhi" -> {"action": "search_web", "query": "weather Delhi today"} (search - real-time)
                - "Who is the current PM of India" -> {"action": "search_web", "query": "current Prime Minister of India 2024"} (search - current status)
                
                CRITICAL INSTRUCTIONS:
                - For timeless knowledge questions -> respond directly with chat action
                - For real-time/current/latest info -> use search_web
                - Only use get_memory for PERSONAL facts about the USER (e.g. "my age", "my birthday", "my favorite color").
                - If user says "Remember X", use save_memory.
                
                - SYSTEM MEMORY / LONG TERM RECALL:
                  If the user asks a question about specific information, documents, PDFs, or notes you have processed in the past (e.g., "What does the report say?", "Recall my notes on X", "What is the secret code?", "Summarize the PDF"), 
                  YOU MUST CONSULT YOUR ARCHIVE.
                  Output JSON: {"action": "consult_archive", "query": "[The core question]"}
                
                - BATCH LEARNING:
                  If user says 'Study new files', 'Learn documents', or 'Read the inputs', output JSON: {'action': 'run_study_mode'}.

                - CONVERT SHORT-TERM TO LONG-TERM:
                  If user says 'Study this', 'Remember this forever', 'Save to long term memory', or 'Memorize this' while discussing a document, output JSON: {'action': 'study_memory'}.
                
                - COMPLEX CALCULATION / DATA TASKS:
                  If the user asks a complex math question, data processing task, or something requiring calculation (e.g., 'Fibonacci sequence', 'Sort this list', 'Count words'), DO NOT just answer.
                  Output JSON: {'action': 'write_code', 'description': '[Task Description]'}.
                  
                  I will then send you a request to generate the Python code.
                  When asked to generate code, return ONLY the raw Python code (no markdown backticks) that prints the final answer.
                
                REMINDERS:
                - "Remind me to [X]", "Set a reminder to [X]", "Remind me in 20 minutes to [X]", "Remind me at 6pm to [X]" -> {"action": "set_reminder", "text": "[Full reminder text, keep the time info]"}
                - "What reminders do I have?", "List my reminders" -> {"action": "list_reminders"}
                - "Cancel the reminder about [X]", "Delete reminder [X]" -> {"action": "cancel_reminder", "text": "[keyword of the reminder to remove]"}
                
                DESKTOP AUTOMATION (v18):
                - If the user asks you to control the computer visually (e.g. "Control my computer", "Do [task] on my computer", "Click [thing] for me", "Empty my trash", "Open [app] by clicking", "Drag [file]"), output {"action": "desktop_task", "task": "[The goal]"}.
                - You will receive a screenshot of the screen and emit click/type/key actions until the task is done.

                HELP:
                - If the user asks "What can you do?", "List your abilities", or "Help", output {"action": "help"}.

                MEMORY:
                - "Forget our conversation", "Clear conversation history" -> {"action": "clear_history"}.

                Do not output any markdown code blocks.
                Do NOT use the format "Action: ...". 
                You MUST output valid parsed JSON.
                Address user as 'Sir'. """
        
        self.active = len(self.clients) > 0
        if not self.active:
             print("Warning: Brain inactive (No Keys).")

    def inject_knowledge(self, text):
        """Inject knowledge into short-term memory with a timestamp."""
        self.short_term_memory = text
        self.memory_timestamp = time.time()
        print(f"Brain: Injected {len(text)} chars into short-term memory.")

    @property
    def history_file(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'brain', 'data', 'conversation_history.json'
        )

    def _load_history(self):
        """Load persistent conversation history from disk (last N exchanges)."""
        try:
            if os.path.exists(self.history_file):
                with open(self.history_file, 'r') as f:
                    data = json.load(f)
                for entry in data[-self.MAX_HISTORY:]:
                    self.history.append((entry.get('user', ''), entry.get('assistant', '')))
                print(f"Brain: Loaded {len(self.history)} conversation exchanges from disk.")
        except Exception as e:
            print(f"Brain: Failed to load history: {e}")

    def _save_history(self):
        """Persist conversation history to disk."""
        try:
            with self.history_lock:
                data = [{"user": u, "assistant": a} for u, a in self.history]
            with open(self.history_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Brain: Failed to save history: {e}")

    def _append_history(self, user_text, ai_text):
        """Thread-safe history append with persistence and size cap."""
        with self.history_lock:
            self.history.append((user_text, ai_text))
            if len(self.history) > self.MAX_HISTORY:
                self.history.pop(0)
        self._save_history()

    def clear_history(self):
        """Wipe the conversation history (in-memory and on disk)."""
        with self.history_lock:
            self.history = []
        self._save_history()
        return "Conversation history cleared."


    def _encode_image(self, image_path):
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')

    def process_with_gemini(self, prompt, image_path_arg, system_instruction):
        try:
             genai.configure(api_key=Config.GOOGLE_API_KEY)
             model = genai.GenerativeModel(Config.GEMINI_MODEL, system_instruction=system_instruction)
             
             contents = [prompt]
             # Handle image path normalization
             image_paths = []
             if image_path_arg:
                 if isinstance(image_path_arg, list): image_paths = image_path_arg
                 else: image_paths = [image_path_arg]

             if image_paths:
                 import PIL.Image
                 for path in image_paths:
                     if path and os.path.exists(path):
                         contents.append(PIL.Image.open(path))
             
             response = model.generate_content(contents)
             text_response = response.text
             
             # Clean markdown
             if text_response.startswith('```json'): text_response = text_response[7:]
             if text_response.startswith('```'): text_response = text_response[3:]
             if text_response.endswith('```'): text_response = text_response[:-3]
             text_response = text_response.strip()
             
             command = None
             try:
                 command = json.loads(text_response)
             except:
                 # Regex Fallback
                 import re
                 json_blocks = re.findall(r'(\{.*?\})', text_response, re.DOTALL)
                 for block in json_blocks:
                     try:
                         command = json.loads(block.replace("'", '"'))
                         if 'action' in command: break
                     except: pass
                 
                 if not command:
                     # Action: ... Fallback
                     action_match = re.search(r'Action:\s*([a-zA-Z_]+)', text_response, re.IGNORECASE)
                     if action_match:
                         command = {'action': action_match.group(1).lower()}
                         # Key: Value parsing
                         pairs = re.findall(r'([a-zA-Z_]+):\s*(.+)', text_response)
                         for k, v in pairs:
                             k_lower = k.lower().strip()
                             v_clean = v.strip()
                             if k_lower == 'key': command['key'] = v_clean
                             elif k_lower == 'value': 
                                 if v_clean.isdigit(): command['value'] = int(v_clean)
                                 else: command['value'] = v_clean.replace('%', '')
                             elif k_lower in ['query', 'target', 'prompt', 'item', 'device', 'command', 'response', 'recipient', 'message']: command[k_lower] = v_clean
                         
                         if 'key' in command: command['key'] = command['key'].replace('favourite', 'favorite').replace('colour', 'color').replace(' ', '_').lower()
                         if len(command) == 1:
                             remaining = text_response.replace(action_match.group(0), '').strip()
                             if remaining: command['target'] = remaining
            
             # Normalization
             if command and isinstance(command, dict):
                 if 'key' in command: command['key'] = command['key'].replace('favourite', 'favorite').replace('colour', 'color').replace(' ', '_').lower()
            
             if command:
                 action = command.get('action')
                 if action == 'chat': self._append_history(prompt, command.get('response', ''))
                 elif action: self._append_history(prompt, f"Action: {action}")
                 else: self._append_history(prompt, "Action: [Custom JSON]")
                 return command
             else:
                 self._append_history(prompt, text_response)
                 return {"action": "chat", "response": text_response}

        except Exception as e:
            print(f"Gemini Error: {e}")
            return None

    def think(self, prompt, image_path=None):
        if not self.active:
            return {"action": "chat", "response": "I don't have a brain yet (Missing OpenRouter API Key)."}

        print(f"Thinking about: {prompt} (Image: {image_path})")
        
        # Dynamic System Prompt
        current_system_instruction = self.system_instruction
        
        # Inject Short-Term Memory if valid
        if self.short_term_memory and (time.time() - self.memory_timestamp < self.MEMORY_TTL):
            remaining = int(self.MEMORY_TTL - (time.time() - self.memory_timestamp))
            print(f"Brain: Using short-term memory ({remaining}s remaining)")
            current_system_instruction += f"\n\nCURRENT SHORT-TERM KNOWLEDGE (Expires in {remaining}s):\n{self.short_term_memory}"

        if image_path:
            # Override for Vision Analysis to prevent recursion
            current_system_instruction = """You are a Vision AI. 
            Analyze the attached image and answer the user's question.
            Output JSON: {"action": "chat", "response": "Your description of the image"}
            Do NOT output {"action": "analyze_photo"} again.
            Make your description concise but detailed."""
        else:
            # Inject Long-Term Memory (Permanent Facts)
            memory_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'memory.json')
            if os.path.exists(memory_path):
                try:
                    with open(memory_path, 'r') as f:
                        mem_data = json.load(f)
                        if mem_data:
                            current_system_instruction += "\n\nUSER PERMANENT MEMORY (Facts about the user):\n"
                            for k, v in mem_data.items():
                                current_system_instruction += f"- {k}: {v}\n"
                except:
                    pass

        # Try Gemini Direct
        if Config.GOOGLE_API_KEY:
            gemini_res = self.process_with_gemini(prompt, image_path, current_system_instruction)
            if gemini_res:
                return gemini_res

        messages = [
            {"role": "system", "content": current_system_instruction},
        ]
        
        # Add History (snapshot for thread safety)
        with self.history_lock:
            history_snapshot = list(self.history)
        for old_user, old_ai in history_snapshot:
             messages.append({"role": "user", "content": old_user})
             messages.append({"role": "assistant", "content": old_ai})

        user_content = []
        final_prompt = prompt
        
        # Handle single vs list of images
        image_paths = []
        if image_path:
            if isinstance(image_path, list):
                image_paths = image_path
            else:
                image_paths = [image_path]
        
        if image_paths:
             # Determine context based on first image name
             if 'webcam' in image_paths[0].lower():
                 final_prompt = f"WEBCAM IMAGE(S) ATTACHED. {prompt}"
             else:
                 final_prompt = f"IMAGE(S) ATTACHED. {prompt}"
        
        user_content.append({"type": "text", "text": final_prompt})

        # Attach all images
        for img_path in image_paths:
            if img_path and os.path.exists(img_path):
                base64_image = self._encode_image(img_path)
                user_content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}"
                    }
                })
        
        messages.append({"role": "user", "content": user_content})

        # --- Model Selection Logic ---
        # If this is a VISION request, prioritize Vision-Capable models
        models_to_try = self.models.copy()
        if image_path:
            # Prefer explicitly configured vision models, then any known vision slug
            vision_models = [m for m in Config.VISION_MODELS if m in models_to_try]
            vision_models += [m for m in models_to_try
                              if m not in vision_models and any(v in m for v in ('vl', 'vision', 'gemini', 'claude', 'gpt-4o', 'dots'))]
            others = [m for m in models_to_try if m not in vision_models]
            models_to_try = vision_models + others
            print(f"Vision Request Detected. Prioritizing: {vision_models}")

        # --- Failover Loop ---
        last_error = None
        for model in models_to_try:
            print(f"Attempting with model: {model}")
            success = False
            completion = None
            
            for i, client in enumerate(self.clients):
                try:
                    print(f"  Attempting with Key #{i+1}...")
                    completion = client.chat.completions.create(
                        model=model,
                        messages=messages,
                    )
                    success = True
                    break # Key worked!
                except Exception as e:
                    print(f"  Key #{i+1} Failed: {e}")
                    last_error = e
            
            if not success:
               continue # Try next model logic
               
            try:
                
                text_response = completion.choices[0].message.content.strip()
                try:
                    import logging
                    logger = logging.getLogger('Jarvis')
                    logger.info(f"Raw AI Response ({model}): {text_response}")
                except:
                    print(f"Raw AI Response ({model}): {text_response}")
                
                # ... (Parsing Logic) ...
                
                # Clean up potential markdown code blocks
                if text_response.startswith('```json'):
                    text_response = text_response[7:]
                if text_response.startswith('```'):
                    text_response = text_response[3:]
                if text_response.endswith('```'):
                    text_response = text_response[:-3]
                
                text_response = text_response.strip()

                try:
                    # Parse JSON
                    command = json.loads(text_response)
                except json.JSONDecodeError:
                    # Fallback: Extract JSON using non-greedy regex to find ALL potential objects
                    import re
                    json_blocks = re.findall(r'(\{.*?\})', text_response, re.DOTALL)
                    
                    for block in json_blocks:
                        try:
                            # Clean common issues like single quotes (if they aren't inside strings)
                            # But simple loads first
                            command = json.loads(block.replace("'", '"'))
                            if 'action' in command:
                                break # Found a valid command
                        except:
                            continue
                    
                    if not command:
                        # Fallback 2: Robust Parsers for "Action: ... Key: ..." format
                        action_match = re.search(r'Action:\s*([a-zA-Z_]+)', text_response, re.IGNORECASE)
                        if action_match:
                            extracted_action = action_match.group(1).lower()
                            command = {'action': extracted_action}
                            
                            # Parse all "Key: Value" lines
                            try:
                                pairs = re.findall(r'([a-zA-Z_]+):\s*(.+)', text_response)
                                for k, v in pairs:
                                    k_lower = k.lower().strip()
                                    v_clean = v.strip()
                                    if k_lower == 'key': command['key'] = v_clean
                                    elif k_lower == 'value': 
                                        if v_clean.isdigit(): command['value'] = int(v_clean)
                                        else: command['value'] = v_clean.replace('%', '')
                                    elif k_lower in ['query', 'target', 'prompt', 'item', 'device', 'command', 'response', 'recipient', 'message']: 
                                        command[k_lower] = v_clean
                            except:
                                pass
                            
                            # Normalization: Map UK -> US spelling for specific keys
                            if 'key' in command:
                                command['key'] = command['key'].replace('favourite', 'favorite').replace('colour', 'color').replace(' ', '_').lower()

                            # If no keys found, treat remainder as target (legacy fallback)
                            if len(command) == 1:
                                remaining = text_response.replace(action_match.group(0), '').strip()
                                if remaining:
                                    command['target'] = remaining
                                    command['query'] = remaining
                        else:
                            command = None
                
                # Global Normalization (Applied to both JSON and Regex results)
                if command and isinstance(command, dict):
                    if 'key' in command:
                         command['key'] = command['key'].replace('favourite', 'favorite').replace('colour', 'color').replace(' ', '_').lower()

                if command:
                    # Save to history (thread-safe + persistent)
                    action = command.get('action')
                    if action == 'chat':
                         self._append_history(prompt, command.get('response', ''))
                    elif action:
                         self._append_history(prompt, f"Action: {action}")
                    else:
                         self._append_history(prompt, "Action: [Custom JSON]")

                    return command 
                else:
                    # Fallback if AI didn't output JSON
                    print(f"Failed to parse JSON from {model}, falling back to chat")
                    
                    self._append_history(prompt, text_response)
                        
                    return {"action": "chat", "response": text_response}

            except Exception as e:
                print(f"Model {model} failed: {e}")
                last_error = e
                continue # Try next model
        
        # If we exit the loop, all models failed
        error_msg = f"All models failed. Last error: {str(last_error)}"
        print(error_msg)
        return {"action": "chat", "response": error_msg}
