import time
import os
import threading
import queue
from playwright.sync_api import sync_playwright
from utils.logger import web_log

class WebAgent:
    def __init__(self):
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.screenshot_path = os.path.join(self.base_dir, 'static', 'agent_view.jpg')
        
        # Thread-safe communication
        self.command_queue = queue.Queue()
        self.result_queue = queue.Queue()
        self.running = True
        
        # Start the dedicated browser thread
        self.thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.thread.start()

    def _worker_loop(self):
        """Runs in a separate thread to keep Playwright happy."""
        try:
            with sync_playwright() as p:
                # Use a real User-Agent to avoid immediate bot detection
                browser = p.chromium.launch(headless=False)
                context = browser.new_context(
                    user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                )
                page = context.new_page()
                
                while self.running:
                    try:
                        # Wait for command
                        cmd = self.command_queue.get(timeout=1)
                        action = cmd.get('action')
                        args = cmd.get('args', {})
                        
                        web_log(f"Agent: Processing action - {action}")
                        
                        result = None
                        try:
                            if action == 'goto':
                                url = args.get('url')
                                if not url.startswith('http'): url = 'https://' + url
                                web_log(f"Agent: Navigating to {url}")
                                page.goto(url)
                                page.wait_for_load_state('networkidle', timeout=10000)
                                result = f"Navigated to {url}"
                                
                            elif action == 'read_title':
                                result = page.title()
                                
                            elif action == 'screenshot':
                                page.screenshot(path=self.screenshot_path)
                                result = self.screenshot_path
                                
                            elif action == 'type':
                                selector = args.get('selector')
                                text = args.get('text')
                                page.fill(selector, text)
                                result = "Typed text."
                                
                            elif action == 'press':
                                key = args.get('key')
                                page.keyboard.press(key)
                                result = f"Pressed {key}"
                                
                            elif action == 'click':
                                selector = args.get('selector')
                                page.click(selector)
                                result = "Clicked element."
                                
                            elif action == 'amazon_search':
                                item = args.get('item')
                                web_log(f"Agent: Searching Amazon for '{item}'")
                                try:
                                    # 1. Go to Amazon
                                    page.goto("https://www.amazon.in", timeout=30000)
                                    page.wait_for_load_state('domcontentloaded', timeout=30000)
                                    
                                    # 2. Search
                                    page.fill("#twotabsearchtextbox", item)
                                    page.keyboard.press("Enter")
                                    # Wait for results container
                                    page.wait_for_selector("div.s-main-slot", timeout=15000)
                                    
                                    # 3. Scrape
                                    # Wait for hydration
                                    page.wait_for_timeout(3000)
                                    
                                    # Find the first actual product card (ignore sponsored/headers)
                                    cards = page.locator("div[data-component-type='s-search-result']")
                                    first_product = cards.first
                                    
                                    # Raw Text Debug extraction
                                    raw_text = "Empty Card"
                                    if first_product.is_visible():
                                        raw_text = first_product.inner_text()
                                    
                                    # Scrape Title inside the card
                                    title = None
                                    if first_product.is_visible():
                                        title_buffer = first_product.locator("h2 a span, .a-text-normal").first
                                        if title_buffer.is_visible():
                                            title = title_buffer.inner_text()
                                    
                                    # Scrape Price inside the card
                                    price = None
                                    if first_product.is_visible():
                                         price_buffer = first_product.locator(".a-price-whole").first
                                         if price_buffer.is_visible():
                                             price = price_buffer.inner_text()
                                    
                                    if title and price:
                                        result = f"Found {title} for {price}"
                                        web_log(f"Agent: Found product - {title} ({price})")
                                    else:
                                        # Fallback to speaking the raw text (Debug Mode)
                                        # Use regex to find price in raw text
                                        import re
                                        price_match = re.search(r'₹[\d,]+', raw_text)
                                        found_price = price_match.group(0) if price_match else "no price"
                                        
                                        # Limit text length for speech
                                        result = f"I am having trouble parsing, but I see: {raw_text[:100].replace(chr(10), ' ')}. Price seems to be {found_price}."
                                        web_log("Agent: parsing failed, using raw text fallback")
                                    
                                except Exception as inner_e:
                                    # Capture debug screenshot on failure
                                    debug_shot = os.path.join(self.base_dir, 'static', 'amazon_debug.jpg')
                                    page.screenshot(path=debug_shot)
                                    result = f"I cannot read the price. (Debug: {str(inner_e)})"
                                    web_log(f"Agent: Amazon search error: {inner_e}")
                                
                            elif action == 'agent_google':
                                query = args.get('query')
                                web_log(f"Agent: Googling '{query}'")
                                # 1. Go to Google
                                page.goto("https://www.google.com")
                                page.wait_for_load_state('domcontentloaded')
                                
                                # 2. Search
                                # Try generic inputs
                                search_box = page.locator("textarea[name='q'], input[name='q']").first
                                search_box.fill(query)
                                page.keyboard.press("Enter")
                                
                                # 3. Wait for results
                                page.wait_for_selector("#search", timeout=10000)
                                
                                # 4. Scrape Top Results
                                results = []
                                # Targeting standard search result headers
                                items = page.locator("#search .g h3").all()
                                
                                count = 0
                                for item in items:
                                    if item.is_visible() and count < 3:
                                        title = item.inner_text()
                                        # Attempt to find snippet (ignoring for now to keep it fast/simple)
                                        results.append(title)
                                        count += 1
                                        
                                if results:
                                    result = "Here are the top results: " + ", ".join(results)
                                    web_log(f"Agent: Found {len(results)} results")
                                else:
                                    result = "I searched Google but couldn't extract the headers."
                                    web_log("Agent: No results found")

                            elif action == 'close':
                                self.running = False
                                result = "Agent stopping"
                                
                            # Send success result
                            self.result_queue.put({'status': 'success', 'data': result})
                            
                        except Exception as e:
                            web_log(f"Agent Action Error: {e}")
                            # Send error result
                            self.result_queue.put({'status': 'error', 'data': str(e)})
                            
                        self.command_queue.task_done()
                        
                        if action == 'close':
                            break
                            
                    except queue.Empty:
                        continue
                        
                browser.close()
                
        except Exception as e:
            web_log(f"Agent Worker Crashed: {e}")
            print(f"Agent Worker Crashed: {e}")

    def _send_command(self, action, args=None):
        """Helper to send command and wait for result."""
        self.command_queue.put({'action': action, 'args': args or {}})
        try:
            # Wait for result
            result = self.result_queue.get(timeout=60) # Increased to 60s to accommodate slow Amazon loads
            if result['status'] == 'success':
                return result['data']
            else:
                return f"Error: {result['data']}"
        except queue.Empty:
            return "Error: Agent timed out."

    def start_browser(self):
        if self.thread.is_alive():
            return "Agent Browser is ready."
        return "Agent Browser failed to start."

    def go_to(self, url):
        return self._send_command('goto', {'url': url})

    def read_page_title(self):
        return self._send_command('read_title')

    def screenshot_page(self):
        return self._send_command('screenshot')
        
    def type_text(self, selector, text):
        return self._send_command('type', {'selector': selector, 'text': text})
        
    def press_key(self, key):
        return self._send_command('press', {'key': key})

    def click_element(self, selector):
        return self._send_command('click', {'selector': selector})
        
    def search_amazon(self, item):
        return self._send_command('amazon_search', {'item': item})

    def close_browser(self):
        return self._send_command('close')
        
    def google_search(self, query):
        """High-level helper to search Google."""
        return self._send_command('agent_google', {'query': query})
