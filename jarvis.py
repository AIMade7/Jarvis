import sys
import os
import glob
import json
import subprocess
import webbrowser
import numpy as np
import sounddevice as sd
import pyttsx3
import psutil
import pygetwindow as gw
from datetime import datetime
from faster_whisper import WhisperModel
import anthropic
from dotenv import load_dotenv

load_dotenv(override=True)  # loads ANTHROPIC_API_KEY from .env, overriding any empty/stale env var

# A blank ANTHROPIC_AUTH_TOKEN makes the SDK send "Authorization: Bearer " (illegal empty header)
# instead of using the API key. Drop it if empty so plain x-api-key auth is used.
if not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from environment
MODEL = "claude-opus-4-8"
USER_NAME = "AI"  # the user likes to be addressed by this name
VOICE_INPUT_DEVICE = "Yeti"  # record from this microphone (partial name match); "" = Windows default

SYSTEM_PROMPT = (
    "You are Jarvis, a friendly and knowledgeable AI assistant (powered by Claude). "
    f"The user's name is {USER_NAME} — address them as {USER_NAME} naturally (greetings, sign-offs, etc.). "
    "You can chat about anything and answer general questions — explanations, advice, ideas, coding, "
    "writing, math, trivia, recommendations — exactly like a general-purpose AI assistant. "
    "You ALSO have tools to fetch real-time data about this Windows PC (processes, open windows, system "
    "stats) and to open apps and websites. Use those tools only when the user asks about their computer "
    "or asks you to open something; for everything else, just answer conversationally. "
    "Format numbers neatly (e.g. '556 MB', '12%') and don't dump raw data — summarise it helpfully."
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

    # If app has a web version, open that instead
    browser_fallbacks = {
        "twitch": "https://www.twitch.tv",
        "netflix": "https://www.netflix.com",
        "hulu": "https://www.hulu.com",
        "youtube": "https://www.youtube.com",
        "prime": "https://www.amazon.com/prime-video",
        "amazon prime": "https://www.amazon.com/prime-video",
    }
    fallback = browser_fallbacks.get(app_name.lower())
    if fallback:
        webbrowser.open(fallback)
        return f"Opened {app_name} in browser (app not found on this PC)"

    # Last resort — Windows shell
    subprocess.Popen(f'start "" "{exe}"', shell=True)
    return f"Tried to open {app_name} — if nothing happened it may not be installed"


def _open_stream(streamer: str) -> str:
    url = f"https://www.twitch.tv/{streamer.lower().replace(' ', '')}"
    webbrowser.open(url)
    return f"Opened {streamer}'s stream on Twitch"


def _play_youtube(query: str) -> str:
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            video = info["entries"][0]
            url = f"https://www.youtube.com/watch?v={video['id']}"
            title = video.get("title", query)
            webbrowser.open(url)
            return f"Playing: {title}"
    except Exception:
        import urllib.parse
        webbrowser.open(f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}")
        return f"Opened YouTube search for: {query}"


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
        "name": "open_stream",
        "description": "Open a Twitch stream for a specific streamer. Call this when the user says 'open [name]'s stream' or 'watch [name] on Twitch'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "streamer": {
                    "type": "string",
                    "description": "The streamer's Twitch username or name, e.g. 'ninja', 'pokimane'.",
                },
            },
            "required": ["streamer"],
        },
    },
    {
        "name": "play_youtube",
        "description": "Search for and open a YouTube video. Call this when the user says 'play [video] on YouTube' or 'search YouTube for [topic]'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The video title or search query, e.g. 'lofi hip hop' or 'how to make pasta'.",
                },
            },
            "required": ["query"],
        },
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
    if name == "open_stream":
        data = _open_stream(tool_input["streamer"])
    elif name == "play_youtube":
        data = _play_youtube(tool_input["query"])
    elif name == "open_app":
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
CHUNK_MS = 80          # process audio in 80ms chunks
SILENCE_AFTER_MS = 900 # stop after 900ms of silence once speech began


def _record_fixed(seconds: float, device=None) -> np.ndarray:
    """Record a fixed number of seconds (used for wake-word listening)."""
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                   channels=CHANNELS, dtype='int16', device=device)
    sd.wait()
    return audio


def _record_dynamic(noise_floor: float, device=None, max_seconds: float = 12.0) -> np.ndarray:
    """Record until the user stops speaking. Stops after SILENCE_AFTER_MS of quiet."""
    chunk = int(SAMPLE_RATE * CHUNK_MS / 1000)
    threshold = max(noise_floor * 2.5, 40)    # speak above 2.5× background noise
    silence_needed = int(SILENCE_AFTER_MS / CHUNK_MS)
    min_speech_chunks = int(200 / CHUNK_MS)   # need at least 200ms of speech

    chunks, silent, voiced = [], 0, 0
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                        dtype='int16', blocksize=chunk, device=device) as stream:
        for _ in range(int(max_seconds * 1000 / CHUNK_MS)):
            data, _ = stream.read(chunk)
            chunks.append(data.copy())
            energy = float(np.abs(data).mean())
            if energy > threshold:
                voiced += 1
                silent = 0
            elif voiced >= min_speech_chunks:
                silent += 1
                if silent >= silence_needed:
                    break
    return np.concatenate(chunks)


def _calibrate_noise(device=None, seconds: float = 0.8) -> float:
    """Measure background noise level so the VAD threshold adapts to the room."""
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                   channels=CHANNELS, dtype='int16', device=device)
    sd.wait()
    return float(np.abs(audio).mean())


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


_whisper: WhisperModel | None = None

def _get_whisper() -> WhisperModel:
    global _whisper
    if _whisper is None:
        print("Loading Whisper model (first run only, ~145 MB)...")
        _whisper = WhisperModel("small", device="cpu", compute_type="int8")
        print("Whisper ready.\n")
    return _whisper


def _transcribe(audio: np.ndarray) -> str | None:
    audio_float = audio.flatten().astype(np.float32) / 32768.0
    segments, _ = _get_whisper().transcribe(audio_float, language="en", beam_size=5)
    text = " ".join(s.text for s in segments).strip()
    return text if text else None


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

    print("Calibrating noise floor, please stay quiet...")
    noise_floor = _calibrate_noise(device=mic)
    print(f"Noise floor: {noise_floor:.0f}  |  Speech threshold: {max(noise_floor * 2.5, 40):.0f}\n")

    _say(engine, f"Voice mode active, {USER_NAME}. Say Hey Jarvis to give me a command.")
    print("(Say 'Hey Jarvis' to wake me up. Say 'Hey Jarvis end' or press Ctrl+C to quit.)\n")

    messages: list[dict] = []

    while True:
        try:
            # Listen dynamically — waits for you to finish speaking
            print("Listening...", end="\r")
            audio = _record_dynamic(noise_floor, device=mic, max_seconds=15.0)
            text = _transcribe(audio)

            if not text:
                continue

            text_lower = text.lower()
            if "jarvis" not in text_lower:
                continue

            # Strip wake phrase — use anything said after it as the command
            for wake in ("hey jarvis", "ok jarvis", "jarvis"):
                text_lower = text_lower.replace(wake, "").strip()

            if text_lower:
                command = text_lower
            else:
                # Wake word only — listen dynamically until they finish speaking
                _say(engine, "Yes?")
                audio = _record_dynamic(noise_floor, device=mic)
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
