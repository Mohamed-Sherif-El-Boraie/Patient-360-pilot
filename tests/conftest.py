import json
from pathlib import Path

import pytest

SAMPLES = Path(__file__).parent / "fixtures" / "samples"


@pytest.fixture(scope="session")
def sample():
    """Load a sample payload in the Backend contract: sample("urgent_mild") returns a new dict each call."""
    def load(name: str):
        return json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    return load
