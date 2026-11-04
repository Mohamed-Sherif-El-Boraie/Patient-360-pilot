"""The Bedrock request for one visit: the system prompt, the processed JSON as the user message, the output schema."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

ANTHROPIC_VERSION = "bedrock-2023-05-31"
MAX_TOKENS = 8192


def aws_record(record_id: str, model_input: Mapping[str, Any]) -> dict:
    """One line of a batch file, as AWS reads it: ``recordId`` comes back in the answer."""
    return {"recordId": record_id, "modelInput": model_input}


def single_visit_file(record_id: str, model_input: Mapping[str, Any]) -> dict:
    """The file of one visit (urgent, on-demand, symptom check), as AWS reads it. Unlike a batch line it is flat: the
    request keys sit next to ``recordId`` (AWS's validator reads ``messages`` at the top, so a ``modelInput`` wrapper is
    refused). The system prompt is cached for an hour. No ``amazon-bedrock-guardrailConfig``: Bedrock refuses it in the
    body ("Extra inputs are not permitted"), AWS sets the guardrail on its own call."""
    system = [{**block, "cache_control": {"type": "ephemeral", "ttl": "1h"}} for block in model_input["system"]]
    return {"recordId": record_id, **model_input, "system": system}


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
