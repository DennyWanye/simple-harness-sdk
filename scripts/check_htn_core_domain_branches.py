"""Fail when core HTN code grows domain-name conditionals."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path


def _hardcoded_domain_comparison(test: ast.AST) -> bool:
    domain_fields = {"domain", "domain_id", "domain_name"}
    for comparison in ast.walk(test):
        if not isinstance(comparison, ast.Compare):
            continue
        operands = (comparison.left, *comparison.comparators)
        has_domain = any(
            isinstance(node, ast.Name) and node.id in domain_fields
            or isinstance(node, ast.Attribute) and node.attr in domain_fields
            for operand in operands for node in ast.walk(operand)
        )
        has_literal = any(
            isinstance(node, ast.Constant) and isinstance(node.value, str)
            for operand in operands for node in ast.walk(operand)
        )
        if has_domain and has_literal:
            return True
    return False


def find_domain_branches(root: Path) -> list[str]:
    hits: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "seed_methods" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.If, ast.IfExp, ast.comprehension)):
                continue
            tests = node.ifs if isinstance(node, ast.comprehension) else (node.test,)
            for test in tests:
                if _hardcoded_domain_comparison(test):
                    hits.append(f"{path}:{test.lineno}:{ast.unparse(test)}")
    return hits


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    hits = find_domain_branches(args.root)
    if hits:
        print("\n".join(hits))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
