"""Check that operational values in an answer come from tool evidence.

The model is instructed to state only what tools returned, but instructions are not a
guarantee. This check extracts "operational" values from the final answer, such as
metrics (0.9861 or 98.6%), commit hashes, run ids and md5 hashes, and verifies that each
one appears in the evidence gathered during the conversation. Values that cannot be
found are reported so the orchestrator can ask for a correction, or warn the user.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

# A decimal, optionally a percentage. It may end a sentence ("0.98."), but version strings
# such as 1.0.0 are not treated as numbers.
_DECIMAL = re.compile(r"(?<![\w.])(\d+\.\d+)(%?)(?!\w)(?!\.\d)")
_PERCENT_INT = re.compile(r"(?<![\w.])(\d{1,3})%")
_HEX = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,64}\b")
_EVIDENCE_NUMBER = re.compile(r"-?\d+\.\d+|-?\d+")


def _evidence_text(evidence: Iterable[Any]) -> str:
    return "\n".join(e if isinstance(e, str) else json.dumps(e, default=str) for e in evidence)


def find_unsupported_values(answer: str, evidence: Iterable[Any]) -> list[str]:
    """Values in ``answer`` that do not appear in ``evidence`` (tool results and user input)."""
    text = _evidence_text(evidence)
    lowered = text.lower()
    numbers = [float(n) for n in _EVIDENCE_NUMBER.findall(text)]
    unsupported: list[str] = []

    def number_supported(value: float, is_percent: bool) -> bool:
        candidates = [value / 100, value] if is_percent else [value, value / 100]
        for cand in candidates:
            tol = max(0.0006, abs(cand) * 0.002)
            if any(abs(n - cand) <= tol for n in numbers):
                return True
        return False

    for raw, pct in _DECIMAL.findall(answer):
        if not number_supported(float(raw), bool(pct)):
            unsupported.append(raw + pct)
    for raw in _PERCENT_INT.findall(answer):
        if not number_supported(float(raw), True):
            unsupported.append(raw + "%")
    for token in _HEX.findall(answer.lower()):
        if token not in lowered:  # short commit hashes are prefixes of full ones in evidence
            unsupported.append(token)
    seen: set[str] = set()
    return [v for v in unsupported if not (v in seen or seen.add(v))]
