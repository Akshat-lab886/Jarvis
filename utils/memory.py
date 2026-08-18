import json
import os

class Memory:
    def __init__(self, filename='memory.json'):
        self.filename = filename
        self.memory = {}
        self._load_memory()
        print("Memory initialized")

    def _load_memory(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    self.memory = json.load(f)
            except json.JSONDecodeError:
                self.memory = {}
        else:
            self.memory = {}
            self._save_to_file()

    def _save_to_file(self):
        with open(self.filename, 'w') as f:
            json.dump(self.memory, f, indent=4)

    def save_memory(self, key, value):
        self.memory[key] = value
        self._save_to_file()
        print(f"Memory saved: {key} = {value}")
        return True

    def get_memory(self, key):
        return self.memory.get(key, None)

    def get_all_memories(self):
        return self.memory
