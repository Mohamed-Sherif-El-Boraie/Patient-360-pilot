"""The system prompts (``*.md``) and the output schemas (``schemas/*.json``) sent to the model.

Arabic is not here: AWS owns it. Edit the files, not this code. Each source has its own prompt and its own schema
(urgent and on-demand are copies, so they can change apart). The schemas are the ``output_config`` of the
``3_aws_input`` flow files.
"""
from __future__ import annotations

import json
from importlib.resources import files
from typing import Any


def _read(name: str) -> str:
    return files(__package__).joinpath(name).read_text(encoding="utf-8")


def _prompt_and_schema(name: str) -> tuple[str, dict[str, Any]]:
    return _read(f"{name}.md"), json.loads(_read(f"schemas/{name}.json"))


PROMPT_AND_SCHEMA_BY_SOURCE = {
    "urgent": _prompt_and_schema("urgent"),
    "on-demand": _prompt_and_schema("on_demand"),
    "batch": _prompt_and_schema("batch"),
    "symptom-checker": _prompt_and_schema("symptom_checker"),
}
