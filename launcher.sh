#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENV_DIR="$ROOT_DIR/.venv"
LOCAL_PYTHON="$VENV_DIR/bin/python"
REQUIREMENTS_FILE="$ROOT_DIR/requirements.txt"
DEPENDENCY_CHECKER="$ROOT_DIR/tools/check_requirements.py"

PYTHON_CMD=""
BOOTSTRAP_PYTHON=""

if [ -x "$LOCAL_PYTHON" ]; then
    PYTHON_CMD="$LOCAL_PYTHON"
fi

if command -v python3 >/dev/null 2>&1; then
    BOOTSTRAP_PYTHON=$(command -v python3)
elif command -v python >/dev/null 2>&1; then
    BOOTSTRAP_PYTHON=$(command -v python)
fi

if [ -z "$PYTHON_CMD" ] && [ -z "$BOOTSTRAP_PYTHON" ]; then
    echo "Python 3 was not found in PATH." >&2
    echo "Expected local interpreter: $LOCAL_PYTHON" >&2
    exit 1
fi

if [ ! -x "$LOCAL_PYTHON" ]; then
    echo "Local virtual environment was not found. Creating .venv..."
    "$BOOTSTRAP_PYTHON" -m venv "$VENV_DIR"
    PYTHON_CMD="$LOCAL_PYTHON"
fi

if [ ! -f "$REQUIREMENTS_FILE" ]; then
    echo "requirements.txt was not found: $REQUIREMENTS_FILE" >&2
    exit 1
fi

if [ ! -f "$DEPENDENCY_CHECKER" ]; then
    echo "Dependency checker was not found: $DEPENDENCY_CHECKER" >&2
    exit 1
fi

echo "Checking Python dependencies..."
if ! "$PYTHON_CMD" "$DEPENDENCY_CHECKER" "$REQUIREMENTS_FILE"; then
    echo "Missing or incompatible dependencies detected. Installing requirements..."
    "$PYTHON_CMD" -m pip install --disable-pip-version-check -r "$REQUIREMENTS_FILE"

    echo "Re-checking Python dependencies..."
    if ! "$PYTHON_CMD" "$DEPENDENCY_CHECKER" "$REQUIREMENTS_FILE"; then
        echo "Dependencies are still missing or incompatible after installation." >&2
        exit 1
    fi
fi

echo "Starting RocketCatShell..."
exec "$PYTHON_CMD" -m rocketcat_shell "$@"