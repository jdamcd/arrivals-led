"""BDF bitmap font parser and PIL renderer.

BDF is the classic X11 bitmap font format. This parser reads only the
fields needed to render text: FONT_ASCENT/DESCENT, DEFAULT_CHAR, and
per-glyph ENCODING, DWIDTH, BBX, BITMAP.
"""


class BDFFont:
    """Bitmap font loaded from a BDF file."""

    def __init__(self, path):
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
        Only pixels in the horizontal clip window [min_x, clip_x) are drawn."""
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
