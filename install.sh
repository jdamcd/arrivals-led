#!/usr/bin/env bash
#
# One-shot setup for arrivals-led on a Raspberry Pi 5.
# - Creates a Python venv
# - Installs Python dependencies
# - Downloads the 6x10 BDF bitmap font
#
# Run once after cloning, then activate the venv with:
#     source venv/bin/activate

set -euo pipefail

cd "$(dirname "$0")"

FONT_URL="https://raw.githubusercontent.com/hzeller/rpi-rgb-led-matrix/master/fonts/6x10.bdf"
FONT_PATH="fonts/6x10.bdf"

if [ ! -d venv ]; then
    echo "Creating venv..."
    python3 -m venv venv
fi

echo "Installing Python dependencies..."
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

mkdir -p fonts
# Re-download if the file is missing OR doesn't start with a BDF header
# (a prior install.sh run could have left a stale/corrupt file behind).
if [ ! -f "$FONT_PATH" ] || ! head -n 1 "$FONT_PATH" | grep -q "^STARTFONT"; then
    echo "Downloading $FONT_PATH..."
    curl -fsSL -o "$FONT_PATH" "$FONT_URL"
else
    echo "$FONT_PATH already present."
fi

echo
echo "Setup complete."
echo "Activate the venv:  source venv/bin/activate"
echo "Run the display:    python arrivals.py \"arrivals --json tfl --station 910GSHRDHST\""
