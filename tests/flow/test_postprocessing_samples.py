"""The schema samples of the body POSTed to the Backend (tests/fixtures/flow/*/8_*.jsonc) must have exactly the keys of
the real output, so they cannot drift."""
import json
import re
from pathlib import Path

import pytest

from patient360.domain.builders.ai_input import build_ai_input
from patient360.domain.builders.backend_output import build_backend_output, for_wrapper
from patient360.domain.payload.ai_answer import AiAnswer
from patient360.domain.rules.packages import Package, package_entries

FLOW = Path(__file__).parents[1] / "fixtures" / "flow"
SAMPLES = Path(__file__).parents[1] / "fixtures" / "samples"
# .gitignore keeps tests/fixtures/flow/ out of the repository, so on a fresh clone these checks have nothing to read
pytestmark = pytest.mark.skipif(not FLOW.exists(), reason="tests/fixtures/flow is not in the repository")
PACKAGES = package_entries([Package(1, "Kidney check", 3, None, None, frozenset({1, 2}), "فحص الكلى")],
                           {1: ("Urea", "اليوريا"), 2: ("Creatinine", "الكرياتينين")})


def read_jsonc(path: Path):
    """Strip // comments (outside strings) and trailing commas, then parse."""
    text, out, in_string, i = path.read_text(encoding="utf-8"), [], False, 0
    while i < len(text):
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\":
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            out.append(ch)
        elif text.startswith("//", i):
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))


def shape(node):
    """The keys of a JSON value; a list is the union of its items, an empty list matches anything."""
    if isinstance(node, dict):
        return {k: shape(v) for k, v in node.items()}
    if isinstance(node, list):
        merged = None
        for item in map(shape, node):
            merged = item if merged is None else merge(merged, item)
        return [] if merged is None else [merged]
    return None


def merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return {k: merge(a.get(k), b.get(k)) if k in a and k in b else a.get(k, b.get(k)) for k in {**a, **b}}
    if isinstance(a, list) and isinstance(b, list):
        return a or b
    return a


def same(real, sample, where="body"):
    if isinstance(real, dict):
        assert isinstance(sample, dict) and list(real) == list(sample), f"{where}: {list(real)} != {sample}"
        for key in real:
            same(real[key], sample[key], f"{where}.{key}")
    elif isinstance(real, list):
        assert isinstance(sample, list), where
        if real and sample:
            same(real[0], sample[0], f"{where}[]")
    else:
        assert not isinstance(sample, (dict, list)), where


def real_body(visit_name, answer_name, source, symptom_flag):
    visit = json.loads((SAMPLES / f"{visit_name}.json").read_text(encoding="utf-8"))
    record = json.loads((SAMPLES / f"{answer_name}_phase7.json").read_text(encoding="utf-8"))
    ai = AiAnswer.model_validate_json(record["content"][0]["text"]).for_backend()
    output = build_backend_output(
        patient_id=visit["patient_id"], visit_id=visit["visit_id"], source=source, rows=build_ai_input(visit).rows,
        ai=ai, packages=PACKAGES, symptom_flag=symptom_flag)
    return for_wrapper(output)


@pytest.mark.parametrize(("folder", "file", "visit", "answer", "source", "flag"), [
    ("urgent", "06-backend-output", "urgent_mild", "urgent_mild", 1, True),
    ("batch", "06-backend-output", "batch_critical", "batch_critical", 2, True),
    ("on-demand", "06-backend-output", "on_demand_critical", "on_demand_critical", 4, True),
    ("symptom-checker", "06-backend-output", "batch_critical", "symptom_check", 3, True),
])
def test_the_sample_has_the_keys_of_the_real_body(folder, file, visit, answer, source, flag):
    body = real_body(visit, answer, source, flag)
    sample = read_jsonc(FLOW / folder / f"{file}.jsonc")
    same(shape(body), sample)
    assert ("tests" in body) is (source != 3) and "critical_direction" not in json.dumps(sample)
    if source != 3:      # the history of at least one real result is filled, so its keys were really compared
        assert any(r["history"] for t in body["tests"] for r in t["results"])


@pytest.mark.parametrize("path", sorted(FLOW.rglob("*.jsonc")), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_flow_sample_parses(path):
    read_jsonc(path)


@pytest.mark.parametrize("path", sorted(FLOW.glob("*/01-input.jsonc")), ids=lambda p: p.parent.name)
def test_the_aws_input_has_the_age_as_a_number_of_days_and_no_phase_3_file(path):
    """The phase-2 file is the English AWS input (there is no RAG step, so no phase 3)."""
    model_input = read_jsonc(path)
    model_input = model_input.get("modelInput", model_input)
    patient = json.loads(model_input["messages"][0]["content"][0]["text"])["patient"]
    assert isinstance(patient["age_days"], int) and set(patient) == {"age_days", "sex"}
    assert not list(path.parent.glob("[0-9]_*"))      # the old names (1_, 2_, 3_ ...) are gone
