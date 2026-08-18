import subprocess
import os
import time
import signal
from utils.logger import logger

class Automation:
    def __init__(self):
        self.chrome_process = None
        self.port = 9222
        # Define paths
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.profile_dir = os.path.join(self.base_dir, 'utils', 'chrome_profile')
        
        # macOS Chrome Path
        self.chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        
        if not os.path.exists(self.profile_dir):
            os.makedirs(self.profile_dir)

    def launch_chrome(self):
        """Launches a dedicated Chrome instance for automation."""
        if self.is_chrome_running():
            return "Automation Browser is already running."

        try:
            cmd = [
                self.chrome_path,
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                # "--headless=new" # Keep visible for now
                "https://www.google.com"
            ]
            
            # Launch in background
            self.chrome_process = subprocess.Popen(
                cmd, 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
            )
            
            time.sleep(2) # Wait for launch
            return "Automation Browser launched. Listening on port 9222."
            
        except Exception as e:
            logger.error(f"Failed to launch Chrome: {e}")
            return f"Error launching browser: {e}"

    def close_chrome(self):
        """Closes the automation browser."""
        # Method 1: Kill via process object
        if self.chrome_process:
            self.chrome_process.terminate()
            self.chrome_process = None
            return "Browser closed."
            
        # Method 2: Kill via pkill (cleanup)
        try:
            subprocess.run(f"pkill -f 'remote-debugging-port={self.port}'", shell=True)
            return "Browser processes terminated."
        except Exception as e:
            return f"Error closing browser: {e}"

    def is_chrome_running(self):
        """Check if the debugging port is open (simple check)."""
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(('127.0.0.1', self.port))
        sock.close()
        return result == 0
