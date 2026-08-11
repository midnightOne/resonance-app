# Resonance – Architecture

Windows push-to-talk dictation app: hold a hotkey, speak, release — audio is sent
to an OpenAI-compatible transcription API and the text is pasted into the active window.

## Components (package `resonance/`)

| File | Responsibility |
|---|---|
| `app.py` | Main tkinter window, settings panel, hotkey orchestration, history, cost tracking, single-instance lock |
| `audio.py` | `AudioRecorder` (sounddevice, 16 kHz mono int16 WAV) + input/output device enumeration |
| `transcribe.py` | Single `transcribe()` call to `{base_url}/audio/transcriptions` via `requests` |
| `config.py` | Defaults + JSON config persistence in `~/.resonance/config.json`, `.env` loading for the API key |
| `sounds/` | MP3 audio cues (start/stop/error), decoded with `miniaudio` |

Entry point: `run.py` → `resonance.app.run()`. Packaged with PyInstaller (`resonance.spec`, `build.bat`).

## Threading model

- **Main thread**: tkinter event loop; drains a `queue.Queue` every 50 ms.
- **Hotkey thread**: `keyboard.hook` daemon (`HotkeyMonitor`), posts start/stop/cancel messages to the queue.
- **Worker thread**: one per transcription — stops the recorder, caches the WAV, calls the API, posts result/error.

## Key behaviors

- **Language**: settings dropdown (Auto-detect / English / Russian). Auto-detect sends no
  `language` param, so Whisper identifies the spoken language per recording; picking a
  language sends its ISO-639-1 code (`en` / `ru`).
- **Audio cache**: WAVs stored in `~/.resonance/audio_cache`, pruned by size/age; failed
  transcriptions can be retried from cache.
- **History**: JSONL log at `~/.resonance/history.jsonl` with per-call cost ($0.006/min, whisper-1).
- **Single instance**: PID lockfile at `~/.resonance/.lock`; liveness is checked via
  `OpenProcess`/`GetExitCodeProcess` (never `os.kill(pid, 0)`, which kills the process on Windows).
- **Auto-paste**: clipboard + `SetForegroundWindow` + Ctrl-V into the window focused when recording began.
- **Start with Windows**: `HKCU\...\Run` registry entry.

## External APIs

- OpenAI Audio Transcriptions API (`POST /v1/audio/transcriptions`, model `whisper-1`),
  configurable base URL for compatible providers.
