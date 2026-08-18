import os
import socket
from dotenv import load_dotenv

def run_diagnostics():
    """
    Runs startup checks for Jarvis system.
    Returns: True if system is Go, False if critical error.
    """
    print("\n--- Running System Diagnostics ---")
    load_dotenv()
    
    all_clear = True
    
    # 1. Environment Check
    llm_keys = [k for k in ["OPENROUTER_API_KEY", "GOOGLE_API_KEY"] if os.getenv(k)]
    if not llm_keys:
        print("\033[91mCRITICAL WARNING: No LLM API key found. Set OPENROUTER_API_KEY or GOOGLE_API_KEY in .env or Jarvis cannot think.\033[0m")
    else:
        print(f"[\033[92mOK\033[0m] LLM API keys found ({', '.join(llm_keys)}).")

    # Gmail/Calendar auth uses client_secret.json + token.json
    if not os.path.exists('client_secret.json'):
        print("\033[93mWARNING: client_secret.json not found — email & calendar features will be disabled.\033[0m")

    # 2. Connectivity Check
    try:
        # Check by connecting to Google DNS
        socket.create_connection(("8.8.8.8", 53), timeout=3)
        print("[\033[92mOK\033[0m] Internet Connection active.")
    except OSError:
        print("\033[93mWARNING: No Internet Connection. Offline Mode active.\033[0m")
        all_clear = False # Technically system can run, but it's degraded

    # 3. Directory Structure Check
    # Ensure critical folders exist
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    critical_folders = [
        "workspace",
        "knowledge_input",
        "static",
        "memory_vault",
        "brain/data"
    ]
    
    for folder in critical_folders:
        path = os.path.join(base_dir, folder)
        if not os.path.exists(path):
            try:
                os.makedirs(path)
                print(f"[\033[94mFIX\033[0m] Created missing directory: {folder}")
            except Exception as e:
                print(f"\033[91mERROR: Could not create directory {folder}: {e}\033[0m")
                return False
    
    print("[\033[92mOK\033[0m] File System Integrity verified.")
    print("----------------------------------\n")
    return True
