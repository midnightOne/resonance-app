# Resonance

A lightweight Windows desktop app for hands-free voice dictation. Hold a hotkey, speak, release — your words are transcribed via the OpenAI Whisper API and pasted directly into whatever you were typing.

## Features

- **Hold-to-record** — hold `Ctrl + Win` (configurable), speak, release to transcribe
- **Auto-paste** — result is pasted into the previously focused window automatically
- **Session history** — scrollable list of all transcriptions with per-entry Copy buttons
- **Persistent log** — transcripts saved to `~/.resonance/history.jsonl` with metadata (timestamp, app, duration)
- **Cost tracking** — running USD total based on Whisper API pricing, shown in the footer
- **Audio cues** — MP3 sounds on start / stop / error (customisable files in `whisper_flow/sounds/`)
- **Audio device selection** — choose mic input and audio output independently
- **Start with Windows** — optional registry entry for auto-launch
- **Standalone exe** — ships as a single `Resonance.exe` built with PyInstaller

## Requirements

- Windows 10/11
- Python 3.10+
- An [OpenAI API key](https://platform.openai.com/api-keys)

## Quick start

```bash
# 1. Clone
git clone https://github.com/midnightOne/resonance-app.git
cd resonance-app

# 2. Install dependencies
pip install -r requirements.txt

# 3. Add your API key
echo OPENAI_API_KEY=sk-... > .env

# 4. Run
python run.py
```

## Build standalone exe

```bat
build.bat
```

The executable lands in `dist\Resonance.exe`. Copy your `.env` file next to it before the first run.

## Configuration

Settings are saved to `~/.resonance/config.json` and can be changed at runtime via the **⚙ Settings** panel:

| Setting | Default | Description |
|---|---|---|
| API Key | *(from .env)* | OpenAI secret key |
| Base URL | `https://api.openai.com/v1` | Compatible with any OpenAI-API server |
| Model | `whisper-1` | Whisper model name |
| Hotkey | `ctrl+left windows` | Key combo to hold for recording |
| Language | `en` | ISO 639-1 code, blank = auto-detect |
| Mic / Input | System default | Recording device |
| Audio Output | System default | Device used for audio cues |
| Auto-paste | On | Paste result into the active window |
| Always on top | Off | Keep window above others |
| Start with Windows | Off | Add to HKCU Run registry key |

## Transcript log format

Each line of `~/.resonance/history.jsonl` is a JSON object:

```json
{
  "timestamp": "2026-02-20T14:32:11",
  "date": "2026-02-20",
  "time": "14:32:11",
  "text": "transcribed text here",
  "pasted": true,
  "app_process": "chrome.exe",
  "app_title": "Google Docs",
  "duration_sec": 4.2,
  "model": "whisper-1",
  "cost_usd": 0.000420
}
```

## License

MIT
