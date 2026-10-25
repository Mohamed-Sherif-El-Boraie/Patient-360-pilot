"""Strict payload models = the Backend -> AI contract (tests/fixtures/flow). Unknown keys are rejected."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, model_validator

from patient360.domain.payload.values import as_whole_number

Date = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
Scalar = str | int | float   # a lab value or limit may arrive as text, decimal or integer
# Every number the Backend sends may arrive as a number or as whole-number text ("123", "123.0"): as_whole_number first
BigId = Annotated[int, BeforeValidator(as_whole_number), Field(ge=1, le=9_223_372_036_854_775_807)]  # bigint
SmallId = Annotated[int, BeforeValidator(as_whole_number), Field(ge=1, le=2_147_483_647)]  # int
AgeDays = Annotated[int, BeforeValidator(as_whole_number), Field(ge=0, le=60_000)]   # whole days (0 = newborn)
Sex = Annotated[int, BeforeValidator(as_whole_number), Field(ge=1, le=2)] # 1 Male, 2 Female (no Literal: true == 1)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class HistoryPoint(Strict):
    history_visit_id: BigId
    history_date: Date
    history_value: Scalar | None


class Reference(Strict):
    low: Scalar | None
    high: Scalar | None
    range: str | None


class Critical(Strict):
    low: Scalar | None
    high: Scalar | None
    range_text: str | None


class Result(Strict):
    parameter_id: SmallId
    parameter_name: str
    result_date: Date | None
    value: Scalar | None
    unit: str | None
    reference: Reference
    lab_flag: Literal["H", "L"] | None
    critical: Critical | None
    parameter_remark: str | None
    history: list[HistoryPoint] | None = None


class Test(Strict):
    test_id: SmallId
    test_name: str
    test_remark: str | None
    results: list[Result]

    @model_validator(mode="after")
    def _parameters_are_unique(self) -> Test:
        ids = [r.parameter_id for r in self.results]
        if len(ids) != len(set(ids)):
            raise ValueError("a parameter appears twice in one test")
        return self


class DescriptiveResult(Strict):
    test_id: SmallId
    test_name: str
    result_date: Date | None
    text: str
    history: list[HistoryPoint] | None = None


class PreparationAnswer(Strict):
    preparation_id: SmallId
    preparation_name: str
    answer: str


class Patient(Strict):
    age_days: AgeDays
    sex: Sex


class VisitPayload(Strict):
    """A visit: source 1 urgent, 2 batch or 4 on-demand (one visit per call)."""
    source: Literal[1, 2, 4]
    patient_id: BigId
    visit_id: BigId
    company_name: SmallId   # the Backend sends the company number here (15 El-Borg, 10 Al-Mokhtbar)
    patient: Patient
    tests: list[Test]
    descriptive_results: list[DescriptiveResult]
    visit_answer_list: list[PreparationAnswer]

    @model_validator(mode="after")
    def _tests_are_unique(self) -> VisitPayload:
        for group in (self.tests, self.descriptive_results):
            ids = [t.test_id for t in group]
            if len(ids) != len(set(ids)):
                raise ValueError("a test appears twice in one visit")
        return self


class SymptomPayload(Strict):
    """A symptom check (source 3): the visit is already stored, so only a few keys arrive."""
    source: Literal[3]
    patient_id: BigId
    visit_id: BigId
    patient_symptoms: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    symptom_recieved_at: Annotated[str, StringConstraints(min_length=1, max_length=40)]   # the Backend's spelling
