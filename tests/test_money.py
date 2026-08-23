"""No floats anywhere near money. AST-walks match/ and ledger/ and fails on any
float literal or float() call. See IMPLEMENTATION.md section 7.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "ledgerloop"
MONEY_SENSITIVE_DIRS = ["match", "ledger"]


def _money_sensitive_files() -> list[Path]:
    files: list[Path] = []
    for dirname in MONEY_SENSITIVE_DIRS:
        d = SRC_ROOT / dirname
        if d.is_dir():
            files.extend(sorted(d.rglob("*.py")))
    return files


class _FloatViolation(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[tuple[int, str]] = []

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, float):
            self.violations.append((node.lineno, f"float literal {node.value!r}"))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == "float":
            self.violations.append((node.lineno, "float() call"))
        self.generic_visit(node)


def test_no_floats_in_money_sensitive_code() -> None:
    files = _money_sensitive_files()
    assert files, "expected at least match/ to exist"

    offenders: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        checker = _FloatViolation()
        checker.visit(tree)
        for lineno, reason in checker.violations:
            offenders.append(f"{path.relative_to(SRC_ROOT.parent)}:{lineno}: {reason}")

    assert not offenders, "float usage in money-sensitive code:\n" + "\n".join(offenders)
