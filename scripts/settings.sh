#!/usr/bin/env bash
# Opens the local settings page (python -m store_monitor.settings) in this
# repository's .venv, creating it and installing requirements.txt when needed.
# Arguments are passed on, for example: scripts/settings.sh --port 8800
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv"
  python3 -m venv .venv
fi
# Reinstall only when requirements.txt is newer than the last install.
if [ ! -f .venv/.requirements-installed ] || [ requirements.txt -nt .venv/.requirements-installed ]; then
  .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
  touch .venv/.requirements-installed
fi
exec .venv/bin/python -m store_monitor.settings "$@"
