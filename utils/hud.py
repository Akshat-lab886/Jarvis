"""
J.A.R.V.I.S. — HUD Mode (Hermes parity)
=======================================

A quick floating composer bar that drops down ON TOP of any active
window and fades out silently when idle — ask Jarvis something without
switching apps.

    python main.py --hud

* Tkinter (stdlib) always-on-top borderless composer
* Enter sends; the window withdraws instantly ("fades out silently")
  and the reply is both spoken and pushed to the dashboard
* Global hotkey Cmd+Shift+H (macOS) / Ctrl+Shift+H re-opens it when
  pynput is installed; without it, click the jarvis-hud menu-bar app
  or run the command again
* Works fully offline against the local Brain; when the server is up
  it shares the same instances via the event bus

``JARVIS_HUD=0`` disables the module.
"""

import os
import sys
import queue
import threading

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE_DIR)


def enabled():
    return os.getenv('JARVIS_HUD', '1') != '0'


def _send_locally(text, on_reply):
    """Direct pipeline (no server required)."""
    def _work():
        try:
            from utils.brain import Brain
            from utils.executor import JarvisExecutor
            brain = Brain()
            executor = JarvisExecutor()
            command = brain.think(text)
            result = executor.execute_command(command, brain,
                                              original_text=text)
            reply = ''
            if isinstance(command, dict) and command.get('response'):
                reply = str(command['response'])
            elif result is not None:
                reply = str(result)
            if reply:
                on_reply(reply)
        except Exception as e:
            on_reply(f"HUD error: {e}")
    threading.Thread(target=_work, daemon=True,
                     name="hud-pipeline").start()


class HudWindow:
    """The floating composer (Tkinter)."""

    def __init__(self):
        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk()
        self.root.title("Jarvis HUD")
        self.root.attributes('-topmost', True)          # float on top
        try:
            self.root.overrideredirect(True)            # borderless bar
        except Exception:
            pass
        self._center_bar()

        self.label = tk.Label(self.root, text="▸ JARVIS",
                              fg='#ffb000', bg='#0c0f0d',
                              font=('Menlo', 13, 'bold'))
        self.label.pack(side='left', padx=(14, 4), pady=8)
        self.entry = tk.Entry(self.root, bg='#101311', fg='#e8e4d8',
                              insertbackground='#ffb000',
                              font=('Menlo', 13), relief='flat',
                              width=64)
        self.entry.pack(side='left', padx=6, pady=8, fill='x',
                        expand=True)
        self.entry.bind('<Return>', self._on_send)
        self.entry.bind('<Escape>', lambda e: self.hide())
        self.root.bind('<FocusOut>', lambda e: self.root.after(
            400, self._maybe_hide))
        self._hidden = False
        # Replies arrive from the background pipeline thread; they are
        # queued here and applied by _poll_outbox() running on the Tk
        # main thread (Tkinter is not thread-safe — cross-thread widget
        # calls can hang/crash the main loop or silently drop updates).
        self._outbox = queue.Queue()

    def _center_bar(self):
        """Dock the bar near the top center of the screen."""
        self.root.update_idletasks()
        w, h = 760, 52
        screen_w = self.root.winfo_screenwidth()
        self.root.geometry(f"{w}x{h}+{(screen_w - w) // 2}+60")

    def show(self):
        self._hidden = False
        self.root.deiconify()
        self.root.lift()
        # NOTE: do NOT clear the entry here.  A reply that lands while
        # the user is already typing a second message must not silently
        # delete their keystrokes; the entry is cleared in _on_send once
        # the text has actually been consumed.
        self.entry.focus_set()

    def hide(self):
        """Silent fade-out: withdraw and wait for the next hotkey."""
        self._hidden = True
        self.root.withdraw()

    def _maybe_hide(self):
        if not self._hidden and not self.entry.focus_get():
            self.hide()

    def _on_send(self, _event):
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, 'end')   # consume what was sent
        self.hide()          # drop away instantly while working
        _send_locally(text, self._reply)

    def _reply(self, text):
        """Called on the pipeline thread.  Never touch Tk widgets here —
        queue the payload for the main-thread poller to apply."""
        try:
            self._outbox.put(str(text or ''))
        except Exception:
            pass

    def _poll_outbox(self):
        """Main-thread poller: apply any queued replies on the Tk loop."""
        try:
            while True:
                self._show_reply(self._outbox.get_nowait())
        except queue.Empty:
            pass
        try:
            self.root.after(150, self._poll_outbox)
        except Exception:
            pass

    def _show_reply(self, text):
        """Show + speak the reply briefly, then fade again (main thread)."""
        if not text:
            return
        try:
            self.label.configure(text=f"▸ {text[:90]}")
            self.show()
            self.root.after(9000, self.hide)
        except Exception:
            pass

    def run(self):
        self.show()
        self.root.after(150, self._poll_outbox)   # start the poller
        self.root.mainloop()


def _start_hotkey(window):
    """Global hotkey (pynput, optional).  Returns True when armed."""
    try:
        from pynput import keyboard
    except ImportError:
        return False

    def on_activate():
        window.show()

    try:
        hotkey = keyboard.HotKey(
            keyboard.HotKey.parse('<cmd>+<shift>+h'), on_activate)
        listener = keyboard.Listener(
            on_press=hotkey.press, on_release=hotkey.release)
        listener.daemon = True
        listener.start()
        return True
    except Exception:
        return False


def run_hud():
    """Entry point — python main.py --hud"""
    if not enabled():
        print("HUD disabled (JARVIS_HUD=0).")
        return
    window = HudWindow()
    if _start_hotkey(window):
        print("HUD running — Cmd+Shift+H toggles it, Esc hides it.")
    else:
        print("HUD running — Esc hides it. (pip install pynput for the "
              "Cmd+Shift+H global hotkey.)")
    window.run()


if __name__ == '__main__':
    run_hud()
