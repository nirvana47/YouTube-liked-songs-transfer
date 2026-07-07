#!/bin/bash
# ---------------------------------------------------------------------------
# Transfer Liked Songs — double-click launcher for macOS (personal use).
#
# Double-click this file in Finder to open the graphical wizard. On the first
# run it creates a private Python environment next to the app and installs the
# packages it needs. Later runs start instantly.
#
# This is unsigned and for personal use only. It does NOT require the App Store,
# code signing, or a paid Apple Developer account.
# ---------------------------------------------------------------------------
set -uo pipefail

# Resolve the folder this script lives in, no matter where it was launched from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$SCRIPT_DIR" || exit 1

VENV_DIR="$SCRIPT_DIR/.venv"
STAMP="$VENV_DIR/.gui-deps.stamp"

log() { printf "\n\033[1;36m==>\033[0m %s\n" "$1"; }

fail() {
  local msg="$1"
  printf "\n\033[1;31mERROR:\033[0m %s\n" "$msg"
  # Show a native macOS alert so the message is visible even if the window is hidden.
  /usr/bin/osascript -e "display dialog \"$msg\" with title \"Liked Songs Transfer\" buttons {\"OK\"} default button 1 with icon caution" >/dev/null 2>&1 || true
  echo
  echo "Press Return to close this window."
  read -r _ || true
  exit 1
}

# --- 1. Find a usable Python 3 -------------------------------------------------
PYTHON_BIN=""
for cand in python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v "$cand")"
    break
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  fail "Python 3 was not found on this Mac. Install it from https://www.python.org/downloads/ and then double-click this file again."
fi

# --- 2. Create the private environment on first run ---------------------------
if [ ! -x "$VENV_DIR/bin/python" ]; then
  log "First-time setup: creating a private Python environment (one time only)…"
  "$PYTHON_BIN" -m venv "$VENV_DIR" || fail "Could not create the Python environment. Make sure the 'venv' module is available."
fi
VPY="$VENV_DIR/bin/python"

# --- 3. Install / refresh dependencies only when needed -----------------------
need_install=0
[ -f "$STAMP" ] || need_install=1
if [ -f requirements.txt ] && [ requirements.txt -nt "$STAMP" ]; then need_install=1; fi
if [ -f requirements-gui.txt ] && [ requirements-gui.txt -nt "$STAMP" ]; then need_install=1; fi

if [ "$need_install" -eq 1 ]; then
  log "Installing required packages. The first time downloads the GUI toolkit and may take a minute…"
  "$VPY" -m pip install --upgrade pip >/dev/null 2>&1 || true
  "$VPY" -m pip install -r requirements.txt || fail "Could not install the core packages. Check your internet connection and try again."
  "$VPY" -m pip install -r requirements-gui.txt || fail "Could not install the GUI toolkit (PySide6). Check your internet connection and try again."
  touch "$STAMP"
fi

# --- 4. Launch the wizard ------------------------------------------------------
log "Launching the Liked Songs Transfer app…"
"$VPY" gui_transfer.py || fail "The app closed unexpectedly. See the messages above for details."
