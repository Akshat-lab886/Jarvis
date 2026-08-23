from google import genai
from config import Config

def list_models():
    client = genai.Client(api_key=Config.GOOGLE_API_KEY)
    print("Listing available models...")
    for m in client.models.list():
        # New SDK uses 'supported_actions' instead of 'supported_generation_methods'
        actions = getattr(m, 'supported_actions', None) or []
        if 'generateContent' in actions or not actions:
            print(m.name)

if __name__ == "__main__":
    list_models()
