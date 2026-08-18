import os.path
import datetime
import dateparser
from dateparser.search import search_dates
import google.auth.transport.requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import base64
from email.mime.text import MIMEText
from googleapiclient.errors import HttpError

# Scopes: Read/Write Calendar, Read Gmail, Send Gmail
SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/gmail.modify'
]

class Secretary:
    # ... (init remains same) ...

    # ... (other methods remain same) ...

    def read_email(self, query):
        if not self.gmail_service:
            return "I cannot access your email (Authentication Missing)."

        try:
            # Search for emails matching query
            results = self.gmail_service.users().messages().list(userId='me', q=query, maxResults=1).execute()
            messages = results.get('messages', [])

            if not messages:
                return f"I couldn't find any emails for: {query}"

            # Get the first matching email
            msg = self.gmail_service.users().messages().get(userId='me', id=messages[0]['id']).execute()
            
            # Extract basic info
            snippet = msg.get('snippet', '')
            subject = next((h['value'] for h in msg['payload']['headers'] if h['name'] == 'Subject'), 'No Subject')
            
            return f"Subject: {subject}\nSnippet: {snippet}"

        except Exception as e:
            return f"Failed to read email: {e}"

    def reply_to_email(self, query, body):
        if not self.gmail_service:
            return "I cannot reply to emails (Authentication Missing)."
            
        try:
            # Step 1: Search for the email
            results = self.gmail_service.users().messages().list(userId='me', q=query, maxResults=1).execute()
            messages = results.get('messages', [])
            
            if not messages:
                return f"I couldn't find an email about '{query}' to reply to."
            
            # Step 2: Get threadId and messageId
            original_msg_id = messages[0]['id']
            original_thread_id = messages[0]['threadId']
            
            # Step 3: Create reply
            message = MIMEText(body)
            message['In-Reply-To'] = original_msg_id
            message['References'] = original_msg_id
            message['Subject'] = f"Re: {query}" # Ideally get original subject, but this works for now or let Gmail handle it
            # Actually, to reply properly we should probably fetch the original subject.
            
            # Fetch original to get subject?
            orig = self.gmail_service.users().messages().get(userId='me', id=original_msg_id).execute()
            headers = orig['payload']['headers']
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '')
            if not subject.lower().startswith('re:'):
                subject = f"Re: {subject}"
            message['Subject'] = subject
            message['To'] = next((h['value'] for h in headers if h['name'] == 'From'), '')
            
            # Encode
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
            body_dict = {'raw': raw, 'threadId': original_thread_id}
            
            # Step 4: Send
            self.gmail_service.users().messages().send(userId='me', body=body_dict).execute()
            return f"Replied to email about '{query}'."
            
        except Exception as e:
            return f"Failed to reply: {e}"
    def send_email(self, to_email, subject, body):
        if not self.gmail_service:
            return "I cannot send emails (Authentication Missing)."
            
        try:
            message = MIMEText(body)
            message['to'] = to_email
            message['subject'] = subject
            
            # Encode the message (base64url)
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
            body_dict = {'raw': raw}
            
            self.gmail_service.users().messages().send(userId='me', body=body_dict).execute()
            return f"Email sent successfully to {to_email}."
            
        except Exception as e:
            return f"Failed to send email: {e}"
    def __init__(self):
        print("Initializing Secretary Module (Google API)...")
        self.creds = None
        self.calendar_service = None
        self.gmail_service = None
        
        # Token Management
        if os.path.exists('token.json'):
            try:
                self.creds = Credentials.from_authorized_user_file('token.json', SCOPES)
            except Exception as e:
                print(f"Token error: {e}")

        # Refresh or Login
        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                try:
                    self.creds.refresh(google.auth.transport.requests.Request())
                except Exception:
                     self.creds = None
            
            if not self.creds:
                if os.path.exists('client_secret.json'):
                    print("Launching Browser for Google Login...")
                    try:
                        flow = InstalledAppFlow.from_client_secrets_file(
                            'client_secret.json', SCOPES)
                        self.creds = flow.run_local_server(port=0)
                        # Save the credentials for the next run
                        with open('token.json', 'w') as token:
                            token.write(self.creds.to_json())
                    except Exception as e:
                        print(f"Auth Flow failed: {e}")
                else:
                    print("Error: client_secret.json not found. Secretary is disabled.")
                    return

        # Build Services
        if self.creds:
            try:
                self.calendar_service = build('calendar', 'v3', credentials=self.creds)
                self.gmail_service = build('gmail', 'v1', credentials=self.creds)
                print("Secretary Module Online.")
            except Exception as e:
                 print(f"Failed to build services: {e}")

    def get_upcoming_events(self, n=5):
        if not self.calendar_service:
            return "I cannot access your calendar (Authentication Missing)."
        
        try:
            # Use UTC explicit for API compatibility
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            events_result = self.calendar_service.events().list(
                calendarId='primary', timeMin=now,
                maxResults=n, singleEvents=True,
                orderBy='startTime').execute()
            events = events_result.get('items', [])

            if not events:
                return "You have no upcoming events."

            event_list = []
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                summary = event.get('summary', 'No Title')
                
                # Format time nicely
                dt = dateparser.parse(start)
                if dt:
                    time_str = dt.strftime("%A, %I:%M %p") # e.g. Monday, 04:00 PM
                    event_list.append(f"{summary} on {time_str}")
                else:
                    event_list.append(f"{summary} at {start}")
            
            return "Upcoming events: " + ". ".join(event_list) + "."

        except HttpError as error:
            return f"An error occurred accessing calendar: {error}"

    def add_event(self, event_text):
        if not self.calendar_service:
            return "I cannot edit your calendar (Authentication Missing)."

        try:
            # Parse natural language time using search_dates (extracts date from long text)
            # search_dates returns list of (substring, datetime_obj)
            dates = search_dates(event_text, settings={'PREFER_DATES_FROM': 'future'})
            
            if not dates:
                return f"Sorry, I couldn't understand the time in '{event_text}'."
            
            # Use the last found date (usually the most specific/final one in a sentence)
            date_info = dates[-1] 
            dt = date_info[1]
            
            # Default duration 1 hour
            end_dt = dt + datetime.timedelta(hours=1)
            
            event = {
                'summary': event_text, # Use the full text as summary
                'start': {
                    'dateTime': dt.isoformat(),
                    'timeZone': 'UTC',
                },
                'end': {
                    'dateTime': end_dt.isoformat(),
                    'timeZone': 'UTC',
                },
            }

            event = self.calendar_service.events().insert(calendarId='primary', body=event).execute()
            # Return nice confirmation
            return f"Event added: {event_text} at {dt.strftime('%I:%M %p, %B %d')}."

        except Exception as e:
            return f"Failed to add event: {e}"

    def get_unread_emails(self, n=3):
        if not self.gmail_service:
            return "I cannot access your email (Authentication Missing)."

        try:
            results = self.gmail_service.users().messages().list(userId='me', q='is:unread', maxResults=n).execute()
            messages = results.get('messages', [])

            if not messages:
                return "You have no new unread emails."

            email_summaries = []
            for msg in messages:
                msg_detail = self.gmail_service.users().messages().get(userId='me', id=msg['id']).execute()
                headers = msg_detail['payload']['headers']
                
                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
                sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown')
                
                if '<' in sender:
                    sender = sender.split('<')[0].strip().strip('"')
                
                email_summaries.append(f"From {sender}: {subject}")

            return f"You have {len(messages)} unread emails. Top {len(email_summaries)}: " + ". ".join(email_summaries) + "."

        except Exception as error:
            return f"An error occurred accessing gmail: {error}"
