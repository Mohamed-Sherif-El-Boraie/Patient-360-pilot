"""Python 3.15 `frozendict` with a fallback so the suite also runs on 3.13/3.14 (CI matrix)."""
from __future__ import annotations

try:  # Python 3.15+: frozendict is a builtin (PEP 814)
    from builtins import frozendict  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - exercised on older interpreters only
    from types import MappingProxyType as frozendict  # type: ignore[assignment]

__all__ = ["frozendict"]