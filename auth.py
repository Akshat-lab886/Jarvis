from google_auth_oauthlib.flow import InstalledAppFlow
import os

# Scopes: Read/Write Calendar, Read Gmail
SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/gmail.readonly'
]

def authenticate():
    print("Starting Authentication...")
    if not os.path.exists('client_secret.json'):
        print("Error: client_secret.json not found!")
        return

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            'client_secret.json', SCOPES)
        print("Launching browser... Please log in.")
        creds = flow.run_local_server(port=0)
        
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
        print("Success! token.json created.")
        
    except Exception as e:
        print(f"Authentication failed: {e}")

if __name__ == '__main__':
    authenticate()
