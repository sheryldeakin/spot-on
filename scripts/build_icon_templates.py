"""Render the bundled set once and store it as ink maps the tool can match against.

A round cannot pay for this: it needs a browser and about half a minute. The output,
assets/icons/templates.npz, is what glyph_identity loads. Rerun it when the icon set
changes or when ICON_NORM changes, and commit the result.

    python scripts/build_icon_templates.py
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("spot_on", ROOT / "spot-on.py")
so = importlib.util.module_from_spec(spec)
sys.modules["spot_on"] = so
spec.loader.exec_module(so)

import numpy as np  # noqa: E402

CELL = 72        # px per cell, with room around a 48px icon
COLS = 20
PER_PAGE = COLS * COLS
SIZE = 48        # drawn large: the template should be the cleanest rendering there is
STROKE = 2.0
INK, GROUND = "#0f172a", "#FFFFFF"


def unique_icons():
    """One entry per drawing. 254 of the 2121 names share a body (house, home)."""
    icons = so._icon_index()["icons"]
    seen = {}
    for name in sorted(icons):
        seen.setdefault(icons[name], name)
    return [(name, body) for body, name in seen.items()]


def page_html(bodies, size=SIZE, stroke=STROKE, ink=INK, rng=None):
    """A grid of icons, one per cell, optionally nudged and scaled within its cell."""
    cells = []
    for i, body in enumerate(bodies):
        r, c = divmod(i, COLS)
        scale = rng.uniform(0.9, 1.1) if rng is not None else 1.0
        dx, dy = (rng.uniform(-3, 3), rng.uniform(-3, 3)) if rng is not None else (0.0, 0.0)
        s = size * scale
        cells.append(
            '<svg style="position:absolute;left:{:.2f}px;top:{:.2f}px" width="{:.2f}" '
            'height="{:.2f}" viewBox="0 0 24 24" fill="none" stroke="{}" stroke-width="{}" '
            'stroke-linecap="round" stroke-linejoin="round">{}</svg>'.format(
                c * CELL + (CELL - s) / 2 + dx, r * CELL + (CELL - s) / 2 + dy,
                s, s, ink, stroke, body))
    return '<div style="position:relative">' + "".join(cells) + "</div>"


def render_pages(bodies, size=SIZE, stroke=STROKE, ink=INK, ground=GROUND, rng=None):
    """Grey arrays, one per page of the grid, with how many cells each page holds."""
    out = []
    tmp = Path(tempfile.mkdtemp(prefix="icon-templates-"))
    side = COLS * CELL
    for p in range(0, len(bodies), PER_PAGE):
        chunk = bodies[p:p + PER_PAGE]
        img = so.render_code(page_html(chunk, size, stroke, ink, rng), "html", side, side,
                             tmp / "page.png", ground=ground)
        out.append((so._gray(np.asarray(img.convert("RGB"), dtype=np.float32)), len(chunk)))
    return out


def maps_of(pages):
    """One ink map per cell, in order, None where a cell came out blank."""
    out = []
    for arr, count in pages:
        for i in range(count):
            r, c = divmod(i, COLS)
            out.append(so._ink_map(arr[r * CELL:(r + 1) * CELL, c * CELL:(c + 1) * CELL]))
    return out


def main():
    icons = unique_icons()
    names = [n for n, _ in icons]
    vecs = maps_of(render_pages([b for _, b in icons]))
    blank = [names[i] for i, v in enumerate(vecs) if v is None]
    if blank:
        raise SystemExit("icons that rendered blank, so the build is wrong: {}".format(blank[:5]))
    out = ROOT / "assets" / "icons" / "templates.npz"
    # float16: the maps are unit vectors compared by dot product, where the last few
    # digits decide nothing, and it halves what the repository carries.
    np.savez_compressed(out, names=np.array(names), maps=np.stack(vecs).astype(np.float16))
    print("wrote {} ({} drawings, {} KB)".format(out, len(names), out.stat().st_size // 1024))


if __name__ == "__main__":
    main()
