#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg unzip libgl1 libglib2.0-0
# ZIP builds must never trust a copied virtualenv: its Python symlinks point
# at the machine that created the archive and can fail with "no encodings".
if [[ -d .venv ]]; then
  if ! .venv/bin/python -c "import encodings" >/dev/null 2>&1; then
    echo "Removing stale/broken .venv from transferred build..."
    rm -rf .venv
  fi
fi
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
# Playwright is optional for direct uploads and direct HLS sources. A broken
# read-only browser cache must not prevent Flask and the VOD pipeline starting.
if ! python -m playwright install --with-deps chromium; then
  echo "WARNING: Playwright browser install failed; continuing without optional browser resolution."
fi
python -c "from flask import Flask; print('Flask import OK')"
python configure_local.py
echo "Setup complete. Edit .env, add your API keys, then run: bash start-wsl.sh"
