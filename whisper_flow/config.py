import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# When frozen by PyInstaller look for .env next to the .exe,
# otherwise look in the project root (next to run.py).
if getattr(sys, "frozen", False):
    _ENV_FILE = Path(sys.executable).parent / ".env"
else:
    _ENV_FILE = Path(__file__).parent.parent / ".env"

load_dotenv(_ENV_FILE)

CONFIG_DIR = Path.home() / ".resonance"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS = {
    "api_key": os.getenv("OPENAI_API_KEY", ""),
    "api_base_url": "https://api.openai.com/v1",
    "model": "whisper-1",
    "hotkey": "ctrl+left windows",
    "language": "en",
    "audio_device": None,
    "audio_output_device": None,
    "auto_paste": True,
    "always_on_top": False,
}


def load() -> dict:
    base = DEFAULTS.copy()
    # Always re-read the env var in case .env changed since startup
    base["api_key"] = os.getenv("OPENAI_API_KEY", "")

    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            for k, v in saved.items():
                if k == "api_key" and not v:
                    continue  # keep the .env value if config has empty key
                base[k] = v
        except Exception:
            pass

    return base


def save(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
