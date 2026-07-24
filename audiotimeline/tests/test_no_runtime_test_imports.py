"""Enforces tools/check_no_test_imports.py as part of the normal test run,
so a regression is caught by plain `pytest` too, not just whoever remembers
to run the standalone script/CI step. See that module's docstring for what
this is guarding against and why.
"""

import importlib.util
import os


def _load_checker():
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    script_path = os.path.join(repo_root, "tools", "check_no_test_imports.py")
    spec = importlib.util.spec_from_file_location("check_no_test_imports", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_runtime_code_imports_the_tests_package():
    checker = _load_checker()
    violations = checker.find_violations()
    assert violations == []
