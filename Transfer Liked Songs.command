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

VENV_DIR="$SCRIPT_DIR/transfer_liked_songs.venv"
STAMP="$VENV_DIR/.gui-deps.stamp"
SETUP_LOG="$SCRIPT_DIR/logs/setup.log"

log() { printf "\n\033[1;36m==>\033[0m %s\n" "$1"; }

setup_line() {
  # Keep setup output to one clean, updating line.
  printf "\r\033[2K\033[1;36m==>\033[0m Setting up... [%s]" "$1"
}

clear_setup_line() {
  printf "\r\033[2K"
}

fail() {
  local msg="$1"
  clear_setup_line
  printf "\n\033[1;31mERROR:\033[0m %s\n" "$msg"
  if [ -f "$SETUP_LOG" ]; then
    printf "Setup details were saved to: logs/setup.log\n"
  fi
  # Show a native macOS alert so the message is visible even if the window is hidden.
  /usr/bin/osascript -e "display dialog \"$msg\" with title \"Liked Songs Transfer\" buttons {\"OK\"} default button 1 with icon caution" >/dev/null 2>&1 || true
  echo
  echo "Press Return to close this window."
  read -r _ || true
  exit 1
}

run_quiet() {
  local label="$1"
  shift
  mkdir -p "$SCRIPT_DIR/logs"
  setup_line "$label"
  "$@" >>"$SETUP_LOG" 2>&1 &
  local pid=$!
  local seconds=0
  while kill -0 "$pid" >/dev/null 2>&1; do
    setup_line "$label ${seconds}s"
    sleep 1
    seconds=$((seconds + 1))
  done
  wait "$pid"
  local code=$?
  setup_line "$label done"
  return "$code"
}

folder_size_mb() {
  if [ -d "$VENV_DIR" ]; then
    du -sm "$VENV_DIR" 2>/dev/null | awk '{print $1}'
  else
    printf "0"
  fi
}

package_count() {
  if [ -x "$VENV_DIR/bin/python" ]; then
    "$VENV_DIR/bin/python" -m pip list --format=freeze --disable-pip-version-check 2>/dev/null | wc -l | tr -d ' '
  else
    printf "0"
  fi
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
  run_quiet "creating private environment" "$PYTHON_BIN" -m venv "$VENV_DIR" || fail "Could not create the Python environment. Make sure the 'venv' module is available."
fi
VPY="$VENV_DIR/bin/python"

# --- 3. Install / refresh dependencies only when needed -----------------------
need_install=0
[ -f "$STAMP" ] || need_install=1
if [ -f requirements.txt ] && [ requirements.txt -nt "$STAMP" ]; then need_install=1; fi
if [ -f requirements-gui.txt ] && [ requirements-gui.txt -nt "$STAMP" ]; then need_install=1; fi

if [ "$need_install" -eq 1 ]; then
  : >"$SETUP_LOG"
  run_quiet "updating installer" "$VPY" -m pip install -q --disable-pip-version-check --upgrade pip || true
  run_quiet "installing core packages" "$VPY" -m pip install -q --disable-pip-version-check -r requirements.txt || fail "Could not install the core packages. Check your internet connection and try again."
  run_quiet "installing GUI packages" "$VPY" -m pip install -q --disable-pip-version-check -r requirements-gui.txt || fail "Could not install the GUI toolkit (PySide6). Check your internet connection and try again."
  touch "$STAMP"
fi

PKG_COUNT="$(package_count)"
SIZE_MB="$(folder_size_mb)"
clear_setup_line
printf "\033[1;36m==>\033[0m Environment ready (%s packages, %sMB)\n" "$PKG_COUNT" "$SIZE_MB"
printf "\033[1;36m==>\033[0m Note: dependencies installed to ./transfer_liked_songs.venv — safe to delete anytime\n"
printf "\033[1;36m==>\033[0m Launching...\n"
"$VPY" gui_transfer.py || fail "The app closed unexpectedly. See the messages above for details."
