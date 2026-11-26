# Relay

Voice for Claude Code on Windows. Your sessions talk to you, and you talk back to any session by name.

- **Sessions speak.** When a session finishes a turn, needs your approval or has a question, Relay says its name and a short summary out loud ("Payments fix. I fixed the button. Want me to commit it?"). If the Claude window is already in front, you get a soft chime instead.
- **You talk back.** Press a hotkey and talk for as long as you like. Local Whisper turns it into text, and Relay sends it to the right session as if you'd typed it. "Tell payments fix, commit it" sends to the session named *payments fix*.
- **Dictation anywhere.** A second hotkey types what you say into whatever app has focus, without sending it.
- **Local by default.** Speech-to-text runs on your PC (faster-whisper, large-v3-turbo) and the default voice is the built-in Windows one. Nothing leaves your machine unless you choose the Edge or ElevenLabs voice.

Sessions never act on their own: Relay only reads out what they report and delivers what you say.

## Requirements

- Windows 10 or 11
- The Claude desktop app (Code tab). Relay opens sessions through its `claude://` links. Sessions running only in a plain terminal can speak but can't receive voice messages.
- Python 3.10 or newer
- A microphone
- Optional: an NVIDIA GPU. Whisper also runs on the CPU, just slower.

## Install

```powershell
git clone https://github.com/vanatorr/relay
cd relay
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python install_hooks.py
```

`install_hooks.py` adds three hooks to `~/.claude/settings.json` (Stop, Notification, and PostToolUse on the desktop app's wrap-up tool) and keeps a backup at `settings.json.before-relay`. Sessions you start after this will talk; restart any that are already open.

On first run, Relay downloads the Whisper model (about 1.6 GB) from Hugging Face.

## Run

```powershell
.\.venv\Scripts\python relay.py
```

This runs in a visible window that shows what Relay heard and where it sent it. You should hear "Relay is on." when it starts.

- Run it in the background instead: `.\start-relay.ps1`
- Stop it: `.\stop-relay.ps1`, or Ctrl+C in its window

Starting a second copy is safe; it exits on its own.

## Hotkeys

| Keys | What it does |
|---|---|
| Ctrl+Shift+Space | Talk to a session. Press, speak, press again to send. |
| Ctrl+Alt+D | Dictate. Press, speak, press again. Types the text where your cursor is, without pressing Enter. |
| Ctrl+Alt+X | Stop speaking. The talk key also cuts speech off. |
| Ctrl+Alt+N | Mute or unmute. |

You hear a beep when recording starts and another when it stops.

## Who your message goes to

- "Tell **payments fix**, commit it and push" sends one message to that session. Names match loosely, so close is fine.
- "Talk to **marketing**" keeps sending to that session until you switch.
- Anything else goes to the session you're attached to, then the last one that spoke, then the one on screen. If none of those fits, Relay asks "Who should I send that to?".
- "List sessions" reads out their names. "Stop" or "be quiet" stops speech.

Before sending, Relay says "To {name}." and waits 1.5 seconds. Press the talk key or Ctrl+Alt+X during that pause to cancel.

## Settings

Defaults are in `config.example.json`. To change any of them, put only the keys you want to change in a `config.json` next to it, for example:

```json
{ "tts": "edge", "hotkeys": { "talk": "ctrl+alt+t" } }
```

| Key | What it does |
|---|---|
| `tts` | `"windows"` (offline, default), `"edge"` (free neural voices, sends text to Microsoft) or `"elevenlabs"` (needs the `ELEVENLABS_API_KEY` environment variable). |
| `elevenlabs_voice_id`, `elevenlabs_model` | Which ElevenLabs voice and model to use. Flash is the fastest; Multilingual v2 sounds more natural. |
| `edge_voice` | Which Edge voice to use. |
| `whisper_model` | Default `large-v3-turbo`. Use `small` on slow PCs. |
| `whisper_language` | `null` detects the language each time; `"en"` or `"fr"` locks it. |
| `speak_when_claude_focused` | `true` speaks even when Claude is in front. |
| `cancel_window_sec` | How long the pause before sending lasts. |
| `cuda_dll_dir` | A folder with `cublas64_12.dll` and cuDNN 9, if you don't use the pip packages in `requirements.txt`. |
| `hotkeys` | Any combination of ctrl, alt, shift and win with a key. |

Restart Relay after changing settings.

## How it works

1. The hooks run `hook.py`, which forwards the event to Relay on `127.0.0.1:47811`. It times out after half a second and never blocks Claude.
2. Relay picks the line to speak: the session's own wrap-up summary and question when there is one, otherwise the first two sentences of its last message. It drops code, links and file paths, and speaks one line at a time.
3. Session names come from `~/.claude/sessions/`.
4. To deliver a message, Relay opens the session's `claude://` link, pastes your text and presses Enter. It then puts your clipboard and previous window back.

This is an unofficial tool and relies on how the desktop app behaves today, so a future app update may break delivery.

## Uninstall

```powershell
.\stop-relay.ps1
.\.venv\Scripts\python install_hooks.py --remove
```

Then delete the folder.

## License

MIT
