# LED Matrix Arrivals

Public-transit arrivals on a 128×32 HUB75 LED display, driven by a Raspberry Pi 5. This is the Python-side renderer that calls the [arrivals-kmp](https://github.com/jdamcd/arrivals-kmp) CLI for data.

## Hardware

- Raspberry Pi 5
- [Adafruit RGB Matrix Bonnet](https://www.adafruit.com/product/3211)
- 2× 64×32 HUB75 RGB LED panels, chained horizontally → 128×32
- 5V power supply (10A recommended to power both panels)

### Chaining the panels

1. The Bonnet's HUB75 socket feeds panel 1's **IN**
2. Run a HUB75 ribbon from panel 1's **OUT** to panel 2's **IN**
3. Power both panels off the same 5V rail
4. The library treats the pair as a single 128×32 display via `Geometry(width=128, height=32, ...)`

If panel 2 appears flipped or mirrored, either flip it physically or change `Orientation.Normal` to `Orientation.R180` in `arrivals.py`.

## Pi software setup

Follow the Adafruit [Pi 5 RGB Matrix Panel guide](https://learn.adafruit.com/rgb-matrix-panels-with-raspberry-pi-5) first to install the Piomatter library system deps and udev rules (so `/dev/pio0` is accessible without `sudo`).

Clone this repo on the Pi and run:

```bash
./install.sh
```

This creates the venv and installs dependencies. The bundled bitmap font was generated based on [London Underground Dot Matrix Regular](https://github.com/petykowski/London-Underground-Dot-Matrix-Typeface).

## Install the arrivals CLI

The Python script calls the `arrivals` binary from [jdamcd/arrivals-kmp](https://github.com/jdamcd/arrivals-kmp). This requires Java:

```bash
sudo apt install default-jdk
```

Follow the install instructions in that repo to build the CLI and put `arrivals` on your `PATH`. Verify:

```bash
arrivals --json tfl --station 910GSHRDHST | jq
```

You should get a JSON object with `station` and `arrivals` fields.

## Running

```bash
source venv/bin/activate
python arrivals.py "arrivals --json tfl --station 910GSHRDHST --platform 2"
```

## Optional: auto-start via systemd (user service)

A template unit is in `systemd/arrivals-led.service`. To install:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/arrivals-led.service ~/.config/systemd/user/
# Edit the ExecStart line to point at your preferred station.
systemctl --user daemon-reload
systemctl --user enable --now arrivals-led
loginctl enable-linger $USER   # so it runs when you're not logged in
```

## Troubleshooting

- **`/dev/pio0: permission denied`** — the Adafruit udev rule isn't in place. Re-check the Pi 5 guide's udev section.
- **Second panel blank / mirrored** — check the HUB75 ribbon between panels and try `Orientation.R180`.
- **Panels flicker or glitch under load** — usually a power-supply issue; check the 5V rail under load.
