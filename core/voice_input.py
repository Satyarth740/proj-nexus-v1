"""
core/voice_input.py
═══════════════════
Nexus's ears — improved listening for natural speech completion.

WHAT CHANGED IN THIS VERSION
──────────────────────────────
  OLD PROBLEM:
    - pause_threshold too low (0.8s) — cut off mid-sentence
    - phrase_time_limit too short — long commands got truncated
    - ambient noise calibration on EVERY listen cycle (adds latency)

  FIXES APPLIED:
    1. pause_threshold     0.8s  →  1.4s
       How long silence must last before Nexus decides you're done speaking.
       1.4s feels natural — long enough to pause mid-thought, short enough
       to not feel sluggish.

    2. phrase_time_limit   12s   →  25s
       Hard cap on how long a single utterance can be. 25s handles even
       long requests like "explain the difference between supervised and
       unsupervised machine learning" comfortably.

    3. timeout             5s    →  8s
       How long Nexus waits for speech to START before giving up.
       8s reduces false "nothing heard" loops when the user hesitates.

    4. Ambient calibration moved to boot-time only
       Calibrating every iteration added ~0.3-0.5s of dead time per loop.
       Now we calibrate once at startup and let dynamic threshold handle
       ongoing noise changes.

    5. non_speaking_duration = 1.0s
       Controls how long after speech ends the mic stays "active".
       Prevents clipping the last word of a sentence.
"""

import speech_recognition as sr
from core.voice_output import speak

# ── State ─────────────────────────────────────────────────────────────────────
is_sleeping: bool = False

# ── Recognizer — tuned for natural speech ─────────────────────────────────────
_recognizer = sr.Recognizer()

# How long a pause must last to end the phrase.
# 0.8 = cut off too fast | 1.4 = natural conversational pause
_recognizer.pause_threshold = 1.2

# Minimum audio energy to treat as speech (vs background hiss).
# Auto-calibrated at boot, then kept dynamic throughout session.
_recognizer.energy_threshold = 400

# Continuously auto-adjust sensitivity to ambient noise changes.
_recognizer.dynamic_energy_threshold = True

# Damping factor for dynamic adjustments (0-1). Lower = reacts faster to noise.
_recognizer.dynamic_energy_adjustment_damping = 0.15

# How long after speech ends before the phrase is considered complete.
# Prevents clipping the final word of longer sentences.
_recognizer.non_speaking_duration = 1.0


def calibrate_microphone() -> None:
    """
    Called ONCE at boot from main.py.
    Samples ambient noise for 1.5 seconds and sets energy_threshold
    just above the room noise floor.

    Moving this out of the listen loop removes 0.3-0.5s of dead time
    per command cycle that was making Nexus feel sluggish.
    """
    print("[Nexus][mic] Calibrating microphone to ambient noise...")
    try:
        with sr.Microphone() as source:
            _recognizer.adjust_for_ambient_noise(source, duration=1.5)
        print(f"[Nexus][mic] Calibration done. Energy threshold: {_recognizer.energy_threshold:.0f}")
    except OSError as exc:
        print(f"[Nexus][mic] Calibration failed: {exc}")
        speak("I had trouble accessing the microphone. Check your audio settings.")


# ── Wake / sleep triggers ─────────────────────────────────────────────────────
_WAKE_PHRASES  = {"nexus wake up", "wake up nexus", "nexus start", "hey nexus"}
_SLEEP_PHRASES = {"nexus stop", "stop nexus", "nexus sleep", "go to sleep nexus"}


def _raw_listen() -> str:
    """
    Opens the mic and blocks until a complete phrase is captured.

    timeout=8             wait up to 8s for speech to START
    phrase_time_limit=25  capture up to 25s of continuous speech
    """
    with sr.Microphone() as source:
        try:
            audio = _recognizer.listen(
                source,
                timeout=8,
                phrase_time_limit=25,
            )
        except sr.WaitTimeoutError:
            return ""

    try:
        text = _recognizer.recognize_google(audio)
        return text.lower().strip()

    except sr.UnknownValueError:
        return ""

    except sr.RequestError as exc:
        print(f"[Nexus][mic] Speech API error: {exc}")
        speak("I lost connection to the speech service. Check your internet.")
        return ""


def _handle_state_commands(text: str) -> bool:
    """
    Checks for wake/sleep phrases. Mutates is_sleeping if matched.
    Returns True if the text was a state command — caller should NOT dispatch it.
    """
    global is_sleeping

    if any(phrase in text for phrase in _SLEEP_PHRASES):
        if not is_sleeping:
            is_sleeping = True
            speak("Going to sleep. Say 'Nexus wake up' when you need me.")
        return True

    if any(phrase in text for phrase in _WAKE_PHRASES):
        if is_sleeping:
            is_sleeping = False
            speak("I'm back. What do you need?")
        return True

    return False


def listen_once() -> str:
    """
    PUBLIC API — called by main.py on every loop iteration.

    Returns:
        Non-empty string → command ready for the router
        ""               → silence, error, sleeping, or state command consumed
    """
    text = _raw_listen()

    if not text:
        return ""

    print(f"[Nexus heard]: {text}")

    if _handle_state_commands(text):
        return ""

    if is_sleeping:
        return ""

    return text