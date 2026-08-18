import speech_recognition as sr


def _send_to_ui(event, data):
    """Lazy import to avoid any module-load order issues with utils.server."""
    try:
        from utils.server import send_to_ui
        send_to_ui(event, data)
    except Exception:
        pass

class Ear:
    def __init__(self):
        self.recognizer = sr.Recognizer()
        print("Ear initialized")
        try:
            import pyaudio
            self.mic_available = True
        except ImportError:
            self.mic_available = False
            print("PyAudio not installed. Voice features disabled.")
            _send_to_ui('status', {'message': 'Error: PyAudio not installed (No Mic)'})

    def listen(self):
        if not self.mic_available:
            return None

        try:
            with sr.Microphone() as source:
                print("Listening...")
                _send_to_ui('status', {'message': 'Listening...'})
                self.recognizer.adjust_for_ambient_noise(source)
                audio = self.recognizer.listen(source)

                print("Processing...")
                _send_to_ui('status', {'message': 'Processing...'})
                
                text = self.recognizer.recognize_google(audio)
                print(f"User said: {text}")
                _send_to_ui('user_text', {'text': text})
                return text
        except sr.UnknownValueError:
            print("Could not understand audio")
            _send_to_ui('status', {'message': 'Could not understand audio'})
            return None
        except sr.RequestError as e:
            print(f"Could not request results; {e}")
            _send_to_ui('status', {'message': f"API Error: {e}"})
            return None
        except Exception as e:
            print(f"Error in Ear: {e}")
            _send_to_ui('status', {'message': f"Error: {e}"})
            return None
