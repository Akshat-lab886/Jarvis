import os
import sys

def setup():
    print("Installing Playwright Browsers...")
    # Use absolute path to venv binary
    result = os.system("./venv/bin/playwright install")
    if result == 0:
        print("Browsers installed successfully.")
    else:
        print("Error installing browsers.")

if __name__ == "__main__":
    setup()
