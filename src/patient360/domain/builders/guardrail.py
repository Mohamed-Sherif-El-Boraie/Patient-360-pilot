"""The guardrail part of an AWS answer as rows for ``guardrail_checks`` and ``guardrail_findings``.

Three shapes, as in ``blocked.py``: InvokeModel (``amazon-bedrock-guardrailAction`` and a trace; urgent, on-demand,
symptom check), Converse (``stopReason`` and ``trace``) and ApplyGuardrail (a ``guardrail`` object; batch).
Real samples exist only for the batch shape with action NONE. How an intervened assessment looks (topicPolicy,
contentPolicy, wordPolicy, sensitiveInformationPolicy, contextualGroundingPolicy, invocationMetrics) follows the AWS
Bedrock documentation, so the assessment is also kept as given (``raw``). Nothing here raises: an odd shape gives
fewer rows.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from patient360.domain.builders.blocked import GUARDRAIL_ACTION_KEY, TRACE_KEY

UNKNOWN_ID = "unknown"      # a batch guardrail object has no guardrail id
_UNITS = {"topicPolicyUnits": "topic_policy_units", "contentPolicyUnits": "content_policy_units",
          "wordPolicyUnits": "word_policy_units", "sensitiveInformationPolicyUnits": "sensitive_info_policy_units",
          "sensitiveInformationPolicyFreeUnits": "sensitive_info_policy_free_units",
          "contextualGroundingPolicyUnits": "contextual_grounding_policy_units"}


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _entries(node: Any, key: str) -> Iterator[Mapping[str, Any]]:
    return (e for e in (node.get(key) if isinstance(node.get(key), list) else []) if isinstance(e, Mapping))


def _findings(assessment: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One finding per entry of every policy. ``raw`` is the entry as AWS gave it."""
    rows = []

    def add(policy: str, entry: Mapping[str, Any], name: Any, kind: Any, strength: Any = None, conf: Any = None):
        rows.append({"policy": policy, "name": name, "type": str(kind or "unknown"), "confidence": conf,
                     "filter_strength": strength, "action": str(entry.get("action") or "unknown"), "raw": dict(entry)})

    for e in _entries(_map(assessment.get("topicPolicy")), "topics"):
        add("topic", e, e.get("name"), e.get("type"))
    for e in _entries(_map(assessment.get("contentPolicy")), "filters"):
        add("content", e, None, e.get("type"), e.get("filterStrength"), e.get("confidence"))
    words = _map(assessment.get("wordPolicy"))
    for e in _entries(words, "customWords"):
        add("word", e, e.get("match"), "CUSTOM")
    for e in _entries(words, "managedWordLists"):
        add("word", e, e.get("match"), e.get("type"))
    pii = _map(assessment.get("sensitiveInformationPolicy"))
    for e in _entries(pii, "piiEntities"):
        add("sensitive_info", e, None, e.get("type"))
    for e in _entries(pii, "regexes"):
        add("sensitive_info", e, e.get("name"), "REGEX")
    for e in _entries(_map(assessment.get("contextualGroundingPolicy")), "filters"):
        add("contextual_grounding", e, None, e.get("type"))
    return rows


def _check(direction: str, guardrail_id: str, assessments: list[Any], usage: Mapping[str, Any],
           bypassed: Any = None) -> dict[str, Any]:
    maps = [_map(a) for a in assessments]
    findings = [f for a in maps for f in _findings(a)]
    intervened = any(f["action"] not in ("NONE", "unknown") for f in findings)
    metrics = [_map(a.get("invocationMetrics")) for a in maps]
    units = {column: _int(usage.get(key)) for key, column in _UNITS.items()}
    if not usage:       # the units sit in each assessment's invocationMetrics: add them up
        for key, column in _UNITS.items():
            seen = [_int(_map(m.get("usage")).get(key)) for m in metrics]
            units[column] = sum(n for n in seen if n is not None) if any(n is not None for n in seen) else None
    chars = [_map(_map(m.get("guardrailCoverage")).get("textCharacters")) for m in metrics]
    return {"direction": direction, "guardrail_id": guardrail_id, "action": "INTERVENED" if intervened else "NONE",
            "bypassed": bypassed if isinstance(bypassed, bool) else None,
            "latency_ms": next((_int(m.get("guardrailProcessingLatency")) for m in metrics
                                if _int(m.get("guardrailProcessingLatency")) is not None), None),
            "text_chars_guarded": next((_int(c.get("guarded")) for c in chars if _int(c.get("guarded")) is not None),
                                       None),
            "text_chars_total": next((_int(c.get("total")) for c in chars if _int(c.get("total")) is not None), None),
            **units, "findings": findings, "raw": assessments}


def _by_id(value: Any) -> Iterator[tuple[str, list[Any]]]:
    """``{id: assessment}``, ``{id: [assessments]}`` or a list of those: the assessments of each guardrail id."""
    for part in value if isinstance(value, list) else [value]:
        for gid, found in _map(part).items():
            yield str(gid), found if isinstance(found, list) else [found]


def _trace_checks(trace: Mapping[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[Any]] = {}
    for key, value in _map(trace.get("guardrail")).items():
        lowered = key.lower()
        direction = "input" if lowered.startswith("input") else "output" if lowered.startswith("output") else None
        for gid, assessments in _by_id(value) if direction else []:
            grouped.setdefault((direction, gid), []).extend(assessments)
    if not grouped:     # a guardrail that checked nothing we can read: still one row, so the check is on record
        grouped[("output", UNKNOWN_ID)] = []
    return [_check(direction, gid, found, {}) for (direction, gid), found in grouped.items()]


def _settled(checks: list[dict[str, Any]], top_action: bool) -> list[dict[str, Any]]:
    """AWS said INTERVENED but no assessment we could read shows what: every row is then marked INTERVENED."""
    if top_action and not any(c["action"] == "INTERVENED" for c in checks):
        for check in checks:
            check["action"] = "INTERVENED"
    return checks


def guardrail_checks(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The rows for the guardrail of one answer record (the output as saved); an empty list when it has none."""
    actions = [record.get(GUARDRAIL_ACTION_KEY)] + [_map(p).get(GUARDRAIL_ACTION_KEY)
                                                    for p in _map(record.get("metadata")).values()]
    action = next((a for a in actions if isinstance(a, str)), None)
    traces = [record.get(TRACE_KEY)] + [_map(p).get(TRACE_KEY) for p in _map(record.get("metadata")).values()]
    trace = next((t for t in traces if isinstance(t, Mapping)), None)
    if action is not None or trace is not None:        # InvokeModel
        return _settled(_trace_checks(_map(trace)), action == "INTERVENED")
    if record.get("stopReason") == "guardrail_intervened" and isinstance(record.get("trace"), Mapping):   # Converse
        return _settled(_trace_checks(record["trace"]), True)
    apply = record.get("guardrail")
    if isinstance(apply, Mapping):                    # ApplyGuardrail (batch)
        assessments = apply.get("assessments")
        check = _check("output", UNKNOWN_ID, assessments if isinstance(assessments, list) else [],
                       _map(apply.get("usage")), apply.get("bypassed"))
        return _settled([check], apply.get("action") in ("GUARDRAIL_INTERVENED", "INTERVENED"))
    return []
