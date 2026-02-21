import wave
import tempfile
import threading
import numpy as np

try:
    import sounddevice as sd
    SOUNDDEVICE_AVAILABLE = True
except Exception:
    SOUNDDEVICE_AVAILABLE = False

SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"


def list_input_devices() -> list[tuple[int, str]]:
    """Return (index, name) for every device that has at least one input channel."""
    if not SOUNDDEVICE_AVAILABLE:
        return []
    devices = sd.query_devices()
    return [
        (i, dev["name"])
        for i, dev in enumerate(devices)
        if dev.get("max_input_channels", 0) > 0
    ]


def list_output_devices() -> list[tuple[int, str]]:
    """Return (index, name) for every device that has at least one output channel."""
    if not SOUNDDEVICE_AVAILABLE:
        return []
    devices = sd.query_devices()
    return [
        (i, dev["name"])
        for i, dev in enumerate(devices)
        if dev.get("max_output_channels", 0) > 0
    ]


class AudioRecorder:
    def __init__(self):
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream = None
        self.is_recording = False

    def start(self, device=None) -> None:
        """Begin capturing audio.

        Parameters
        ----------
        device:
            sounddevice device index (int) or None for the system default.
        """
        if not SOUNDDEVICE_AVAILABLE:
            raise RuntimeError("sounddevice is not installed.")
        with self._lock:
            self._frames = []
            self.is_recording = True
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            callback=self._callback,
            blocksize=1024,
            device=device,
        )
        self._stream.start()

    def _callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if self.is_recording:
            with self._lock:
                self._frames.append(indata.copy())

    def stop(self) -> str | None:
        """Stop recording and return path to a temporary WAV file, or None if no audio."""
        self.is_recording = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        with self._lock:
            frames = list(self._frames)
            self._frames = []

        if not frames:
            return None

        audio = np.concatenate(frames, axis=0)
        # Drop recordings shorter than ~0.3 seconds (likely accidental)
        if len(audio) < SAMPLE_RATE * 0.3:
            return None

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        with wave.open(tmp.name, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)  # int16 = 2 bytes
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio.tobytes())
        return tmp.name
