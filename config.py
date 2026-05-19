"""
config.py
═════════
Single source of truth for all Nexus settings.
Edit values here only — never hardcode them inside modules.
"""

import os

# ── Voice output ───────────────────────────────────────────────────────────────
VOICE_RATE   = 175     # words per minute  (150 = calm, 200 = fast)
VOICE_VOLUME = 1.0     # 0.0 (silent) → 1.0 (full)
VOICE_GENDER = "male"  # "male" → David,  "female" → Zira  (Windows SAPI5)

# ── Wake word ──────────────────────────────────────────────────────────────────
WAKE_WORD = "nexus"

# ── Ollama local AI ────────────────────────────────────────────────────────────
# The model to use. Must be pulled first: ollama pull <model>
#
# Recommended options:
#   "llama3"    → best overall quality, needs ~8GB RAM       (ollama pull llama3)
#   "phi3"      → fastest, lightest, needs ~4GB RAM          (ollama pull phi3)
#   "mistral"   → great at reasoning, needs ~8GB RAM         (ollama pull mistral)
#   "gemma2"    → strong at coding tasks, needs ~8GB RAM     (ollama pull gemma2)
#
OLLAMA_MODEL    = "phi3"                      # change to "llama3" for best quality
OLLAMA_BASE_URL = "http://localhost:11434"    # Ollama's default address — don't change
OLLAMA_TIMEOUT  = 60                          # seconds to wait for a model response
                                              # increase to 120 if your PC is slow

# ── Application paths (Windows) ───────────────────────────────────────────────
VSCODE_PATH   = "code"
NOTEPAD_PATH  = "notepad"
EXPLORER_PATH = "explorer"
CHROME_PATH   = "chrome"
# CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

# ── Default folder ─────────────────────────────────────────────────────────────
DEFAULT_FOLDER = os.path.expanduser("~/Documents")

# ── Storage ────────────────────────────────────────────────────────────────────
NOTES_DIR      = "storage/notes"
REMINDERS_FILE = "storage/reminders.json"