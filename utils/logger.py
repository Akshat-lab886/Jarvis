import logging
import sys
from logging.handlers import RotatingFileHandler

def setup_logger():
    # Create a custom logger
    logger = logging.getLogger("Jarvis")
    logger.setLevel(logging.DEBUG)

    # Create handlers
    c_handler = logging.StreamHandler(sys.stdout)
    f_handler = RotatingFileHandler('jarvis.log', maxBytes=5*1024*1024, backupCount=3)
    
    c_handler.setLevel(logging.DEBUG)
    f_handler.setLevel(logging.DEBUG)

    # Create formatters and add it to handlers
    c_format = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    f_format = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    c_handler.setFormatter(c_format)
    f_handler.setFormatter(f_format)

    # Add handlers to the logger
    logger.addHandler(c_handler)
    logger.addHandler(f_handler)

    return logger

# Create a singleton instance
logger = setup_logger()

socket_instance = None

def register_socketio(socketio):
    global socket_instance
    socket_instance = socketio

def web_log(message):
    logger.info(message)
    if socket_instance:
        try:
            socket_instance.emit('new_log', {'data': message})
        except Exception as e:
            print(f"Socket emit failed: {e}")
