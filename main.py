"""
main.py
═══════
Nexus entry point.

WHAT CHANGED IN THIS VERSION
──────────────────────────────
  ADDED:
    - Voice mode status shown in boot banner
    - get_voice_mode() imported for status display in the listen loop
    - speak_force() used for boot greeting so it's always audible
      regardless of whatever mode was previously saved

  The listen loop status line now shows voice mode alongside sleep state:
    [listening… | voice: SPEAK]
    [sleeping   | voice: MUTE ]

  Everything else is identical to the previous version.
"""

import os
import sys
from colorama import init, Fore, Style
init(autoreset=True)
from rich.console import Console
console = Console()
from rich.panel import Panel
from rich.text import Text
from core.voice_input  import listen_once, is_sleeping, calibrate_microphone
from core.voice_output import speak_force, get_voice_mode, VoiceMode
from ai_router         import route, startup_check


def _ensure_storage() -> None:
    os.makedirs("storage/notes", exist_ok=True)
    if not os.path.exists("storage/reminders.json"):
        with open("storage/reminders.json", "w") as f:
            f.write("[]")


def _boot() -> None:
    _ensure_storage()
    banner = Text()

    ascii_logo = r"""
        ███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗
        ████╗  ██║██╔════╝╚██╗██╔╝██║   ██║██╔════╝
        ██╔██╗ ██║█████╗   ╚███╔╝ ██║   ██║███████╗
        ██║╚██╗██║██╔══╝   ██╔██╗ ██║   ██║╚════██║
        ██║ ╚████║███████╗██╔╝ ██╗╚██████╔╝███████║
        ╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝
        """

    banner = Text()

    banner.append(
        ascii_logo,
        style="bold cyan"
)

    banner.append(
        "\nVoice-Powered Local AI Assistant",
        style="white"
)

    console.print(
        Panel.fit(
            banner,
            border_style="bright_blue",
            padding=(1, 4),
            title="NEXUS",
            subtitle="V1"
    )
)

    console.print("[bold green]● SYSTEM ONLINE[/bold green]")
    console.print("[cyan]Voice Commands Enabled[/cyan]")
    console.print("[yellow]Model:[/yellow] phi3")
    console.print()

    calibrate_microphone()
    startup_check()

    # speak_force() — boot greeting is always audible regardless of mode
    speak_force("Nexus online. Say 'Nexus mute' to silence me, or 'Nexus speak' to restore voice.")


def _loop() -> None:
    """
    Listen loop. Status line now shows both sleep state and voice mode
    so the user can glance at the terminal to see the current state.
    """
    while True:
        command = listen_once()

        if not command:
            # Build a clear status indicator
            sleep_status = "sleeping   " if is_sleeping else "listening…"
            voice_status = "MUTE" if get_voice_mode() == VoiceMode.MUTE else "SPEAK"
            color = Fore.YELLOW if is_sleeping else Fore.GREEN
            print(color + f"[{sleep_status} | voice: {voice_status}]",
            end="\r",
            flush=True)
            continue

        print()   # newline before routing logs
        route(command)


if __name__ == "__main__":
    try:
        _boot()
        _loop()

    except KeyboardInterrupt:
        print("\n\n[Nexus] Shutting down.")
        speak_force("Shutting down. Goodbye.")
        sys.exit(0)

    except Exception as exc:
        import traceback

        print("\n[Nexus] Fatal error:")
        traceback.print_exc()

        speak_force("A critical error occurred. Check the console.")
        sys.exit(1) 