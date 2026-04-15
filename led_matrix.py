"""HUB75 LED matrix drivers for the Adafruit RGB Matrix Bonnet.

Backends:
    - piomatter: Raspberry Pi 5 (PIO-based)
    - rgbmatrix: Pi Zero 2 / 3 / 4 (hzeller's rpi-rgb-led-matrix)

Use detect_driver() to auto-select from /proc/device-tree/model, then
create_driver(name, width, height, n_addr_lines, ...) to instantiate.
"""

from PIL import Image


# Maps an RGB-sequence character to its index in a standard RGB tuple, so
# "RBG" -> [0, 2, 1] for framebuffer channel reordering.
_RGB_INDEX = {"R": 0, "G": 1, "B": 2}


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

    def __init__(self, width, height, n_addr_lines, rgb_sequence="RGB"):
        import adafruit_blinka_raspberry_pi5_piomatter as piomatter
        import numpy as np

        self._np = np
        # Piomatter has no RGB-sequence option, so we reorder channels in
        # software when copying the PIL frame into the framebuffer.
        self._channel_perm = [_RGB_INDEX[c] for c in rgb_sequence]
        self.frame_img = Image.new("RGB", (width, height), (0, 0, 0))
        # np.asarray returns a read-only view of the PIL buffer; + 0 forces a writable copy.
        self._framebuffer = np.asarray(self.frame_img) + 0
        geometry = piomatter.Geometry(
            width=width,
            height=height,
            n_addr_lines=n_addr_lines,
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
        self._framebuffer[:] = self._np.asarray(self.frame_img)[:, :, self._channel_perm]
        self._matrix.show()


class RGBMatrixDriver:
    """Drives HUB75 panels via hzeller's rpi-rgb-led-matrix (Pi Zero 2, 3, 4)."""

    def __init__(self, width, height, rgb_sequence="RGB", gpio_slowdown=2):
        from rgbmatrix import RGBMatrix, RGBMatrixOptions

        # 64x32 HUB75 panels chained horizontally.
        options = RGBMatrixOptions()
        options.rows = 32
        options.cols = 64
        options.chain_length = width // 64
        options.hardware_mapping = "adafruit-hat"
        options.led_rgb_sequence = rgb_sequence
        options.gpio_slowdown = gpio_slowdown
        # The Bonnet has no PWM circuit on the OE line.
        options.disable_hardware_pulsing = True
        self._matrix = RGBMatrix(options=options)
        self.frame_img = Image.new("RGB", (width, height), (0, 0, 0))
        self._canvas = self._matrix.CreateFrameCanvas()

    def commit(self):
        """Push PIL frame to the matrix via double-buffered swap."""
        self._canvas.SetImage(self.frame_img)
        self._canvas = self._matrix.SwapOnVSync(self._canvas)


def create_driver(name, width, height, n_addr_lines,
                  rgb_sequence="RGB", gpio_slowdown=2):
    if name == "piomatter":
        return PiomatterDriver(width, height, n_addr_lines, rgb_sequence=rgb_sequence)
    if name == "rgbmatrix":
        return RGBMatrixDriver(width, height, rgb_sequence=rgb_sequence,
                               gpio_slowdown=gpio_slowdown)
    raise ValueError(f"Unknown driver: {name}")
