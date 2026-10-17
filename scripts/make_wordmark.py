"""Outlines VALR's wordmark and lockups (static/icons/valr-wordmark.svg, valr-lockup-*.svg).

    pip install fonttools && python scripts/make_wordmark.py

The wordmark is "valr" in Instrument Serif (mac/dmg/fonts, SIL OFL), tracked +0.01 em,
converted to outlines so it never depends on a font being present. The lockups put the
mark (scripts/make_icons.py) to its left: the mark's strokes span the wordmark's
ascender-to-baseline height, with a gap of 0.3 x that height.
"""
from pathlib import Path

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

import make_icons

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / "mac" / "dmg" / "fonts" / "InstrumentSerif-Regular.ttf"
OUT = ROOT / "static" / "icons"
TEXT, TRACK = "valr", 0.01


def wordmark() -> tuple[str, float, float]:
    """(path data, width, height of the "l") in font units, baseline at y = 0, y down."""
    font = TTFont(FONT)
    upm = font["head"].unitsPerEm
    cmap, glyphs, hmtx = font.getBestCmap(), font.getGlyphSet(), font["hmtx"]
    pen = SVGPathPen(glyphs)
    x = 0.0
    names = [cmap[ord(c)] for c in TEXT]
    for i, name in enumerate(names):
        glyphs[name].draw(TransformPen(pen, (1, 0, 0, -1, x, 0)))
        x += hmtx[name][0] + (TRACK * upm if i < len(names) - 1 else 0)
    bp = BoundsPen(glyphs)
    glyphs[cmap[ord("l")]].draw(bp)
    return pen.getCommands(), x, bp.bounds[3]


def main() -> None:
    d, width, asc = wordmark()
    pad = asc * 0.04
    (OUT / "valr-wordmark.svg").write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 {-asc - pad:.0f} {width:.0f} {asc + 2 * pad:.0f}">'
        f'<path fill="currentColor" d="{d}"/></svg>\n')
    # Lockup: the mark's stroke span (y 9..91 of 100) matches the wordmark's height (asc).
    k = asc / 82
    mark_w = 50 * k  # The strokes span x 25..75 of the mark's 100-unit box.
    gap = asc * 0.30
    total = mark_w + gap + width
    for theme, (outer, centre, ink) in {"light": ("#15172C", "#4B4C9E", "#15172C"),
                                         "dark": ("#F3EFE6", "#A9AAF0", "#F3EFE6")}.items():
        mark = make_icons.mark_svg(outer, centre)
        (OUT / f"valr-lockup-{theme}.svg").write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 {-asc - pad:.0f} {total:.0f} {asc + 2 * pad:.0f}">'
            f'<g transform="translate({-25 * k:.1f} {-asc - 9 * k:.1f}) scale({k:.4f})">{mark}</g>'
            f'<path fill="{ink}" transform="translate({mark_w + gap:.0f} 0)" d="{d}"/></svg>\n')
    print("wrote valr-wordmark.svg, valr-lockup-light.svg, valr-lockup-dark.svg")


if __name__ == "__main__":
    main()
