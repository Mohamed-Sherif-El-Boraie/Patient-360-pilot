"""Internal AI output -> public output for the Backend (no model ids, no tokens, no cost)."""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

PRIVATE_PATHS = ("meta.model_id", "meta.translation_model_id", "meta.usage")


def to_public(record: Mapping[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(dict(record))
    for path in PRIVATE_PATHS:
        *parents, leaf = path.split(".")
        node: Any = out
        for key in parents:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(leaf, None)
    return out
