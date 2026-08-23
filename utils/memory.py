"""
Jarvis Memory Module (Legacy Wrapper)

This file is kept for backward compatibility. The actual memory system
lives in utils/episodic_memory.py with the EpisodicMemory class.

This Memory class wraps EpisodicMemory so that existing code that does:
    from utils.memory import Memory
continues to work.
"""

from utils.episodic_memory import Memory as _EpisodicMemory

# Re-export so `from utils.memory import Memory` still works
Memory = _EpisodicMemory
