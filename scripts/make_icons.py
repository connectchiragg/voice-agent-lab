"""Draws VALR's Home Screen icons (static/icons/*.png): the app's brand mark, a lit indigo
orb with a soft glow on a night-sky square (static/icons/valr-mark.svg; the same orb as the
top bar's .brand-mark in static/valr-ds.css).

    python scripts/make_icons.py

Pure Python (no Pillow): every pixel is shaded analytically (radial gradients, an
anti-aliased edge), so the icons are crisp at every size. The badge stays a white V.
"""
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "static" / "icons"
COBALT = (0x24, 0x59, 0xD6)   # --signal-fill
BONE = (0xF4, 0xF6, 0xFA)     # a touch lighter than --bone, for contrast on cobalt
# The V in a unit square (the same points as valr-mark.svg's viewBox 0 0 100 100, divided by 100).
V = [(0.25, 0.27), (0.365, 0.27), (0.5, 0.605), (0.635, 0.27), (0.75, 0.27), (0.556, 0.74), (0.444, 0.74)]
SS = 8  # Supersampling per axis.


def inside(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    hit = False
    j = len(poly) - 1
    for i, (xi, yi) in enumerate(poly):
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


def coverage(size: int, poly: list[tuple[float, float]]) -> list[list[float]]:
    """How much of each pixel the polygon covers, 0..1."""
    left, right = min(p[0] for p in poly) * size, max(p[0] for p in poly) * size
    top, bottom = min(p[1] for p in poly) * size, max(p[1] for p in poly) * size
    grid = [[0.0] * size for _ in range(size)]
    for py in range(max(0, int(top) - 1), min(size, int(bottom) + 2)):
        for px in range(max(0, int(left) - 1), min(size, int(right) + 2)):
            hits = sum(inside((px + (sx + 0.5) / SS) / size, (py + (sy + 0.5) / SS) / size, poly)
                       for sy in range(SS) for sx in range(SS))
            grid[py][px] = hits / (SS * SS)
    return grid


def rounded(size: int, radius: float) -> list[list[float]]:
    """Coverage of a rounded square (radius as a fraction of the size)."""
    r = radius * size
    grid = [[1.0] * size for _ in range(size)]
    for py in range(size):
        for px in range(size):
            cx = min(max(px + 0.5, r), size - r)
            cy = min(max(py + 0.5, r), size - r)
            if (px + 0.5 - cx) ** 2 + (py + 0.5 - cy) ** 2 <= (r - 1.5) ** 2:
                continue
            hits = 0
            for sy in range(SS):
                for sx in range(SS):
                    x, y = px + (sx + 0.5) / SS, py + (sy + 0.5) / SS
                    cx, cy = min(max(x, r), size - r), min(max(y, r), size - r)
                    hits += (x - cx) ** 2 + (y - cy) ** 2 <= r * r
            grid[py][px] = hits / (SS * SS)
    return grid


def scaled(poly: list[tuple[float, float]], factor: float) -> list[tuple[float, float]]:
    """The polygon scaled about its centre (the maskable icon keeps the V in the safe zone)."""
    cx = (min(p[0] for p in poly) + max(p[0] for p in poly)) / 2
    cy = (min(p[1] for p in poly) + max(p[1] for p in poly)) / 2
    return [(0.5 + (x - cx) * factor, 0.5 + (y - cy) * factor) for x, y in poly]


def png(path: Path, pixels: list[list[tuple[int, int, int, int]]]) -> None:
    size = len(pixels)
    raw = b"".join(b"\x00" + bytes(c for px in row for c in px) for row in pixels)
    chunk = lambda kind, data: (struct.pack(">I", len(data)) + kind + data  # noqa: E731
                                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))  # type: ignore[return-value]


def icon(size: int, corner: float | None, mark_scale: float = 1.0) -> list[list[tuple[int, int, int, int]]]:
    """Cobalt square (rounded when `corner`, else full-bleed and opaque) with the bone V."""
    mark = coverage(size, scaled(V, mark_scale) if mark_scale != 1.0 else V)
    shape = rounded(size, corner) if corner else None
    return [[(*mix(COBALT, BONE, mark[y][x]), round(255 * (shape[y][x] if shape else 1.0)))
             for x in range(size)] for y in range(size)]


NIGHT_IN, NIGHT_OUT = (0x16, 0x20, 0x4A), (0x06, 0x09, 0x14)
ORB_HI, ORB_MID, ORB_LO = (0xC7, 0xC8, 0xFF), (0x6F, 0x70, 0xF5), (0x2B, 0x2D, 0x8F)
GLOW = (0x63, 0x66, 0xF1)


def orb(size: int, corner: float | None, orb_scale: float = 1.0) -> list[list[tuple[int, int, int, int]]]:
    """The night-sky square (rounded when `corner`) with the lit orb and its glow."""
    shape = rounded(size, corner) if corner else None
    radius = 0.29 * orb_scale
    out = []
    for py in range(size):
        row = []
        for px in range(size):
            x, y = (px + 0.5) / size, (py + 0.5) / size
            d = ((x - 0.5) ** 2 + (y - 0.5) ** 2) ** 0.5
            sky = mix(NIGHT_IN, NIGHT_OUT, min(1.0, d / 0.75))
            glow = max(0.0, 1 - (d - radius) / (0.22 * orb_scale)) if d > radius else 1.0
            color = mix(sky, GLOW, 0.55 * glow * glow)
            # The orb: lit from the top left, darker towards the far edge.
            hx, hy = 0.5 - radius * 0.3, 0.5 - radius * 0.4
            t = min(1.0, ((x - hx) ** 2 + (y - hy) ** 2) ** 0.5 / (radius * 1.55))
            lit = mix(ORB_HI, ORB_MID, min(1.0, t / 0.45)) if t < 0.45 else mix(ORB_MID, ORB_LO, (t - 0.45) / 0.55)
            edge = max(0.0, min(1.0, (radius - d) * size + 0.5))  # Anti-aliased rim.
            color = mix(color, lit, edge)
            row.append((*color, round(255 * (shape[py][px] if shape else 1.0))))
        out.append(row)
    return out


def badge(size: int) -> list[list[tuple[int, int, int, int]]]:
    """The monochrome badge (Android's status bar): a white V on transparent."""
    mark = coverage(size, scaled(V, 1.35))
    return [[(255, 255, 255, round(255 * mark[y][x])) for x in range(size)] for y in range(size)]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    png(OUT / "icon-192.png", orb(192, 0.22))
    png(OUT / "icon-512.png", orb(512, 0.22))
    png(OUT / "icon-maskable-512.png", orb(512, None, 0.8))  # The orb stays inside the 80% safe zone.
    png(OUT / "apple-touch-icon.png", orb(180, None))         # iOS rounds the corners itself; opaque.
    png(OUT / "badge-96.png", badge(96))
    print("wrote", ", ".join(sorted(p.name for p in OUT.glob("*.png"))))


if __name__ == "__main__":
    main()
