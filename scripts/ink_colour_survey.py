"""How often is the text the wrong colour, and does the report ever say so?

Noticed by eye rather than by the tool: the font colour differs on things that
otherwise look right. The type measurements are height, density, spacing and lines,
so colour is only ever felt through the page-wide colour score, which cannot say
which element is wrong. This counts how often matched text elements differ in ink
colour across the runs already here, before anything is built.

Ink colour is taken from the pixels furthest from the local background rather than
from the mean of the box, which would be mostly background: a glyph is a thin stroke
and its edges are antialiased towards whatever is behind it.

    python scripts/ink_colour_survey.py      # writes scripts/ink-colour-survey.json
"""
import importlib.util
import json
import statistics
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("spot_on", ROOT / "spot-on.py")
so = importlib.util.module_from_spec(spec)
sys.modules["spot_on"] = so
spec.loader.exec_module(so)

INK_SHARE = 0.04     # the core of the stroke only: a wider share pulls in the
                     # antialiased edge, which blends toward the background and
                     # shifts with the typeface rather than with the colour
MIN_PIXELS = 24


def ink_colour(rgb, box):
    """The mean colour of the pixels that are most unlike the box's background."""
    y0, x0 = max(0, box["y"]), max(0, box["x"])
    crop = rgb[y0:box["y"] + box["h"], x0:box["x"] + box["w"]]
    if crop.size < MIN_PIXELS * 3:
        return None
    flat = crop.reshape(-1, 3)
    border = np.concatenate([crop[0], crop[-1], crop[:, 0], crop[:, -1]])
    ground = np.median(border, axis=0)
    far = np.abs(flat - ground).sum(axis=1)
    # The very core of the stroke. A wider sample is weight-dependent: a bolder
    # face has more fully-inked pixels and reads darker at the same declared colour.
    keep = max(MIN_PIXELS, int(len(flat) * INK_SHARE))
    idx = np.argsort(-far)[:keep]
    if far[idx].mean() < 30:          # nothing drawn here worth calling ink
        return None
    return flat[idx].mean(axis=0)


def main():
    rows = []
    for run_dir in sorted(p for p in (ROOT / "runs").iterdir() if p.is_dir()):
        ref_path = run_dir / "reference.png"
        run_file = run_dir / "run.json"
        if not ref_path.exists() or not run_file.exists():
            continue
        run = json.loads(run_file.read_text(encoding="utf-8"))
        best = run.get("best_attempt")
        att_path = run_dir / "attempts" / "{:03d}.png".format(best or 0)
        if not best or not att_path.exists():
            continue
        ref = np.asarray(Image.open(ref_path).convert("RGB"), dtype=np.float32)
        att = np.asarray(Image.open(att_path).convert("RGB"), dtype=np.float32)
        if ref.shape != att.shape:
            continue
        els = so.compare_elements(so._gray(ref), so._gray(att))
        design = so._elements(so._gray(ref))
        attempt = so._elements(so._gray(att))
        matched, _ = so._match_elements(design, attempt, so._gray(ref), so._gray(att))
        for d, a in matched:
            # Only things the tool itself reads as type. The detector labels any
            # small box "text", and the first pass through this was measuring icon
            # tiles and illustrations: a Discord logo and a brain drawing were among
            # the worst offenders, which says nothing about font colour.
            # A text RUN, not any small box. The detector labels a 14x9 dot and a
            # Gmail logo "text", and those were the worst offenders in the first two
            # passes. Real type is wider than it is tall and wide enough to hold
            # several letters.
            if d["kind"] != "text" or d["w"] < 60 or d["h"] < 10 or d["w"] < d["h"] * 2:
                continue
            td = so._type_metrics(so._gray(ref), d)
            ta = so._type_metrics(so._gray(att), a)
            if not td or not ta or td.get("lines", 0) < 1 or ta.get("lines", 0) < 1:
                continue
            cd, ca = ink_colour(ref, d), ink_colour(att, a)
            if cd is None or ca is None:
                continue
            gap = so._lab_gap(cd, ca)
            rows.append({"run": run_dir.name, "x": int(d["x"]), "y": int(d["y"]),
                         "w": int(d["w"]), "h": int(d["h"]),
                         "design": [int(v) for v in cd], "attempt": [int(v) for v in ca],
                         "deltaE": round(gap, 2),
                         "area": int(d["w"]) * int(d["h"])})
        print(run_dir.name, len(rows), flush=True)

    # Split each gap into the part lightness explains and the part it does not. A
    # bolder face at the same declared colour reads darker here, so lightness alone
    # is not evidence about colour, and the finding is built on the other half.
    for r in rows:
        ld = so._srgb_to_lab(np.asarray(r["design"], dtype=np.float64))
        la = so._srgb_to_lab(np.asarray(r["attempt"], dtype=np.float64))
        r["lightness"] = round(abs(float(ld[0] - la[0])), 2)
        r["chroma"] = round(float(((ld[1] - la[1]) ** 2 + (ld[2] - la[2]) ** 2) ** 0.5), 2)
    over = [r for r in rows if r["deltaE"] >= 10]
    mostly_light = [r for r in over if r["lightness"] > r["chroma"] * 1.5]
    mostly_colour = [r for r in over if r["chroma"] > r["lightness"] * 1.5]

    gaps = [r["deltaE"] for r in rows]
    out = {"ink_share": INK_SHARE, "text_elements": len(rows),
           "median_deltaE": round(statistics.median(gaps), 2) if gaps else None,
           "over_5": sum(1 for g in gaps if g >= 5),
           "over_10": sum(1 for g in gaps if g >= 10),
           "over_20": sum(1 for g in gaps if g >= 20),
           "over_10_mostly_lightness": len(mostly_light),
           "over_10_mostly_colour": len(mostly_colour),
           "over_10_mixed": len(over) - len(mostly_light) - len(mostly_colour),
           "rows": rows}
    path = ROOT / "scripts" / "ink-colour-survey.json"
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")

    print()
    print("matched text elements on best attempts: {}".format(len(rows)))
    if gaps:
        print("median deltaE {:.1f}   at or over 5: {}   over 10: {}   over 20: {}".format(
            out["median_deltaE"], out["over_5"], out["over_10"], out["over_20"]))
        print()
        print("the worst, which is what a finding would name:")
        for r in sorted(rows, key=lambda x: -x["deltaE"])[:10]:
            print("  {:<28} x{:<5} y{:<5} dE {:>5.1f}  design {} attempt {}".format(
                r["run"][:28], r["x"], r["y"], r["deltaE"],
                tuple(r["design"]), tuple(r["attempt"])))
    print()
    print("of the {} over 10: {} mostly lightness (which weight moves), {} mostly "
          "hue or chroma, {} mixed".format(
              len(over), len(mostly_light), len(mostly_colour),
              len(over) - len(mostly_light) - len(mostly_colour)))
    print("wrote", path)


if __name__ == "__main__":
    main()
