import asyncio
import edge_tts
import pygame
import os

async def main():
    print("1. Initializing Pygame Mixer...")
    try:
        pygame.mixer.init()
        print("   Mixer Initialized.")
    except Exception as e:
        print(f"   Mixer Failed: {e}")
        return

    text = "Audio test initiated. Systems online."
    voice = "en-US-ChristopherNeural"
    output_file = "test_audio.mp3"

    print(f"2. Generating TTS for: '{text}'...")
    try:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(output_file)
        if os.path.exists(output_file):
            print(f"   TTS File Created: {output_file} ({os.path.getsize(output_file)} bytes)")
        else:
            print("   TTS Failed: File not found.")
            return
    except Exception as e:
        print(f"   TTS Failed with Exception: {e}")
        return

    print("3. Playing Audio...")
    try:
        pygame.mixer.music.load(output_file)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            pygame.time.Clock().tick(10)
        print("   Audio Finished.")
    except Exception as e:
        print(f"   Playback Failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())
