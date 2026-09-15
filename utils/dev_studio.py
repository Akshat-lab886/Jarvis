import os
import subprocess

class ProjectManager:
    def __init__(self):
        self.workspace_dir = "Jarvis_Projects"
        # Ensure projects directory exists
        if not os.path.exists(self.workspace_dir):
            os.makedirs(self.workspace_dir)

    def _resolve(self, *parts):
        """Resolve *parts* under Jarvis_Projects; None if a '..' or
        absolute part would escape it.  project_name / file_path /
        command come from the LLM, so no path built from them may ever
        address the host filesystem."""
        ws = os.path.abspath(self.workspace_dir)
        cand = os.path.abspath(os.path.join(ws, *parts))
        if cand == ws or cand.startswith(ws + os.sep):
            return cand
        return None

    def create_project(self, project_name, project_type):
        """
        Scaffolds a new project.
        project_type: 'python' or 'web'
        """
        try:
            project_name = project_name.replace(" ", "_")
            project_path = self._resolve(project_name)
            if project_path is None:
                return f"Error: Invalid project name '{project_name}'."

            if os.path.exists(project_path):
                return f"Error: Project '{project_name}' already exists."

            os.makedirs(project_path)
            
            if 'python' in project_type.lower():
                self._scaffold_python(project_path, project_name)
                return f"Created Python project '{project_name}' successfully."
            
            elif 'web' in project_type.lower():
                self._scaffold_web(project_path, project_name)
                return f"Created Web project '{project_name}' successfully."
            
            else:
                return f"Unknown project type: {project_type}. Use 'python' or 'web'."

        except Exception as e:
            return f"Failed to create project: {e}"

    def write_to_file(self, project_name, file_path, code_content):
        """
        Writes code code_content to projects/project_name/file_path.
        """
        try:
            project_path = self._resolve(project_name)
            if project_path is None:
                return f"Error: Invalid project '{project_name}'."
            if not os.path.exists(project_path):
                return f"Error: Project '{project_name}' does not exist."

            full_path = self._resolve(project_name, file_path)
            if full_path is None:
                return (f"Error: Refusing to write outside Jarvis_Projects "
                        f"({file_path!r}).")

            # Ensure subdirectories exist if file_path is complex (e.g. utils/math.py)
            dir_name = os.path.dirname(full_path)
            if dir_name and not os.path.exists(dir_name):
                os.makedirs(dir_name)
                
            with open(full_path, "w") as f:
                f.write(code_content)
                
            return f"Successfully wrote code to {file_path} in {project_name}."
        except Exception as e:
            return f"Failed to write file: {e}"

    def build_full_project(self, project_data):
        """
        Builds a full project from a JSON definition containing multiple files.
        """
        try:
            name = project_data.get('project_name')
            if not name:
                return "Error: No project name provided."
                
            name = name.replace(" ", "_")
            
            # Create base scaffold (Web assumed for now as per prompt)
            res = self.create_project(name, 'web')
            
            files = project_data.get('files', [])
            succ_count = 0
            
            for file_def in files:
                fname = file_def.get('filename')
                content = file_def.get('content')
                if fname and content:
                    self.write_to_file(name, fname, content)
                    succ_count += 1
            
            return f"Software Factory: Built {name} with {succ_count} generated files."
            
        except Exception as e:
            return f"Build failed: {e}"

    def open_in_vscode(self, project_name):
        """Opens the project folder in VS Code."""
        try:
            project_path = self._resolve(project_name)
            if project_path is None:
                return f"Error: Invalid project '{project_name}'."
            if not os.path.exists(project_path):
                return f"Error: Project '{project_name}' does not exist."

            # Determine VS Code Executable
            import shutil
            code_cmd = "code"
            if not shutil.which(code_cmd):
                 # Fallback for macOS
                 fallback = "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
                 if os.path.exists(fallback):
                     code_cmd = fallback # No quotes needed for list format
                 else:
                     return "VS Code command 'code' not found in PATH. Please install 'code' command in VS Code (Cmd+Shift+P > Install 'code')."

            # Using list format avoids shell=True and handles spaces/quoting automatically
            subprocess.run([code_cmd, project_path])
            return f"Opening Visual Studio Code for {project_name}."
        except Exception as e:
            return f"Failed to launch VS Code: {e}"

    def run_project_command(self, project_name, command):
        """
        Runs a shell command inside the project directory.
        Includes basic safety checks.
        """
        try:
            project_path = self._resolve(project_name)
            if project_path is None:
                return f"Error: Invalid project '{project_name}'."
            if not os.path.exists(project_path):
                return f"Error: Project '{project_name}' does not exist."

            # Safety Blocklist
            dangerous_terms = ["rm -rf", "sudo", "mv /", "mkfs", ":(){:|:&};:", "> /dev/sda"]
            if any(term in command for term in dangerous_terms):
                return f"Command blocked by Safety Protocol: {command}"

            # Execute
            # cwd ensures we run INSIDE the project folder
            process = subprocess.run(
                command, 
                cwd=project_path, 
                shell=True, 
                capture_output=True, 
                text=True
            )
            
            output = process.stdout
            error = process.stderr
            
            if process.returncode == 0:
                return f"Command executed successfully.\nOutput:\n{output}"
            else:
                return f"Command failed (Exit Code {process.returncode}).\nError:\n{error}\nOutput:\n{output}"

        except Exception as e:
            return f"Failed to execute command: {e}"

    def _scaffold_python(self, path, name):
        # main.py
        with open(os.path.join(path, "main.py"), "w") as f:
            f.write(f"# {name} - Generated by Jarvis\n\n")
            f.write("def main():\n")
            f.write("    print('Hello from Jarvis-generated Code!')\n\n")
            f.write("if __name__ == '__main__':\n")
            f.write("    main()\n")
        
        # requirements.txt
        with open(os.path.join(path, "requirements.txt"), "w") as f:
            f.write("# Add dependencies here\n")
            
        # README.md
        with open(os.path.join(path, "README.md"), "w") as f:
            f.write(f"# {name}\n")
            f.write("A Python application Generated by Jarvis.\n")

    def _scaffold_web(self, path, name):
        # Directories
        os.makedirs(os.path.join(path, "css"))
        os.makedirs(os.path.join(path, "js"))
        os.makedirs(os.path.join(path, "images"))
        
        # index.html
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{name}</title>
    <link rel="stylesheet" href="css/style.css">
</head>
<body>
    <h1>Welcome to {name}</h1>
    <p>Generated by Jarvis.</p>
    <script src="js/app.js"></script>
</body>
</html>"""
        with open(os.path.join(path, "index.html"), "w") as f:
            f.write(html)
            
        # style.css
        css = """body {
    font-family: sans-serif;
    background-color: #f0f0f0;
    text-align: center;
    padding: 50px;
}
h1 { color: #333; }"""
        with open(os.path.join(path, "css/style.css"), "w") as f:
            f.write(css)
            
        # app.js
        with open(os.path.join(path, "js/app.js"), "w") as f:
            f.write(f"console.log('Hello from {name}');")

    def zip_project(self, project_name):
        """
        Compresses the project folder into a .zip file.
        Returns the absolute path to the zip file.
        """
        try:
            import shutil
            project_path = self._resolve(project_name)
            if project_path is None or not os.path.isdir(project_path):
                return None
            # Archive lands in the workspace root, derived from a resolved
            # path only (never a raw LLM-supplied name that could escape).
            base_name = os.path.join(
                os.path.abspath(self.workspace_dir), project_name)
            shutil.make_archive(base_name, 'zip', project_path)
            return base_name + ".zip"
        except Exception as e:
            print(f"Zip Error: {e}")
            return None
