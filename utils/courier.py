import yagmail
import os
import logging
try:
    import config
except ImportError:
    import utils.secretary as config # Fallback if config is not direct

logger = logging.getLogger("Jarvis.Courier")

class Courier:
    def __init__(self):
        # Prefer environment variables securely loaded
        self.user = os.environ.get("GMAIL_USER")
        self.password = os.environ.get("GMAIL_PASSWORD")
        
        self.yag = None
        
        if self.user and self.password:
            try:
                self.yag = yagmail.SMTP(self.user, self.password)
                logger.info("Courier initialized (Yagmail Client Ready).")
            except Exception as e:
                logger.error(f"Courier Init Failed: {e}")
        else:
            logger.warning("Courier Warning: GMAIL_USER or GMAIL_PASSWORD missing.")

    def send_file(self, to_email, subject, body, attachment_path):
        """
        Sends an email with an attachment.
        """
        if not self.yag:
            return "Courier Error: Email credentials not configured."

        if not os.path.exists(attachment_path):
            return f"Courier Error: Attachment not found at {attachment_path}"

        try:
            self.yag.send(
                to=to_email,
                subject=subject,
                contents=body,
                attachments=attachment_path
            )
            return f"Package sent to {to_email} successfully."
        except Exception as e:
            logger.error(f"Delivery Failed: {e}")
            return f"Delivery Failed: {e}"
