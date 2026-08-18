import os
import sys
import subprocess
import time

class Coder:
    """
    The Architect's Hand.
    Allows Jarvis to write and execute Python code dynamically.
    """
    def __init__(self, workspace_dir="workspace"):
        self.workspace_dir = workspace_dir
        if not os.path.exists(self.workspace_dir):
            os.makedirs(self.workspace_dir)
            
    def validate_safety(self, code_string):
        """Basic filter to prevent catastrophic commands."""
        forbidden_terms = [
            "rm -rf",
            "shutil.rmtree",
            "os.system('format",
            "os.system(\"format",
            "mkfs",
            ":(){ :|:& };:" # Fork bomb
        ]
        
        for term in forbidden_terms:
            if term in code_string:
                return False, f"Safety Protocol: Forbidden command '{term}' detected."
        return True, "Safe"

    def execute_python(self, code_string):
        """Writes code to a file and executes it."""
        
        # 1. Safety Check
        is_safe, message = self.validate_safety(code_string)
        if not is_safe:
            return message
            
        # 2. Prepare Workspace
        timestamp = int(time.time())
        filename = f"dynamic_task_{timestamp}.py"
        file_path = os.path.join(self.workspace_dir, filename)
        
        try:
            # 3. Write Code
            with open(file_path, "w") as f:
                f.write(code_string)
                
            # 4. Execute
            # Capture stdout and stderr
            # Timeout set to 10 seconds to prevent infinite loops
            result = subprocess.run(
                [sys.executable, filename],
                cwd=self.workspace_dir, # Run INSIDE workspace
                capture_output=True,
                text=True,
                timeout=10
            )
            
            output = ""
            if result.stdout:
                output += f"Output:\n{result.stdout}\n"
            if result.stderr:
                output += f"Errors:\n{result.stderr}\n"
                
            if not output:
                output = "Code executed successfully (No Output)."
                
            return output.strip()
            
        except subprocess.TimeoutExpired:
            return "Error: Code execution timed out (limit: 10s)."
        except Exception as e:
            return f"Execution Error: {str(e)}"
        finally:
            # Clean up the file? 
            # For now, let's keep it for debugging, or maybe delete it.
            # Let's keep the last few, but maybe strictly we should clean up.
            # For 'The Architect', persistence might be nice, but to avoid clutter let's clean up for now.
            if os.path.exists(file_path):
                os.remove(file_path)

if __name__ == "__main__":
    # Test
    coder = Coder()
    print(coder.execute_python("print('Hello from The Architect')"))
