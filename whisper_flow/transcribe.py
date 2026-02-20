import os
import requests


def transcribe(
    audio_path: str,
    api_key: str,
    api_base_url: str,
    model: str = "whisper-1",
    language: str = "",
) -> str:
    if not api_key:
        raise ValueError("API key is not configured. Open Settings to add it.")

    url = api_base_url.rstrip("/") + "/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"}

    with open(audio_path, "rb") as f:
        files = {"file": (os.path.basename(audio_path), f, "audio/wav")}
        data: dict = {"model": model}
        if language:
            data["language"] = language

        resp = requests.post(url, headers=headers, files=files, data=data, timeout=60)

    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        body = resp.text[:400]
        raise RuntimeError(f"API error {resp.status_code}: {body}") from e

    text = resp.json().get("text", "").strip()
    return text
