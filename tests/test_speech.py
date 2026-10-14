"""Speech recognition runs in a process of its own: when it dies (out of memory), the worker lives on."""
from __future__ import annotations

import sys

from tpf.tools import video


async def _fake_recogniser(monkeypatch, tmp_path, code: str) -> None:
    script = tmp_path / "speech_child.py"
    script.write_text(code)
    monkeypatch.setattr(video, "SPEECH_CMD", [sys.executable, str(script)])


async def test_the_recogniser_being_killed_costs_only_the_transcript(monkeypatch, tmp_path):
    await _fake_recogniser(monkeypatch, tmp_path, "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n")
    text, note = await video._transcribe(str(tmp_path / "a.wav"), "ru")
    assert text == "" and "signal 9" in note and "memory" in note


async def test_the_words_come_back_from_the_recogniser(monkeypatch, tmp_path):
    await _fake_recogniser(monkeypatch, tmp_path, "import sys\nsys.stdout.write(' привет, нужен сайт \\n')\n")
    assert await video._transcribe(str(tmp_path / "a.wav"), "ru") == ("привет, нужен сайт", "")


async def test_no_recogniser_installed_is_said_plainly(monkeypatch, tmp_path):
    await _fake_recogniser(monkeypatch, tmp_path, "import sys\nsys.exit(3)\n")
    assert (await video._transcribe(str(tmp_path / "a.wav")))[1] == "speech recognition is not installed on this worker"


def test_the_recogniser_asks_to_be_killed_first(tmp_path, monkeypatch):
    from tpf.tools import speech
    monkeypatch.setattr(speech, "transcribe", lambda wav, lang: open("/proc/self/oom_score_adj").read().strip())
    import io
    out = io.StringIO()
    monkeypatch.setattr(speech.sys, "stdout", out)
    assert speech.main(["x.wav", "ru"]) == 0 and out.getvalue() == "1000"
