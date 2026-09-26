import os
import time
import logging
from utils.brain import Brain
from utils.tools import Tools

logger = logging.getLogger("Jarvis.Security")

class Security:
    def __init__(self):
        self._brain = None
        self._tools = None
        self.admin_photo = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'admin.jpg')

    @property
    def brain(self):
        # Lazy so module startup doesn't spin up extra LLM clients
        if self._brain is None:
            self._brain = Brain()
        return self._brain

    @property
    def tools(self):
        if self._tools is None:
            self._tools = Tools()
        return self._tools

    def verify_admin(self):
        """
        Captures a live photo and compares it with the stored admin photo using Gemini Vision.
        Returns: True (Match) or False (No Match/Error)
        """
        logger.info("Initiating identity verification...")
        
        # 1. Check if Admin Photo exists
        if not os.path.exists(self.admin_photo):
            return "Setup Error: No admin photo found. Please save a photo as static/admin.jpg"

        # 2. Capture Intruder Photo
        intruder_photo = self.tools.capture_photo()
        if not intruder_photo or "Error" in intruder_photo:
            return "Camera Error: Could not see you."

        # 3. Ask Brain to Compare
        prompt = """
        I am providing two images. 
        The first image is the ADMIN (Reference).
        The second image is the CURRENT USER (Live Camera).
        
        Deeply analyze facial features, bone structure, and eyes.
        Are they the same person?
        
        Answer ONLY 'MATCH' if they are the same person.
        Answer ONLY 'NO_MATCH' if they are different.
        Do not add any other text.
        """
        
        # Send [Admin, Intruder]
        try:
            result = self.brain.think(prompt, image_path=[self.admin_photo, intruder_photo])
            
            # Extract Text Response
            response_text = result.get('response', '')
            if isinstance(result, str): response_text = result
            
            logger.info("FaceID result: %s", response_text)
            
            if "MATCH" in response_text and "NO_MATCH" not in response_text:
                return True
            else:
                return False
                
        except Exception as e:
            logger.warning("FaceID verification failed: %s", e)
            return False
