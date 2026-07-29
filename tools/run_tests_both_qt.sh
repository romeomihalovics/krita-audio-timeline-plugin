#!/usr/bin/env bash
# Runs the test suite twice: once against the system PyQt5 (matching
# Krita 5.x), and once against PyQt6 in an isolated venv with PyQt5
# absent (matching Krita 6.x) -- so qtcompat.py's PyQt6 fallback branch
# actually executes, rather than just being unit-tested in isolation.
#
# Why a separate venv is required (not just `pip install PyQt6` alongside
# the system PyQt5): qtcompat.py's `try: import PyQt5 / except ImportError:
# import PyQt6` only ever reaches the PyQt6 branch if PyQt5 genuinely isn't
# importable. With both installed in the same environment, `import PyQt5`
# always succeeds and the PyQt6 path never runs -- silently defeating the
# whole point of this script.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

venv_dir="$repo_root/.venv-pyqt6"

echo "=== Pass 1/2: PyQt5 (system) ==="
python3 -m pytest "$@"

echo
echo "=== Pass 2/2: PyQt6 (isolated venv, no PyQt5 present) ==="
if [ ! -d "$venv_dir" ]; then
    python3 -m venv "$venv_dir"
fi
"$venv_dir/bin/pip" install --quiet --upgrade pip
"$venv_dir/bin/pip" install --quiet pytest pytest-qt numpy PyQt6

# Sanity check: this venv must NOT be able to import PyQt5, or pass 2
# would silently retest the PyQt5 path instead of PyQt6's.
if "$venv_dir/bin/python" -c "import PyQt5" 2>/dev/null; then
    echo "ERROR: $venv_dir can import PyQt5 -- it must be PyQt6-only." >&2
    echo "Delete $venv_dir and rerun this script." >&2
    exit 1
fi

"$venv_dir/bin/python" -m pytest "$@"

echo
echo "=== Both PyQt5 and PyQt6 passes succeeded ==="
