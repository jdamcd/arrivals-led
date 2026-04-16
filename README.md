# LED Matrix Arrivals

Public transit arrivals on a 128×32 HUB75 LED display, driven by a Raspberry Pi. This is the Python-side renderer that calls the [arrivals-kmp](https://github.com/jdamcd/arrivals-kmp) CLI for data.

![LED matrix showing train arrivals](readme-img/led_matrix.jpeg)

## Hardware

Common setup:
- [Adafruit RGB Matrix Bonnet](https://www.adafruit.com/product/3211)
- 2× 64×32 HUB75 RGB LED panels (2.5mm pitch), chained horizontally → 128×32
- 5V power supply (10A recommended to power both panels)

Supported Pi boards:
- **Raspberry Pi 5** — uses the [Piomatter](https://github.com/adafruit/Adafruit_Blinka_Raspberry_Pi5_Piomatter) driver (PIO-based)
- **Raspberry Pi Zero 2 W** — uses hzeller's [rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) driver via the Adafruit install script

### Chaining the panels

1. The Bonnet's HUB75 socket feeds panel 1's **IN**
2. Run a HUB75 ribbon from panel 1's **OUT** to panel 2's **IN**
3. Power both panels from the same 5V rail
4. The library treats the pair as a single 128×32 display via `Geometry(width=128, height=32, ...)`

If panel 2 appears flipped or mirrored, either flip it physically or change `Orientation.Normal` to `Orientation.R180` in `led_matrix.py`.

## Pi software setup

### Raspberry Pi 5

Follow the Adafruit [Pi 5 RGB Matrix Panel guide](https://learn.adafruit.com/rgb-matrix-panels-with-raspberry-pi-5) first to install the Piomatter library system deps and udev rules (so `/dev/pio0` is accessible without `sudo`).

### Raspberry Pi Zero 2 W

Follow the Adafruit [RGB Matrix Bonnet guide](https://learn.adafruit.com/adafruit-rgb-matrix-bonnet-for-raspberry-pi) and run the install script to compile and install the `rgbmatrix` Python library.

### Install project dependencies

Clone this repo on the Pi and run:

```bash
./install.sh
```

The script detects your Pi model and installs the correct driver. On Pi 5 it installs Piomatter via pip; on Pi Zero 2 it builds the rgbmatrix Python bindings from `~/rpi-rgb-led-matrix` into the venv.

## Install the arrivals CLI

The Python script calls the `arrivals` binary from [jdamcd/arrivals-kmp](https://github.com/jdamcd/arrivals-kmp). Cross-compile the native CLI for ARM Linux from a macOS or x86 Linux machine:

```bash
./gradlew :cli:linkReleaseExecutableLinuxArm64
```

Then copy the binary to the Pi and put it on your `PATH`:

```bash
scp cli/build/bin/linuxArm64/releaseExecutable/cli.kexe <user>@<host>:/tmp/arrivals
ssh <user>@<host> 'sudo mv /tmp/arrivals /usr/local/bin/arrivals'
```

Verify on the Pi:

```bash
arrivals --json tfl --station 910GSHRDHST --platform 2
```

You should get a JSON object with `station` and `arrivals` fields.

## Running

```bash
source venv/bin/activate
python arrivals.py "arrivals --json tfl --station 910GSHRDHST --platform 2"
```

The `rgbmatrix` driver needs root for GPIO access, so on the Pi Zero 2:

```bash
sudo venv/bin/python arrivals.py "arrivals --json tfl --station 910GSHRDHST --platform 2"
```

## Optional: auto-start via systemd

### Pi 5 (user service)

There's a template in `systemd/arrivals-led.service`. To install:

```bash
# Edit the ExecStart line to point at your preferred station
mkdir -p ~/.config/systemd/user
cp systemd/arrivals-led.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now arrivals-led
loginctl enable-linger $USER   # So it runs when you're not logged in
```

### Pi Zero 2 (system service, runs as root)

There's a template in `systemd/arrivals-led-root.service`. To install:

```bash
# Edit the ExecStart line to point at your preferred station and user home
sudo cp systemd/arrivals-led-root.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now arrivals-led-root
```

## Parts

I've included a couple of models for 3D-printed parts that might be useful:

- Bracket to connect the 2x LED panels horizontally (with M3 screws)
- Riser to attach a Pi 5 or Pi Zero 2 (with M2.5 & M3 screws)

![The back of the hardware](readme-img/led_back.jpeg)

## Tips & troubleshooting

- **`/dev/pio0: permission denied`** (Pi 5): The Adafruit udev rule isn't in place. Check the Pi 5 guide's udev section.
- **Flicker during data refresh**: The LED refresh thread can be preempted by the Linux scheduler, especially when a subprocess is running. Isolating a CPU core helps on both boards:
  1. Append `isolcpus=3` to the existing line in `/boot/firmware/cmdline.txt`, then reboot.
  2. **Pi 5 only**: Install the patched Piomatter from [this branch](https://github.com/lehni/Adafruit_Blinka_Raspberry_Pi5_Piomatter/tree/pin-blit-thread-to-isolated-cpu) which pins the blit thread to the isolated core:
     ```bash
     pip install git+https://github.com/lehni/Adafruit_Blinka_Raspberry_Pi5_Piomatter.git@pin-blit-thread-to-isolated-cpu
     ```
     See [Piomatter PR #79](https://github.com/adafruit/Adafruit_Blinka_Raspberry_Pi5_Piomatter/pull/79) for details. The rgbmatrix driver picks up the isolated core automatically.
- **Colours look wrong**: The panels are assumed to be wired in RBG order. If your panels use standard RGB wiring, change `RGB_SEQUENCE = "RBG"` to `"RGB"` in `arrivals.py`.
- **Power**: If you have any power issues, try powering the Pi separately via its standard USB adapter.

## Attribution

The bundled bitmap font was generated based on [London Underground Dot Matrix Regular](https://github.com/petykowski/London-Underground-Dot-Matrix-Typeface).
