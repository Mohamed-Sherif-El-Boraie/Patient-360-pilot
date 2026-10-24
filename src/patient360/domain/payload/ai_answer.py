"""The model's answer, as it arrives from AWS (English and Arabic). A trust boundary: a wrong shape is refused.

Keys we do not know are dropped, so nothing internal can leak through ``model_dump``. ``change_type`` and
``explanation`` exist only in a symptom-check answer.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Answer(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class EnAr(_Answer):
    en: str
    ar: str


class Advisory(_Answer):
    level: EnAr
    message: EnAr


class Recommendation(_Answer):
    test_name: str
    priority: EnAr
    reason: EnAr
    timing: EnAr
    guideline: EnAr
    advisory: Advisory
    change_type: EnAr | None = None
    explanation: EnAr | None = None


class AiAnswer(_Answer):
    summary: EnAr
    simple_summary: EnAr
    critical_alerts: list[EnAr] = []
    note: EnAr
    recommendations: list[Recommendation] = []

    def for_backend(self) -> dict:
        return self.model_dump(exclude_none=True)
