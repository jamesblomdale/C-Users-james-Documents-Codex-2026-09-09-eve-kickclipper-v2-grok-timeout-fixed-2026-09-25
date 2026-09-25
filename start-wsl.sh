#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
unset PYTHONHOME PYTHONPATH || true
if [[ ! -x .venv/bin/python ]] || ! .venv/bin/python -c "import encodings" >/dev/null 2>&1; then
  echo "Python environment is missing or stale. Run: bash setup-wsl.sh"
  exit 1
fi
# Repair an incomplete Flask install before importing app.py.
if ! .venv/bin/python -c "from flask import Flask" >/dev/null 2>&1; then
  echo "Flask installation is incomplete; repairing dependencies..."
  .venv/bin/python -m pip install --upgrade --force-reinstall "flask==3.1.3" "werkzeug>=3.1,<4" "jinja2>=3.1,<4" "itsdangerous>=2.2,<3" "click>=8.1,<9" "blinker>=1.9,<2"
fi
if ! .venv/bin/python -c "from flask import Flask" >/dev/null 2>&1; then
  echo "ERROR: Flask is still unavailable. Run: bash setup-wsl.sh" >&2
  exit 1
fi
export PORT=5059
exec .venv/bin/python app.py
