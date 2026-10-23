"""Small pure helpers shared by the domain logic (no I/O)."""
from __future__ import annotations

import math
import re
from html.parser import HTMLParser
from typing import Any

_NOT_A_NUMBER_TAIL = re.compile(r"[:/]|-\d")  # "1:64" (titre), "5/10", "2026-01-10", "5-10"
_NUMBER_PREFIX = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def parse_numeric(value: Any) -> int | float | None:
    """Port of v3 ``labValues.parseNumeric``: drop commas/spaces, read the leading number."""
    if value is None or value == "":
        return None
    text = re.sub(r"[,\s]", "", str(value))
    match = _NUMBER_PREFIX.match(text)
    if not match or _NOT_A_NUMBER_TAIL.match(text[match.end():]):
        return None
    number = float(match.group(0))
    if math.isnan(number) or math.isinf(number):
        return None
    return int(number) if number.is_integer() else number


def remove_empty_values(value: Any) -> Any:
    """Drop ``None``, ``""`` and empty containers recursively. Keeps ``0`` and ``False``."""
    if isinstance(value, list):
        items = [kept for kept in (remove_empty_values(item) for item in value) if kept is not None]
        return items or None
    if isinstance(value, dict):
        out = {key: kept for key, item in value.items() if (kept := remove_empty_values(item)) is not None}
        return out or None
    if value is None or value == "":
        return None
    return value


_BREAKING_TAGS = frozenset({"br", "p", "div", "li", "tr", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5", "h6"})
_HIDDEN_TAGS = frozenset({"script", "style"})


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _HIDDEN_TAGS:
            self._hidden += 1
        elif tag in _BREAKING_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN_TAGS:
            self._hidden = max(0, self._hidden - 1)
        elif tag in _BREAKING_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)


def html_to_plain_text(value: Any) -> Any:
    """Text with HTML tags -> plain text: tags removed, ``<br>``/``<p>``/``<li>`` become line breaks, entities decoded,
    ``<script>``/``<style>`` content dropped, spaces collapsed. Anything that is not a string is returned unchanged."""
    if not isinstance(value, str):
        return value
    parser = _TextOnly()
    parser.feed(value)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)
