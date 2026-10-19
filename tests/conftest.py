import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def flow():
    """Load a JSON file from the copied flow examples: flow("02-batch-critical/1-backend-input.json")."""
    def load(rel: str):
        return json.loads((FIXTURES / "flow" / rel).read_text(encoding="utf-8"))
    return load
