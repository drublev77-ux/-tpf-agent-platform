"""Speech recognition in a process of its own: `python -m tpf.tools.speech <wav> [language]` prints the words.

The model needs most of the worker's memory. Run inside the worker, a long clip pushed the whole container
past its limit and the kernel killed the worker with every task it held; the task then came back after each
restart and killed it again. In a child process that asks the kernel to kill it first, running out of memory
costs only the transcript."""
from __future__ import annotations

import sys

SPEECH_MODEL = "small"
NOT_INSTALLED = 3          # exit code: faster-whisper is not installed here


def _kill_me_first() -> None:
    try:
        with open("/proc/self/oom_score_adj", "w") as f:   # raising one's own score needs no privileges
            f.write("1000")
    except OSError:
        pass


def transcribe(wav: str, language: str | None = None) -> str:
    import wave

    import numpy as np
    from faster_whisper import WhisperModel

    # ffmpeg has already written 16 kHz mono 16-bit sound; handing the samples over directly keeps the
    # recogniser's own audio decoder (and its version quirks) out of the way.
    with wave.open(wav, "rb") as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    model = WhisperModel(SPEECH_MODEL, device="cpu", compute_type="int8", cpu_threads=2)
    # The language of the order form is a strong hint; without one a short or noisy clip is easily misheard.
    segments, _ = model.transcribe(audio, vad_filter=True, beam_size=5, language=language)
    return " ".join(s.text.strip() for s in segments).strip()


def main(argv: list[str]) -> int:
    _kill_me_first()
    try:
        text = transcribe(argv[0], argv[1] if len(argv) > 1 and argv[1] else None)
    except ImportError:
        return NOT_INSTALLED
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
