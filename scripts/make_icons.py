"""Draws VALR's icons from its mark: three pen-like ink strokes stepping down into a V (the
"Signal V", Porcelain & Ink). One geometry, defined here, feeds every size.

    python scripts/make_icons.py          # static/icons/: the SVG marks, favicons, PWA icons, badge
    python scripts/make_icons.py --mac    # mac/VALRRecorderSync/Resources/: the menu-bar template

The mark (viewBox 0 0 100 100): three strokes centred at x 30, 50, 70; the outer two run
y 9-61, the centre one y 39-91 (the V's point, the voice's peak). Each is 10 wide through
its middle and eases to a fine rounded tip over the last fifth of its length. Outer strokes
are Ink (#15172C), the centre one Muted Indigo (#4B4C9E); on dark, Porcelain (#F3EFE6) and
Indigo Mist (#A9AAF0). The app icons put it on a warm porcelain tile.

The Mac app icon (AppIcon-1024.png) is drawn by mac/dmg/make_app_icon.swift from the same
numbers. Pure Python (no Pillow): curves are flattened to polygons and filled with exact
horizontal coverage and vertical supersampling, so the icons are crisp at every size.
"""
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "static" / "icons"
MAC_OUT = ROOT / "mac" / "VALRRecorderSync" / "Resources"

INK, PORCELAIN, INDIGO, MIST = (0x15, 0x17, 0x2C), (0xF3, 0xEF, 0xE6), (0x4B, 0x4C, 0x9E), (0xA9, 0xAA, 0xF0)
TILE_TOP, TILE_BOTTOM = (0xF7, 0xF4, 0xEE), (0xE8, 0xE2, 0xD5)  # The porcelain tile, lit from above.
BARS = [(30, 9, 61), (50, 39, 91), (70, 9, 61)]  # (centre x, top y, bottom y) in the 100-unit box.
WIDTH = 10
SS = 6  # Vertical supersampling.


def bar_segments(cx: float, y0: float, y1: float, w: float = WIDTH) -> tuple[tuple[float, float], list]:
    """One stroke as a start point and ('L'|'Q'|'C', points...) segments."""
    h, k, t = y1 - y0, w / 2, w * 0.2
    e = h * 0.2
    return (cx - t, y0 + 0.8), [
        ("Q", (cx, y0 - 0.8), (cx + t, y0 + 0.8)),
        ("C", (cx + k * 0.8, y0 + e * 0.35), (cx + k, y0 + e * 0.7), (cx + k, y0 + e)),
        ("L", (cx + k, y1 - e)),
        ("C", (cx + k, y1 - e * 0.7), (cx + k * 0.8, y1 - e * 0.35), (cx + t, y1 - 0.8)),
        ("Q", (cx, y1 + 0.8), (cx - t, y1 - 0.8)),
        ("C", (cx - k * 0.8, y1 - e * 0.35), (cx - k, y1 - e * 0.7), (cx - k, y1 - e)),
        ("L", (cx - k, y0 + e)),
        ("C", (cx - k, y0 + e * 0.7), (cx - k * 0.8, y0 + e * 0.35), (cx - t, y0 + 0.8)),
    ]


def bar_path(cx: float, y0: float, y1: float) -> str:
    """The stroke as SVG path data."""
    f = lambda v: f"{v:.2f}".rstrip("0").rstrip(".")  # noqa: E731
    start, segs = bar_segments(cx, y0, y1)
    d = [f"M{f(start[0])} {f(start[1])}"]
    for kind, *pts in segs:
        d.append(kind + " ".join(f"{f(x)} {f(y)}" for x, y in pts))
    return "".join(d) + "Z"


def bar_polygon(cx: float, y0: float, y1: float, steps: int = 24, w: float = WIDTH) -> list[tuple[float, float]]:
    start, segs = bar_segments(cx, y0, y1, w)
    poly, p = [start], start
    for kind, *pts in segs:
        if kind == "L":
            poly.append(pts[0])
        elif kind == "Q":
            (c, q) = pts
            poly += [((1 - s) ** 2 * p[0] + 2 * (1 - s) * s * c[0] + s * s * q[0],
                      (1 - s) ** 2 * p[1] + 2 * (1 - s) * s * c[1] + s * s * q[1]) for s in (i / steps for i in range(1, steps + 1))]
        else:
            (c1, c2, q) = pts
            poly += [((1 - s) ** 3 * p[0] + 3 * (1 - s) ** 2 * s * c1[0] + 3 * (1 - s) * s * s * c2[0] + s ** 3 * q[0],
                      (1 - s) ** 3 * p[1] + 3 * (1 - s) ** 2 * s * c1[1] + 3 * (1 - s) * s * s * c2[1] + s ** 3 * q[1])
                     for s in (i / steps for i in range(1, steps + 1))]
        p = poly[-1]
    return poly


def coverage(size: int, poly: list[tuple[float, float]]) -> list[list[float]]:
    """How much of each pixel the polygon (in pixels) covers, 0..1: scanlines with exact spans."""
    grid = [[0.0] * size for _ in range(size)]
    top, bottom = max(0, int(min(y for _, y in poly))), min(size, int(max(y for _, y in poly)) + 1)
    edges = list(zip(poly, poly[1:] + poly[:1]))
    for py in range(top, bottom):
        row = grid[py]
        for sy in range(SS):
            y = py + (sy + 0.5) / SS
            xs = sorted(x0 + (y - y0) * (x1 - x0) / (y1 - y0) for (x0, y0), (x1, y1) in edges if (y0 > y) != (y1 > y))
            for a, b in zip(xs[0::2], xs[1::2]):
                a, b = max(a, 0.0), min(b, float(size))
                for px in range(int(a), min(size, int(b) + 1)):
                    row[px] += max(0.0, min(b, px + 1) - max(a, px)) / SS
    return grid


def mark_layers(size: int, scale: float = 1.0, w: float = WIDTH, bars=BARS):
    """Coverage of [outer strokes, centre stroke] with the 100-unit box scaled to `size` px."""
    k = size / 100 * scale
    off = (size - size * scale) / 2
    polys = [[(off + x * k, off + y * k) for x, y in bar_polygon(*b, w=w)] for b in bars]
    left, right = coverage(size, polys[0]), coverage(size, polys[2])
    return ([[min(1.0, left[y][x] + right[y][x]) for x in range(size)] for y in range(size)],
            coverage(size, polys[1]))


def mix(a, b, t):
    return tuple(round(x + (y - x) * min(1.0, t)) for x, y in zip(a, b))


def rounded(size: int, radius: float) -> list[list[float]]:
    """Coverage of a rounded square (radius as a fraction of the size)."""
    r = radius * size
    out = []
    for py in range(size):
        row = []
        for px in range(size):
            cx, cy = min(max(px + 0.5, r), size - r), min(max(py + 0.5, r), size - r)
            d = ((px + 0.5 - cx) ** 2 + (py + 0.5 - cy) ** 2) ** 0.5
            row.append(max(0.0, min(1.0, r - d + 0.5)))
        out.append(row)
    return out


def tile_icon(size: int, corner: float | None, scale: float = 0.54):
    """The porcelain tile (rounded when `corner`, else full-bleed and opaque) with the mark."""
    outer, centre = mark_layers(size, scale)
    shape = rounded(size, corner) if corner else None
    rows = []
    for y in range(size):
        bg = mix(TILE_TOP, TILE_BOTTOM, y / max(1, size - 1))
        row = []
        for x in range(size):
            c = mix(mix(bg, INK, outer[y][x]), INDIGO, centre[y][x])
            row.append((*c, round(255 * (shape[y][x] if shape else 1.0))))
        rows.append(row)
    return rows


def solid(size: int, rgb: tuple[int, int, int], scale: float = 1.0, w: float = WIDTH, bars=BARS):
    """The mark alone in one colour on transparent (badge, menu-bar template)."""
    outer, centre = mark_layers(size, scale, w, bars)
    return [[(*rgb, round(255 * min(1.0, outer[y][x] + centre[y][x]))) for x in range(size)] for y in range(size)]


def menu_template(size: int):
    """The menu-bar cut, hinted to the 18 pt pixel grid: each stroke exactly 2 pt wide on
    whole points (crisp at @1x), 2 pt apart, the mark 15 pt tall like system glyphs; the
    centre stroke drops 5 pt, as in the mark. Black on transparent (a template image)."""
    f = size / 18
    bars = [(5, 1.5, 11), (9, 6.5, 16.5), (13, 1.5, 11)]  # (centre x, top, bottom) in points.
    cover = [coverage(size, [(x * 1, y * 1) for x, y in bar_polygon(cx * f, y0 * f, y1 * f, w=2 * f)]) for cx, y0, y1 in bars]
    return [[(0, 0, 0, round(255 * min(1.0, sum(c[y][x] for c in cover)))) for x in range(size)] for y in range(size)]


def png_bytes(pixels) -> bytes:
    size = len(pixels)
    raw = b"".join(b"\x00" + bytes(c for px in row for c in px) for row in pixels)
    chunk = lambda kind, data: (struct.pack(">I", len(data)) + kind + data  # noqa: E731
                                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def png(path: Path, pixels) -> None:
    path.write_bytes(png_bytes(pixels))


def ico(path: Path, images: list[bytes], sizes: list[int]) -> None:
    """A .ico holding PNG images (the Vista+ format; every browser reads it)."""
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for data, s in zip(images, sizes):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset + len(blobs))
        blobs += data
    path.write_bytes(head + entries + blobs)


def svg(body: str) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">{body}</svg>\n'


def mark_svg(outer: str, centre: str) -> str:
    return (f'<path fill="{outer}" d="{bar_path(*BARS[0])}{bar_path(*BARS[2])}"/>'
            f'<path fill="{centre}" d="{bar_path(*BARS[1])}"/>')


def favicon_svg() -> str:
    """Porcelain tile in light browser chrome, an ink tile in dark."""
    return svg('<style>.t{fill:#F3EFE6}.o{fill:#15172C}.c{fill:#4B4C9E}'
               '@media (prefers-color-scheme:dark){.t{fill:#1B1D36}.o{fill:#F3EFE6}.c{fill:#A9AAF0}}</style>'
               '<rect class="t" width="100" height="100" rx="22"/>'
               f'<g transform="translate(14 14) scale(.72)"><path class="o" d="{bar_path(*BARS[0])}{bar_path(*BARS[2])}"/>'
               f'<path class="c" d="{bar_path(*BARS[1])}"/></g>')


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    one = "".join(bar_path(*b) for b in BARS)
    (OUT / "valr-mark.svg").write_text(svg(f'<path fill="currentColor" d="{one}"/>'))  # One colour: templates, video.
    (OUT / "valr-mark-light.svg").write_text(svg(mark_svg("#15172C", "#4B4C9E")))
    (OUT / "valr-mark-dark.svg").write_text(svg(mark_svg("#F3EFE6", "#A9AAF0")))
    (OUT / "favicon.svg").write_text(favicon_svg())
    fav = {s: png_bytes(tile_icon(s, 0.22, 0.72)) for s in (16, 32, 48)}
    (OUT / "favicon-32.png").write_bytes(fav[32])
    ico(OUT / "favicon.ico", [fav[16], fav[32], fav[48]], [16, 32, 48])
    png(OUT / "icon-192.png", tile_icon(192, 0.2237))
    png(OUT / "icon-512.png", tile_icon(512, 0.2237))
    png(OUT / "icon-maskable-512.png", tile_icon(512, None, 0.46))  # Inside the 80% safe zone.
    png(OUT / "apple-touch-icon.png", tile_icon(180, None))         # iOS rounds the corners itself; opaque.
    png(OUT / "badge-96.png", solid(96, (255, 255, 255), 1.1))      # Android's status bar: white on transparent.
    print("wrote", ", ".join(sorted(p.name for p in OUT.iterdir() if p.is_file())))


def mac() -> None:
    MAC_OUT.mkdir(parents=True, exist_ok=True)
    # The menu bar: 18 pt, black on transparent, so macOS tints it for light and dark bars.
    png(MAC_OUT / "MenuBarIconTemplate.png", menu_template(18))
    png(MAC_OUT / "MenuBarIconTemplate@2x.png", menu_template(36))
    print("wrote MenuBarIconTemplate.png, MenuBarIconTemplate@2x.png (AppIcon-1024.png: mac/dmg/make_app_icon.swift)")


if __name__ == "__main__":
    import sys
    mac() if "--mac" in sys.argv[1:] else main()
