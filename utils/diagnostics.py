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

    # 1. Environment Check (BYOK-aware: any provider key lights the fleet;
    # keystore overlay holds dashboard-added keys, env holds the rest).
    llm_keys = [k for k in ["GROQ_API_KEY", "GOOGLE_API_KEY",
                            "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                            "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY",
                            "CUSTOM_OPENAI_API_KEY"] if os.getenv(k)]
    if not llm_keys:
        try:
            from utils.llm.keystore import get_keystore
            ks = get_keystore()
            for _name, _label in (
                    ('groq', 'GROQ_API_KEY'), ('google', 'GOOGLE_API_KEY'),
                    ('openai', 'OPENAI_API_KEY'),
                    ('anthropic', 'ANTHROPIC_API_KEY'),
                    ('deepseek', 'DEEPSEEK_API_KEY'),
                    ('openrouter', 'OPENROUTER_API_KEY'),
                    ('custom', 'CUSTOM_OPENAI_API_KEY')):
                try:
                    if ks.get(_name):
                        llm_keys.append(f"{_label}(keystore)")
                except Exception:
                    pass
        except Exception:
            pass
    # Offline-first: a reachable local server (Ollama/LM Studio, no key
    # needed) also lights the fleet.  Without this a local-only user gets
    # a CRITICAL warning even though the router serves them fine and the
    # circuit breaker reports ok — and that scare text pushes users to
    # add a cloud key they don't need, defeating offline-first.  Reuses
    # the provider's own probe (short timeout, cached), only when no key
    # was found, so the boot path never hangs on it.  Honors
    # JARVIS_DISABLE_LOCAL=1 and off/none/disabled URLs, mirroring the
    # factory + breaker so all three can never disagree about "local".
    if not llm_keys:
        try:
            _local_off = (os.getenv('JARVIS_DISABLE_LOCAL', '') == '1')
            _ollama_url = os.getenv(
                'OLLAMA_BASE_URL',
                'http://localhost:11434/v1').strip()
            _lmstudio_url = os.getenv(
                'LMSTUDIO_BASE_URL',
                'http://localhost:1234/v1').strip()
            if not _local_off:
                from utils.llm.providers.openai_compat import (
                    OpenAICompatProvider as _Compat)
                if _ollama_url.lower() not in (
                        '', 'off', 'none', 'disabled'):
                    try:
                        if _Compat("ollama", _ollama_url,
                                   key_optional=True,
                                   dynamic_models=True).available():
                            llm_keys.append("Ollama(local)")
                    except Exception:
                        pass
                if _lmstudio_url.lower() not in (
                        '', 'off', 'none', 'disabled'):
                    try:
                        if _Compat("lmstudio", _lmstudio_url,
                                   key_optional=True,
                                   dynamic_models=True).available():
                            llm_keys.append("LMStudio(local)")
                    except Exception:
                        pass
        except Exception:
            pass
    if not llm_keys:
        print("\033[91mCRITICAL WARNING: No LLM API key found. Set any one of GROQ_API_KEY, GOOGLE_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, DEEPSEEK_API_KEY, OPENROUTER_API_KEY, CUSTOM_OPENAI_* or run a local Ollama/LM Studio server — otherwise Jarvis cannot think.\033[0m")
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
        # No connectivity is a degrade, not a failure: the local brain and
        # tooling keep working, and the check is reported above for the user.
        print("\033[93mWARNING: No Internet Connection. Offline Mode active.\033[0m")

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
