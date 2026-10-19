"""Strict payload models = the Backend -> AI contract (see the flow files). Unknown keys are rejected."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Date = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
Number = int | float
Scalar = str | int | float   # a lab value or limit may arrive as text, decimal or integer


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Reference(Strict):
    low: Scalar | None
    high: Scalar | None
    range: str | None


class Critical(Strict):
    low: Number | None
    high: Number | None
    range_text: str | None


class HistoryPoint(Strict):
    date: Date
    value: Scalar | None


class Result(Strict):
    parameter_name: str
    result_date: Date | None
    value: Scalar | None
    unit: str | None
    reference: Reference
    lab_flag: Literal["H", "L"] | None
    critical: Critical | None
    previous_value: Scalar | None
    previous_date: Date | None
    parameter_remark: str | None
    history: list[HistoryPoint] | None


class Test(Strict):
    test_name: str
    test_remark: str | None
    results: list[Result]


class DescriptiveResult(Strict):
    test_name: str
    result_date: Date | None
    text: str


class PreparationAnswer(Strict):
    PreparationID: int
    PreparationName: str
    Answer: str


class Patient(Strict):
    age_years: Annotated[int, Field(ge=0, le=130)]
    sex: Literal["male", "female"]


class VisitPayload(Strict):
    """A visit: source urgent, on-demand or batch (one visit per call)."""
    source: Literal["batch", "urgent", "on-demand"]
    patient_id: int
    visit_id: int
    company_name: str | None
    patient: Patient
    tests: list[Test]
    descriptive_results: list[DescriptiveResult]
    visitAnswerList: list[PreparationAnswer]


class SymptomPayload(Strict):
    """A symptom check: the visit is already stored, so only 4 keys arrive."""
    source: Literal["symptom-checker"]
    patient_id: int
    visit_id: int
    patient_symptoms: Annotated[str, StringConstraints(min_length=1, max_length=500)]
