"""One readable sentence per payload problem: where it is (test_id, parameter_id) and what is wrong.

Never holds an input value: only the ids found in the payload (whole numbers) and the field names (see MAX_LOC_PART)."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

MAX_LOC_PART = 60   # an unknown key name is sender-chosen text; keep what we store and return short
UNION_TAGS = {"str", "int", "float"}   # pydantic adds the member type to the path of a ``str | int | float`` error
LISTS = {"tests", "descriptive_results", "visit_answer_list"}
ID_KEYS = {"tests": "test_id", "descriptive_results": "test_id", "visit_answer_list": "preparation_id",
           "results": "parameter_id", "history": "history_visit_id"}
EXPECTED = {"string_type": "text", "int_type": "a whole number", "int_parsing": "a whole number",
            "list_type": "a list", "dict_type": "an object", "model_attributes_type": "an object",
            "bool_type": "true or false"}
LITERALS = {"lab_flag": "H or L", "source": "1, 2, 3 or 4"}
OUT_OF_RANGE = {"greater_than_equal", "less_than_equal", "greater_than", "less_than"}


def _at(node: Any, path: Sequence[Any]) -> Any:
    for part in path:
        try:
            node = node[part]
        except (KeyError, IndexError, TypeError):
            return None
    return node


def _id(value: Any) -> int | str | None:
    """An id that is safe to show: a whole number (or whole-number text), never free text."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return value if isinstance(value, str) and value.isdigit() and len(value) <= 20 else None


def _duplicates(items: Any, key: str) -> list[str]:
    ids = [_id(item.get(key)) for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    return [str(i) for i in dict.fromkeys(ids) if i is not None and ids.count(i) > 1]


def explain(obj: Any, loc: Sequence[Any], kind: str, msg: str) -> str:
    """``loc`` is pydantic's path, for example ``("tests", 3, "results", 0, "value")``."""
    parts, labels, node = list(loc), [], obj
    while len(parts) > 1 and isinstance(parts[1], int):
        name, index = parts[0], parts[1]
        node = _at(node, (name, index))
        id_key = ID_KEYS.get(name)
        ident = _id(node.get(id_key)) if id_key and isinstance(node, dict) else None
        label = f"{id_key} {ident}" if ident is not None else f"{name} #{index + 1}"
        labels.append(f"{name}, {label}" if not labels else label)
        parts = parts[2:]
    tagged = len(parts) > 1 and parts[-1] in UNION_TAGS
    field = ".".join(str(part)[:MAX_LOC_PART] for part in (parts[:-1] if tagged else parts))
    value = _at(node, parts[:-1] if tagged else parts)
    if kind == "value_error" and "twice" in msg:
        problem = _twice(node)
    elif not field:
        problem = msg.removeprefix("Value error, ")
    elif kind == "value_error":          # a rule of ours on one field, worded with ids only (for example the history)
        problem = f"{field}: {msg.removeprefix('Value error, ')}"
    elif kind == "missing":
        problem = f"{field} is required" + (" (send [] if there are none)" if field in LISTS else "")
    elif kind == "extra_forbidden":
        problem = f'unknown field "{field}"'
    elif value is None:
        problem = f"{field} is empty"
    elif kind == "string_pattern_mismatch":
        problem = f"{field} must be yyyy-MM-dd"
    elif kind == "literal_error":
        problem = f"{field} must be {LITERALS[field]}" if field in LITERALS else f"{field} has a value not allowed"
    elif tagged:
        problem = f"{field} must be text or a number"
    elif kind in EXPECTED:
        problem = f"{field} must be {EXPECTED[kind]}"
    elif kind in OUT_OF_RANGE:
        problem = f"{field} is out of range"
    else:
        problem = f"{field}: {msg}"
    return f"{', '.join(labels)}: {problem}" if labels else problem


def _twice(node: Any) -> str:
    """The visit (a test twice) or one test (a parameter twice): name the repeated ids."""
    if isinstance(node, dict) and "results" in node:
        ids, key = _duplicates(node["results"], "parameter_id"), "parameter_id"
    else:
        ids = _duplicates(_at(node, ["tests"]), "test_id") + _duplicates(_at(node, ["descriptive_results"]), "test_id")
        key = "test_id"
    return f"{key} {', '.join(ids)} appears twice" if ids else "an id appears twice"
