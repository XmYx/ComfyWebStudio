#!/usr/bin/env bash
#
# ComfyWebStudio launcher for macOS and Linux.
#
# Sets up whatever is missing on first run, then starts the server and opens a browser.
#
#   ./start.sh              start normally (builds the UI once, then serves it)
#   ./start.sh --dev        run the Vite dev server too, with hot reload
#   ./start.sh --port 9000  serve on a different port
#   ./start.sh --setup      install/refresh dependencies and exit
#   ./start.sh --no-browser don't open a browser
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PORT=8500
DEV=0
OPEN_BROWSER=1
SETUP_ONLY=0
NODE_VERSION=22

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev) DEV=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    --setup) SETUP_ONLY=1; shift ;;
    --no-browser) OPEN_BROWSER=0; shift ;;
    # Print the header comment block, stopping at the first line that is not a comment.
    -h|--help) sed -n '2,${/^#/!q; s/^# \?//p;}' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 1 ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
warn() { printf '\033[33m%s\033[0m\n' "$1"; }
fail() { printf '\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

# -- Python ---------------------------------------------------------------------------------------------

PYTHON=""
for candidate in python3.13 python3.12 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    # 3.12+ is required; anything older will fail on modern typing syntax rather than at import.
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
      PYTHON="$candidate"
      break
    fi
  fi
done
[[ -n "$PYTHON" ]] || fail "Python 3.12 or newer is required but was not found on PATH."

if [[ ! -d .venv ]]; then
  bold "Creating the Python environment (first run only)…"
  "$PYTHON" -m venv .venv
fi

VENV_PY=".venv/bin/python"

# Reinstall when the dependency list is newer than the marker we drop after a successful install.
if [[ ! -f .venv/.deps-installed || pyproject.toml -nt .venv/.deps-installed ]]; then
  bold "Installing Python dependencies…"
  "$VENV_PY" -m pip install --quiet --upgrade pip
  "$VENV_PY" -m pip install --quiet -e ".[dev]"
  touch .venv/.deps-installed
fi

# -- Node (frontend only) -------------------------------------------------------------------------------

# nvm is not on PATH in a non-interactive shell, so source it if it is installed.
if ! command -v node >/dev/null 2>&1 && [[ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]]; then
  # shellcheck disable=SC1091
  . "${NVM_DIR:-$HOME/.nvm}/nvm.sh"
  nvm use "$NODE_VERSION" >/dev/null 2>&1 || true
fi

HAVE_NODE=0
if command -v node >/dev/null 2>&1; then
  HAVE_NODE=1
fi

need_frontend_build() {
  [[ ! -f frontend/dist/index.html ]] && return 0
  # Rebuild when any source file is newer than the build output.
  [[ -n "$(find frontend/src frontend/index.html frontend/package.json \
            -newer frontend/dist/index.html -print -quit 2>/dev/null)" ]]
}

if [[ "$HAVE_NODE" == 1 ]]; then
  if [[ ! -d frontend/node_modules ]]; then
    bold "Installing frontend dependencies (first run only)…"
    (cd frontend && npm install --no-fund --no-audit)
  fi
  if [[ "$DEV" == 0 ]] && need_frontend_build; then
    bold "Building the interface…"
    (cd frontend && npm run build)
  fi
elif [[ ! -f frontend/dist/index.html ]]; then
  warn "Node.js was not found and the interface has not been built."
  warn "Install Node 20+ (https://nodejs.org) and run ./start.sh --setup, or use the API at /docs."
fi

if [[ "$SETUP_ONLY" == 1 ]]; then
  bold "Setup complete."
  exit 0
fi

# -- Run ------------------------------------------------------------------------------------------------

open_browser() {
  local url="$1"
  sleep 2
  if command -v open >/dev/null 2>&1; then open "$url"           # macOS
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$url"  # Linux
  fi >/dev/null 2>&1 || true
}

cleanup() {
  # Make sure the dev server does not outlive the launcher.
  [[ -n "${VITE_PID:-}" ]] && kill "$VITE_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if [[ "$DEV" == 1 ]]; then
  [[ "$HAVE_NODE" == 1 ]] || fail "--dev needs Node.js installed."
  bold "Starting the Vite dev server on http://localhost:5173"
  (cd frontend && npm run dev) &
  VITE_PID=$!
  [[ "$OPEN_BROWSER" == 1 ]] && open_browser "http://localhost:5173" &
  bold "Starting the API on http://127.0.0.1:${PORT}"
  exec "$VENV_PY" -m comfywebstudio.main --port "$PORT" --reload
fi

bold "ComfyWebStudio → http://127.0.0.1:${PORT}"
echo "Press Ctrl+C to stop."
[[ "$OPEN_BROWSER" == 1 ]] && open_browser "http://127.0.0.1:${PORT}" &
exec "$VENV_PY" -m comfywebstudio.main --port "$PORT"
