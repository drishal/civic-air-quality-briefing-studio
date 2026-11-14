#!/usr/bin/env bash
# Start Civic Air Quality Briefing Studio on Linux/macOS.
# Usage: ./start.sh [run.py options]      e.g. ./start.sh --port 8700 --no-browser
# On NixOS with devenv available it runs inside the devenv shell (python, libz etc. provided);
# otherwise it uses plain python3 + a local .venv via run.py (same as Windows).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
if [ -z "${AIR_PLAIN:-}" ] && command -v devenv >/dev/null 2>&1 && [ -f devenv.nix ]; then
  exec devenv shell -- python run.py "$@"
fi
PY="$(command -v python3 || command -v python)"
exec "$PY" run.py "$@"
