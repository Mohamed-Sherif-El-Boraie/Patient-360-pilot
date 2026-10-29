"""docker-entrypoint.sh: LF line endings, fails fast, migrates before loading the catalogue, then runs the command."""
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "docker-entrypoint.sh"


def test_the_entrypoint_migrates_then_loads_then_execs_the_command():
    raw = SCRIPT.read_bytes()
    assert b"\r" not in raw                                   # CRLF would break `sh` inside the Linux image
    lines = [line for line in raw.decode("utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert lines[0] == "set -eu" and lines[-1] == 'exec "$@"'
    text = "\n".join(lines)
    assert 0 <= text.index("python -m patient360.migrate") < text.index("python -m patient360.load_catalogue")
    assert "RUN_STARTUP_JOBS" in text
