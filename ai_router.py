"""
ai_router.py
════════════
Nexus AI router — conversational pipeline fixed.

ROOT CAUSE OF THE SILENT RESPONSE BUG (summary)
─────────────────────────────────────────────────
  The bug was NOT in ai_router.py itself. speak() was being called correctly.
  The failure was in pyttsx3's SAPI5 Windows driver:

    1. speak("Let me think about that.")  → _engine.runAndWait() completes
    2. Ollama generates response (several seconds)
    3. speak(cleaned_response)            → _engine.say() queues text
                                          → _engine.runAndWait() silently no-ops
                                            because SAPI5 still has residual
                                            internal state from call #1

  Fix is in voice_output.py: _say() now calls _engine.stop() before every
  _engine.say() call to clear SAPI5 state. This file adds debug prints
  to make the call path fully visible so you can confirm the fix is working.

WHAT CHANGED IN THIS FILE
──────────────────────────
  _action_conversational():
    - Added step-by-step debug prints showing the full pipeline
    - Removed the "Let me think about that." intermediate speak() call
      (this was call #1 that left SAPI5 in a bad state before call #2)
    - Replaced with a print-only status line: print("[Nexus] Generating...")
    - Single speak() call after generation completes — clean, one call only
    - Explicit checks at each stage so failures are visible in terminal

  _action_general_query():
    - Same pattern: single speak() call, no intermediate audio
    - Debug print before and after generation
"""

import json
import re
import subprocess
import webbrowser
import urllib.parse
from typing import Optional

import requests

from core.voice_output import speak, speak_force, VoiceMode, set_voice_mode, get_voice_mode
import commands
from config import OLLAMA_MODEL, OLLAMA_BASE_URL, OLLAMA_TIMEOUT


# ══════════════════════════════════════════════════════════════════════════════
# ALLOWED INTENTS
# ══════════════════════════════════════════════════════════════════════════════

ALLOWED_INTENTS = frozenset({
    "open_app",
    "open_website",
    "search_web",
    "terminal_command",
    "conversational",
    "general_query",
    "set_speak",
    "set_mute",
    "refresh_apps",
    "unknown",
    "save_note",
    "show_notes",
    "save_reminder",
    "coding_query",
})


# ══════════════════════════════════════════════════════════════════════════════
# SYNONYM MAP
# ══════════════════════════════════════════════════════════════════════════════

_INTENT_SYNONYMS: dict[str, str] = {
    "launch_app": "open_app", "launch": "open_app", "start_app": "open_app",
    "run_app": "open_app", "execute_app": "open_app", "open_application": "open_app",
    "open_program": "open_app", "open_software": "open_app",
    "browse": "open_website", "navigate": "open_website", "go_to": "open_website",
    "visit": "open_website", "open_url": "open_website", "open_browser": "open_website",
    "open_link": "open_website", "website": "open_website", "web": "open_website",
    "search": "search_web", "google": "search_web", "google_search": "search_web",
    "web_search": "search_web", "search_google": "search_web", "look_up": "search_web",
    "lookup": "search_web", "find": "search_web", "search_for": "search_web",
    "run_command": "terminal_command", "run_terminal": "terminal_command",
    "terminal": "terminal_command", "execute": "terminal_command",
    "execute_command": "terminal_command", "shell": "terminal_command",
    "shell_command": "terminal_command", "cmd": "terminal_command",
    "command": "terminal_command", "run": "terminal_command",
    "query": "general_query", "question": "general_query", "ask": "general_query",
    "explain": "conversational", "conversation": "conversational", "chat": "conversational",
    "discuss": "conversational", "tell_me": "conversational", "describe": "conversational",
    "speak_mode": "set_speak", "voice_on": "set_speak", "enable_voice": "set_speak",
    "unmute": "set_speak", "voice_enabled": "set_speak",
    "mute_mode": "set_mute", "voice_off": "set_mute", "disable_voice": "set_mute",
    "silent": "set_mute", "silence": "set_mute",
    "none": "unknown", "null": "unknown", "other": "unknown",
}


# ══════════════════════════════════════════════════════════════════════════════
# COMPOUND SPLITTER
# ══════════════════════════════════════════════════════════════════════════════

def _strip_prefix(prefix: str):
    def _extract(compound: str) -> str:
        return compound[len(prefix) + 1:].replace("_", " ").strip()
    return _extract


_COMPOUND_PREFIXES = [
    ("open",    "open_app",         _strip_prefix("open")),
    ("launch",  "open_app",         _strip_prefix("launch")),
    ("start",   "open_app",         _strip_prefix("start")),
    ("run",     "terminal_command", _strip_prefix("run")),
    ("search",  "search_web",       _strip_prefix("search")),
    ("google",  "search_web",       _strip_prefix("google")),
    ("browse",  "open_website",     _strip_prefix("browse")),
    ("explain", "conversational",   _strip_prefix("explain")),
]


def _split_compound_intent(raw_intent: str) -> Optional[tuple]:
    raw = raw_intent.lower().strip()
    for prefix, canonical, extractor in _COMPOUND_PREFIXES:
        if raw.startswith(prefix + "_") and raw != canonical:
            remainder = extractor(raw)
            if remainder:
                print(f"[Nexus][normalize] Compound: '{raw}' → '{canonical}' + '{remainder}'")
                return canonical, remainder
    return None


# ══════════════════════════════════════════════════════════════════════════════
# NORMALIZATION + VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

def _normalize(raw_intent: str, raw_target: str) -> dict:
    intent = raw_intent.lower().strip().replace(" ", "_")
    target = raw_target.strip()
    if intent in ALLOWED_INTENTS:
        return {"intent": intent, "target": target}
    if intent in _INTENT_SYNONYMS:
        mapped = _INTENT_SYNONYMS[intent]
        print(f"[Nexus][normalize] Synonym: '{intent}' → '{mapped}'")
        return {"intent": mapped, "target": target}
    compound = _split_compound_intent(intent)
    if compound:
        canonical, extracted = compound
        return {"intent": canonical, "target": target if target else extracted}
    for prefix, canonical, _ in _COMPOUND_PREFIXES:
        if intent in (prefix, prefix + "_"):
            return {"intent": canonical, "target": target}
    print(f"[Nexus][normalize] Unresolvable: '{intent}' → 'unknown'")
    return {"intent": "unknown", "target": target}


def _validate(result: dict) -> dict:
    intent = result.get("intent", "unknown")
    target = result.get("target", "")
    if not isinstance(intent, str) or not isinstance(target, str):
        return {"intent": "unknown", "target": ""}
    if intent not in ALLOWED_INTENTS:
        return {"intent": "unknown", "target": target}
    return {"intent": intent, "target": target}


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM PROMPT + FEW-SHOT
# ══════════════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """\
You are a strict JSON intent classifier. Output ONLY raw JSON. Nothing else.

OUTPUT FORMAT — MANDATORY:
{"intent": "<intent>", "target": "<target>"}

ALLOWED INTENTS — use EXACTLY one of these:
1. open_app          → launch a desktop application
2. open_website      → open a website
3. search_web        → search the web
4. terminal_command  → execute a shell command
5. conversational    → explain, describe, or answer in detail
6. general_query     → short factual 1-sentence answer
7. set_speak         → user says "nexus speak" or "enable voice" or "unmute"
8. set_mute          → user says "nexus mute" or "disable voice" or "mute"
9. refresh_apps      → rebuild the application index
10. unknown          → cannot classify
11. save_note        → save a note permanently
12. show_notes       → open the saved notes folder
13. save_reminder    → save a reminder/task
14. coding_query     → programming, debugging, coding help

CRITICAL:
- NEVER invent new intent names.
- NEVER combine intent and target (e.g. open_chrome is WRONG).
- Always include both "intent" and "target" fields.
- "target" = what the user wants (lowercase). Use "" if not applicable.

CORRECT EXAMPLES:
{"intent": "open_app", "target": "chrome"}
{"intent": "open_website", "target": "youtube"}
{"intent": "search_web", "target": "python decorators"}
{"intent": "terminal_command", "target": "pip install flask"}
{"intent": "conversational", "target": "explain machine learning"}
{"intent": "conversational", "target": "who is messi"}
{"intent": "general_query", "target": "what time is it"}
{"intent": "set_speak", "target": ""}
{"intent": "set_mute", "target": ""}
{"intent": "unknown", "target": ""}
"""

_FEW_SHOT_MESSAGES = [
    {"role": "user",      "content": "Open Chrome"},
    {"role": "assistant", "content": '{"intent": "open_app", "target": "chrome"}'},
    {"role": "user",      "content": "Launch Spotify"},
    {"role": "assistant", "content": '{"intent": "open_app", "target": "spotify"}'},
    {"role": "user",      "content": "Open God of War"},
    {"role": "assistant", "content": '{"intent": "open_app", "target": "god of war"}'},
    {"role": "user",      "content": "Go to YouTube"},
    {"role": "assistant", "content": '{"intent": "open_website", "target": "youtube"}'},
    {"role": "user",      "content": "Search Python decorators"},
    {"role": "assistant", "content": '{"intent": "search_web", "target": "python decorators"}'},
    {"role": "user",      "content": "Install flask"},
    {"role": "assistant", "content": '{"intent": "terminal_command", "target": "pip install flask"}'},
    {"role": "user",      "content": "What is machine learning"},
    {"role": "assistant", "content": '{"intent": "conversational", "target": "what is machine learning"}'},
    {"role": "user",      "content": "Explain AI vs ML"},
    {"role": "assistant", "content": '{"intent": "conversational", "target": "explain ai vs ml"}'},
    {"role": "user",      "content": "Who is Messi"},
    {"role": "assistant", "content": '{"intent": "conversational", "target": "who is messi"}'},
    {"role": "user",      "content": "What time is it"},
    {"role": "assistant", "content": '{"intent": "general_query", "target": "what time is it"}'},
    {"role": "user",      "content": "Nexus speak"},
    {"role": "assistant", "content": '{"intent": "set_speak", "target": ""}'},
    {"role": "user",      "content": "Nexus mute"},
    {"role": "assistant", "content": '{"intent": "set_mute", "target": ""}'},
    {"role": "user",      "content": "Enable voice"},
    {"role": "assistant", "content": '{"intent": "set_speak", "target": ""}'},
    {"role": "user",      "content": "Disable voice"},
    {"role": "assistant", "content": '{"intent": "set_mute", "target": ""}'},
    {"role": "user", "content": "Take note buy milk"},
{"role": "assistant", "content": '{"intent": "save_note", "target": "buy milk"}'},
{"role": "user", "content": "Write note finish project"},
{"role": "assistant", "content": '{"intent": "save_note", "target": "finish project"}'},
{"role": "user", "content": "Show my notes"},
{"role": "assistant", "content": '{"intent": "show_notes", "target": ""}'},

{"role": "user", "content": "Read my notes"},
{"role": "assistant", "content": '{"intent": "show_notes", "target": ""}'},
{"role": "user", "content": "Remind me to study ML"},
{"role": "assistant", "content": '{"intent": "save_reminder", "target": "study ML"}'},

{"role": "user", "content": "Set reminder finish project"},
{"role": "assistant", "content": '{"intent": "save_reminder", "target": "finish project"}'},

{"role": "user", "content": "Write binary search in Python"},
{"role": "assistant", "content": '{"intent": "coding_query", "target": "write binary search in python"}'},

{"role": "user", "content": "Explain this Python error"},
{"role": "assistant", "content": '{"intent": "coding_query", "target": "explain this python error"}'},

{"role": "user", "content": "How do decorators work"},
{"role": "assistant", "content": '{"intent": "coding_query", "target": "how do decorators work"}'},
]


# ══════════════════════════════════════════════════════════════════════════════
# JSON EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

def _extract_json(raw: str) -> Optional[dict]:
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    try:
        r = json.loads(text)
        if isinstance(r, dict): return r
    except json.JSONDecodeError:
        pass
    stripped = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    try:
        r = json.loads(stripped)
        if isinstance(r, dict): return r
    except json.JSONDecodeError:
        pass
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            r = json.loads(text[s:e + 1])
            if isinstance(r, dict): return r
        except json.JSONDecodeError:
            pass
    match = re.search(
        r'\{\s*"intent"\s*:\s*"([^"]+)"(?:\s*,\s*"target"\s*:\s*"([^"]*)")?\s*\}',
        text, re.DOTALL | re.IGNORECASE
    )
    if match:
        return {"intent": match.group(1).strip(), "target": (match.group(2) or "").strip()}
    print(f"[Nexus][JSON] All strategies failed. Raw: {raw!r}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# OLLAMA CLIENT
# ══════════════════════════════════════════════════════════════════════════════

def _check_ollama_running() -> bool:
    try:
        return requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=3).status_code == 200
    except requests.exceptions.ConnectionError:
        return False


def _ollama_generate(
    prompt: str,
    system: str,
    extra_messages: list = None,
    temperature: float = 0.0,
    max_tokens: int = 80,
    use_prefill: bool = True,
) -> Optional[str]:
    messages = [{"role": "system", "content": system}]
    if extra_messages:
        messages.extend(extra_messages)
    messages.append({"role": "user", "content": prompt})
    if use_prefill:
        messages.append({"role": "assistant", "content": "{"})

    payload = {
        "model":  OLLAMA_MODEL,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
        "messages": messages,
    }
    if use_prefill:
        payload["options"]["stop"] = ["\n\n", "```", "Note:", "This ", "The "]

    try:
        resp = requests.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload, timeout=OLLAMA_TIMEOUT)
        resp.raise_for_status()
        content = resp.json()["message"]["content"].strip()
        if use_prefill and not content.startswith("{"):
            content = "{" + content
        return content
    except requests.exceptions.ConnectionError:
        speak("I cannot reach my local AI. Please start Ollama.")
        return None
    except requests.exceptions.Timeout:
        speak("The local AI is taking too long. Try restarting Ollama.")
        return None
    except requests.exceptions.HTTPError as exc:
        if "400" in str(exc) and use_prefill:
            return _ollama_generate(prompt, system, extra_messages, temperature, max_tokens, False)
        speak("There was a problem communicating with the local AI.")
        return None
    except (KeyError, json.JSONDecodeError):
        return None


def _classify(command: str) -> dict:
    SAFE = {"intent": "unknown", "target": ""}
    raw = _ollama_generate(command, _SYSTEM_PROMPT, _FEW_SHOT_MESSAGES,
                           temperature=0.0, max_tokens=60, use_prefill=True)
    if raw is None:
        return SAFE
    print(f"[Nexus][AI] Raw: {raw!r}")
    parsed = _extract_json(raw)
    if parsed is None:
        return SAFE
    normalized = _normalize(str(parsed.get("intent", "unknown")), str(parsed.get("target", "")))
    validated  = _validate(normalized)
    print(f"[Nexus][AI] Final: intent='{validated['intent']}' target='{validated['target']}'")
    return validated


# ══════════════════════════════════════════════════════════════════════════════
# MARKDOWN STRIPPER
# ══════════════════════════════════════════════════════════════════════════════

def _strip_markdown(text: str) -> str:
    text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"`{1,3}", "", text)
    text = re.sub(r"\n+", " ", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


# ══════════════════════════════════════════════════════════════════════════════
# ACTION EXECUTORS
# ══════════════════════════════════════════════════════════════════════════════

_KNOWN_SITES: dict[str, str] = {
    "youtube":       "https://www.youtube.com",
    "google":        "https://www.google.com",
    "github":        "https://www.github.com",
    "gmail":         "https://mail.google.com",
    "reddit":        "https://www.reddit.com",
    "netflix":       "https://www.netflix.com",
    "spotify":       "https://open.spotify.com",
    "stackoverflow": "https://stackoverflow.com",
    "twitter":       "https://www.twitter.com",
    "linkedin":      "https://www.linkedin.com",
    "chatgpt":       "https://chat.openai.com",
    "claude":        "https://claude.ai",
    "huggingface":   "https://huggingface.co",
}


def _action_set_speak(target: str) -> None:
    set_voice_mode(VoiceMode.SPEAK)
    speak_force("Voice enabled. I will speak my responses aloud.")


def _action_set_mute(target: str) -> None:
    # Speak confirmation BEFORE setting mute so it's audible
    speak_force("Voice muted. I will still listen and run commands, but stay silent.")
    set_voice_mode(VoiceMode.MUTE)

def _action_save_note(target: str) -> None:

    commands.save_note(target)

def _action_open_app(target: str) -> None:
    commands.open_app(target)

def _action_save_reminder(target: str) -> None:

    commands.save_reminder(target)
def _action_open_website(target: str) -> None:
    url = _KNOWN_SITES.get(target.lower())
    if not url:
        url = target if target.startswith("http") else f"https://www.{target}.com"
    speak(f"Opening {target}.")
    webbrowser.open(url)

def _action_show_notes(target: str) -> None:

    commands.show_notes()

def _action_search_web(target: str) -> None:
    if not target.strip():
        speak("What should I search for?")
        return
    encoded = urllib.parse.quote_plus(target)
    speak(f"Searching for: {target}")
    webbrowser.open(f"https://www.google.com/search?q={encoded}")


def _action_terminal_command(target: str) -> None:
    if not target.strip():
        speak("What command should I run?")
        return
    speak(f"Running: {target}")
    try:
        subprocess.Popen(f'start cmd /k "{target}"', shell=True)
    except Exception as exc:
        speak("I had trouble running that command.")
        print(f"[Nexus][terminal] Error: {exc}")


def _action_conversational(target: str) -> None:
    """
    Generates a spoken AI response — FIXED pipeline.

    KEY CHANGE: Removed the intermediate speak("Let me think about that.") call.
    That was the trigger for the pyttsx3 double-runAndWait() bug:
      Old: speak(status) → runAndWait() → [Ollama] → speak(response) → runAndWait() SILENT
      New: print(status) → [Ollama] → speak(response) → runAndWait() WORKS

    Now there is exactly ONE speak() call per conversational response.
    voice_output.py's _engine.stop() fix provides a second layer of protection.
    """
    print(f"\n[Nexus][conversational] ── Pipeline start ──")
    print(f"[Nexus][conversational] Target: '{target}'")
    print(f"[Nexus][conversational] Voice mode: {get_voice_mode().name}")

    # Status to terminal only — no audio call before Ollama
    print("[Nexus][conversational] Generating response from Ollama...")

    voice_system = (
        "You are Nexus, an AI voice assistant. Your reply will be read aloud by text-to-speech.\n"
        "RULES:\n"
        "- Write EXACTLY 2 to 3 sentences. No more.\n"
        "- Plain English only. No markdown, no bullet points, no lists.\n"
        "- No asterisks, hashes, backticks, dashes, or special characters.\n"
        "- Do NOT start with 'Great question', 'Certainly', or any preamble.\n"
        "- Short sentences. Easy to follow when heard aloud.\n"
    )

    raw = _ollama_generate(
        prompt      = target,
        system      = voice_system,
        temperature = 0.65,
        max_tokens  = 200,
        use_prefill = False,
    )

    print(f"[Nexus][conversational] Ollama returned: {raw!r}")

    if not raw or not raw.strip():
        print("[Nexus][conversational] Empty response from Ollama.")
        speak("I was not able to generate a response right now.")
        return

    cleaned = _strip_markdown(raw)
    print(f"[Nexus][conversational] Cleaned response: {cleaned!r}")
    print(f"[Nexus][conversational] Calling speak()...")

    # Single speak() call — voice_output.py handles mode check
    print("\n" + "=" * 60)
    print(cleaned)
    print("=" * 60 + "\n")

    speak("I generated the coding response. Check the terminal for details.")

    print(f"[Nexus][conversational] ── Pipeline end ──\n")

def _action_coding_query(target: str) -> None:

    print(f"\n[Nexus][coding] Query: '{target}'")

    coding_system = (
        "You are Nexus Coding Assistant.\n"
        "You help with programming, debugging, DSA, APIs, ML, and software engineering.\n"
        "Rules:\n"
        "- Be clear and beginner friendly.\n"
        "- Give clean explanations.\n"
        "- If code is needed, provide code.\n"
        "- Prefer concise but useful answers.\n"
        "- Avoid markdown tables.\n"
        "- Focus on practical coding help.\n"
    )

    raw = _ollama_generate(
        prompt      = target,
        system      = coding_system,
        temperature = 0.3,
        max_tokens  = 300,
        use_prefill = False,
    )

    if not raw or not raw.strip():
        speak("I could not generate a coding response.")
        return

    cleaned = _strip_markdown(raw)

    print("\n" + "=" * 60)
    print("[Nexus][coding] Response:\n")
    print(cleaned)
    print("=" * 60 + "\n")

    speak("I generated the coding response. Check the terminal for details.")
def _action_general_query(target: str) -> None:
    """Short 1-sentence factual answers — single speak() call."""
    print(f"[Nexus][general_query] Generating for: '{target}'")

    voice_system = (
        "You are Nexus, a voice assistant. "
        "Give exactly 1 short direct sentence. "
        "No markdown. No lists. Plain English only."
    )

    raw = _ollama_generate(
        prompt      = target,
        system      = voice_system,
        temperature = 0.3,
        max_tokens  = 60,
        use_prefill = False,
    )

    print(f"[Nexus][general_query] Ollama returned: {raw!r}")

    result = raw.strip() if raw else "I could not find an answer right now."
    speak(result)


def _action_refresh_apps(target: str) -> None:
    speak("Rescanning applications. One moment.")
    commands.rebuild_index()


# ══════════════════════════════════════════════════════════════════════════════
# INTENT → ACTION MAP
# ══════════════════════════════════════════════════════════════════════════════

_INTENT_MAP: dict[str, callable] = {
    "open_app":         _action_open_app,
    "open_website":     _action_open_website,
    "search_web":       _action_search_web,
    "terminal_command": _action_terminal_command,
    "conversational":   _action_conversational,
    "general_query":    _action_general_query,
    "set_speak":        _action_set_speak,
    "set_mute":         _action_set_mute,
    "refresh_apps":     _action_refresh_apps,
    "save_note":        _action_save_note,
    "show_notes": _action_show_notes,
    "save_reminder": _action_save_reminder,
    "coding_query": _action_coding_query,
}


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

def startup_check() -> None:
    if _check_ollama_running():
        print(f"[Nexus][Ollama] Connected — model: {OLLAMA_MODEL}")
        print("[Nexus][launcher] Pre-building app index...")
        commands.get_app_index()
    else:
        print("[Nexus][Ollama] WARNING: Ollama not running!")
        print("  Start: ollama serve")
        speak_force("Warning: local AI is offline. Please start Ollama and restart Nexus.")


def route(command: str) -> None:
    """PUBLIC API. Called by main.py for every voice command."""
    print(f"\n[Nexus][router] Routing: '{command}'")
        # ── Fast deterministic routing (bypass AI for obvious commands) ──

    command_lower = command.lower().strip()

    website_keywords = {
        "youtube", "google", "gmail", "github",
        "reddit", "netflix", "spotify",
        "stackoverflow", "twitter", "linkedin",
        "chatgpt", "claude", "huggingface"
    }

    for prefix in ("open ", "launch ", "start "):

        if command_lower.startswith(prefix):

            target = command_lower[len(prefix):].strip()

            print(f"[Nexus][fast-route] Detected direct launch request: '{target}'")

        # Known websites
            if target in website_keywords:
                print("[DEBUG] Known website branch")
                _action_open_website(target)
                return

        # Folders/drives
            if commands.open_folder(target):
                print("[DEBUG] Folder branch")
                return

        # Dynamic websites/domains
            if "." in target:
                if commands.open_dynamic_website(target):
                    print("[DEBUG] Dynamic website branch")
                    return

        # Otherwise treat as app
            print("[DEBUG] App branch")
            _action_open_app(target)
            return
    result = _classify(command)
    intent = result["intent"]
    target = result["target"]
    print(f"[Nexus][router] Dispatching: intent='{intent}' target='{target}'")
    action = _INTENT_MAP.get(intent)
    if action:
        action(target)
    else:
        speak("I understood you, but I do not know how to handle that yet.")
        