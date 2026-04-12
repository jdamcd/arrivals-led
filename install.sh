#!/usr/bin/env bash
#
# One-shot setup for arrivals-led on a Raspberry Pi 5.
# - Creates a Python venv
# - Installs Python dependencies
#
# Run once after cloning, then activate the venv with:
#     source venv/bin/activate

set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d venv ]; then
    echo "Creating venv..."
    python3 -m venv venv
fi

if ! dpkg -s libcurl4 &>/dev/null; then
    echo "Installing libcurl (required by arrivals CLI)..."
    sudo apt-get install -y libcurl4
fi

echo "Installing Python dependencies..."
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo
echo "Setup complete."
echo "Activate the venv:  source venv/bin/activate"
echo "Run the display:    python arrivals.py \"arrivals --json tfl --station 910GSHRDHST\""
