#!/usr/bin/env bash
#
# Setup for arrivals-led on a Raspberry Pi.
# - Creates a Python venv
# - Installs Python dependencies and the appropriate LED matrix driver
#
# Run once after cloning, then activate the venv with:
#     source venv/bin/activate

set -euo pipefail

cd "$(dirname "$0")"

PI_MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "unknown")
echo "Detected: $PI_MODEL"

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

if echo "$PI_MODEL" | grep -q "Pi 5"; then
    echo "Installing Piomatter driver for Pi 5..."
    ./venv/bin/pip install numpy adafruit-blinka-raspberry-pi5-piomatter
else
    RGBMATRIX_REPO="$HOME/rpi-rgb-led-matrix"
    if [ -d "$RGBMATRIX_REPO" ]; then
        echo "Building rgbmatrix Python bindings..."
        ./venv/bin/pip install setuptools
        make -C "$RGBMATRIX_REPO" install-python PYTHON="$(pwd)/venv/bin/python"
    else
        echo ""
        echo "WARNING: $RGBMATRIX_REPO not found."
        echo "Run the Adafruit RGB Matrix Bonnet install script first:"
        echo "  curl https://raw.githubusercontent.com/adafruit/Raspberry-Pi-Installer-Scripts/main/rgb-matrix.sh >rgb-matrix.sh"
        echo "  sudo bash rgb-matrix.sh"
        exit 1
    fi
fi

echo ""
echo "Setup complete."
echo "Activate the venv:  source venv/bin/activate"
echo "Run the display:    sudo venv/bin/python arrivals.py \"arrivals --json tfl --station 910GSHRDHST --platform 2\""
