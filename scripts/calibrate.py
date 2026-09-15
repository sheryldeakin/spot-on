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

    out = ROOT / "docs" / "calibration.txt"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sys.stdout.write(out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
