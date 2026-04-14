#!/usr/bin/env python3
"""
Arrivals LED Matrix Display

Drives 2x chained 64x32 HUB75 panels (128x32 pixels) via the Adafruit RGB
Matrix Bonnet. Fetches arrival data by calling the arrivals-kmp CLI with
--json output.

Supported hardware:
    - Raspberry Pi 5 (piomatter driver, PIO-based)
    - Raspberry Pi Zero 2 W (rgbmatrix driver, hzeller's rpi-rgb-led-matrix)

Usage:
    python3 arrivals.py "arrivals --json tfl --station 910GSHRDHST --platform 2"
"""

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time

from PIL import Image


# The font supports A-Z, a-z, 0-9, space, and - ' & * + : , .
# Mirrors filterLedChars in the shared Kotlin module.
_LED_FILTER = re.compile(r"[^a-zA-Z0-9 \-'&*+:,.]")
_LED_CHAR_MAP = str.maketrans({
    "\u2018": "'",
    "\u2019": "'",
    "\u2013": "-",
    "\u2014": "-",
})
_LED_MULTI_MAP = {
    "\u00df": "ss",
    "\u00e4": "ae",
    "\u00f6": "oe",
    "\u00fc": "ue",
    "\u00c4": "Ae",
    "\u00d6": "Oe",
    "\u00dc": "Ue",
}


def filter_led_chars(text):
    text = text.translate(_LED_CHAR_MAP)
    for src, dst in _LED_MULTI_MAP.items():
        text = text.replace(src, dst)
    return _LED_FILTER.sub("", text)


class BDFFont:
    """Minimal BDF bitmap font parser + PIL renderer.

    BDF is the classic X11 bitmap font format. We only parse the bits we
    need to render ASCII text: FONT_ASCENT/DESCENT, and per-glyph ENCODING,
    DWIDTH, BBX, and BITMAP.
    """

    def __init__(self, path):
        self.path = path
        self.glyphs = {}  # codepoint -> dict(dwidth, bbx, bitmap)
        self.ascent = 0
        self.descent = 0
        self.default_char = None
        self._parse(path)

    def _parse(self, path):
        cp = None
        dwidth = 0
        bbx = None
        bitmap = None
        in_bitmap = False
        with open(path, "r", encoding="latin-1") as f:
            for line in f:
                parts = line.split()
                if not parts:
                    continue
                kw = parts[0]
                if kw == "FONT_ASCENT":
                    self.ascent = int(parts[1])
                elif kw == "FONT_DESCENT":
                    self.descent = int(parts[1])
                elif kw == "DEFAULT_CHAR":
                    self.default_char = int(parts[1])
                elif kw == "STARTCHAR":
                    cp = None
                    dwidth = 0
                    bbx = None
                    bitmap = []
                    in_bitmap = False
                elif kw == "ENCODING":
                    cp = int(parts[1])
                elif kw == "DWIDTH":
                    dwidth = int(parts[1])
                elif kw == "BBX":
                    bbx = (int(parts[1]), int(parts[2]),
                           int(parts[3]), int(parts[4]))
                elif kw == "BITMAP":
                    in_bitmap = True
                elif kw == "ENDCHAR":
                    if cp is not None and cp >= 0 and bbx is not None:
                        self.glyphs[cp] = {
                            "dwidth": dwidth,
                            "bbx": bbx,
                            "bitmap": bitmap or [],
                        }
                    in_bitmap = False
                elif in_bitmap:
                    bitmap.append(int(parts[0], 16))

    @property
    def line_height(self):
        return self.ascent + self.descent

    def _glyph(self, cp):
        g = self.glyphs.get(cp)
        if g is None and self.default_char is not None:
            g = self.glyphs.get(self.default_char)
        return g

    def char_width(self, ch="M"):
        g = self._glyph(ord(ch))
        return g["dwidth"] if g else self.line_height

    def text_width(self, text):
        total = 0
        for ch in text:
            g = self._glyph(ord(ch))
            if g is not None:
                total += g["dwidth"]
        return total

    def truncate_to_width(self, text, max_width):
        """Return the longest prefix of text that fits within max_width pixels."""
        w = 0
        for i, ch in enumerate(text):
            g = self._glyph(ord(ch))
            cw = g["dwidth"] if g is not None else 0
            if w + cw > max_width:
                return text[:i]
            w += cw
        return text

    def truncate_from_end(self, text, max_width):
        """Return the longest suffix of text that fits within max_width pixels."""
        w = 0
        for i in range(len(text) - 1, -1, -1):
            g = self._glyph(ord(text[i]))
            cw = g["dwidth"] if g is not None else 0
            if w + cw > max_width:
                return text[i + 1:]
            w += cw
        return text

    def draw_text(self, pil_image, xy, text, fill, min_x=None, clip_x=None):
        """Draw `text` into `pil_image` with (x, y_top) at `xy`.
        Only pixels in [min_x, clip_x) are drawn (used for scroll clipping)."""
        x0, y_top = xy
        pixels = pil_image.load()
        width, height = pil_image.size
        lo_x = min_x if min_x is not None else 0
        hi_x = clip_x if clip_x is not None else width
        baseline = y_top + self.ascent
        x = x0
        for ch in text:
            g = self._glyph(ord(ch))
            if g is None:
                continue
            if x >= hi_x:
                break
            dwidth = g["dwidth"]
            if x + dwidth <= lo_x:
                x += dwidth
                continue
            bbx_w, bbx_h, bbx_x, bbx_y = g["bbx"]
            total_bits = ((bbx_w + 7) // 8) * 8
            gy_top = baseline - bbx_y - bbx_h
            gx_left = x + bbx_x
            for row_idx, row_val in enumerate(g["bitmap"]):
                py = gy_top + row_idx
                if py < 0 or py >= height:
                    continue
                for col in range(bbx_w):
                    if (row_val >> (total_bits - 1 - col)) & 1:
                        px = gx_left + col
                        if lo_x <= px < hi_x:
                            pixels[px, py] = fill
            x += dwidth
        return x


# Display constants
DISPLAY_WIDTH = 128
DISPLAY_HEIGHT = 32
N_ADDR_LINES = 4  # 64x32 panels: 16 scan rows -> log2(16) = 4
MAX_ROWS = 3
ROW_GAP = 2   # pixels between rows
LEFT_PAD = 1  # right side needs none — glyphs have a 1 px right-side bearing

# Timing (seconds)
REFRESH_INTERVAL = 60  # between CLI calls
BLINK_INTERVAL = 0.75  # animate "Due"
SCROLL_TICK = 0.05     # seconds between scroll steps (1 px/step = 20 px/sec)
SCROLL_PAUSE_START = 20.0
SCROLL_PAUSE_END = 5.0

SCROLL_END_GAP = 4     # extra pixels to scroll past the end

# 50% of LED yellow #FFDD00 (Piomatter has no brightness attribute,
# so we scale the colour constant up-front instead of the framebuffer).
BRIGHTNESS = 0.5
YELLOW_RAW = (255, 221, 0)
YELLOW = tuple(int(c * BRIGHTNESS) for c in YELLOW_RAW)
BLACK = (0, 0, 0)

# This panel is wired in "RBG" order: byte 1 drives blue, byte 2 drives
# green. The piomatter driver swaps channels when copying to the framebuffer;
# the rgbmatrix driver handles this via its led_rgb_sequence option.
CHANNEL_PERM = [0, 2, 1]


def detect_driver():
    """Auto-detect the appropriate driver from /proc/device-tree/model."""
    try:
        with open("/proc/device-tree/model", "r") as f:
            model = f.read().strip("\x00").strip()
        print(f"Detected: {model}")
        if "Pi 5" in model:
            return "piomatter"
        return "rgbmatrix"
    except OSError:
        return "piomatter"


class PiomatterDriver:
    """Drives HUB75 panels on a Raspberry Pi 5 via PIO hardware."""

    def __init__(self):
        import adafruit_blinka_raspberry_pi5_piomatter as piomatter
        import numpy as np

        self._np = np
        self.frame_img = Image.new("RGB", (DISPLAY_WIDTH, DISPLAY_HEIGHT), BLACK)
        self._framebuffer = np.asarray(self.frame_img) + 0  # mutable numpy copy
        geometry = piomatter.Geometry(
            width=DISPLAY_WIDTH,
            height=DISPLAY_HEIGHT,
            n_addr_lines=N_ADDR_LINES,
            rotation=piomatter.Orientation.Normal,
        )
        self._matrix = piomatter.PioMatter(
            colorspace=piomatter.Colorspace.RGB888Packed,
            pinout=piomatter.Pinout.AdafruitMatrixBonnet,
            framebuffer=self._framebuffer,
            geometry=geometry,
        )

    def commit(self):
        """Copy PIL frame into the piomatter framebuffer with channel swap."""
        self._framebuffer[:] = self._np.asarray(self.frame_img)[:, :, CHANNEL_PERM]
        self._matrix.show()


class RGBMatrixDriver:
    """Drives HUB75 panels via hzeller's rpi-rgb-led-matrix (Pi Zero 2, 3, 4)."""

    def __init__(self, gpio_slowdown=2):
        from rgbmatrix import RGBMatrix, RGBMatrixOptions

        options = RGBMatrixOptions()
        options.rows = 32
        options.cols = 64
        options.chain_length = 2
        options.hardware_mapping = "adafruit-hat"
        options.led_rgb_sequence = "RBG"
        options.gpio_slowdown = gpio_slowdown
        # The Bonnet has no PWM circuit on the OE line.
        options.disable_hardware_pulsing = True
        self._matrix = RGBMatrix(options=options)
        self.frame_img = Image.new("RGB", (DISPLAY_WIDTH, DISPLAY_HEIGHT), BLACK)
        self._canvas = self._matrix.CreateFrameCanvas()

    def commit(self):
        """Push PIL frame to the matrix via double-buffered swap."""
        self._canvas.SetImage(self.frame_img)
        self._canvas = self._matrix.SwapOnVSync(self._canvas)


def create_driver(name, gpio_slowdown=2):
    if name == "piomatter":
        return PiomatterDriver()
    if name == "rgbmatrix":
        return RGBMatrixDriver(gpio_slowdown=gpio_slowdown)
    raise ValueError(f"Unknown driver: {name}")


_PAUSE_START = 0
_SCROLLING = 1
_PAUSE_END = 2


class TextScroller:
    """Synchronised pause-scroll-pause animation for overflowing rows.

    All rows share a single phase clock so they start, scroll, and reset
    together. Rows with less overflow hold at their end position until the
    longest row catches up.
    """

    def __init__(self):
        self._rows = [{"offset": 0, "overflow": 0, "name": ""}
                      for _ in range(MAX_ROWS)]
        self._phase = _PAUSE_START
        self._phase_time = 0.0
        self._any_overflow = False

    def configure(self, row, name, overflow):
        """Set name/overflow for a row. Returns True if anything changed."""
        r = self._rows[row]
        if name == r["name"] and overflow == r["overflow"]:
            return False
        r["name"] = name
        r["offset"] = 0
        r["overflow"] = overflow
        self._any_overflow = any(s["overflow"] > 0 for s in self._rows)
        return True

    def reset_phase(self, now):
        """Zero all offsets and restart the shared pause-scroll cycle."""
        for r in self._rows:
            r["offset"] = 0
        self._phase = _PAUSE_START
        self._phase_time = now

    def get_offset(self, row):
        return self._rows[row]["offset"]

    def is_at_end(self, row):
        """True if the row has scrolled to (or is holding at) its end position."""
        r = self._rows[row]
        return r["overflow"] > 0 and r["offset"] >= r["overflow"]

    def tick(self, now):
        """Advance shared scroll state by one step. Returns True if a redraw is needed."""
        if not self._any_overflow:
            return False

        if self._phase == _PAUSE_START:
            if now - self._phase_time >= SCROLL_PAUSE_START:
                self._phase = _SCROLLING
            return False

        if self._phase == _SCROLLING:
            changed = False
            done = True
            for r in self._rows:
                ov = r["overflow"]
                if ov <= 0:
                    continue
                if r["offset"] < ov:
                    r["offset"] += 1
                    changed = True
                    if r["offset"] < ov:
                        done = False
            if done:
                self._phase = _PAUSE_END
                self._phase_time = now
            return changed

        if self._phase == _PAUSE_END:
            if now - self._phase_time >= SCROLL_PAUSE_END:
                for r in self._rows:
                    if r["overflow"] > 0:
                        r["offset"] = 0
                self._phase = _PAUSE_START
                self._phase_time = now
                return True
            return False

        return False


def fetch_arrivals(cmd):
    try:
        result = subprocess.run(
            shlex.split(cmd),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            print(f"CLI error: {result.stderr.strip()}", file=sys.stderr)
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        print("CLI timed out", file=sys.stderr)
        return None
    except (json.JSONDecodeError, OSError) as e:
        print(f"Fetch error: {e}", file=sys.stderr)
        return None


def prepare_rows(font, layout, data, short_times=False):
    """Pre-compute per-row display data from arrival data."""
    gap_px = layout["gap_px"]
    arrivals = (data or {}).get("arrivals", [])
    rows = []
    for arrival in arrivals[:MAX_ROWS]:
        name = filter_led_chars(arrival["displayName"])
        time_str = arrival["displayTime"]
        is_due = arrival["isDue"]
        if short_times:
            time_str = time_str.replace(" min", "")
            if is_due:
                time_str = "0"
                is_due = False
        time_width = font.text_width(time_str)
        name_clip_x = DISPLAY_WIDTH - time_width - gap_px
        name_budget = name_clip_x - LEFT_PAD
        # Ignore 1 px of overflow — that's the glyph's trailing bearing, not
        # a missing pixel.
        raw_overflow = font.text_width(name) - name_budget
        overflow = raw_overflow + SCROLL_END_GAP if raw_overflow > 1 else 0
        # Char-boundary truncation for the paused states so no partial glyph
        # shows at either edge. display_name_end's SCROLL_END_GAP margin
        # matches where the scroll animation lands at the end.
        if overflow:
            display_name = font.truncate_to_width(name, name_budget)
            display_name_end = font.truncate_from_end(name, name_budget - SCROLL_END_GAP)
        else:
            display_name = name
            display_name_end = name
        end_width = font.text_width(display_name_end)
        rows.append({
            "name": name, "display_name": display_name,
            "display_name_end": display_name_end, "end_width": end_width,
            "time_str": time_str, "time_width": time_width,
            "name_clip_x": name_clip_x, "overflow": overflow,
            "is_due": is_due,
        })
    return rows


def update_scroll_state(scroller, rows, now):
    """Configure scroller from prepared row data. Resets the shared phase
    clock if any row changed, so newly-overflowing rows don't start mid-scroll."""
    changed = False
    for i in range(MAX_ROWS):
        name, overflow = ("", 0)
        if i < len(rows):
            name, overflow = rows[i]["name"], rows[i]["overflow"]
        if scroller.configure(i, name, overflow):
            changed = True
    if changed:
        scroller.reset_phase(now)


def render(driver, font, layout, rows, blink_on, scroller):
    row_tops = layout["row_tops"]
    frame_img = driver.frame_img
    frame_img.paste(BLACK, (0, 0, DISPLAY_WIDTH, DISPLAY_HEIGHT))

    if not rows:
        font.draw_text(frame_img, (LEFT_PAD, row_tops[1]), "No data", YELLOW)
    else:
        for i, row in enumerate(rows):
            y = row_tops[i]
            offset = scroller.get_offset(i)

            if scroller.is_at_end(i):
                end_x = row["name_clip_x"] - SCROLL_END_GAP - row["end_width"]
                font.draw_text(frame_img, (end_x, y), row["display_name_end"], YELLOW)
            elif offset == 0:
                font.draw_text(frame_img, (LEFT_PAD, y), row["display_name"], YELLOW)
            else:
                font.draw_text(frame_img, (LEFT_PAD - offset, y), row["name"], YELLOW,
                               min_x=LEFT_PAD, clip_x=row["name_clip_x"])
            if blink_on or not row["is_due"]:
                font.draw_text(
                    frame_img,
                    (DISPLAY_WIDTH - row["time_width"], y),
                    row["time_str"],
                    YELLOW,
                )

    driver.commit()


_FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "LUR.bdf")


def load_font():
    """Load the bundled bitmap font."""
    return BDFFont(_FONT_PATH)


def measure_layout(font):
    """Compute per-row top-y positions and the name/time gap in pixels."""
    char_height = max(1, font.line_height)

    # Gap between name and time: one pixel less than a space glyph (the
    # font already leaves ~1 px of right-side bearing on each side).
    gap_px = max(2, font.char_width(" ") - 1)

    row_tops = [i * (char_height + ROW_GAP) for i in range(MAX_ROWS)]

    return {
        "row_tops": row_tops,
        "char_height": char_height,
        "gap_px": gap_px,
    }


def main():
    parser = argparse.ArgumentParser(description="Arrivals LED matrix display")
    parser.add_argument("command", help="CLI command to fetch arrivals JSON")
    parser.add_argument(
        "--refresh",
        type=int,
        default=REFRESH_INTERVAL,
        help=f"Refresh interval in seconds (default: {REFRESH_INTERVAL})",
    )
    parser.add_argument(
        "--driver",
        choices=["auto", "piomatter", "rgbmatrix"],
        default="auto",
        help="LED matrix driver (default: auto-detect from Pi model)",
    )
    parser.add_argument(
        "--short-times",
        action="store_true",
        help='Drop " min" from arrival times to show more of the destination',
    )
    parser.add_argument(
        "--gpio-slowdown",
        type=int,
        default=2,
        help="GPIO slowdown for rgbmatrix driver (default: 2, try 4 for Pi 4)",
    )
    args = parser.parse_args()

    driver_name = args.driver
    if driver_name == "auto":
        driver_name = detect_driver()

    font = load_font()
    layout = measure_layout(font)

    driver = create_driver(driver_name, gpio_slowdown=args.gpio_slowdown)
    scroller = TextScroller()

    data = None
    rows = []
    last_fetch = 0
    last_data_id = None
    blink_on = True
    last_blink = time.monotonic()
    last_scroll_tick = time.monotonic()

    # Translate SIGTERM (e.g. `systemctl stop`) into the same clean shutdown
    # path as Ctrl+C, so the display is cleared before the process exits.
    def _raise_interrupt(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _raise_interrupt)

    print(f"Starting LED display ({driver_name}), refreshing every {args.refresh}s")
    print(f"Command: {args.command}")
    print(f"Layout: row tops {layout['row_tops']}, "
          f"line height {layout['char_height']}, gap {layout['gap_px']}px")

    try:
        while True:
            now = time.monotonic()
            need_redraw = False

            if now - last_fetch >= args.refresh:
                data = fetch_arrivals(args.command) or data  # keep stale on failure
                last_fetch = now

            if id(data) != last_data_id:
                rows = prepare_rows(font, layout, data, args.short_times)
                update_scroll_state(scroller, rows, now)
                last_data_id = id(data)
                need_redraw = True

            if now - last_blink >= BLINK_INTERVAL:
                blink_on = not blink_on
                last_blink = now
                need_redraw = True

            if now - last_scroll_tick >= SCROLL_TICK:
                if scroller.tick(now):
                    need_redraw = True
                last_scroll_tick = now

            if need_redraw:
                render(driver, font, layout, rows, blink_on, scroller)

            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\nShutting down")
        driver.frame_img.paste(BLACK, (0, 0, DISPLAY_WIDTH, DISPLAY_HEIGHT))
        driver.commit()


if __name__ == "__main__":
    main()
