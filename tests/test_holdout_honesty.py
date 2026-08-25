"""Static enforcement of the holdout honesty rule (data/README.md, IMPLEMENTATION.md
section 5): data/holdout/answer_key.json may be read from exactly one place in the
whole source tree -- eval/harness.py::load_answer_key. Every other eval/*.py module
gets the answer key by calling that function, never by opening the file itself, so
the pipeline genuinely cannot see its own answer sheet outside the one place that's
allowed to score against it.

This is what data/README.md means by "CI enforces this": this test runs as part of
the normal `pytest` step in .github/workflows/ci.yml.
"""

from __future__ import annotations

from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "ledgerloop"
ALLOWED_READER = SRC_ROOT / "eval" / "harness.py"


def test_answer_key_json_is_read_only_from_harness():
    offenders = []
    for path in SRC_ROOT.rglob("*.py"):
        if path == ALLOWED_READER:
            continue
        text = path.read_text(encoding="utf-8")
        if "answer_key.json" in text and "read_text" in text:
            offenders.append(str(path.relative_to(SRC_ROOT)))
    assert offenders == [], (
        f"answer_key.json must only be read from eval/harness.py, found reads in: {offenders}"
    )
