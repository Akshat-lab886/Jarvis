"""
J.A.R.V.I.S. — Background Computer Use (Hermes parity)
======================================================

Drives the desktop natively — scrolling, clicking, typing and dragging
on macOS, Linux or Windows — WITHOUT moving the mouse cursor or
interrupting the active window wherever the platform allows it.

Driver tiers (chosen automatically, best available first):

    ax      macOS accessibility (AppleScript/System Events): clicks a
            named UI element of a BACKGROUND app and sets field values
            directly — the physical cursor never moves, the frontmost
            window keeps focus.  This is the genuinely-background tier.
    quartz  macOS CGEvent posting via pyobjc (if installed): posts
            synthesized events; fast and pixel-accurate.
    pyautogui   legacy foreground fallback (the old Operator) — moves
            the real cursor.  Used only when neither tier above exists.

All ops also expose ``screenshot()`` (macOS `screencapture`, or
pyautogui elsewhere) so the agent can look before it acts.
``JARVIS_COMPUTER_USE=0`` disables the module entirely.
"""

import os
import platform
import logging
import subprocess
import tempfile

logger = logging.getLogger("Jarvis.ComputerUse")

_SYSTEM = platform.system()   # Darwin | Linux | Windows
_TIMEOUT = 20


def enabled():
    return os.getenv('JARVIS_COMPUTER_USE', '1') != '0'


def _as_quote(text):
    """Escape a value for safe interpolation inside an AppleScript
    double-quoted string literal.  ``\\`` and ``"`` are the only two
    characters AppleScript escapes, so a hostile value (app name, UI
    element, field value) can never break out of the string and inject
    script into a desktop-controlling subsystem."""
    return str(text).replace('\\', '\\\\').replace('"', '\\"')


def _osascript(script, timeout=_TIMEOUT):
    """Run AppleScript; returns (ok, output).  macOS only."""
    if _SYSTEM != 'Darwin':
        return False, "AppleScript is macOS-only."
    try:
        proc = subprocess.run(['osascript', '-e', script],
                              capture_output=True, text=True,
                              timeout=timeout)
        out = (proc.stdout or '').strip()
        if proc.returncode != 0:
            return False, (proc.stderr or 'osascript failed').strip()
        return True, out
    except subprocess.TimeoutExpired:
        return (False,
                "AppleScript timed out — the app may be showing "
                "a permission dialog (System Settings → Privacy "
                "& Security → Accessibility).")
    except Exception as e:
        return False, str(e)


class ComputerUse:
    """Tiered desktop driver."""

    def __init__(self):
        self._pyautogui = None
        self._has_quartz = None

    # ------------------------------------------------------------------ #
    # Tier selection
    # ------------------------------------------------------------------ #
    def _tier(self):
        if _SYSTEM == 'Darwin':
            return 'ax'          # AppleScript/System Events always present
        if self._quartz():
            return 'quartz'
        if self._autogui():
            return 'pyautogui'
        return None

    def _quartz(self):
        if self._has_quartz is None:
            try:
                import Quartz          # noqa: F401  (pyobjc)
                self._has_quartz = True
            except ImportError:
                self._has_quartz = False
        return self._has_quartz

    def _autogui(self):
        if self._pyautogui is None:
            try:
                import pyautogui
                pyautogui.FAILSAFE = True
                self._pyautogui = pyautogui
            except ImportError:
                self._pyautogui = False
        return self._pyautogui

    # ------------------------------------------------------------------ #
    # Background (AX) operations — macOS
    # ------------------------------------------------------------------ #
    def click_element(self, app_name, element_name, role=None):
        """
        Click a UI element of a possibly-background app by name — the
        cursor never moves and focus stays where it was.
        """
        if not enabled():
            return "Computer use disabled (JARVIS_COMPUTER_USE=0)."
        if _SYSTEM != 'Darwin':
            return ("Background element clicks need macOS accessibility; "
                    "on this platform use click(x, y).")
        role_filter = (f" whose role is \"{_as_quote(role)}\""
                       if role else '')
        script = (
            f'tell application "System Events"\n'
            f'  tell process "{_as_quote(app_name)}"\n'
            f'    set hits to (every UI element of every window'
            f'{role_filter} whose name contains '
            f'"{_as_quote(element_name)}")\n'
            f'    if (count of hits) is 0 then return "NOTFOUND"\n'
            f'    click item 1 of hits\n'
            f'    return "OK"\n'
            f'  end tell\n'
            f'end tell')
        ok, out = _osascript(script)
        if not ok:
            return f"AX click failed: {out}"
        if out == 'NOTFOUND':
            return (f"No UI element named '{element_name}' visible in "
                    f"{app_name}.")
        return f"Clicked '{element_name}' in {app_name} (background)."

    def set_field(self, app_name, field_name, value):
        """Type *value* into a named field of an app — background."""
        if not enabled():
            return "Computer use disabled."
        if _SYSTEM != 'Darwin':
            return "Background field typing needs macOS accessibility."
        script = (
            f'tell application "System Events"\n'
            f'  tell process "{_as_quote(app_name)}"\n'
            f'    set hits to (every text field of every window whose '
            f'name contains "{_as_quote(field_name)}")\n'
            f'    if (count of hits) is 0 then return "NOTFOUND"\n'
            f'    set value of item 1 of hits to "{_as_quote(value)}"\n'
            f'    return "OK"\n'
            f'  end tell\n'
            f'end tell')
        ok, out = _osascript(script)
        if not ok:
            return f"AX set failed: {out}"
        if out == 'NOTFOUND':
            return f"No text field named '{field_name}' in {app_name}."
        return f"Set '{field_name}' in {app_name} (background)."

    # ------------------------------------------------------------------ #
    # Coordinate-level operations (tiered)
    # ------------------------------------------------------------------ #
    def click(self, x, y):
        """Click at screen coordinates using the best driver."""
        if not enabled():
            return "Computer use disabled."
        try:
            x = int(x)
            y = int(y)
        except (TypeError, ValueError):
            # Match the other tiers, which degrade to a message instead
            # of letting a malformed coordinate raise out of the API.
            return (f"Click failed: coordinates must be integers — "
                    f"got ({x!r}, {y!r}).")
        if _SYSTEM == 'Darwin':
            ok, out = _osascript(
                f'tell application "System Events" to click at '
                f'{{{x}, {y}}}')
            return "Clicked." if ok else f"Click failed: {out}"
        if self._quartz():
            try:
                import Quartz
                from Quartz import (CGEventCreateMouseEvent,
                                    CGEventPost, kCGEventMouseMoved,
                                    kCGEventLeftMouseDown,
                                    kCGEventLeftMouseUp,
                                    kCGHIDEventTap)
                point = (int(x), int(y))
                down = CGEventCreateMouseEvent(
                    None, kCGEventLeftMouseDown, point, 0)
                up = CGEventCreateMouseEvent(
                    None, kCGEventLeftMouseUp, point, 0)
                CGEventPost(kCGHIDEventTap, down)
                CGEventPost(kCGHIDEventTap, up)
                return "Clicked (Quartz)."
            except Exception as e:
                return f"Quartz click failed: {e}"
        if self._autogui():
            try:
                self._pyautogui.click(int(x), int(y))
                return "Clicked (pyautogui)."
            except Exception as e:
                return f"Click failed: {e}"
        return "No desktop driver available."

    def type_text(self, text):
        """Keystrokes go to the frontmost application."""
        if not enabled():
            return "Computer use disabled."
        text = str(text)
        if _SYSTEM == 'Darwin':
            escaped = _as_quote(text)
            ok, out = _osascript(
                'tell application "System Events" to keystroke "'
                f'{escaped}"')
            return "Typed." if ok else f"Typing failed: {out}"
        if self._autogui():
            try:
                self._pyautogui.write(text, interval=0.02)
                return "Typed."
            except Exception as e:
                return f"Typing failed: {e}"
        return "No desktop driver available."

    def press_key(self, key):
        if not enabled():
            return "Computer use disabled."
        if _SYSTEM == 'Darwin':
            ok, out = _osascript(
                f'tell application "System Events" to key code '
                f'{_mac_keycode(key)}')
            return f"Pressed {key}." if ok else f"Key press failed: {out}"
        if self._autogui():
            try:
                self._pyautogui.press(str(key))
                return f"Pressed {key}."
            except Exception as e:
                return f"Key press failed: {e}"
        return "No desktop driver available."

    def scroll(self, amount=3):
        """Positive scrolls up, negative scrolls down."""
        if not enabled():
            return "Computer use disabled."
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return (f"Scroll failed: amount must be an integer — "
                    f"got {amount!r}.")
        if _SYSTEM == 'Darwin':
            ok, out = _osascript(
                'tell application "System Events" to repeat '
                f'{abs(amount)} times\n'
                f'  scroll {"down" if amount < 0 else "up"} 1\n'
                f'end repeat')
            return "Scrolled." if ok else f"Scroll failed: {out}"
        if self._autogui():
            try:
                self._pyautogui.scroll(int(amount))
                return "Scrolled."
            except Exception as e:
                return f"Scroll failed: {e}"
        return "No desktop driver available."

    def activate(self, app_name):
        """Bring an app to the front (the one foreground op)."""
        if not enabled():
            return "Computer use disabled."
        if _SYSTEM == 'Darwin':
            ok, out = _osascript(
                f'tell application "{_as_quote(app_name)}" to activate')
            return f"Activated {app_name}." if ok else \
                f"Activate failed: {out}"
        try:
            subprocess.run(['open', '-a', str(app_name)] if _SYSTEM ==
                           'Darwin' else [str(app_name)],
                           capture_output=True, timeout=_TIMEOUT)
            return f"Launched {app_name}."
        except Exception as e:
            return f"Launch failed: {e}"

    def screenshot(self, out_path=None):
        """Capture the screen without any extra dependency on macOS."""
        if not enabled():
            return "Computer use disabled."
        # Phase 1 hardening: captured screens are sensitive — never put
        # them in the web-served static/ tree (grep: zero consumers of the
        # old path). Private temp dir instead; callers may still pass
        # out_path explicitly.
        out_path = out_path or os.path.join(
            tempfile.gettempdir(), 'jarvis_computer_view.png')
        try:
            if _SYSTEM == 'Darwin':
                subprocess.run(['screencapture', '-x', out_path],
                               capture_output=True, timeout=_TIMEOUT,
                               check=True)
            elif self._autogui():
                self._pyautogui.screenshot(out_path)
            else:
                return "No screenshot driver available."
            return f"Screenshot saved: {out_path}"
        except Exception as e:
            return f"Screenshot failed: {e}"


# macOS virtual key codes for the common keys
_MAC_KEYCODES = {
    'enter': 36, 'return': 36, 'tab': 48, 'space': 49, 'esc': 53,
    'escape': 53, 'delete': 51, 'backspace': 51, 'up': 126, 'down': 125,
    'left': 123, 'right': 124, 'home': 115, 'end': 119, 'pageup': 116,
    'pagedown': 121, 'f1': 122, 'f2': 120, 'cmd': 55, 'command': 55,
    'shift': 56, 'ctrl': 59, 'control': 59, 'alt': 58, 'option': 58,
}


def _mac_keycode(key):
    try:
        low = str(key).lower()
        if low in _MAC_KEYCODES:
            return _MAC_KEYCODES[low]
        return int(key)          # numeric keycode passed through
    except (TypeError, ValueError):
        return 36     # default: return


# --------------------------------------------------------------------- #
# Singleton
# --------------------------------------------------------------------- #

_singleton = None


def get_driver():
    global _singleton
    if _singleton is None:
        _singleton = ComputerUse()
    return _singleton


def _reset_singleton():
    global _singleton
    _singleton = None
