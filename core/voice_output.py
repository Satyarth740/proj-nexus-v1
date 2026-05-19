"""
core/voice_output.py
════════════════════
Nexus TTS — per-call engine architecture replacing the broken singleton.

WHY STARTUP SPEECH WORKS BUT LATER SPEECH FAILS
─────────────────────────────────────────────────
  pyttsx3 wraps Windows SAPI5 via a COM object. COM objects on Windows
  have thread affinity — they belong to the thread that created them and
  carry state across calls in that thread.

  Here is the exact sequence that silences all post-startup speech:

  BOOT TIME
  ─────────
    _engine = pyttsx3.init()          ← creates COM object on main thread
    speak_force("Nexus online...")    ← _engine.say() + runAndWait()
                                         SAPI5 speaks. ✓
                                         runAndWait() exits.
                                         COM object now has "completed" state.

  LISTEN LOOP RUNS
  ─────────────────
    listen_once() calls sr.Recognizer().listen()
    PyAudio opens the microphone stream.
    Under the hood, PyAudio initialises PortAudio which calls
    CoInitializeEx(COINIT_MULTITHREADED) on the main thread.

    THIS IS THE KILL SHOT.
    COM was already initialised as COINIT_APARTMENTTHREADED by pyttsx3.
    Re-initialising it as MULTITHREADED changes the threading model.
    The existing SAPI5 COM object is now in an inconsistent apartment.
    Subsequent calls to _engine.say() + runAndWait() silently no-op
    because the COM object can no longer marshal calls correctly.
    No exception is raised. Audio simply does not play.

  WHY STOP() + SAY() DOESN'T FIX IT
  ────────────────────────────────────
    _engine.stop() resets pyttsx3's internal queue but does NOT
    re-initialise the underlying COM object. The apartment mismatch
    persists regardless of how many times stop() is called.

THE REAL FIX — per-call engine, not a singleton
─────────────────────────────────────────────────
  Create a fresh pyttsx3 engine for every single speak() call.
  Each _make_engine() call runs pyttsx3.init() which:
    1. Creates a new COM object in the correct apartment for the
       current COM state of the thread.
    2. Speaks the text.
    3. Calls engine.stop() to release the COM object immediately.

  This is slightly slower (~50-100ms overhead per call) but 100% reliable.
  For a voice assistant where calls are seconds apart, that overhead is
  completely imperceptible.

  speak_async() already used this pattern (fresh engine per thread).
  We are now applying the same pattern to synchronous speak() as well.
"""

import pyttsx3
import threading
from enum import Enum, auto

from config import VOICE_RATE, VOICE_VOLUME, VOICE_GENDER


# ── Voice mode ─────────────────────────────────────────────────────────────────

class VoiceMode(Enum):
    SPEAK = auto()
    MUTE  = auto()


_voice_mode: VoiceMode = VoiceMode.SPEAK


def get_voice_mode() -> VoiceMode:
    return _voice_mode


def set_voice_mode(mode: VoiceMode) -> None:
    global _voice_mode
    _voice_mode = mode
    print(f"[Nexus][voice] Mode set to: {mode.name}")


def is_muted() -> bool:
    return _voice_mode == VoiceMode.MUTE


# ── Engine factory ─────────────────────────────────────────────────────────────

def _make_engine() -> pyttsx3.Engine:
    """
    Creates, configures, and returns a fresh pyttsx3 engine.

    Called for every speak() invocation — this is intentional.
    See module docstring for why a singleton is unreliable after
    PyAudio's CoInitializeEx call changes the COM threading model.
    """
    engine = pyttsx3.init()
    engine.setProperty("rate",   VOICE_RATE)
    engine.setProperty("volume", VOICE_VOLUME)
    _apply_voice(engine)
    return engine


def _apply_voice(engine: pyttsx3.Engine) -> None:
    """
    Selects the first SAPI5 voice whose name or ID contains VOICE_GENDER.
    Falls back silently to system default if no match is found.

    Common Windows voices:
      Male   → "Microsoft David Desktop"
      Female → "Microsoft Zira Desktop"
    """
    try:
        voices = engine.getProperty("voices")
        if not voices:
            print("[Nexus][voice] No SAPI5 voices found. Check Windows TTS settings.")
            return

        gender = VOICE_GENDER.lower()
        print(f"[Nexus][voice] Available voices ({len(voices)}):")
        for v in voices:
            print(f"  - {v.name}  |  id: {v.id}")
            if gender in v.name.lower() or gender in v.id.lower():
                engine.setProperty("voice", v.id)
                print(f"[Nexus][voice] Selected: {v.name}")
                return

        print(f"[Nexus][voice] No '{gender}' voice found — using system default.")

    except Exception as exc:
        print(f"[Nexus][voice] Voice selection error: {exc}")


# ── Core TTS primitive ─────────────────────────────────────────────────────────

def _say(text: str) -> None:
    """
    Creates a fresh engine, speaks text, then tears the engine down.

    The engine.stop() call after runAndWait() releases the COM object
    cleanly, preventing any state from carrying over to the next call.

    Wrapped in a broad try/except so a TTS failure never crashes the
    listen loop — Nexus just keeps running silently.
    """
    engine = None
    try:
        print(f"[Nexus][voice] _say() → initialising engine...")
        engine = _make_engine()

        print(f"[Nexus][voice] _say() → saying: {text!r}")
        engine.say(text)
        engine.runAndWait()
        print(f"[Nexus][voice] _say() → complete.")

    except Exception as exc:
        print(f"[Nexus][voice] _say() ERROR: {type(exc).__name__}: {exc}")
        print("[Nexus][voice] TTS failed — Nexus will continue without audio.")

    finally:
        # Always release the engine, even if say()/runAndWait() threw
        if engine is not None:
            try:
                engine.stop()
            except Exception:
                pass


# ── Public output functions ────────────────────────────────────────────────────

def speak(text: str) -> None:
    """
    Standard output. Respects voice mode.

    SPEAK → print + audio
    MUTE  → print only
    """
    if not text or not text.strip():
        return

    print(f"Nexus: {text}")
    print(f"[Nexus][voice] speak() | mode={_voice_mode.name}")

    if _voice_mode == VoiceMode.SPEAK:
        _say(text)
    else:
        print("[Nexus][voice] Muted — skipping audio.")


def speak_force(text: str) -> None:
    """
    Always speaks aloud regardless of voice mode.
    Reserved for: boot greeting, mode-change confirmations, sleep/wake, errors.
    Do NOT use for AI-generated responses.
    """
    if not text or not text.strip():
        return

    print(f"Nexus [system]: {text}")
    print("[Nexus][voice] speak_force() → bypassing mute.")
    _say(text)


def print_response(text: str) -> None:
    """Terminal-only output. Never produces audio."""
    if text and text.strip():
        print(f"Nexus: {text}")


def speak_async(text: str) -> None:
    """
    Non-blocking TTS for background notifications (reminders, alerts).
    Respects voice mode. Runs in a daemon thread — each call gets its
    own engine instance for thread safety.
    """
    if not text or not text.strip():
        return

    print(f"Nexus (async): {text}")

    if _voice_mode == VoiceMode.MUTE:
        print("[Nexus][voice] speak_async() → muted.")
        return

    def _run() -> None:
        _say(text)

    threading.Thread(target=_run, daemon=True).start()