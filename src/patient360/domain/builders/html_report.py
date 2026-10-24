"""The ``html`` key of a result, for the Backend's PDF: the summary and the names of the recommended tests."""
from __future__ import annotations

from collections.abc import Mapping
from html import escape
from typing import Any

LABELS = {
    "en": {"summary": "Summary", "tests": "Recommended follow-up tests"},
    "ar": {"summary": "الملخص", "tests": "الفحوصات الموصى بها"},
}
WRAPPER = {"en": '<div lang="en">{}</div>', "ar": '<div lang="ar" dir="rtl">{}</div>'}


def _text(node: Any, lang: str) -> str:
    value = node.get(lang) if isinstance(node, Mapping) else None
    return escape(str(value)) if value else ""


def _test_names(ai: Mapping[str, Any], lang: str, arabic_names: Mapping[str, str]) -> list[str]:
    """Names of the recommended tests. A test the symptom check removed is not recommended any more."""
    names = []
    for rec in ai.get("recommendations") or []:
        if (rec.get("change_type") or {}).get("en") == "removed":
            continue
        name = str(rec.get("test_name", ""))
        names.append(escape(arabic_names.get(name, name) if lang == "ar" else name))
    return names


def build_html(ai: Mapping[str, Any], arabic_names: Mapping[str, str] | None = None) -> dict[str, str]:
    """``{"en": "<div ...>", "ar": "<div dir=rtl ...>"}``: the summary, then one bullet per recommended test.

    ``arabic_names`` maps an English test name to ``tests.test_name_ar``; a test without one keeps its English name.
    All text is HTML-escaped.
    """
    arabic_names = arabic_names or {}
    pages = {}
    for lang, words in LABELS.items():
        parts = []
        summary = _text(ai.get("summary"), lang)
        if summary:
            parts += [f"<h2>{words['summary']}</h2>", f"<p>{summary}</p>"]
        names = _test_names(ai, lang, arabic_names)
        if names:
            parts += [f"<h2>{words['tests']}</h2>", "<ul>" + "".join(f"<li>{name}</li>" for name in names) + "</ul>"]
        pages[lang] = WRAPPER[lang].format("".join(parts))
    return pages
