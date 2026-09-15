import os
import subprocess
import webbrowser
import platform
import psutil
from datetime import datetime
from utils.secretary import Secretary

class Tools:
    def __init__(self):
        self.os_name = platform.system()
        print(f"Tools initialized on {self.os_name}")
        self.secretary = Secretary()

    def open_app(self, app_name):
        print(f"Opening app: {app_name}")
        try:
            if self.os_name == 'Darwin':  # macOS
                # 1. Try direct open
                try:
                    subprocess.run(["open", "-a", app_name], check=True, stderr=subprocess.DEVNULL)
                    return True
                except subprocess.CalledProcessError:
                    pass

                # 2. Alias Mapping
                aliases = {
                    "vscode": "Visual Studio Code",
                    "vs code": "Visual Studio Code",
                    "chrome": "Google Chrome",
                    "ppt": "Microsoft PowerPoint",
                    "excel": "Microsoft Excel",
                    "word": "Microsoft Word",
                }
                search_name = aliases.get(app_name.lower(), app_name)
                print(f"Direct open failed. Searching for '{search_name}'...")

                # Spotlight queries run as argv, never through a shell: the
                # search name comes from the LLM, and interpolating it into
                # ``sh -c`` would make ``foo; rm -rf ~`` executable.  A
                # missing mdfind degrades to empty, same as the old pipe.
                def _mdfind(query):
                    try:
                        return (subprocess.run(["mdfind", query],
                                               capture_output=True,
                                               text=True).stdout or '')
                    except Exception:
                        return ''

                # 3. Spotlight Search (Filename) — first hit only
                mdfind_out = _mdfind(
                    f"kMDItemKind == 'Application' && "
                    f"kMDItemFSName == '*{search_name}*.app'")
                app_path = (mdfind_out.strip().splitlines() or [''])[0]

                # 4. Fallback: Content match (case-insensitive, first hit)
                if not app_path:
                    all_apps = _mdfind(
                        "kMDItemKind == 'Application'").splitlines()
                    needle = search_name.lower()
                    app_path = next(
                        (ln.strip() for ln in all_apps
                         if needle in ln.lower()), '')

                # 5. Last Resort: Manual Directory Scan (Fuzzy)
                if not app_path:
                    print("Spotlight failed. Scanning directories...")
                    app_path = self._scan_dirs_for_app(search_name)

                if app_path and app_path.endswith(".app"):
                    print(f"Found app at: {app_path}")
                    subprocess.run(["open", app_path], check=True)
                    return True
                else:
                    print(f"Could not find app '{app_name}'")
                    return False
            
            elif self.os_name == 'Windows':
                os.startfile(app_name)
                return True
            else:
                subprocess.run([app_name], check=True)
                return True
        except Exception as e:
            print(f"Error opening app {app_name}: {e}")
            return False

    def _scan_dirs_for_app(self, search_name):
        # Manual scan of common app directories
        dirs = [
            "/Applications",
            "/System/Applications",
            os.path.expanduser("~/Applications")
        ]
        
        search_lower = search_name.lower().replace(" ", "").replace("-", "")
        
        for d in dirs:
            if os.path.exists(d):
                try:
                    for item in os.listdir(d):
                        if item.endswith(".app"):
                            # Check: "Cleaner-App.app" -> "cleanerapp"
                            item_name = item.lower().replace(".app", "").replace(" ", "").replace("-", "")
                            if search_lower in item_name or item_name in search_lower:
                                return os.path.join(d, item)
                except PermissionError:
                    continue
        return None

    def open_website(self, url):
        print(f"Opening website: {url}")
        try:
            if not url.startswith('http'):
                url = 'https://' + url
            webbrowser.open(url)
            return True
        except Exception as e:
            print(f"Error opening website {url}: {e}")
            return False

    def get_system_info(self):
        now = datetime.now().strftime("%I:%M %p")
        date = datetime.now().strftime("%A, %B %d, %Y")
        battery = psutil.sensors_battery()
        percent = battery.percent if battery else "Unknown"
        return f"Time: {now}, Date: {date}, Battery: {percent}%"

    def set_volume(self, level):
        try:
            # level is 0-100
            level = max(0, min(100, int(level)))
            if self.os_name == 'Darwin':
                subprocess.run(["osascript", "-e", f"set volume output volume {level}"],
                               check=True, timeout=5)
                return True
        except subprocess.CalledProcessError as e:
            print(f"Error setting volume (Permission?): {e}")
            return False
        except Exception as e:
            print(f"Error setting volume: {e}")
            return False

    def media_play_pause(self):
        try:
            if self.os_name == 'Darwin':
                print("Attempting Media Key...")
                subprocess.run(["osascript", "-e",
                               'tell application "System Events" to key code 100'],
                               timeout=5)

                # FALLBACK: Explicitly pause YouTube in Browsers via JavaScript
                browsers = ['Google Chrome', 'Safari', 'Brave Browser', 'Microsoft Edge']
                for browser in browsers:
                    try:
                        # Check if browser is running (use list-form, no shell)
                        check = subprocess.run(["pgrep", "-f", browser],
                                               stdout=subprocess.DEVNULL, timeout=3)
                        if check.returncode == 0:
                            print(f"Sending pause command to {browser}...")
                            js = "document.querySelectorAll('video').forEach(v => v.pause())"
                            if browser == 'Safari':
                                script = f'tell application "{browser}" to do JavaScript "{js}" in document 1'
                            else: # Chrome/Edge/Brave
                                script = f'tell application "{browser}" to execute front window\'s active tab javascript "{js}"'

                            subprocess.run(["osascript", "-e", script], stderr=subprocess.DEVNULL, timeout=5)

                            # Aggressive Fallback: Focus and Press 'K' (YouTube Shortcut)
                            subprocess.run(["osascript", "-e",
                                           f'tell application "{browser}" to activate'],
                                           timeout=5)
                            
                            import time
                            time.sleep(0.2)
                            import pyautogui
                            pyautogui.press('k')
                            
                    except Exception as e:
                        print(f"Browser pause error: {e}")
                        continue
                
                return True
            
            # Windows/Linux Fallback
            import pyautogui
            pyautogui.press('playpause')
            return True
        except Exception as e:
            print(f"Error toggling media: {e}")
            return "Error: " + str(e)
            
    def play_on_youtube(self, topic):
        try:
            import pywhatkit
            print(f"Playing on YouTube: {topic}")
            pywhatkit.playonyt(topic)
            return True
        except Exception as e:
            print(f"Error playing on YouTube: {e}")
            return False

    def get_battery_status(self):
        try:
            battery = psutil.sensors_battery()
            if battery:
                percent = battery.percent
                charging = "charging" if battery.power_plugged else "not charging"
                return f"Battery is at {percent}% and {charging}."
            return "Battery information unavailable."
        except Exception as e:
            return f"Error reading battery: {str(e)}"

    def take_screenshot(self):
        print("Taking screenshot...")
        try:
            import pyautogui
            screenshot = pyautogui.screenshot()
            
            # Convert to RGB (remove alpha channel) if necessary
            if screenshot.mode in ("RGBA", "P"):
                screenshot = screenshot.convert("RGB")
            
            # Save to static folder so UI can potentially show it
            file_path = os.path.join(os.getcwd(), 'static', 'screenshot.jpg')
            screenshot.save(file_path)
            print(f"Screenshot saved to {file_path}")
            return file_path
        except Exception as e:
            print(f"Screenshot error: {e}")
            return None

    def get_calendar(self):
        return self.secretary.get_upcoming_events()

    def add_calendar_event(self, event_text):
        return self.secretary.add_event(event_text)

    def get_emails(self):
        return self.secretary.get_unread_emails()

    def send_email(self, to_email, subject, body):
        return self.secretary.send_email(to_email, subject, body)

    def get_weather(self):
        try:
            import requests
            # format=3 gives "City: Temp C" simple one-line
            response = requests.get("https://wttr.in/?format=3")
            if response.status_code == 200:
                return response.text.strip()
            return "Weather unavailable (API Error)"
        except Exception as e:
            return f"Weather unavailable: {e}"

    def read_email(self, query):
        return self.secretary.read_email(query)

    def set_mode(self, mode_name):
        mode_name = mode_name.lower()
        if mode_name == 'work':
            # 1. Close Distractions
            apps_to_close = ["Spotify", "Discord", "Minecraft", "Steam"]
            for app in apps_to_close:
                self._close_app(app)
            
            # 2. Open Productivity
            self.open_app("Visual Studio Code")
            self.open_app("Google Chrome")
            self.open_app("Slack")
            
            # 3. Set Volume
            self.set_volume(20)
            return "Work mode engaged. Distractions eliminated."

        elif mode_name == 'chill':
            # 1. Close Work
            apps_to_close = ["Code", "Visual Studio Code", "Microsoft Teams", "zoom.us"]
            for app in apps_to_close:
                self._close_app(app)
            
            # 2. Open Entertainment
            self.open_app("Spotify")
            
            # 3. Set Volume
            self.set_volume(70)
            return "Relaxation protocols initiated."
            
        return f"Unknown mode: {mode_name}"

    def _close_app(self, app_name):
        print(f"Closing app: {app_name}")
        try:
            if self.os_name == 'Darwin':
                # 'pkill -x' matches exact process name. 
                # Sometimes GUI names differ from process names (e.g. 'Code' vs 'Visual Studio Code')
                subprocess.run(["pkill", "-x", app_name], stderr=subprocess.DEVNULL)
                # Fallback: try 'killall'
                subprocess.run(["killall", app_name], stderr=subprocess.DEVNULL)
            elif self.os_name == 'Windows':
                subprocess.run(["taskkill", "/F", "/IM", f"{app_name}.exe"], stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"Error closing {app_name}: {e}")

    def search_web(self, query):
        """
        Searches the web using DuckDuckGo and returns context from top results.
        Includes retry logic for reliability.
        
        Args:
            query: The search query
            
        Returns:
            str: Combined context from top 3 search results
        """
        import time
        
        if not query or not query.strip():
            return "No search query provided."
        
        query = query.strip()
        print(f"Searching web for: {query}")
        
        # Try up to 3 times with slight delays
        for attempt in range(3):
            try:
                from duckduckgo_search import DDGS
                
                # Use different search variations
                ddgs = DDGS()
                results = ddgs.text(query, max_results=5)
                
                if results:
                    # Combine the body text from top results
                    context_parts = []
                    for i, result in enumerate(results[:3], 1):
                        title = result.get('title', '')
                        body = result.get('body', '')
                        if title or body:
                            context_parts.append(f"{i}. {title}: {body}")
                    
                    if context_parts:
                        context = "\n".join(context_parts)
                        print(f"Search results (attempt {attempt + 1}):\n{context}")
                        return context
                
                # If no results, try with "news" appended for current events
                if attempt == 0 and 'news' not in query.lower():
                    results = ddgs.text(f"{query} latest", max_results=3)
                    if results:
                        context_parts = []
                        for i, result in enumerate(results[:3], 1):
                            title = result.get('title', '')
                            body = result.get('body', '')
                            if title or body:
                                context_parts.append(f"{i}. {title}: {body}")
                        
                        if context_parts:
                            return "\n".join(context_parts)
                
                time.sleep(0.5)  # Small delay between retries
                
            except Exception as e:
                print(f"Search attempt {attempt + 1} failed: {e}")
                time.sleep(1)
                continue
        
        # Fallback: Try using requests to get a simple answer
        try:
            import requests
            # Try wttr.in for weather queries
            if 'weather' in query.lower():
                response = requests.get("https://wttr.in/?format=3", timeout=5)
                if response.status_code == 200:
                    return f"Weather: {response.text.strip()}"
        except:
            pass
        
        return "I couldn't find current information on that. The search service might be temporarily unavailable."

    def get_stock_price(self, symbol_or_name):
        """
        Gets the current stock price for a given symbol or company name.
        
        Args:
            symbol_or_name: Stock symbol (e.g., "AAPL") or company name (e.g., "Apple")
            
        Returns:
            str: Stock price information
        """
        try:
            import yfinance as yf
            
            # Common company name to symbol mapping
            name_to_symbol = {
                'apple': 'AAPL',
                'google': 'GOOGL',
                'alphabet': 'GOOGL',
                'microsoft': 'MSFT',
                'amazon': 'AMZN',
                'meta': 'META',
                'facebook': 'META',
                'tesla': 'TSLA',
                'nvidia': 'NVDA',
                'netflix': 'NFLX',
                'disney': 'DIS',
                'twitter': 'X',
                'intel': 'INTC',
                'amd': 'AMD',
                'ibm': 'IBM',
                'oracle': 'ORCL',
                'salesforce': 'CRM',
                'adobe': 'ADBE',
                'paypal': 'PYPL',
                'spotify': 'SPOT',
                'uber': 'UBER',
                'airbnb': 'ABNB',
                'zoom': 'ZM',
                'snapchat': 'SNAP',
                'reliance': 'RELIANCE.NS',
                'tata': 'TCS.NS',
                'infosys': 'INFY.NS',
                'wipro': 'WIPRO.NS',
            }
            
            # Convert name to symbol if needed
            query = symbol_or_name.lower().strip()
            symbol = name_to_symbol.get(query, symbol_or_name.upper())
            
            print(f"Fetching stock price for: {symbol}")
            
            ticker = yf.Ticker(symbol)
            info = ticker.info
            
            if not info or info.get('regularMarketPrice') is None:
                # Try adding .NS for Indian stocks
                if not symbol.endswith('.NS'):
                    ticker = yf.Ticker(symbol + '.NS')
                    info = ticker.info
            
            if info and info.get('regularMarketPrice'):
                price = info.get('regularMarketPrice')
                currency = info.get('currency', 'USD')
                name = info.get('shortName', symbol)
                change = info.get('regularMarketChange', 0)
                change_percent = info.get('regularMarketChangePercent', 0)
                
                direction = "up" if change >= 0 else "down"
                
                return (
                    f"{name} ({symbol}) is currently trading at {currency} {price:.2f}, "
                    f"{direction} {abs(change_percent):.2f}% today."
                )
            else:
                return f"I couldn't find stock data for '{symbol_or_name}'. Please check the symbol or company name."
                
        except Exception as e:
            print(f"Error fetching stock price: {e}")
            return f"I couldn't fetch the stock price right now: {str(e)}"

    def get_system_vitals(self):
        """
        Gets real-time system health metrics.
        
        Returns:
            dict: CPU usage, RAM usage, battery status, and charging state
        """
        try:
            # CPU Usage (average over 1 second for accuracy)
            cpu_percent = psutil.cpu_percent(interval=1.0)
            
            # RAM Usage
            memory = psutil.virtual_memory()
            ram_percent = memory.percent
            
            # Battery Status
            battery = psutil.sensors_battery()
            if battery:
                battery_percent = battery.percent
                charging = battery.power_plugged
            else:
                battery_percent = 100  # Desktop without battery
                charging = True  # Assume plugged in
            
            # Disk Usage
            disk = psutil.disk_usage('/')
            disk_percent = disk.percent
            
            return {
                "cpu": round(cpu_percent, 1),
                "ram": round(ram_percent, 1),
                "battery": round(battery_percent, 1),
                "charging": charging,
                "disk": round(disk_percent, 1),
                "timestamp": datetime.now().strftime("%H:%M:%S")
            }
            
        except Exception as e:
            print(f"Error getting system vitals: {e}")
            return {
                "cpu": 0, "ram": 0, "battery": 100, 
                "charging": True, "disk": 0,
                "timestamp": datetime.now().strftime("%H:%M:%S")
            }

    def get_top_processes(self, n=5):
        """
        Returns the top N processes by CPU and by memory usage.
        """
        try:
            procs = []
            for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
                try:
                    procs.append(proc.info)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            # cpu_percent needs two samples to be meaningful; use the info snapshot
            by_cpu = sorted(procs, key=lambda p: p.get('cpu_percent') or 0, reverse=True)[:n]
            by_mem = sorted(procs, key=lambda p: p.get('memory_percent') or 0, reverse=True)[:n]

            cpu_lines = [f"{p['name']} ({p.get('cpu_percent') or 0:.1f}%)" for p in by_cpu]
            mem_lines = [f"{p['name']} ({p.get('memory_percent') or 0:.1f}%)" for p in by_mem]

            return (
                "Top CPU: " + ", ".join(cpu_lines) + "\n"
                "Top RAM: " + ", ".join(mem_lines)
            )
        except Exception as e:
            return f"Couldn't read process info: {e}"

    def kill_process(self, process_name):
        """
        Terminates a process by name (safe: only matching processes are killed).
        """
        if not process_name:
            return "Which process should I terminate, Sir?"
        try:
            killed = []
            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    name = (proc.info.get('name') or '').lower()
                    if process_name.lower() in name:
                        proc.terminate()
                        killed.append(proc.info['name'])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            if killed:
                return f"Terminated: {', '.join(set(killed))}."
            return f"I couldn't find a process named '{process_name}'."
        except Exception as e:
            return f"Failed to terminate process: {e}"

    def capture_photo(self):
        """
        Captures a photo from the webcam.
        
        Returns:
            str: Path to the captured image or error message
        """
        try:
            import cv2
            
            # Get the static folder path
            static_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static')
            photo_path = os.path.join(static_path, 'webcam_capture.jpg')
            
            print("Initializing webcam...")
            
            # Initialize webcam (0 = default camera)
            cam = cv2.VideoCapture(0)
            
            # Check if camera opened successfully
            if not cam.isOpened():
                print("Webcam not found. Switching to MOCK MODE.")
                return self._use_mock_image(photo_path)
            
            # Allow camera to warm up
            import time
            time.sleep(0.5)
            
            # Capture a frame
            ret, frame = cam.read()
            
            # Release the camera immediately
            cam.release()
            
            if not ret or frame is None:
                print("Failed to read from webcam. Switching to MOCK MODE.")
                return self._use_mock_image(photo_path)
            
            # Save the frame
            cv2.imwrite(photo_path, frame)
            
            print(f"Photo captured and saved to: {photo_path}")
            
            # Return the absolute path (for brain analysis)
            return photo_path
            
        except Exception as e:
            print(f"Error capturing photo: {e}")
            return f"I couldn't capture a photo: {str(e)}"

    def _use_mock_image(self, photo_path):
        """Helper to use or create a mock image when webcam fails."""
        import cv2
        import numpy as np
        
        static_path = os.path.dirname(photo_path)
        mock_source = os.path.join(static_path, 'mock_webcam.jpg')
        
        # Check if user provided a mock image
        if os.path.exists(mock_source):
            print(f"Using user provided mock image: {mock_source}")
            # Read and verify it's a valid image
            img = cv2.imread(mock_source)
            if img is not None:
                cv2.imwrite(photo_path, img)
                return photo_path
                
        # Generate a placeholder "NO CAMERA" image
        print("Generating placeholder mock image.")
        # Create a black image
        img = np.zeros((480, 640, 3), np.uint8)
        
        # Add text
        font = cv2.FONT_HERSHEY_SIMPLEX
        cv2.putText(img, 'NO WEBCAM DETECTED', (50, 240), font, 1.5, (0, 0, 255), 2, cv2.LINE_AA)
        cv2.putText(img, 'SIMULATION MODE', (150, 300), font, 1, (0, 255, 0), 2, cv2.LINE_AA)
        
        # Save it
        cv2.imwrite(photo_path, img)
        return photo_path
        
    def lock_system(self):
        """Locks the computer screen."""
        print("Initiating LOCKDOWN PROTOCOL")
        try:
            if self.os_name == 'Darwin':
                # Method 1: Simulate Cmd+Ctrl+Q (Native Lock Shortcut)
                try:
                    subprocess.run(["osascript", "-e",
                                   'tell application "System Events" to keystroke "q" using {command down, control down}'],
                                   timeout=5)
                except:
                    pass

                # Method 2: PMSET locks display immediately
                subprocess.run(["pmset", "displaysleepnow"], timeout=5)

                return "System locked."
            elif self.os_name == 'Windows':
                import ctypes
                ctypes.windll.user32.LockWorkStation()
                return "System locked."
            else:
                # Linux (Try common ones)
                subprocess.run(["xdg-screensaver", "lock"], timeout=5)
                return "Lock command sent."
        except Exception as e:
            print(f"Lock error: {e}")
            return f"Error locking system: {e}"
