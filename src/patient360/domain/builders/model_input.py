"""The Bedrock request for one visit: the system prompt, the processed JSON as the user message, the output schema."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

ANTHROPIC_VERSION = "bedrock-2023-05-31"
MAX_TOKENS = 2048


def build_model_input(*, system_prompt: str, output_schema: Mapping[str, Any], processed: Mapping[str, Any]) -> dict:
    """What AWS sends to the model. Refuses an empty prompt or schema, so a missing prompt can never reach AWS."""
    if not system_prompt.strip() or not output_schema:
        raise ValueError("the system prompt and the output schema must be filled in")
    return {
        "anthropic_version": ANTHROPIC_VERSION,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": system_prompt}],
        "messages": [{"role": "user",
                      "content": [{"type": "text", "text": json.dumps(processed, ensure_ascii=False)}]}],
        "output_config": {"format": {"type": "json_schema", "schema": output_schema}},
    }
