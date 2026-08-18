import os
import subprocess
from dotenv import load_dotenv

load_dotenv()

class GitManager:
    def __init__(self):
        self.token = os.getenv("GITHUB_TOKEN")
        self.username = os.getenv("GITHUB_USERNAME")

    def init_local_git(self, project_path):
        """
        Initializes a local git repository, adds all files, and makes the first commit.
        """
        try:
            if not os.path.exists(project_path):
                return f"Error: Project path {project_path} does not exist."
            
            # Check if .git already exists
            git_dir = os.path.join(project_path, ".git")
            if os.path.exists(git_dir):
                return "Git is already initialized for this project."

            print(f"--- Initializing Git for {project_path} ---")
            
            # git init
            subprocess.run(["git", "init"], cwd=project_path, check=True)
            
            # git add .
            subprocess.run(["git", "add", "."], cwd=project_path, check=True)
            
            # git commit -m "Initial commit by Jarvis"
            subprocess.run(["git", "commit", "-m", "Initial commit by Jarvis"], cwd=project_path, check=True)
            
            return "Local Git repository initialized and first commit created."

        except subprocess.CalledProcessError as e:
            return f"Git Error (Command failed): {e}"

    def create_remote_repo(self, repo_name):
        """
        Creates a new public repository on GitHub.
        Returns the clone URL (https).
        """
        try:
            from github import Github
            if not self.token: return "Error: GITHUB_TOKEN not found in .env"
            
            g = Github(self.token)
            user = g.get_user()
            
            # Check if repo exists? PyGithub might error.
            print(f"--- Creating Remote Repo: {repo_name} ---")
            repo = user.create_repo(repo_name)
            
            return repo.clone_url
        except Exception as e:
            # Handle "name already exists" gracefully if possible, or just return error
            return f"GitHub API Error: {e}"

    def push_to_remote(self, project_path, repo_url):
        """
        Pushes local commits to the remote repository.
        Injects authentication into the URL.
        """
        try:
            if not self.token or not self.username:
                 return "Error: GitHub credentials not found in .env"
            
            # Auth URL: https://User:Token@github.com/User/Repo.git
            # repo_url is typically https://github.com/User/Repo.git
            auth_url = repo_url.replace("https://", f"https://{self.username}:{self.token}@")
            
            print(f"--- Pushing to Remote ---")
            
            # git remote add origin [url]
            # Handle if origin already exists
            try:
                subprocess.run(["git", "remote", "add", "origin", auth_url], cwd=project_path, check=True, capture_output=True)
            except subprocess.CalledProcessError:
                # Maybe origin exists, try set-url
                subprocess.run(["git", "remote", "set-url", "origin", auth_url], cwd=project_path, check=True)
            
            # git branch -M main
            subprocess.run(["git", "branch", "-M", "main"], cwd=project_path, check=True)
            
            # git push -u origin main
            subprocess.run(["git", "push", "-u", "origin", "main"], cwd=project_path, check=True)
            
            return "Code pushed to GitHub successfully."
            
        except subprocess.CalledProcessError as e:
            return f"Push Error: {e}"
        except Exception as e:
            return f"Git Push Error: {e}"
