#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
PYTHON=/opt/homebrew/opt/python@3.12/bin/python3.12
VENV="$PROJECT_ROOT/.venv/desktop-macos"
APP="$SCRIPT_DIR/dist/Remote Explorer.app"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "error: macOS is required" >&2
    exit 1
fi

if [ "$(uname -m)" != "arm64" ]; then
    echo "error: this MVP build targets Apple Silicon arm64" >&2
    exit 1
fi

if [ ! -x "$PYTHON" ]; then
    echo "error: Homebrew python@3.12 is required at $PYTHON" >&2
    exit 1
fi

"$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install --disable-pip-version-check \
    -r "$PROJECT_ROOT/requirements-desktop-macos.txt"

rm -rf "$SCRIPT_DIR/build" "$SCRIPT_DIR/dist"
(
    cd "$SCRIPT_DIR"
    PYTHONDONTWRITEBYTECODE=1 "$VENV/bin/python" setup.py py2app
)

test -d "$APP"
test -x "$APP/Contents/MacOS/Remote Explorer"
test -x "$APP/Contents/MacOS/sshbridge_broker"
test -f "$APP/Contents/Resources/web_assets/index.html"
test -f "$APP/Contents/Resources/web_assets/app.js"
test -f "$APP/Contents/Resources/web_assets/styles.css"

printf '%s\n' "$APP"
