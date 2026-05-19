"""
commands.py
═══════════
Nexus dynamic Windows application launcher — definitive rebuild.

THREE ROOT CAUSES OF THE LAUNCH FAILURES (diagnosed from code audit)
──────────────────────────────────────────────────────────────────────

BUG A — subprocess.Popen(["notepad.exe"], shell=False) fails silently
  System32 apps like notepad.exe, calc.exe, cmd.exe may not be in the
  Python process PATH depending on how Python was installed (conda, pyenv,
  Microsoft Store Python all have different PATH inheritance).
  shell=False + no full path = FileNotFoundError, even though notepad.exe
  is a valid command in any CMD or PowerShell window.

  FIX: Use shell=True for all builtin system commands. Windows CMD always
  resolves System32 commands correctly when shell=True is set.

BUG B — os.startfile() fails silently on malformed .lnk paths
  Some .lnk files have paths with Unicode characters, trailing spaces,
  or point to targets that have moved. os.startfile() raises OSError
  with no useful message when this happens.

  FIX: Use ShellExecute via ctypes — the native Windows API call.
  ShellExecute("open", path) does full Windows shell resolution including
  shortcut targets, COM-based launchers, and UWP app protocol handlers.
  It raises a more descriptive error and handles edge cases os.startfile
  cannot handle.

BUG C — No registry scan (misses apps with no Start Menu shortcut)
  Apps installed silently, portable apps, and some game launchers don't
  create Start Menu shortcuts. The previous version would miss all of these.

  FIX: Add winreg scanning of the Windows Uninstall registry keys.
  Every properly installed app writes InstallLocation and DisplayIcon here,
  giving us a direct path to the executable regardless of shortcuts.

LAUNCH STRATEGY — four tiers, tried in order
──────────────────────────────────────────────
  Tier 1: KNOWN_BUILTINS dict   — Windows built-in system tools (shell=True)
  Tier 2: Registry scan         — installed apps via Uninstall registry keys
  Tier 3: Filesystem scan       — Start Menu + Desktop .lnk shortcuts + exes
  Tier 4: ShellExecute fallback — attempt to run target name directly via shell

HOW EACH LAUNCH METHOD WORKS
──────────────────────────────
  shell=True subprocess       → CMD.exe resolves the command, finds System32 apps
  ShellExecute (ctypes)       → Native Windows API, handles all app types
  os.startfile(.lnk)          → Shell resolves the shortcut to its target .exe
  subprocess.Popen([path])    → Direct .exe launch, fastest for known full paths
"""

import os
import subprocess
import time
import winreg
import webbrowser
import ctypes
import ctypes.wintypes
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from rapidfuzz import process, fuzz

from core.voice_output import speak


# ══════════════════════════════════════════════════════════════════════════════
# TIER 1 — KNOWN BUILTINS
# Windows built-in tools that live in System32. Launched with shell=True
# because that is the most reliable way to resolve them across all Python
# installation types (store, conda, pyenv, etc.)
# ══════════════════════════════════════════════════════════════════════════════

KNOWN_BUILTINS: dict[str, str] = {
    # Core Windows tools
    "notepad":              "notepad",
    "calculator":           "calc",
    "calc":                 "calc",
    "paint":                "mspaint",
    "wordpad":              "wordpad",
    "task manager":         "taskmgr",
    "taskmgr":              "taskmgr",
    "file explorer":        "explorer",
    "explorer":             "explorer",
    "control panel":        "control",
    "command prompt":       "cmd",
    "cmd":                  "cmd",
    "powershell":           "powershell",
    "registry editor":      "regedit",
    "regedit":              "regedit",
    "snipping tool":        "snippingtool",
    "character map":        "charmap",
    "disk cleanup":         "cleanmgr",
    "device manager":       "devmgmt.msc",
    "event viewer":         "eventvwr.msc",
    "services":             "services.msc",
    "system information":   "msinfo32",
    "system info":          "msinfo32",
    "resource monitor":     "resmon",
    "performance monitor":  "perfmon",
    "remote desktop":       "mstsc",
    "magnifier":            "magnify",
    "on screen keyboard":   "osk",
    "narrator":             "narrator",
    "sticky notes":         "stikynot",
    # Common third-party apps whose shell command is well-known
    "chrome":               "chrome",
    "google chrome":        "chrome",
    "firefox":              "firefox",
    "edge":                 "msedge",
    "microsoft edge":       "msedge",
    "vs code":              "code",
    "vscode":               "code",
    "visual studio code":   "code",
    "git bash":             "git-bash",
    "wsl":                  "wsl",
}
KNOWN_FOLDERS = {
    "desktop": os.path.expanduser("~/Desktop"),
    "downloads": os.path.expanduser("~/Downloads"),
    "documents": os.path.expanduser("~/Documents"),
    "pictures": os.path.expanduser("~/Pictures"),
    "music": os.path.expanduser("~/Music"),
    "videos": os.path.expanduser("~/Videos"),

    "c drive": "C:\\",
    "d drive": "D:\\",
    "e drive": "E:\\",
}

_BUILTIN_THRESHOLD = 72   # fuzzy score cutoff for builtin name matching


# ══════════════════════════════════════════════════════════════════════════════
# SHELLEXECUTE — native Windows launcher
# More reliable than subprocess for .lnk files and Store apps.
# ══════════════════════════════════════════════════════════════════════════════

def _shell_execute(path: str) -> bool:
    """
    Calls Windows ShellExecuteW via ctypes.
    This is the same API Windows Explorer uses when you double-click a file.
    Handles .lnk shortcuts, .msc consoles, UWP protocol handlers, and .exe files.

    Returns True if Windows accepted the call (hInstance > 32).
    Windows ShellExecute returns > 32 on success, <= 32 on failure.

    This replaces os.startfile() which has no return value and raises
    vague OSErrors without details.
    """
    try:
        shell32 = ctypes.windll.shell32
        result = shell32.ShellExecuteW(
            None,           # hwnd — no parent window
            "open",         # verb — "open" runs the file
            path,           # file path or command
            None,           # parameters
            None,           # working directory (None = inherit)
            1,              # nShowCmd — SW_SHOWNORMAL
        )
        if result > 32:
            print(f"[Nexus][launcher] ShellExecute success: {path}")
            return True
        else:
            print(f"[Nexus][launcher] ShellExecute returned {result} for: {path}")
            return False
    except Exception as exc:
        print(f"[Nexus][launcher] ShellExecute error: {exc}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# TIER 1 — BUILTIN LAUNCH
# ══════════════════════════════════════════════════════════════════════════════

def _lookup_builtin(query: str) -> Optional[str]:
    """
    Searches KNOWN_BUILTINS with exact match then fuzzy match.
    Returns the shell command string, or None if not found.
    """
    q = query.lower().strip()

    # Exact match — O(1)
    if q in KNOWN_BUILTINS:
        print(f"[Nexus][launcher] Builtin exact: '{q}' → '{KNOWN_BUILTINS[q]}'")
        return KNOWN_BUILTINS[q]

    # Fuzzy match
    names  = list(KNOWN_BUILTINS.keys())
    result = process.extractOne(q, names, scorer=fuzz.WRatio, score_cutoff=_BUILTIN_THRESHOLD)
    if result:
        matched_name, score, _ = result
        cmd = KNOWN_BUILTINS[matched_name]
        print(f"[Nexus][launcher] Builtin fuzzy: '{matched_name}' score={score:.0f} → '{cmd}'")
        return cmd

    return None


def _launch_builtin(cmd: str, name: str) -> bool:
    """
    Launches a builtin app using shell=True subprocess.
    shell=True ensures CMD.exe handles the resolution, which correctly
    searches System32 regardless of the Python process's PATH.
    """
    print(f"[Nexus][launcher] Launching builtin shell=True: '{cmd}'")
    try:
        subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return True
    except Exception as exc:
        print(f"[Nexus][launcher] Builtin shell launch failed: {exc}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# TIER 2 — WINDOWS REGISTRY SCAN
# Reads the Uninstall registry keys — the authoritative list of installed apps.
# Every properly installed app writes here. No shortcuts needed.
# ══════════════════════════════════════════════════════════════════════════════

_UNINSTALL_KEYS: list[tuple] = [
    # (hive,              key path,                                             flag)
    (winreg.HKEY_LOCAL_MACHINE,
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0),
    (winreg.HKEY_LOCAL_MACHINE,
     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", 0),
    (winreg.HKEY_CURRENT_USER,
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0),
]


def _scan_registry() -> list[dict]:
    """
    Reads Windows Uninstall registry keys to find installed applications.

    For each installed app, we look for:
      DisplayName    → the human-readable app name
      DisplayIcon    → path to .exe (often used as the launch path)
      InstallLocation → folder where the app is installed

    We combine DisplayIcon + InstallLocation to find the main executable.

    Returns list of { name, path, type:"exe" } dicts.
    """
    found   = []
    seen    = set()

    for hive, key_path, flag in _UNINSTALL_KEYS:
        try:
            key = winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | flag)
        except OSError:
            continue

        i = 0
        while True:
            try:
                subkey_name = winreg.EnumKey(key, i)
                i += 1
            except OSError:
                break   # no more subkeys

            try:
                subkey = winreg.OpenKey(key, subkey_name)

                # Get DisplayName — skip entries without one
                try:
                    display_name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                except OSError:
                    winreg.CloseKey(subkey)
                    continue

                display_name = display_name.strip()
                if not display_name or len(display_name) < 2:
                    winreg.CloseKey(subkey)
                    continue

                # Try to find a launchable path from DisplayIcon or InstallLocation
                launch_path = None

                # DisplayIcon often looks like "C:\Path\app.exe,0" — strip the ,N part
                try:
                    icon_val = winreg.QueryValueEx(subkey, "DisplayIcon")[0]
                    icon_path = icon_val.split(",")[0].strip().strip('"')
                    if icon_path.lower().endswith(".exe") and os.path.isfile(icon_path):
                        launch_path = icon_path
                except OSError:
                    pass

                # InstallLocation + guess main exe if DisplayIcon didn't work
                if not launch_path:
                    try:
                        install_loc = winreg.QueryValueEx(subkey, "InstallLocation")[0].strip().strip('"')
                        if install_loc and os.path.isdir(install_loc):
                            # Look for an exe matching the app name in the install folder
                            safe_name = re.sub(r"[^\w]", "", display_name.lower())
                            for exe in Path(install_loc).glob("*.exe"):
                                exe_safe = re.sub(r"[^\w]", "", exe.stem.lower())
                                if safe_name in exe_safe or exe_safe in safe_name:
                                    launch_path = str(exe)
                                    break
                    except OSError:
                        pass

                winreg.CloseKey(subkey)

                if launch_path:
                    key_str = display_name.lower()
                    if key_str not in seen:
                        seen.add(key_str)
                        found.append({
                            "name": display_name,
                            "path": launch_path,
                            "type": "exe",
                        })

            except OSError:
                pass

        winreg.CloseKey(key)

    print(f"[Nexus][launcher]   Registry entries with launch paths: {len(found)}")
    return found


# ══════════════════════════════════════════════════════════════════════════════
# TIER 3 — FILESYSTEM SCAN
# ══════════════════════════════════════════════════════════════════════════════

_SHORTCUT_DIRS: list[str] = [
    os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
    os.path.expandvars(r"%PROGRAMDATA%\Microsoft\Windows\Start Menu\Programs"),
    os.path.expandvars(r"%USERPROFILE%\Desktop"),
    os.path.expandvars(r"%PUBLIC%\Desktop"),
]

_EXE_DIRS: list[str] = [
    os.path.expandvars(r"%PROGRAMFILES%"),
    os.path.expandvars(r"%PROGRAMFILES(X86)%"),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs"),
]

_WINDOWS_APPS_DIR: str = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps")

# EXACT name match skip lists (not substring — that caused false positives)
_SKIP_FOLDER_NAMES: set[str] = {
    "uninstall", "uninst", "_uninstall",
    "crashreport", "crashreporter", "crashpad",
    "redist", "redistributable", "vcredist",
    "directx", "dotnetfx", "runtime", "runtimes",
    "installer", "setup", "__pycache__", "cache", "temp", "tmp",
}

_SKIP_EXE_STEMS: set[str] = {
    "uninstall", "uninst", "unins000",
    "setup", "install", "installer",
    "update", "updater", "autoupdater",
    "crashreporter", "crashpad_handler", "crashhandler",
    "helper", "launcher_helper", "elevatedinstaller",
    "maintenancetool", "dxwebsetup", "vc_redist", "dotnetfx",
}

_FS_THRESHOLD  = 65
_CACHE_TTL     = 300

_app_index:       list[dict] = []
_index_built_at:  float      = 0.0

import re   # needed for registry name cleaning above


def _should_skip_folder(name: str) -> bool:
    return name.lower() in _SKIP_FOLDER_NAMES   # exact match only


def _should_skip_exe(stem: str) -> bool:
    return stem.lower() in _SKIP_EXE_STEMS       # exact match only


def _scan_shortcuts(base_dir: str) -> list[dict]:
    found = []
    base  = Path(base_dir)
    if not base.exists():
        return found
    try:
        for lnk in base.rglob("*.lnk"):
            if _should_skip_folder(lnk.parent.name):
                continue
            stem = lnk.stem.strip()
            if len(stem) < 2 or _should_skip_exe(stem.lower()):
                continue
            found.append({"name": stem, "path": str(lnk), "type": "lnk"})
    except (PermissionError, OSError):
        pass
    return found


def _scan_exes(base_dir: str, max_depth: int = 3) -> list[dict]:
    found = []
    base  = Path(base_dir)
    if not base.exists():
        return found

    def _walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            for item in directory.iterdir():
                if item.is_dir() and not _should_skip_folder(item.name):
                    _walk(item, depth + 1)
                elif item.suffix.lower() == ".exe":
                    stem = item.stem
                    if len(stem) >= 2 and not _should_skip_exe(stem):
                        found.append({"name": stem, "path": str(item), "type": "exe"})
        except (PermissionError, OSError):
            pass

    _walk(base, 0)
    return found


def _scan_windows_apps(apps_dir: str) -> list[dict]:
    found = []
    base  = Path(apps_dir)
    if not base.exists():
        return found
    try:
        for exe in base.glob("*.exe"):
            if len(exe.stem) >= 2 and not _should_skip_exe(exe.stem):
                found.append({"name": exe.stem, "path": str(exe), "type": "exe"})
    except (PermissionError, OSError):
        pass
    return found


def _build_index() -> None:
    """
    Builds the full app index using all three tiers:
      1. Registry entries (most reliable paths)
      2. Start Menu + Desktop shortcuts (best display names)
      3. Filesystem exe scan (catches everything else)

    Shortcuts win deduplication over raw exes.
    Registry entries win over filesystem exes (they have exact paths).
    """
    global _app_index, _index_built_at

    print("[Nexus][launcher] ── Building app index ──")
    all_apps: list[dict] = []

    # Tier 2 — registry
    reg_apps = _scan_registry()
    all_apps.extend(reg_apps)

    # Tier 3a — shortcuts
    for d in _SHORTCUT_DIRS:
        batch = _scan_shortcuts(d)
        print(f"[Nexus][launcher]   Shortcuts in {Path(d).name}: {len(batch)}")
        all_apps.extend(batch)

    # Tier 3b — exe directories
    for d in _EXE_DIRS:
        batch = _scan_exes(d, max_depth=3)
        print(f"[Nexus][launcher]   Exes in {Path(d).name}: {len(batch)}")
        all_apps.extend(batch)

    # Tier 3c — Store apps
    store = _scan_windows_apps(_WINDOWS_APPS_DIR)
    print(f"[Nexus][launcher]   Store stubs: {len(store)}")
    all_apps.extend(store)

    # Deduplicate — priority: lnk > registry exe > filesystem exe
    seen          = set()
    deduplicated  = []

    priority_order = (
        [a for a in all_apps if a["type"] == "lnk"] +
        reg_apps +
        [a for a in all_apps if a["type"] == "exe" and a not in reg_apps]
    )

    for app in priority_order:
        key = app["name"].lower().strip()
        if key and key not in seen:
            seen.add(key)
            deduplicated.append(app)

    _app_index      = deduplicated
    _index_built_at = time.time()
    print(f"[Nexus][launcher] Index complete: {len(_app_index)} unique apps.")


def get_app_index() -> list[dict]:
    age = time.time() - _index_built_at
    if not _app_index or age > _CACHE_TTL:
        _build_index()
    return _app_index


# ══════════════════════════════════════════════════════════════════════════════
# FUZZY MATCHING
# ══════════════════════════════════════════════════════════════════════════════

def _fuzzy_find(query: str, index: list[dict]) -> Optional[dict]:
    """
    Finds the best match using WRatio scorer.
    Prints top-3 candidates so every match decision is visible in the terminal.
    """
    if not index or not query:
        return None

    names   = [app["name"] for app in index]
    results = process.extract(query, names, scorer=fuzz.WRatio, limit=3)

    print(f"[Nexus][launcher] Fuzzy top-3 for '{query}':")
    for name, score, idx in results:
        print(f"  [{score:3.0f}] {name}  →  {index[idx]['path']}")

    best_name, best_score, best_idx = results[0] if results else ("", 0, 0)

    if best_score < _FS_THRESHOLD:
        print(f"[Nexus][launcher] Best score {best_score:.0f} < threshold {_FS_THRESHOLD} — no match.")
        return None

    return index[best_idx]

def save_note(content: str) -> bool:

    content = content.strip()

    if not content:
        speak("Your note is empty.")
        return False

    try:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        filename = f"note_{timestamp}.txt"

        filepath = os.path.join("storage", "notes", filename)

        with open(filepath, "w", encoding="utf-8") as file:
            file.write(content)

        print(f"[Nexus][notes] Saved note: {filepath}")

        speak("Note saved successfully.")
        return True

    except Exception as exc:
        print(f"[Nexus][notes] Error: {exc}")
        speak("I could not save the note.")
        return False
def show_notes() -> bool:

    notes_path = os.path.join("storage", "notes")

    try:
        os.startfile(notes_path)

        speak("Opening your notes.")

        return True

    except Exception as exc:
        print(f"[Nexus][notes] Error opening notes: {exc}")

        speak("I could not open your notes.")

        return False    
def save_reminder(task: str) -> bool:

    task = task.strip()

    if not task:
        speak("Reminder is empty.")
        return False

    try:
        filepath = os.path.join("storage", "reminders.json")

        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as file:
                reminders = json.load(file)
        else:
            reminders = []

        reminder = {
            "task": task,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        reminders.append(reminder)

        with open(filepath, "w", encoding="utf-8") as file:
            json.dump(reminders, file, indent=4)

        print(f"[Nexus][reminders] Saved reminder: {task}")

        speak("Reminder saved successfully.")

        return True

    except Exception as exc:
        print(f"[Nexus][reminders] Error: {exc}")

        speak("I could not save the reminder.")

        return False    
# ══════════════════════════════════════════════════════════════════════════════
# LAUNCH DISPATCH
# ══════════════════════════════════════════════════════════════════════════════

def _launch_entry(app: dict) -> bool:
    """
    Launches an app index entry using the best available method.

    .lnk  → ShellExecute (handles all shortcut types reliably)
    .exe  → subprocess.Popen([path]) then ShellExecute fallback
    """
    path = app["path"]
    name = app["name"]
    kind = app["type"]

    print(f"[Nexus][launcher] Launching '{name}' ({kind}): {path}")

    if kind == "lnk":
        # ShellExecute is the right API for .lnk — handles COM-based launchers
        success = _shell_execute(path)
        if not success:
            # Fallback to os.startfile
            try:
                os.startfile(path)
                return True
            except Exception as exc:
                speak(f"I found {name} but could not open it. Try opening it manually.")
                print(f"[Nexus][launcher] startfile fallback also failed: {exc}")
                return False
        return True

    else:   # exe
        # Direct Popen first (fastest, no shell overhead)
        try:
            subprocess.Popen(
                [path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            return True
        except FileNotFoundError:
            # Path in index is stale — try ShellExecute as fallback
            print(f"[Nexus][launcher] Popen FileNotFoundError — trying ShellExecute")
            return _shell_execute(path)
        except PermissionError:
            speak(f"I don't have permission to open {name}. Try running Nexus as administrator.")
            print(f"[Nexus][launcher] PermissionError: {path}")
            return False
        except Exception as exc:
            speak(f"Something went wrong opening {name}.")
            print(f"[Nexus][launcher] Exception: {type(exc).__name__}: {exc}")
            return False


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════
def open_folder(target: str) -> bool:

    target = target.lower().strip()
    # Domains/websites are NOT folders
    if "." in target:
        return False

    path = KNOWN_FOLDERS.get(target)

    if not path:
        return False

    if not os.path.exists(path):
        speak(f"{target} does not exist on this PC.")
        return False

    try:
        os.startfile(path)
        speak(f"Opening {target}.")
        return True

    except Exception as exc:
        print(f"[Nexus][folder] Error: {exc}")
        speak(f"I could not open {target}.")
        return False

def open_dynamic_website(target: str) -> bool:

    target = target.lower().strip()

    if not target:
        return False

    # Already looks like a domain
    if "." in target:
        url = f"https://{target}"

    else:
        # Assume .com website
        url = f"https://www.{target}.com"

    try:
        webbrowser.open(url)
        speak(f"Opening {target}.")
        return True

    except Exception as exc:
        print(f"[Nexus][web] Error: {exc}")
        return False
            
def open_app(target: str) -> bool:
    """
    PUBLIC API — called by ai_router._action_open_app().

    Four-tier strategy:
      Tier 1: KNOWN_BUILTINS  → shell=True subprocess (handles all System32 apps)
      Tier 2: Registry index  → direct .exe paths for installed apps
      Tier 3: Filesystem scan → Start Menu .lnk + Program Files .exe
      Tier 4: ShellExecute    → last resort, runs target name directly
    """
    if not target or not target.strip():
        speak("Which app should I open?")
        return False

    print(f"\n[Nexus][launcher] ── open_app('{target}') ──")

    # ── Tier 1: builtins ──────────────────────────────────────────────────────
    builtin_cmd = _lookup_builtin(target)
    if builtin_cmd:
        speak(f"Opening {target}.")
        if _launch_builtin(builtin_cmd, target):
            return True
        print("[Nexus][launcher] Builtin launch failed, falling through to index...")

    # ── Tiers 2 & 3: index (registry + filesystem combined) ──────────────────
    index = get_app_index()
    match = _fuzzy_find(target, index)

    if match:
        speak(f"Opening {match['name']}.")
        return _launch_entry(match)

    # ── Tier 4: ShellExecute with raw name ────────────────────────────────────
    # Last resort — attempt to run the target name directly.
    # Works for apps that are in PATH but not in our index.
    print(f"[Nexus][launcher] Tier 4: ShellExecute raw name '{target}'")
    if _shell_execute(target):
        speak(f"Opening {target}.")
        return True

    # Nothing worked
    speak(
        f"I could not find {target} on your PC. "
        "Make sure it is installed and appears in your Start Menu or Desktop."
    )
    return False


def rebuild_index() -> None:
    """Force-rebuilds the app index. Called by 'refresh apps' command."""
    global _index_built_at
    _index_built_at = 0.0
    get_app_index()
    speak(f"App index rebuilt. Found {len(_app_index)} applications.")


def list_indexed_apps() -> list[str]:
    """Returns sorted list of all indexed app names. Useful for debugging."""
    return sorted(app["name"] for app in get_app_index())