"""What a worker can get out of a client's video without watching it: still frames across its length and
the words spoken in it. ffmpeg cuts the frames; the speech is recognised on the worker itself."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("tpf.video")

MAX_SECONDS = 300          # only the first five minutes are looked at
MAX_FRAMES = 16
SPEECH_MODEL = "small"


class VideoError(RuntimeError):
    pass


@dataclass
class Watched:
    seconds: float
    frames: list[tuple[float, bytes]] = field(default_factory=list)    # (second, jpeg)
    speech: str = ""
    speech_note: str = ""       # why there is no transcript, when there is none


async def _run(*cmd: str, timeout: int = 180) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError as e:
        proc.kill()
        raise VideoError(f"{cmd[0]} took too long") from e
    return proc.returncode or 0, out, err


def _transcribe(wav: str, language: str | None = None) -> str:
    import wave

    import numpy as np
    from faster_whisper import WhisperModel      # heavy: loaded only when a video actually arrives

    # ffmpeg has already written 16 kHz mono 16-bit sound; handing the samples over directly keeps the
    # recogniser's own audio decoder (and its version quirks) out of the way.
    with wave.open(wav, "rb") as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    model = WhisperModel(SPEECH_MODEL, device="cpu", compute_type="int8")
    # The language of the order form is a strong hint; without one a short or noisy clip is easily misheard.
    segments, _ = model.transcribe(audio, vad_filter=True, beam_size=5, language=language)
    return " ".join(s.text.strip() for s in segments).strip()


async def watch(path: Path, workdir: Path, language: str | None = None) -> Watched:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise VideoError("ffmpeg is not installed on this worker")
    code, out, err = await _run("ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path), timeout=60)
    if code != 0:
        raise VideoError("the file is not a video ffmpeg can read: " + err.decode("utf-8", "replace")[-200:])
    info = json.loads(out or b"{}")
    streams = info.get("streams", [])
    if not any(s.get("codec_type") == "video" for s in streams):
        raise VideoError("the file has no video track")
    seconds = min(float(info.get("format", {}).get("duration") or 0) or MAX_SECONDS, MAX_SECONDS)
    watched = Watched(seconds)

    step = max(seconds / MAX_FRAMES, 1.0)
    frames_dir = workdir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    code, _, err = await _run(
        "ffmpeg", "-v", "error", "-y", "-t", str(MAX_SECONDS), "-i", str(path),
        "-vf", f"fps=1/{step:.3f},scale='min(1280,iw)':-2", "-frames:v", str(MAX_FRAMES), "-q:v", "5",
        str(frames_dir / "f%03d.jpg"))
    if code != 0:
        raise VideoError("could not take frames: " + err.decode("utf-8", "replace")[-200:])
    for i, f in enumerate(sorted(frames_dir.glob("f*.jpg"))):
        watched.frames.append((round(i * step + step / 2, 1), f.read_bytes()))
    if not watched.frames:
        raise VideoError("the video has no frames")

    if not any(s.get("codec_type") == "audio" for s in streams):
        watched.speech_note = "the video has no sound"
        return watched
    wav = workdir / "speech.wav"
    code, _, _ = await _run("ffmpeg", "-v", "error", "-y", "-t", str(MAX_SECONDS), "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav))
    if code != 0 or not wav.exists():
        watched.speech_note = "the sound could not be read"
        return watched
    try:
        watched.speech = (await asyncio.wait_for(asyncio.to_thread(_transcribe, str(wav), language), 900))[:12000]
        if not watched.speech:
            watched.speech_note = "nobody speaks in the video"
    except ImportError:
        watched.speech_note = "speech recognition is not installed on this worker"
    except Exception as e:  # noqa: BLE001 — frames alone still tell a lot
        log.exception("speech recognition failed")
        watched.speech_note = f"speech recognition failed ({type(e).__name__})"
    return watched
