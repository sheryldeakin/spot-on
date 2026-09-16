"""Regenerate docs/calibration.txt: the seven calibration cases, rendered by Chrome.

    python scripts/calibrate.py

The README quotes this file verbatim and tests/test_docs.py fails if the two
drift apart, so rerun this after any change to the scoring.
"""

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("spot_on", ROOT / "spot-on.py")
so = importlib.util.module_from_spec(spec)
spec.loader.exec_module(so)

W, H = 400, 300
SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">'
       '<rect width="400" height="300" fill="#FFFFFF"/>{}</svg>')
CIRCLE = '<circle cx="{}" cy="{}" r="{}" fill="{}"/>'
SQUARE = '<rect x="{}" y="30" width="{}" height="60" fill="{}"/>'

CASES = [
    ("exact", "identical to the design",
     CIRCLE.format(200, 150, 80, "#52796F") + SQUARE.format(30, 60, "#C2703F")),
    ("close", "3px offset and a slight hue shift",
     CIRCLE.format(203, 152, 78, "#55796C") + SQUARE.format(32, 58, "#C07038")),
    ("half", "the square left out entirely",
     CIRCLE.format(200, 150, 80, "#52796F")),
    ("hue", "right geometry, wrong colour",
     CIRCLE.format(200, 150, 80, "#2F6FA8") + SQUARE.format(30, 60, "#C2703F")),
    ("shift", "right colours, 40px to the right",
     CIRCLE.format(240, 150, 80, "#52796F") + SQUARE.format(70, 60, "#C2703F")),
    ("wrong", "one wrong shape in the wrong place",
     CIRCLE.format(230, 160, 60, "#3355AA")),
    ("blank", "nothing drawn",
     ""),
]


# A second design, closer to a real page: a soft full-bleed gradient with a card and
# some text on it. Flat shapes on flat white never exercise the background at all, and
# the background is where the ink mask decides what counts as drawn content.
GW, GH = 480, 320
PAGE = ('<svg xmlns="http://www.w3.org/2000/svg" width="480" height="320">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="{c0}"/><stop offset="1" stop-color="{c1}"/>'
        '</linearGradient></defs>'
        '<rect width="480" height="320" fill="url(#g)"/>{content}</svg>')
CARD = '<rect x="40" y="60" width="400" height="180" rx="14" fill="#FFFFFF"/>'
TEXT = ('<rect x="70" y="92" width="180" height="16" fill="#1B2A3A"/>'
        '<rect x="70" y="128" width="300" height="10" fill="#5A6B7C"/>'
        '<rect x="70" y="150" width="260" height="10" fill="#5A6B7C"/>'
        '<rect x="70" y="190" width="120" height="30" rx="8" fill="#1B2A3A"/>')
DESIGN_G0, DESIGN_G1 = "#F7FAFF", "#E8EEFB"

PAGE_CASES = [
    ("same", "identical to the design", DESIGN_G0, DESIGN_G1, CARD + TEXT),
    ("deeper", "same content, gradient a little stronger", DESIGN_G0, "#D3E0F7", CARD + TEXT),
    ("strong", "same content, gradient clearly stronger", DESIGN_G0, "#BBD0F0", CARD + TEXT),
    ("flatbg", "same content, no gradient at all", "#F0F4FC", "#F0F4FC", CARD + TEXT),
    ("nocard", "right gradient, the card left out", DESIGN_G0, DESIGN_G1, TEXT),
]


def page_table():
    """How the score reacts to the background, holding the content still.

    'deeper' and 'strong' change nothing a reader would call content. 'nocard' is the
    control in the other direction: a large flat panel that really is missing, which
    has to stay expensive or the measure has just stopped noticing missing elements.
    """
    tmp = Path(tempfile.mkdtemp(prefix="spot-on-page-"))
    lines = ["case    match  structure  shape  colour  detail  coverage  what it is"]
    try:
        design = so.render_code(
            PAGE.format(c0=DESIGN_G0, c1=DESIGN_G1, content=CARD + TEXT), "svg",
            GW, GH, tmp / "design.png")
        for name, what, c0, c1, content in PAGE_CASES:
            att = so.render_code(PAGE.format(c0=c0, c1=c1, content=content), "svg",
                                 GW, GH, tmp / (name + ".png"))
            r, _, _ = so.score_images(design, att)
            c = r["components"]
            lines.append("{:<6}  {:>5.1f}  {:>9.1f}  {:>5.1f}  {:>6.1f}  {:>6.1f}  {:>8.1f}  {}".format(
                name, r["match"], c["structure"], c["shape"], c["colour"], c["detail"],
                c["coverage"], what))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return lines


def main():
    design = Image.new("RGB", (W, H), "#FFFFFF")
    d = ImageDraw.Draw(design)
    d.ellipse([120, 70, 280, 230], fill="#52796F")
    d.rectangle([30, 30, 89, 89], fill="#C2703F")

    tmp = Path(tempfile.mkdtemp(prefix="spot-on-calibrate-"))
    lines = ["case    match  structure  shape  colour  detail  coverage  what it is"]
    try:
        for name, what, inner in CASES:
            att = so.render_code(SVG.format(inner), "svg", W, H, tmp / (name + ".png"))
            r, _, _ = so.score_images(design, att)
            c = r["components"]
            lines.append("{:<6}  {:>5.1f}  {:>9.1f}  {:>5.1f}  {:>6.1f}  {:>6.1f}  {:>8.1f}  {}".format(
                name, r["match"], c["structure"], c["shape"], c["colour"], c["detail"],
                c["coverage"], what))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    lines += ["", "a page-like design: a full-bleed gradient with a card and text on it",
              ""] + page_table()

    out = ROOT / "docs" / "calibration.txt"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sys.stdout.write(out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
