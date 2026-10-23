"""Age of the patient in days.

shortcut: temporary. The AI model needs the exact age in days (some critical ranges depend on it, a baby of 28 days
must not be "0 years"). The owner will take the age from the Backend later; then delete this file.
"""
from __future__ import annotations

from datetime import date


def age_in_days(birthdate: str, today: date | None = None) -> int:
    """Whole days from ``birthdate`` (YYYY-MM-DD) to ``today`` (default: today); never negative."""
    return max(0, ((today or date.today()) - date.fromisoformat(birthdate)).days)
