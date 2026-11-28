# Relay

[![ci](https://github.com/vanatorr/relay/actions/workflows/ci.yml/badge.svg)](https://github.com/vanatorr/relay/actions/workflows/ci.yml)

**A voice for your Claude Code sessions. They tell you what they need, you answer out loud, and your words land in the right one.**

```
Relay:  Payments fix. Tests pass. Want me to commit?
You:    Tell payments fix to commit and push.
Relay:  (a soft tick, then it sends)
```

If you run several Claude Code sessions at once, you know the cost: one finished ten minutes ago, one is waiting for a yes, and you only find out when you switch windows. Relay reads out what each session reports, in one short sentence, and delivers what you say to the session you mean. Speech recognition and the default voice run on your machine. No account, no API key, no audio leaves your computer.

- **Hands off the keyboard.** Hold a key, talk, let go. Delivery takes about two seconds.
- **Any language.** Speech recognition is Whisper, which understands about 100 languages and detects the language each time you talk.
- **Never guesses.** If it is not clear which session you meant, Relay asks "Which one?" and waits. It does not pick for you.
- **Each session has a name and a number.** Names are only spoken when the speaker changes, so a conversation sounds like one.
- **Never loses an update.** If a session finishes while you are talking, Relay waits until you are done, then tells you.
- **Dictation anywhere.** A second key types what you say into whatever app has focus, without sending it to a session.

## Two versions

| | Status | Platforms | Start with |
|---|---|---|---|
| **v0** (`relay.py`) | Tested, used daily | Windows | [Quick start (v0)](#quick-start-v0-windows) |
| **v1** (`relayvox` package) | **Preview.** Adds wake word, automatic end of speech and spoken status questions. | Windows, macOS, Linux | [Relay v1](#relay-v1-preview) |

v1 is tested in CI on Windows, macOS and Linux, but it has not yet been run end to end on real hardware. macOS and Linux are untested on hardware. If you want something that works today, use v0.

## Quick start (v0, Windows)

You need the Claude desktop app (Code tab), Python 3.10 or newer, and a microphone. An NVIDIA GPU makes speech recognition near-instant. The CPU works too.

```powershell
git clone https://github.com/vanatorr/relay
cd relay
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python install_hooks.py
.\.venv\Scripts\python relay.py
```

`install_hooks.py` adds three small hooks to `~/.claude/settings.json` and keeps a backup at `settings.json.before-relay`. Every session you open after that can talk. You will hear "Relay is on." when it starts. The Whisper model (about 1.6 GB) downloads once on first run.

For a better voice, install Kokoro (local and free):

```powershell
.\.venv\Scripts\python -m pip install kokoro-onnx
```

Download `kokoro-v1.0.onnx` and `voices-v1.0.bin` from the [kokoro-onnx releases](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0) into a `models` folder next to `relay.py`, then add `{ "tts": "kokoro" }` to a `config.json`.

### Everyday use (v0)

| Key | What it does |
|---|---|
| Ctrl+Shift+Space | Talk to a session. Press, speak, press again to send. |
| Ctrl+Alt+D | Dictate into whatever app has focus, without sending. |
| Ctrl+Alt+X | Stop speaking, or cancel a message before it sends. |
| Ctrl+Alt+N | Mute or unmute. |

What you can say:

- **"Tell payments fix to commit it and push."** One message to that session. A close match on the name is enough.
- **"Talk to docs site."** Everything you say goes there until you switch.
- **Anything else** goes to the session on screen, or the one you last talked to.
- **"Auto switch on"** or **"auto switch off"** (also "turn off auto switch") changes whether the speaking session's chat opens, until Relay restarts.
- **"List sessions"** reads out what is open. **"Be quiet"** stops speech.

When the target changes, Relay says "To payments fix." first, so a wrong target is heard before it is sent, with a moment to cancel.

### Settings (v0)

Put only what you want to change in `config.json` next to `relay.py`. Defaults are in `config.example.json`. Restart Relay after changing them.

```json
{ "tts": "kokoro", "kokoro_voice": "bm_george", "mic": "part of your mic's name", "hotkeys": { "talk": "f8" } }
```

| Setting | What it does |
|---|---|
| `tts` | `"kokoro"` (local neural voice, recommended), `"windows"` (built in, offline), `"edge"` (free Microsoft voices, online) or `"elevenlabs"` (needs `ELEVENLABS_API_KEY` in a `.env` file). |
| `kokoro_voice`, `kokoro_speed` | Kokoro voice (`bm_george`, `bm_lewis`, `bf_emma`, `af_heart`, `ff_siwis` for French, ...) and speed. |
| `sentence_pause_ms`, `paragraph_pause_ms` | Silence between spoken sentences (default 280) and after a paragraph, list or heading (default 600). Each sentence is synthesized separately, so the next one is ready while the current one plays. |
| `auto_switch` | `true` by default. When a session starts speaking, open its chat so you see what it is talking about. Skipped when that chat is already on screen, while you are recording, and for replays. Say "auto switch off" or "auto switch on" to change it until Relay restarts. |
| `auto_switch_only_when_claude_in_front` | `true` by default: only switch while the Claude window is in front, so another app never loses focus. |
| `mic` | Part of your microphone's name. Quiet mics are boosted automatically. |
| `whisper_model` | Default `large-v3-turbo`. Use `small` on slower PCs. |
| `whisper_language` | `null` detects the language each time. `"en"` or `"fr"` locks it. |
| `speak_when_claude_focused` | `true` by default. `false` plays a chime instead while the Claude window is in front. |
| `cancel_window_sec` | How long you have to cancel before a message sends. |
| `hotkeys` | Any key, or ctrl/alt/shift/win plus a key. |
| `cuda_dll_dir` | A folder with `cublas64_12.dll` and cuDNN 9, if Whisper cannot find your CUDA libraries. |

To run it in the background use `.\start-relay.ps1`. To stop it, `.\stop-relay.ps1`.

## Relay v1 (preview)

v1 is a rewrite in `src/relayvox`. It needs Python 3.11 or newer. It uses single-key push-to-talk instead of chords, asks a numbered question when the target is unclear, answers status questions out loud, and can listen for a wake word. Run it from a fresh clone, or in the same folder as v0 (but not both at once).

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e .
relayvox models              # download the Kokoro voice and the speech models now
relayvox install-hooks --dry-run   # show what would change in settings.json
relayvox install-hooks       # apply it, with a timestamped backup
relayvox doctor              # one ok / warn / fail line per check
relayvox run
```

`install-hooks` also removes v0's hook entries, so the two versions do not both fire. `relayvox uninstall-hooks` removes v1's.

### Everyday use (v1)

| Key | Windows / Linux | macOS | What it does |
|---|---|---|---|
| Talk | Right Ctrl | Right Option | Hold, speak, release. Double-tap to keep the mic open, tap again to send. |
| Dictate | Right Alt | Right Command | Hold, speak, release. Types into the focused app and sends nothing. |
| Stop | Ctrl+Alt+X | Ctrl+Alt+X | Stops speech and clears the queue. Also cancels a send before it goes out. |

A quick tap of the talk key also stops speech or cancels a send. Esc discards a recording. All keys are configurable under `[keys]` (`keys.talk`, `keys.dictate`, `keys.stop`, `keys.mouse` for a mouse side button). Set one to `""` to turn it off. If your keyboard has no Right Ctrl, `f13` to `f24` and `pause` work, and `relayvox doctor --keys` checks that your keys are being seen.

What you can say:

| Say | What happens |
|---|---|
| "Tell payments fix to commit it" / "number two, ship it" | Sent to that session. |
| "Talk to docs site" / "detach" | Everything goes there until you detach. |
| "Status" / "what's everyone doing?" | A spoken summary. Sessions that need you come first. Answered locally, nothing is sent. |
| "What did payments fix say?" / "recap docs site" | Reads that session's latest report. |
| "Repeat that" | Replays the last thing Relay said. |
| "Call number two docs" | Names session 2 "docs" for good, so "tell docs ..." goes straight there. |
| "Be quiet" / "mute" | Stops speech / mutes. |

You can ask the same questions from a terminal with `relayvox ask "status"`.

### Commands

| Command | What it does |
|---|---|
| `relayvox run` / `stop` | Start Relay (one instance at a time) or stop it. `relayvox-bg` is the same program without a console window. |
| `status` | Sessions, mic, voice and mute state. |
| `doctor [--keys]` | Checks the setup. `--keys` is a live key test. |
| `models [--wake]` | Downloads the voice and speech models now. `--wake` adds the wake-word models. |
| `voices` | Lists the voices of the current voice engine. |
| `mics` | Lists capture devices. |
| `say TEXT` | Plays text with the current voice. |
| `mute [on\|off]` | Mutes or unmutes. |
| `alias N NAME` | Names session N. |
| `ask TEXT` | Asks Relay a read-only question as if spoken. |
| `install-hooks` / `uninstall-hooks` | Adds or removes the Claude Code hooks. `--dry-run` shows the diff first. |
| `config` | Writes a commented `config.toml` if missing and prints its path. |
| `ptt down\|up\|toggle` | Push-to-talk from outside, for compositor key binds on Wayland. |

### Wake word (off by default)

Turn it on with `[wake] enabled = true`. The default phrase is "hey jarvis" (openWakeWord). There is no pretrained "Relay" or "Claude" model, but you can train your own and set `wake.model` to its `.onnx` path. The pretrained wake-word models are licensed CC BY-NC-SA 4.0, so Relay does not bundle them. `relayvox models --wake` downloads them only when you ask.

With the wake word on, the microphone is open all the time (macOS shows its mic indicator), and audio is processed on your machine only. Relay ends your message after a pause (`listen.end_silence_ms`, 1000 by default). If you pause a lot while thinking, set `listen.end = "word"` and finish with "send it" or "over". Say "cancel" or "never mind" to discard. The push-to-talk keys keep working alongside it.

### Status questions and the brain (off by default)

"Status", "recap" and "repeat" are answered from local data with no model. The optional brain (`[brain] enabled = true`) handles open questions such as "what are you all stuck on?" by calling the `claude` CLI with your existing Claude Code login. It can suggest an action, but nothing is sent to a session until you say "yes".

## How routing works

Relay sends a message only when exactly one session clearly matches. Otherwise it asks.

It matches on identity, in this order: a number ("two"), a recency phrase ("the last one", "the one that needs me"), an alias, the session title, then the project folder name. What a session recently said is only a hint, never enough to send on its own. A session that talks about a topic is often not the one working on it, so Relay does not score by content.

With no target ("ship it"), the message goes to the session you attached with "talk to", then the one you last sent to, then the only live session. If none of those applies, Relay asks.

When it asks ("Which one: 1, infra, or 3, docs site?"), it holds your words for 60 seconds. Answer with a number or a name. "Neither", a wrong answer or silence drops the message and Relay says "Not sent."

Before every send there is a short cancel window (`routing.cancel_sec`, 0.6 s by default). Tap the talk key, press Esc, or say the cancel word to stop it.

Known gap: the Claude desktop app does not say which session is on screen, so v1 cannot send to "the one in front". v0 can.

## Privacy and safety

- **Local first.** Speech recognition (Whisper) and the default voice (Kokoro `bm_george`) run on your machine. The only network use is the one-time model download, plus any cloud voice (`edge`, `elevenlabs`) you choose to switch on. There is no telemetry.
- **It only listens when you tell it to.** The microphone opens while you hold the talk key, or after the wake word if you turned that on.
- **Nothing on the network can type into a session.** Hooks send events to Relay on `127.0.0.1` with a per-run token. The message types are `event`, `say`, `status`, `ptt`, `mute` and `stop`. None delivers text. The hook gives up after about half a second, so it cannot slow Claude Code down.
- **Session text is data.** What a session says is spoken or summarized, never acted on. In v1 the brain has no tools, and its suggestions need your spoken "yes".
- **Logs are plain.** By default they record event types, session names and timings, not what you or the sessions said (`log.transcripts = false`).
- Hook installers edit `settings.json` only after making a backup, and never touch hooks that are not theirs.

## Config (v1)

Spoken sentences are separated by `speech.sentence_pause_ms` (default 280) of silence.

`relayvox config` writes a commented `config.toml` and prints where it is. Change only what you need. Any setting can also be set from the environment as `RELAYVOX_<SECTION>_<KEY>`, for example `RELAYVOX_TTS_VOICE=bm_lewis`. Unknown keys produce a warning naming the closest match.

```toml
[tts]
voice = "bm_george"

[audio]
mic = "part of your microphone's name"

[keys]
talk = "f8"
stop = "ctrl+alt+x"
```

| Section | Key settings (default) |
|---|---|
| `[keys]` | `talk` and `dictate` (`"auto"`, the OS defaults above), `stop` (`ctrl+alt+x`), `mouse` (off), `latch_auto_end` (false). |
| `[speech]` | `max_sentences` (2), `max_chars` (240), `volume` (0.6), `speak_when_claude_focused` (true). |
| `[tts]` | `engine` (`kokoro`; also `elevenlabs`, `edge`, `system`), `voice` (`bm_george`), `voice_fr` (`ff_siwis`), `speed` (1.0), `model_dir`. |
| `[audio]` | `mic` (system default), `max_gain` (20). |
| `[stt]` | `model` (`auto`), `language` (detect), `vocabulary` (words Whisper should expect). |
| `[wake]` | `enabled` (false), `model` (`hey_jarvis`), `threshold` (0.5). |
| `[listen]` | `end` (`silence`), `end_silence_ms` (1000), `end_words`, `cancel_words`. |
| `[routing]` | `cancel_sec` (0.6). |
| `[brain]` | `enabled` (false), `model` (`haiku`). |
| `[log]` | `transcripts` (false). |

Cloud voices are opt-in. Persian and other languages without a local voice are spoken in English unless you set `tts.persian = "edge"` (needs `pip install -e ".[edge]"`). ElevenLabs reads its key from `ELEVENLABS_API_KEY` in the environment or a `.env` in the config folder.

On an NVIDIA GPU, `pip install -e ".[cuda]"` adds the CUDA libraries Whisper needs on Windows and Linux.

## How it works

1. Claude Code hooks (`Stop`, `Notification`, and the session's wrap-up report) call a tiny script. It forwards the event to Relay on `127.0.0.1` and gives up quickly if Relay is not running.
2. Relay picks one line to say: the session's own summary and question when there is one, otherwise the start of its last message, without code, links or file paths. Updates are queued so they never talk over you.
3. When you talk, Whisper turns your voice into text on your machine. Relay works out the target, opens that session through the desktop app's `claude://` link, types your words and presses Enter, then restores your clipboard and window. (v1 has a tmux path for sessions that run in a terminal.)

Design notes for v1 are in [DESIGN.md](DESIGN.md).

## Platform status

| Platform | v0 | v1 |
|---|---|---|
| Windows | Tested, daily use | Tested in CI. Not yet run end to end on hardware. |
| macOS | Not supported | Tested in CI. Untested on hardware. Needs Input Monitoring and Accessibility permissions. |
| Linux (X11) | Not supported | Tested in CI. Untested on hardware. |
| Linux (Wayland) | Not supported | Tested in CI. Untested on hardware. Bind keys in your compositor to `relayvox ptt`. |

## Uninstall

v0:

```powershell
.\stop-relay.ps1
.\.venv\Scripts\python install_hooks.py --remove
```

v1:

```bash
relayvox stop
relayvox uninstall-hooks
pip uninstall relayvox
```

Then delete the folder. Models and config live in your user data and config folders (`relayvox config` prints the config path).

Relay is an unofficial tool. It relies on how the Claude desktop app behaves today, so an app update may break delivery until Relay catches up. Questions or reports: open an issue, or write to info@delam.ai.

## License

MIT
