"""
Jarvis Always-On Audio Processing

Inspired by Otter.ai — runs quietly in the background during offline meetings
or phone calls to auto-extract tasks and follow-ups.

Capabilities:
- Live speech-to-text transcription using speech_recognition
- Auto-detect meeting boundaries (start/end of conversations)
- Extract action items, follow-ups, and key decisions
- Store meeting summaries in episodic memory
- Optionally push tasks to the todo list

Trigger: "start meeting mode" / "stop meeting mode" / voice button hold
"""

import os
import io
import json
import time
import datetime
import threading
import logging
import queue

logger = logging.getLogger("Jarvis.MeetingAudio")


class MeetingTranscriber:
    """
    Background audio processor that:
    1. Continuously captures audio from the microphone
    2. Runs speech-to-text on chunks
    3. Accumulates a transcript
    4. On stop, summarizes and extracts action items
    """

    def __init__(self, brain=None, episodic_memory=None, tasks=None, mouth=None):
        self.brain = brain
        self.memory = episodic_memory
        self.tasks = tasks
        self.mouth = mouth

        self._recording = False
        self._thread = None
        self._transcript_lines = []
        self._lock = threading.Lock()
        self._meeting_start = None
        self._audio_queue = queue.Queue()

        # State
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.meetings_dir = os.path.join(self.base_dir, 'workspace', 'meetings')
        os.makedirs(self.meetings_dir, exist_ok=True)

        logger.info("MeetingTranscriber initialized")

    @property
    def is_recording(self):
        return self._recording

    def start_recording(self):
        """Start background meeting transcription."""
        if self._recording:
            return "Already recording, Sir."

        self._recording = True
        self._transcript_lines = []
        self._meeting_start = datetime.datetime.now()

        self._thread = threading.Thread(
            target=self._listen_loop, daemon=True, name="MeetingAudio"
        )
        self._thread.start()

        msg = f"Meeting recording started at {self._meeting_start.strftime('%I:%M %p')}."
        logger.info(msg)
        return msg

    def stop_recording(self):
        """Stop recording and process the transcript."""
        if not self._recording:
            return "Not currently recording, Sir."

        self._recording = False

        # Wait for thread to finish
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

        # Process the transcript
        result = self._process_transcript()

        # Save the meeting
        self._save_meeting()

        logger.info("Meeting recording stopped and processed")
        return result

    def get_live_transcript(self):
        """Get the current transcript (for dashboard display)."""
        with self._lock:
            return "\n".join(self._transcript_lines[-50:])

    def _listen_loop(self):
        """Background loop that captures and transcribes audio."""
        try:
            import speech_recognition as sr
            recognizer = sr.Recognizer()

            # Use energy threshold for sensitivity
            recognizer.energy_threshold = 300
            recognizer.dynamic_energy_threshold = True
            recognizer.pause_threshold = 1.5  # Longer pause = sentence break

        except ImportError:
            logger.error("speech_recognition not available")
            self._recording = False
            return

        try:
            with sr.Microphone() as source:
                recognizer.adjust_for_ambient_noise(source, duration=2)
                logger.info("MeetingAudio: Listening...")

                while self._recording:
                    try:
                        # Use a short timeout so we can check _recording periodically
                        audio = recognizer.listen(source, timeout=3, phrase_time_limit=30)

                        # Check again after listen returns (in case stop was called during listen)
                        if not self._recording:
                            break

                        # Transcribe
                        try:
                            text = recognizer.recognize_google(audio)
                            if text and text.strip():
                                with self._lock:
                                    timestamp = datetime.datetime.now().strftime('%H:%M:%S')
                                    self._transcript_lines.append(f"[{timestamp}] {text}")
                                logger.debug(f"Meeting transcript: {text[:80]}")
                        except sr.UnknownValueError:
                            pass  # Silence or unrecognizable
                        except sr.RequestError as e:
                            logger.warning(f"STT API error: {e}")
                            time.sleep(2)

                    except sr.WaitTimeoutError:
                        continue  # No speech detected, keep listening
                    except Exception as e:
                        logger.error(f"Listen error: {e}")
                        time.sleep(1)

        except Exception as e:
            logger.error(f"MeetingAudio: Microphone error: {e}")
            self._recording = False

    def _process_transcript(self):
        """Use the Brain to summarize the transcript and extract action items."""
        with self._lock:
            full_transcript = "\n".join(self._transcript_lines)

        if not full_transcript.strip():
            return "No speech was captured during the meeting, Sir."

        duration = "unknown"
        if self._meeting_start:
            elapsed = (datetime.datetime.now() - self._meeting_start).total_seconds() / 60
            duration = f"{elapsed:.1f} minutes"

        # Ask the Brain to analyze the transcript
        if self.brain:
            prompt = (
                f"Analyze this meeting transcript (duration: {duration}).\n\n"
                f"TRANSCRIPT:\n{full_transcript[:6000]}\n\n"
                f"Provide:\n"
                f"1. A brief summary (2-3 sentences)\n"
                f"2. Key decisions made\n"
                f"3. Action items (as a numbered list)\n"
                f"4. Follow-ups needed\n\n"
                f"Format your response clearly with headers."
            )

            try:
                result = self.brain.think(prompt)
                analysis = result.get('response', full_transcript[:500])
            except Exception as e:
                logger.error(f"Brain analysis error: {e}")
                analysis = f"Transcript captured ({len(self._transcript_lines)} segments). Analysis failed."
        else:
            analysis = f"Transcript captured: {len(self._transcript_lines)} segments, {duration}."

        # Store in episodic memory
        if self.memory:
            self.memory.remember(
                text=f"Meeting ({duration}): {analysis[:200]}",
                category='context',
                tags=['meeting', 'transcript', 'auto-captured'],
                source='passive',
                importance=6,
                metadata={
                    'duration': duration,
                    'segments': len(self._transcript_lines),
                    'transcript_preview': full_transcript[:500]
                }
            )

        # Auto-extract action items and add to todo list
        if self.tasks and 'action items' in analysis.lower():
            self._extract_and_add_tasks(analysis)

        return analysis

    def _extract_and_add_tasks(self, analysis_text):
        """Extract action items from the analysis and add them to the todo list."""
        import re

        # Look for numbered or bulleted action items
        patterns = [
            r'(?:action\s*items?|tasks?|follow[\s-]*ups?):?\s*\n((?:\s*[-•*]\s*.+\n?)+)',
            r'\d+\.\s+(?:action\s*[:\s]+)?(.+)',
        ]

        tasks_found = []
        for pattern in patterns:
            matches = re.findall(pattern, analysis_text, re.IGNORECASE)
            for match in matches:
                task_text = match.strip().strip('-•*').strip()
                if task_text and len(task_text) > 5:
                    tasks_found.append(task_text)

        # Add to todo list
        for task in tasks_found[:10]:  # Max 10 tasks
            try:
                self.tasks.add(f"[Meeting] {task}")
                logger.info(f"Auto-added meeting task: {task[:60]}")
            except Exception as e:
                logger.error(f"Failed to add meeting task: {e}")

    def _save_meeting(self):
        """Save the meeting transcript and analysis to a file."""
        if not self._transcript_lines:
            return

        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"meeting_{timestamp}.json"
        filepath = os.path.join(self.meetings_dir, filename)

        with self._lock:
            transcript = list(self._transcript_lines)

        data = {
            'timestamp': self._meeting_start.isoformat() if self._meeting_start else timestamp,
            'segments': len(transcript),
            'transcript': transcript,
            'duration_seconds': (
                (datetime.datetime.now() - self._meeting_start).total_seconds()
                if self._meeting_start else 0
            )
        }

        try:
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2)
            logger.info(f"Meeting saved: {filepath}")
        except Exception as e:
            logger.error(f"Failed to save meeting: {e}")
