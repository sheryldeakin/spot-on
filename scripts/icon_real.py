"""What the bundled set has to say about the icons in real designs.

The known-answer trial (icon_trial.py) says whether a SET icon, redrawn, can be found
again. It cannot say what matters here, because the designs this tool is pointed at
are generated images whose glyphs were drawn from no set at all. The question those
pose is the opposite one: how often is a real design's glyph in the set?

This answers it over every distinct design on this machine, by running the tool's own
glyph_identity over every icon-sized box and recording its verdict. It also writes a
contact sheet per design, each box beside the three icons nearest to it, so the
verdicts can be checked by eye rather than taken on trust.

    python scripts/icon_real.py [out-dir]
"""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
spec = importlib.util.spec_from_file_location("build_icon_templates",
                                              HERE / "build_icon_templates.py")
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)
so = bt.so

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

PAD = 4
SHOW = 48


def candidates(gray):
    """Boxes the size and shape of an icon: squarish, between 14 and 64 pixels."""
    out = []
    for el in so._elements(gray):
        w, h = el["w"], el["h"]
        if 14 <= w <= 64 and 14 <= h <= 64 and 0.6 <= w / float(h) <= 1.6:
            out.append(el)
    return out


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE
    names, T = so._icon_templates()
    names = [str(n) for n in names]
    bodies = dict(bt.unique_icons())
    shown = bt.render_pages([b for _, b in bt.unique_icons()], size=40)
    order_of = {n: i for i, (n, _) in enumerate(bt.unique_icons())}

    def tile(name):
        i = order_of[name]
        arr = shown[i // bt.PER_PAGE][0]
        r, c = divmod(i % bt.PER_PAGE, bt.COLS)
        o = (bt.CELL - SHOW) // 2
        y, x = r * bt.CELL + o, c * bt.CELL + o
        return Image.fromarray(arr[y:y + SHOW, x:x + SHOW].clip(0, 255).astype(np.uint8))

    seen, report = set(), []
    totals = {"boxes": 0, "readable": 0, "absent": 0, "named": 0, "no_opinion": 0}
    for ref in sorted(ROOT.joinpath("runs").glob("*/reference.png")):
        digest = hashlib.sha1(ref.read_bytes()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        rgb = np.asarray(Image.open(ref).convert("RGB"), dtype=np.float32)
        gray = so._gray(rgb)
        rows = []
        for el in candidates(gray):
            cell = gray[el["y"]:el["y"] + el["h"], el["x"]:el["x"] + el["w"]]
            v = so._ink_map(cell)
            if v is None:
                continue
            sims = T @ v
            top = np.argsort(-sims)[:3]
            verdict = so.glyph_identity(gray, el)
            rows.append({"x": int(el["x"]), "y": int(el["y"]), "w": int(el["w"]),
                         "h": int(el["h"]),
                         "top": [[names[j], round(float(sims[j]), 3)] for j in top],
                         "verdict": verdict, "_crop": rgb[
                             max(0, el["y"] - PAD):el["y"] + el["h"] + PAD,
                             max(0, el["x"] - PAD):el["x"] + el["w"] + PAD]})
            totals["boxes"] += 1
            if min(el["w"], el["h"]) >= so.ICON_READABLE:
                totals["readable"] += 1
            if verdict is None:
                totals["no_opinion"] += 1
            elif verdict.get("absent"):
                totals["absent"] += 1
            else:
                totals["named"] += 1
        if not rows:
            continue
        rows.sort(key=lambda r: -r["top"][0][1])
        sheet = Image.new("RGB", (SHOW * 4 + 40 + 400, (SHOW + 8) * len(rows)), "white")
        draw = ImageDraw.Draw(sheet)
        for k, r in enumerate(rows):
            y = k * (SHOW + 8)
            c = Image.fromarray(r["_crop"].clip(0, 255).astype(np.uint8))
            c.thumbnail((SHOW, SHOW))
            sheet.paste(c, (0, y))
            for m, (n, _) in enumerate(r["top"]):
                sheet.paste(tile(n), (SHOW + 12 + m * (SHOW + 4), y))
            v = r["verdict"]
            said = ("not in the set" if v and v.get("absent")
                    else "NAMED {}".format(v["name"]) if v else "no opinion")
            draw.text((SHOW * 4 + 40, y + 4), "x{} y{} {}x{}   {}".format(
                r["x"], r["y"], r["w"], r["h"], said), fill="black")
            draw.text((SHOW * 4 + 40, y + 20), "  ".join(
                "{} {:.2f}".format(n, s) for n, s in r["top"]), fill="black")
        sheet.save(out_dir / "icon-real-{}.png".format(ref.parent.name[:40]))
        report.append({"run": ref.parent.name, "boxes": [
            {k: v for k, v in r.items() if not k.startswith("_")} for r in rows]})
        print(ref.parent.name, len(rows), flush=True)
    out = {"designs": len(report), "totals": totals, "runs": report}
    # The numbers go beside the script that made them, whatever was asked for; only
    # the contact sheets follow out-dir, because they are for looking at once.
    (HERE / "icon-real.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(totals)


if __name__ == "__main__":
    main()
