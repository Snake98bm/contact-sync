"""The app icon: a white person between two sync arrows on a teal tile.
One shape list drives both the SVG (browser tabs) and the PNGs (phone home screens, bookmarks),
drawn here with the standard library so no image packages are needed."""
import os
import struct
import zlib

TEAL, WHITE = (13, 128, 120), (255, 255, 255)

# all coordinates on a 100 x 100 canvas; later shapes paint over earlier ones
SHAPES = [
    ("rrect", (0, 0, 100, 100, 22), TEAL),
    # top arrow, pointing right
    ("line", (24, 21, 74, 21, 6), WHITE),
    ("line", (74, 21, 65, 13, 6), WHITE),
    ("line", (74, 21, 65, 29, 6), WHITE),
    # person
    ("circle", (50, 43, 11), WHITE),
    ("rrect", (30, 58, 70, 74, 12), WHITE),
    # bottom arrow, pointing left
    ("line", (76, 85, 26, 85, 6), WHITE),
    ("line", (26, 85, 35, 77, 6), WHITE),
    ("line", (26, 85, 35, 93, 6), WHITE),
]


def _hex(c):
    return "#%02x%02x%02x" % c


def svg():
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">']
    for kind, a, c in SHAPES:
        if kind == "rrect":
            x0, y0, x1, y1, r = a
            out.append(f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" rx="{r}" fill="{_hex(c)}"/>')
        elif kind == "circle":
            out.append(f'<circle cx="{a[0]}" cy="{a[1]}" r="{a[2]}" fill="{_hex(c)}"/>')
        else:
            x0, y0, x1, y1, w = a
            out.append(f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y1}" stroke="{_hex(c)}" '
                       f'stroke-width="{w}" stroke-linecap="round"/>')
    out.append("</svg>")
    return "".join(out)


def _inside(kind, a, x, y):
    if kind == "rrect":
        x0, y0, x1, y1, r = a
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            return False
        cx = min(max(x, x0 + r), x1 - r)
        cy = min(max(y, y0 + r), y1 - r)
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r
    if kind == "circle":
        return (x - a[0]) ** 2 + (y - a[1]) ** 2 <= a[2] ** 2
    x0, y0, x1, y1, w = a          # round-capped thick line
    dx, dy = x1 - x0, y1 - y0
    t = max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / (dx * dx + dy * dy)))
    return (x - x0 - t * dx) ** 2 + (y - y0 - t * dy) ** 2 <= (w / 2) ** 2


def png(size, full_bleed=False):
    """full_bleed: square background with no transparent corners (iOS rounds it itself)."""
    ss = 3                                  # 3x3 supersampling for smooth edges
    shapes = list(SHAPES)
    if full_bleed:
        shapes[0] = ("rrect", (0, 0, 100, 100, 0), TEAL)
    rows = []
    step = 100.0 / (size * ss)
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            r = g = b = a = 0.0
            for sy in range(ss):
                y = (py * ss + sy + 0.5) * step
                for sx in range(ss):
                    x = (px * ss + sx + 0.5) * step
                    col = None
                    for kind, args, c in shapes:
                        if _inside(kind, args, x, y):
                            col = c
                    if col:
                        r += col[0]; g += col[1]; b += col[2]; a += 1
            n = ss * ss
            if a:
                row += bytes((round(r / a), round(g / a), round(b / a), round(255 * a / n)))
            else:
                row += b"\0\0\0\0"
        rows.append(bytes(row))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


MANIFEST = """{"name":"Contact Sync","short_name":"Contacts","start_url":"/","display":"standalone",
"background_color":"#f6f7f9","theme_color":"#0d8078","icons":[
{"src":"/icon-192.png","sizes":"192x192","type":"image/png"},
{"src":"/icon-512.png","sizes":"512x512","type":"image/png"},
{"src":"/favicon.svg","sizes":"any","type":"image/svg+xml"}]}"""

_cache = {}
STATIC = os.environ.get("ICON_DIR", "/app/static")
FILES = ("favicon.svg", "favicon.ico", "apple-touch-icon.png", "icon-192.png", "icon-512.png")


def build(folder=STATIC):
    """Draw every icon once at image build time (the 512 px one takes a while in pure Python)."""
    os.makedirs(folder, exist_ok=True)
    for f in FILES:
        data = get(f)[0]        # draw first: get() would read the file we are about to write
        if not data:
            raise RuntimeError(f"icon {f} came out empty")
        with open(os.path.join(folder, f), "wb") as fh:
            fh.write(data)


def get(name):
    """Return (bytes, content type) for an icon path, or None. Uses the pre-drawn files when present."""
    if name not in _cache and name in FILES:
        try:
            with open(f"{STATIC}/{name}", "rb") as fh:
                data = fh.read()
            if data:
                _cache[name] = (data, "image/svg+xml" if name.endswith(".svg") else "image/png")
        except OSError:
            pass
    if name not in _cache:
        if name == "favicon.svg":
            _cache[name] = (svg().encode(), "image/svg+xml")
        elif name == "favicon.ico":
            _cache[name] = (png(32), "image/png")
        elif name == "apple-touch-icon.png":
            _cache[name] = (png(180, full_bleed=True), "image/png")
        elif name == "icon-192.png":
            _cache[name] = (png(192), "image/png")
        elif name == "icon-512.png":
            _cache[name] = (png(512), "image/png")
        elif name == "manifest.webmanifest":
            _cache[name] = (MANIFEST.encode(), "application/manifest+json")
        else:
            return None
    return _cache[name]
