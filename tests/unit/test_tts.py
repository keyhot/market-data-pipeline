"""Local Piper TTS with an injected subprocess runner — tests never invoke the
binary. Must degrade gracefully: on any failure it returns None and the stream
keeps running silently, never raising."""

from pathlib import Path

from director.tts import synthesize


def test_synthesize_invokes_the_runner_with_voice_and_returns_path(tmp_path):
    calls = []

    def fake_runner(cmd, **kw):
        calls.append(cmd)
        # pretend Piper wrote a wav to the -f output path
        out = cmd[cmd.index("-f") + 1]
        Path(out).write_bytes(b"RIFF")

        class R:
            returncode = 0

        return R()

    out = synthesize("hi", "en_US-amy", tmp_path / "l.wav", runner=fake_runner)
    assert out == tmp_path / "l.wav" and out.exists()
    assert any("amy" in part for part in calls[0])  # voice threaded into the command


def test_synthesize_returns_none_when_piper_is_missing(tmp_path):
    def failing_runner(cmd, **kw):
        raise FileNotFoundError("piper not installed")

    assert synthesize("hi", "v", tmp_path / "x.wav", runner=failing_runner) is None


def test_synthesize_returns_none_on_nonzero_exit(tmp_path):
    def bad_runner(cmd, **kw):
        class R:
            returncode = 1

        return R()

    assert synthesize("hi", "v", tmp_path / "x.wav", runner=bad_runner) is None


def test_synthesize_returns_none_when_no_output_file_produced(tmp_path):
    # Runner "succeeds" but writes nothing — treat as failure, not a phantom path.
    def empty_runner(cmd, **kw):
        class R:
            returncode = 0

        return R()

    assert synthesize("hi", "v", tmp_path / "x.wav", runner=empty_runner) is None
