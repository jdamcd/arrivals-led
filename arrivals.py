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

from bitmap_font import BDFFont
from led_matrix import create_driver, detect_driver


# ---- Configuration ----

# Foreground colour and brightness. Piomatter has no brightness attribute,
# so we scale the colour up-front instead of the framebuffer.
YELLOW_RAW = (255, 221, 0)  # LED yellow #FFDD00
BRIGHTNESS = 0.5

# Arrivals refresh interval in seconds.
REFRESH_INTERVAL = 60

# Animation timings in seconds.
BLINK_INTERVAL = 0.75       # "Due" pulse rate
SCROLL_PAUSE_START = 20.0   # hold before scrolling long destinations
SCROLL_PAUSE_END = 5.0      # hold at the end of the scroll
SCROLL_TICK = 0.05          # seconds per 1-pixel scroll step (20 px/sec)

# Panel geometry. Defaults assume 2x 64x32 HUB75 panels chained horizontally.
DISPLAY_WIDTH = 128
DISPLAY_HEIGHT = 32
N_ADDR_LINES = 4            # 1/16 scan on 32-row panels -> log2(16) = 4
RGB_SEQUENCE = "RBG"        # panel wiring; use "RGB" if R/G/B aren't swapped
GPIO_SLOWDOWN = 2           # rgbmatrix only; try 4 on Pi 4 if the display glitches

# ---- End configuration ----


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


# Layout internals (tied to the bundled LUR.bdf font).
MAX_ROWS = 3
ROW_GAP = 2           # pixels between rows
LEFT_PAD = 1          # right side needs none — glyphs have a 1 px right-side bearing
SCROLL_END_GAP = 4    # extra pixels to scroll past the end of the destination


YELLOW = tuple(int(c * BRIGHTNESS) for c in YELLOW_RAW)
BLACK = (0, 0, 0)


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
        name_width = font.text_width(name)
        # Ignore 1 px of overflow — that's the glyph's trailing bearing, not
        # a missing pixel.
        if name_width - name_budget > 1:
            # Both paused states use character-aligned truncations so no partial
            # glyph shows at the edges. overflow is sized so the scroll lands
            # with display_name_end's first character at LEFT_PAD.
            display_name = font.truncate_to_width(name, name_budget)
            display_name_end = font.truncate_from_end(name, name_budget - SCROLL_END_GAP)
            overflow = name_width - font.text_width(display_name_end)
        else:
            display_name = name
            display_name_end = name
            overflow = 0
        rows.append({
            "name": name, "display_name": display_name,
            "display_name_end": display_name_end,
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
                font.draw_text(frame_img, (LEFT_PAD, y), row["display_name_end"], YELLOW)
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
        "--short-times",
        action="store_true",
        help='Drop " min" from arrival times to show more of the destination',
    )
    args = parser.parse_args()

    driver_name = detect_driver()

    font = load_font()
    layout = measure_layout(font)

    driver = create_driver(
        driver_name,
        width=DISPLAY_WIDTH,
        height=DISPLAY_HEIGHT,
        n_addr_lines=N_ADDR_LINES,
        rgb_sequence=RGB_SEQUENCE,
        gpio_slowdown=GPIO_SLOWDOWN,
    )
    scroller = TextScroller()

    data = None
    rows = []
    last_fetch = 0
    last_data = None
    blink_on = True
    last_blink = time.monotonic()
    last_scroll_tick = time.monotonic()

    # Translate SIGTERM (e.g. `systemctl stop`) into the same clean shutdown
    # path as Ctrl+C, so the display is cleared before the process exits.
    def _raise_interrupt(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _raise_interrupt)

    print(f"Starting LED display ({driver_name}), refreshing every {REFRESH_INTERVAL}s")
    print(f"Command: {args.command}")
    print(f"Layout: row tops {layout['row_tops']}, "
          f"line height {layout['char_height']}, gap {layout['gap_px']}px")

    try:
        while True:
            now = time.monotonic()
            need_redraw = False

            if now - last_fetch >= REFRESH_INTERVAL:
                data = fetch_arrivals(args.command) or data  # keep stale on failure
                last_fetch = now

            if data is not last_data:
                rows = prepare_rows(font, layout, data, args.short_times)
                update_scroll_state(scroller, rows, now)
                last_data = data
                need_redraw = True

            if now - last_blink >= BLINK_INTERVAL:
                blink_on = not blink_on
                last_blink = now
                if any(row["is_due"] for row in rows):
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
