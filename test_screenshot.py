from utils.tools import Tools
import os

print("Testing screenshot...")
t = Tools()
path = t.take_screenshot()

if path and os.path.exists(path):
    print(f"SUCCESS: Screenshot saved at {path}")
else:
    print("FAILURE: Screenshot not saved.")
