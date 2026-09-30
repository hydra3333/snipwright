#!/bin/sh
# Start the subtitle-defaults repair tool with Snipwright's own Python, so it
# has PySide6 without anything else being installed. Falls back to python3.
here="$(cd "$(dirname "$0")" && pwd)"
py="$here/../.venv/bin/python"
[ -x "$py" ] || py=python3
exec "$py" "$here/fix-subtitle-defaults.py" "$@"
