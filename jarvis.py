import sys
import os
import glob
import json
import io
import wave
import subprocess
import webbrowser
import numpy as np
import sounddevice as sd
import speech_recognition as sr
import pyttsx3
import psutil
import pygetwindow as gw
from datetime import datetime
import anthropic
from dotenv import load_dotenv

load_dotenv(override=True)  # loads ANTHROPIC_API_KEY from .env, overriding any empty/stale env var

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from environment
MODEL = "claude-opus-4-8"
USER_NAME = "AI"  # the user likes to be addressed by this name
VOICE_INPUT_DEVICE = "Yeti"  # record from this microphone (partial name match); "" = Windows default

SYSTEM_PROMPT = (
    "You are Jarvis, an intelligent Windows system assistant. "
    f"The user's name is {USER_NAME} — address them as {USER_NAME} naturally (greetings, sign-offs, etc.). "
    "You have tools to fetch real-time data about processes, open windows, and system stats. "
    "Answer questions naturally and concisely. Format numbers neatly (e.g. '556 MB', '12%'). "
    "Do not dump raw data — summarise it helpfully."
)

# ── System data functions ────────────────────────────────────────────────────

def _get_running_processes(sort_by: str = "memory", limit: int = 20) -> list[dict]:
    procs = []
    for p in psutil.process_iter(['pid', 'name', 'status', 'cpu_percent', 'memory_info']):
        try:
            info = p.info
            mem = info['memory_info'].rss / (1024 * 1024) if info['memory_info'] else 0.0
            procs.append({
                'pid': info['pid'],
                'name': info['name'],
                'status': info['status'],
                'cpu_percent': info['cpu_percent'],
                'memory_mb': round(mem, 1),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    key = 'memory_mb' if sort_by == 'memory' else 'cpu_percent'
    procs.sort(key=lambda x: x[key], reverse=True)
    return procs[:limit]


def _get_open_windows() -> list[str]:
    return [w.title for w in gw.getAllWindows() if w.title.strip()]


def _open_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    webbrowser.open(url)
    return f"Opened {url}"


def _open_app(app_name: str) -> str:
    local = os.environ.get("LOCALAPPDATA", "")
    appdata = os.environ.get("APPDATA", "")

    # Common name → exe mappings
    exe_map = {
        "discord": "Discord.exe",
        "spotify": "Spotify.exe",
        "steam": "Steam.exe",
        "chrome": "chrome.exe",
        "google chrome": "chrome.exe",
        "firefox": "firefox.exe",
        "notepad": "notepad.exe",
        "calculator": "calc.exe",
        "obs": "obs64.exe",
        "vlc": "vlc.exe",
        "vscode": "Code.exe",
        "vs code": "Code.exe",
        "visual studio code": "Code.exe",
        "epic": "EpicGamesLauncher.exe",
        "epic games": "EpicGamesLauncher.exe",
    }

    exe = exe_map.get(app_name.lower(), app_name.replace(" ", "") + ".exe")

    # Known install locations (supports glob for versioned folders like Discord app-1.0.x)
    known = {
        "Discord.exe": [os.path.join(local, "Discord", "app-*", "Discord.exe")],
        "Spotify.exe": [
            os.path.join(appdata, "Spotify", "Spotify.exe"),
            os.path.join(local, "Microsoft", "WindowsApps", "Spotify.exe"),
        ],
        "Steam.exe": [
            "C:\\Program Files (x86)\\Steam\\Steam.exe",
            "C:\\Program Files\\Steam\\Steam.exe",
        ],
        "chrome.exe": [
            os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
            "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        ],
        "Code.exe": [
            os.path.join(local, "Programs", "Microsoft VS Code", "Code.exe"),
            "C:\\Program Files\\Microsoft VS Code\\Code.exe",
        ],
        "EpicGamesLauncher.exe": [
            "C:\\Program Files (x86)\\Epic Games\\Launcher\\Portal\\Binaries\\Win64\\EpicGamesLauncher.exe",
            "C:\\Program Files\\Epic Games\\Launcher\\Portal\\Binaries\\Win64\\EpicGamesLauncher.exe",
        ],
    }

    # Try known locations first
    for pattern in known.get(exe, []):
        matches = glob.glob(pattern)
        if matches:
            subprocess.Popen([matches[0]])
            return f"Opened {app_name}"

    # Search common install dirs one level deep
    search_bases = [
        os.path.join(local, "Programs"),
        local,
        appdata,
        "C:\\Program Files",
        "C:\\Program Files (x86)",
    ]
    for base in search_bases:
        if not os.path.isdir(base):
            continue
        direct = os.path.join(base, exe)
        if os.path.exists(direct):
            subprocess.Popen([direct])
            return f"Opened {app_name}"
        try:
            for folder in os.listdir(base):
                candidate = os.path.join(base, folder, exe)
                if os.path.exists(candidate):
                    subprocess.Popen([candidate])
                    return f"Opened {app_name}"
        except PermissionError:
            continue

    # Last resort — Windows shell
    subprocess.Popen(f'start "" "{exe}"', shell=True)
    return f"Tried to open {app_name} — if nothing happened it may not be installed"


def _close_window(title: str) -> str:
    all_windows = gw.getAllWindows()
    matches = [w for w in all_windows if title.lower() in w.title.lower() and w.title.strip()]
    if not matches:
        return f"No window found matching '{title}'"
    closed = []
    for w in matches:
        try:
            w.close()
            closed.append(w.title)
        except Exception:
            pass
    return f"Closed: {', '.join(closed)}" if closed else f"Could not close '{title}'"


def _get_system_stats() -> dict:
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    return {
        'cpu_percent': psutil.cpu_percent(interval=0.5),
        'memory_used_gb': round(mem.used / 1024**3, 1),
        'memory_total_gb': round(mem.total / 1024**3, 1),
        'memory_percent': mem.percent,
        'disk_used_gb': round(disk.used / 1024**3, 1),
        'disk_total_gb': round(disk.total / 1024**3, 1),
        'disk_percent': disk.percent,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


# ── Claude tool definitions ──────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_running_processes",
        "description": (
            "Get currently running processes. Call this when the user asks about "
            "processes, memory usage, CPU usage, or what programs are running."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sort_by": {
                    "type": "string",
                    "enum": ["memory", "cpu"],
                    "description": "Sort by 'memory' (default) or 'cpu'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of processes to return (default 20).",
                },
            },
        },
    },
    {
        "name": "get_open_windows",
        "description": (
            "Get currently open window titles. Call this when the user asks about "
            "open windows, running apps, or what is visible on screen."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "open_app",
        "description": "Open an installed application on the computer. Call this when the user asks to open or launch an app like Discord, Spotify, Steam, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "The name of the app to open, e.g. 'Discord', 'Spotify', 'Steam'.",
                },
            },
            "required": ["app_name"],
        },
    },
    {
        "name": "close_window",
        "description": "Close an open window or app by name. Call this when the user asks to close a window, tab, or application like Chrome, Discord, Spotify, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Part of the window title to match, e.g. 'Chrome', 'Discord', 'Spotify'.",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "open_url",
        "description": "Open a website or URL in the browser. Call this when the user asks to open a website, search something, or go to a URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to open, e.g. 'google.com' or 'https://youtube.com'.",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "get_system_stats",
        "description": (
            "Get overall system stats: CPU%, RAM, and disk usage. Call this when the "
            "user asks about system performance, health, or resource usage."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _execute_tool(name: str, tool_input: dict) -> str:
    if name == "open_app":
        data = _open_app(tool_input["app_name"])
    elif name == "close_window":
        data = _close_window(tool_input["title"])
    elif name == "open_url":
        data = _open_url(tool_input["url"])
    elif name == "get_running_processes":
        data = _get_running_processes(
            sort_by=tool_input.get("sort_by", "memory"),
            limit=tool_input.get("limit", 20),
        )
    elif name == "get_open_windows":
        data = _get_open_windows()
    elif name == "get_system_stats":
        data = _get_system_stats()
    else:
        data = {"error": f"Unknown tool: {name}"}
    return json.dumps(data)


def _ask_jarvis(messages: list[dict], voice_mode: bool) -> str:
    """Send messages to Claude and return the text response."""
    system = SYSTEM_PROMPT
    if voice_mode:
        system += " Keep responses short since they will be spoken aloud — 1 to 3 sentences max."

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=512 if voice_mode else 1024,
            system=system,
            tools=TOOLS,
            messages=messages,
            thinking={"type": "adaptive"},
        )
        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if not tool_uses:
            return "".join(b.text for b in response.content if b.type == "text")

        tool_results = []
        for b in tool_uses:
            result = _execute_tool(b.name, b.input)
            tool_results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
        messages.append({"role": "user", "content": tool_results})


# ── Voice helpers ────────────────────────────────────────────────────────────

SAMPLE_RATE = 16000
CHANNELS = 1

def _record_audio(seconds: float, device=None) -> np.ndarray:
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                   channels=CHANNELS, dtype='int16', device=device)
    sd.wait()
    return audio


def _find_input_device(name_fragment: str):
    """Return the index of an input-capable device whose name contains
    name_fragment (case-insensitive), preferring the MME host API. None if not found."""
    try:
        hostapis = sd.query_hostapis()
        matches = [(i, d) for i, d in enumerate(sd.query_devices())
                   if d['max_input_channels'] > 0 and name_fragment.lower() in d['name'].lower()]
        for i, d in matches:
            if 'MME' in hostapis[d['hostapi']]['name']:
                return i
        return matches[0][0] if matches else None
    except Exception:
        return None


def _audio_to_wav_bytes(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    return buf.getvalue()


def _transcribe(audio: np.ndarray) -> str | None:
    recognizer = sr.Recognizer()
    wav_bytes = _audio_to_wav_bytes(audio)
    audio_data = sr.AudioData(wav_bytes, SAMPLE_RATE, 2)
    try:
        return recognizer.recognize_google(audio_data)
    except (sr.UnknownValueError, sr.RequestError):
        return None


def _speak(engine: pyttsx3.Engine, text: str):
    engine.say(text)
    engine.runAndWait()


def _say(engine: pyttsx3.Engine, text: str):
    """Print what Jarvis says as an on-screen subtitle, then speak it aloud."""
    print(f"Jarvis: {text}")
    _speak(engine, text)


# ── Chat loop (text) ─────────────────────────────────────────────────────────

def chat():
    print("\nJARVIS online. Ask me anything about your system. Type 'exit' to quit.\n")
    messages: list[dict] = []

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nJarvis: Signing off.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "bye"):
            print("Jarvis: Goodbye.")
            break

        messages.append({"role": "user", "content": user_input})

        response_started = False
        with client.messages.stream(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
            thinking={"type": "adaptive"},
        ) as stream:
            for text in stream.text_stream:
                if not response_started:
                    print("\nJarvis: ", end="", flush=True)
                    response_started = True
                print(text, end="", flush=True)
            response = stream.get_final_message()

        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if tool_uses:
            tool_results = []
            for b in tool_uses:
                result = _execute_tool(b.name, b.input)
                tool_results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
            messages.append({"role": "user", "content": tool_results})

            reply = _ask_jarvis(messages, voice_mode=False)
            print(f"\nJarvis: {reply}\n")
        else:
            print("\n")


# ── Voice loop ───────────────────────────────────────────────────────────────

def voice_chat():
    engine = pyttsx3.init()
    engine.setProperty('rate', 175)

    mic = _find_input_device(VOICE_INPUT_DEVICE) if VOICE_INPUT_DEVICE else None
    if VOICE_INPUT_DEVICE and mic is None:
        print(f"(Note: mic '{VOICE_INPUT_DEVICE}' not found — using the default input device.)")

    print()
    _say(engine, f"Voice mode active, {USER_NAME}. Say Hey Jarvis to give me a command.")
    print("(Say 'Hey Jarvis' to wake me up. Say 'Hey Jarvis end' or press Ctrl+C to quit.)\n")

    messages: list[dict] = []

    while True:
        try:
            # Listen for wake word in 3-second chunks
            print("Listening...", end="\r")
            audio = _record_audio(3, device=mic)
            text = _transcribe(audio)

            if not text:
                continue

            text_lower = text.lower()
            if "jarvis" not in text_lower:
                continue

            # Strip wake phrase and use anything after it as the command
            for wake in ("hey jarvis", "ok jarvis", "jarvis"):
                text_lower = text_lower.replace(wake, "").strip()

            if text_lower:
                command = text_lower
            else:
                # Wake word only — listen again for the actual command
                _say(engine, "Yes?")
                audio = _record_audio(6, device=mic)
                command = _transcribe(audio)
                if not command:
                    continue

            print(f"\nYou: {command}")

            if command.lower().strip(" .,!?'\"") in ("stop", "exit", "quit", "goodbye", "bye", "end", "and"):  # "and" = common speech-to-text mishearing of "end"
                _say(engine, f"Ending session. Goodbye, {USER_NAME}!")
                break

            messages.append({"role": "user", "content": command})
            reply = _ask_jarvis(messages, voice_mode=True)

            _say(engine, reply)

        except KeyboardInterrupt:
            print()
            _say(engine, "Signing off.")
            break


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\nJARVIS — choose a mode:")
    print("  1. Text chat")
    print("  2. Voice (say 'Hey Jarvis')")
    choice = input("\nEnter 1 or 2: ").strip()

    if choice == "2":
        voice_chat()
    else:
        chat()
