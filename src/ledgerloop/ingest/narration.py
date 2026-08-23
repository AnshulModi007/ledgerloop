"""Best-effort regex extraction from bank narration free text.

This is Tier 0: cheap, deterministic, and allowed to fail. Where it fails to find a
UTR, Tier 3 (adjudicate/) may attempt LLM-based extraction, but only there -- this
module never guesses.
"""

from __future__ import annotations

import re

# Synthetic UTRs are "UTR" + 14 digits (see generate/generator.py::_new_utr). A
# transposed UTR (the TRANSPOSE defect) still matches this shape -- only the digit
# values differ -- which is what lets tier2 attempt a bounded transposition-tolerant
# lookup instead of a blind failure. Six digits is a generous floor in case narration
# formats vary; real UTRs run 12-22 characters.
_UTR_PATTERN = re.compile(r"UTR\d{6,}")


def extract_utr(narration: str) -> str | None:
    """Return the first UTR-shaped token in narration, or None if there isn't one."""
    match = _UTR_PATTERN.search(narration)
    return match.group() if match else None
