# Relay

**Talk to your Claude Code agents. Hear them talk back.**

You run five Claude Code sessions at once. One is fixing a bug, one is writing tests, one is waiting for your approval, and you only find out when you switch windows. Relay gives them a voice. When a session finishes, gets stuck or needs a yes, you hear it in a sentence ("Payments fix. Tests pass. Want me to commit?"). You answer out loud, from anywhere in the room, and your words land in the right session as if you'd typed them.

- **Hands off the keyboard.** Press one key, talk, and your message is delivered in about two seconds.
- **Any language.** Speak English, French, Persian, Spanish, Japanese or any of the ~100 languages Whisper understands. Relay detects it every time you talk, so you can switch from one message to the next.
- **Runs on your machine.** Speech recognition (Whisper large-v3-turbo) and the default voice (Kokoro, a natural British voice) both run locally. No account, no API key, no audio leaves your computer.
- **Knows who's talking.** Each session speaks under its own name, and the name is only said when the speaker changes, so a conversation sounds like one.
- **Never loses an update.** If an agent finishes while you're talking, Relay waits until you're done, then tells you.
- **Dictation everywhere.** A second key types what you say into any app (email, Slack, your editor) without sending it.
- **Safe by design.** Relay only reads out what sessions report and delivers what *you* say. Nothing on the network can make it type into a session.

## Set it up in five minutes

You need the Claude desktop app (Code tab), Python 3.10+ and a microphone. An NVIDIA GPU makes speech recognition near-instant, but the CPU works too.

```powershell
git clone https://github.com/vanatorr/relay
cd relay
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python install_hooks.py
.\.venv\Scripts\python relay.py
```

That's it. `install_hooks.py` connects Relay to Claude Code by adding three small hooks to `~/.claude/settings.json`, keeping a backup at `settings.json.before-relay`. Every session you open from now on can talk. You'll hear "Relay is on." when it starts, and the Whisper model (about 1.6 GB) downloads once on first run.

**For the best voice** (local, free), install Kokoro once:

```powershell
.\.venv\Scripts\python -m pip install kokoro-onnx
```

Then download `kokoro-v1.0.onnx` and `voices-v1.0.bin` from the [kokoro-onnx releases](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0) into a `models` folder next to `relay.py`, and add `{ "tts": "kokoro" }` to a `config.json`.

## Use it

| Key | What it does |
|---|---|
| Ctrl+Shift+Space | Talk to a session. Press, speak, press again to send. |
| Ctrl+Alt+D | Dictate into whatever app has focus, without sending. |
| Ctrl+Alt+X | Stop speaking, or cancel a message before it sends. |
| Ctrl+Alt+N | Mute or unmute. |

Then just talk:

- **"Tell payments fix to commit it and push."** One message to that session. Close enough on the name is fine.
- **"Talk to marketing."** Everything you say goes there until you switch.
- **Anything else** goes to the session on screen, or the one you last talked to.
- **"List sessions"** reads out what's open. **"Be quiet"** stops speech.

Whenever the target changes, Relay says "To payments fix." first, so a wrong target is heard before it sends, with a moment to cancel.

## Make it yours

Put only the settings you want to change in `config.json` next to `relay.py`. Defaults are in `config.example.json`.

```json
{ "tts": "kokoro", "kokoro_voice": "bm_george", "mic": "HyperX", "hotkeys": { "talk": "f8" } }
```

| Setting | What it does |
|---|---|
| `tts` | The voice: `"kokoro"` (local neural voice, recommended), `"windows"` (built in, offline), `"edge"` (free Microsoft neural voices, online) or `"elevenlabs"` (needs `ELEVENLABS_API_KEY` in a `.env` file). |
| `kokoro_voice`, `kokoro_speed` | Kokoro voice (`bm_george`, `bm_lewis`, `bf_emma`, `af_heart`, `ff_siwis` for French, ...) and speed. |
| `mic` | Part of your microphone's name, for example `"HyperX"`. Quiet mics are boosted automatically. |
| `whisper_model` | Default `large-v3-turbo`. Use `small` on slower PCs. |
| `whisper_language` | `null` detects the language each time; `"en"` or `"fr"` locks it. |
| `speak_when_claude_focused` | `true` by default. `false` plays a chime instead while the Claude window is in front. |
| `cancel_window_sec` | How long you have to cancel before a message sends. |
| `hotkeys` | Any key, or ctrl/alt/shift/win plus a key. |
| `cuda_dll_dir` | A folder with `cublas64_12.dll` and cuDNN 9, if Whisper can't find your CUDA libraries. |

Restart Relay after changing settings. To run it in the background, use `.\start-relay.ps1`; to stop it, `.\stop-relay.ps1`.

## How it works

1. Claude Code hooks call `hook.py` when a session finishes, needs approval or posts a summary. It forwards the event to Relay on `127.0.0.1` and gives up after half a second, so it can never slow Claude down.
2. Relay picks one line to say: the session's own summary and question when there is one, otherwise the start of its last message, without code, links or file paths.
3. When you talk, Whisper turns it into text on your machine. Relay works out the target, opens that session through the desktop app's `claude://` link, types your words and presses Enter, then puts your clipboard and window back.

## Relay v1 (`relayvox`, preview)

The rewrite lives in `src/relayvox` and runs next to the script above. It adds push-to-talk on a single key, a numbered "Which one?" question instead of guessing, spoken status and recap answers, aliases, and optional hands-free mode.

```powershell
.\.venv\Scripts\python -m pip install -e .
relayvox config          # writes a commented config.toml and prints its path
relayvox install-hooks --dry-run
relayvox install-hooks   # keeps a timestamped backup of settings.json
relayvox doctor          # one ok / warn / fail line per check
relayvox run
```

- **Talk:** hold Right Ctrl (Right Option on macOS), speak, release. A tap stops speech or cancels a send in its 0.6 s window. Double-tap to latch hands-free.
- **Ask Relay:** "status", "recap payments", "where's docs site", "repeat". Answered locally, nothing is sent to a session.
- **Who gets it:** a number or name ("2, ship it", "tell payments to ship it"), the attached session, then the last session you sent to. If that is not clear, Relay asks "Which one?" and holds your words for 60 s.
- **Optional, off by default:** the wake word (`[wake] enabled`) and the brain (`[brain] enabled`, which uses the `claude` CLI and your Claude Code login). A brain suggestion is only sent after you say "yes".

| OS | Status |
|---|---|
| Windows | Tested |
| macOS | Untested on hardware |
| Linux (X11, Wayland via `relayvox ptt`) | Untested on hardware |

Not in v1 yet: sending to the session that is on screen (the app does not say which one it is), `relayvox voices`, and `relayvox models` (models download on first use).

## Uninstall

```powershell
.\stop-relay.ps1
.\.venv\Scripts\python install_hooks.py --remove
```

Then delete the folder.

Relay is an unofficial tool. It relies on how the Claude desktop app behaves today, so an app update may break delivery until Relay catches up.

## License

MIT
