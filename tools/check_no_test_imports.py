#!/usr/bin/env python3
"""Guardrail: the runtime plugin code (everything under audiotimeline/,
except audiotimeline/tests/ itself) must never import anything from
audiotimeline.tests -- test helpers/fixtures (generated wav files, fake
Krita Document stand-ins, etc.) have no business being reachable from code
that actually runs inside Krita. This is the closest equivalent this
plain-Python plugin has to a TypeScript project's "no importing from the
tests directory in app code" lint rule.

Runs as a plain script (exit code 1 on any violation -- wire into a
pre-commit hook or CI step) and is also exercised by
audiotimeline/tests/test_no_runtime_test_imports.py so `pytest` alone
already catches a regression.
"""

import ast
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_ROOT = os.path.join(REPO_ROOT, "audiotimeline")
TESTS_DIR = os.path.join(PACKAGE_ROOT, "tests")


def _iter_runtime_py_files():
    for root, dirs, files in os.walk(PACKAGE_ROOT):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        if os.path.commonpath([root, TESTS_DIR]) == TESTS_DIR:
            continue  # inside audiotimeline/tests/ itself -- allowed to import its own helpers
        for filename in files:
            if filename.endswith(".py"):
                yield os.path.join(root, filename)


def _imports_tests_package(node):
    if isinstance(node, ast.Import):
        return any(alias.name == "tests" or alias.name.startswith("tests.")
                   or ".tests" in alias.name or alias.name.endswith(".tests")
                   for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        if node.module is None:
            # `from . import tests` / `from .. import tests` style
            return any(alias.name == "tests" for alias in node.names)
        module = node.module
        return module == "tests" or module.startswith("tests.") or module.endswith(".tests") or ".tests." in module
    return False


def find_violations():
    violations = []
    for path in _iter_runtime_py_files():
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and _imports_tests_package(node):
                rel = os.path.relpath(path, REPO_ROOT)
                violations.append(f"{rel}:{node.lineno}: runtime code must not import the tests package")
    return violations


def main():
    violations = find_violations()
    if violations:
        print("Found runtime imports of the tests package:", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
