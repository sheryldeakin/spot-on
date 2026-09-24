"""The same three pairings on real design-and-attempt pairs, where the answer is unknown.

The synthetic trial says which matcher recovers a pairing it was built to know. This
says how each behaves on pages nobody arranged: how much of the design it pairs at all,
and how far it is willing to reach to do it. A matcher that pairs two things half a page
apart on a rebuild that is mostly in the right place is inventing a relationship, and
the sentence it writes would send the next round somewhere pointless.

    python scripts/match_real.py            # every run with a saved attempt
    python scripts/match_real.py hud-concept
"""
import importlib.util
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("spot_on", ROOT / "spot-on.py")
so = importlib.util.module_from_spec(spec)
sys.modules["spot_on"] = so
spec.loader.exec_module(so)

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

HOWS = ("geometry", "content", "overlay")


def centre(b):
    return b["x"] + b["w"] / 2.0, b["y"] + b["h"] / 2.0


def look(slug):
    run = so._load_run(slug)
    best = run.get("best_attempt")
    shot = so._run_dir(slug) / "attempts" / "{:03d}.png".format(best or 0)
    ref = so._run_dir(slug) / "reference.png"
    if not shot.exists() or not ref.exists():
        return None
    with Image.open(ref) as a, Image.open(shot) as b:
        g_ref = so._gray(np.asarray(a.convert("RGB"), dtype=np.float64))
        g_att = so._gray(np.asarray(b.convert("RGB").resize(a.size, Image.LANCZOS),
                                    dtype=np.float64))
    design, attempt = so._elements(g_ref), so._elements(g_att)
    diag = math.hypot(*g_ref.shape[:2])
    rows = {}
    pairing = {}
    for how in HOWS:
        matched, missing = so._match_elements(design, attempt, g_ref, g_att, how=how)
        reach = sorted(math.hypot(centre(a)[0] - centre(d)[0], centre(a)[1] - centre(d)[1])
                       for d, a in matched)
        rows[how] = {
            "paired": len(matched),
            "of": len(design),
            "paired_pct": round(100.0 * len(matched) / max(1, len(design)), 1),
            "median_reach_px": round(reach[len(reach) // 2], 1) if reach else None,
            "furthest_reach_px": round(reach[-1], 1) if reach else None,
            "reach_over_quarter_page": sum(1 for r in reach if r > diag / 4),
        }
        pairing[how] = {(d["x"], d["y"], d["w"], d["h"]):
                        (a["x"], a["y"], a["w"], a["h"]) for d, a in matched}
    agree = 0
    both = set(pairing["geometry"]) & set(pairing["content"])
    for k in both:
        if pairing["geometry"][k] == pairing["content"][k]:
            agree += 1
    return {"run": slug, "design_boxes": len(design), "attempt_boxes": len(attempt),
            "score": run.get("best_match"), "matchers": rows,
            "geometry_content_agree": agree, "geometry_content_both": len(both)}


def show_far(slug, **kw):
    """Print the pairs content matching reaches furthest for, to see if they are real."""
    d = so._run_dir(slug)
    run = so._load_run(slug)
    shot = d / "attempts" / "{:03d}.png".format(run.get("best_attempt") or 0)
    with Image.open(d / "reference.png") as a, Image.open(shot) as b:
        g_ref = so._gray(np.asarray(a.convert("RGB"), dtype=np.float64))
        g_att = so._gray(np.asarray(b.convert("RGB").resize(a.size, Image.LANCZOS),
                                    dtype=np.float64))
    fd, fa = so._elements(g_ref), so._elements(g_att)
    diag = math.hypot(*g_ref.shape[:2])
    matched, _ = so._match_content(fd, fa, g_ref, g_att, **kw)
    rows = []
    for dd, aa in matched:
        r = math.hypot(*[aa[k] + aa[s] / 2.0 - dd[k] - dd[s] / 2.0
                         for k, s in (("x", "w"), ("y", "h"))])
        if r > diag / 4:
            rows.append((r, dd, aa))
    print("{}: {} pairs over a quarter page ({} matched of {})".format(
        slug, len(rows), len(matched), len(fd)))
    for r, dd, aa in sorted(rows, reverse=True):
        print("  {:>5.0f}px  {:<5} {:>4}x{:<4} at {:>4},{:<4}  ->  {:>4}x{:<4} at {:>4},{:<4}"
              .format(r, dd["kind"], dd["w"], dd["h"], dd["x"], dd["y"],
                      aa["w"], aa["h"], aa["x"], aa["y"]))


def main():
    if sys.argv[1:2] == ["--show-far"]:
        kw = {}
        for a in sys.argv[3:]:
            k, v = a.split("=")
            kw[k] = float(v)
        show_far(sys.argv[2], **kw)
        return
    slugs = sys.argv[1:] or [p.name for p in sorted((ROOT / "runs").iterdir()) if p.is_dir()]
    out = [r for r in (look(s) for s in slugs) if r]
    print("{:<34}{:>7}{}".format("run", "boxes", "".join(
        "{:>12}".format(h) for h in HOWS)))
    for r in out:
        print("{:<34}{:>7}{}".format(r["run"][:33], r["design_boxes"], "".join(
            "{:>11.1f}%".format(r["matchers"][h]["paired_pct"]) for h in HOWS)))
    print("\nhow far each is willing to reach for a pair (median px, then furthest)")
    for r in out:
        print("  {:<32}{}".format(r["run"][:31], "".join(
            "{:>6.0f}/{:<6.0f}".format(r["matchers"][h]["median_reach_px"] or 0,
                                       r["matchers"][h]["furthest_reach_px"] or 0)
            for h in HOWS)))
    print("\npairs over a quarter page apart")
    for r in out:
        print("  {:<32}{}".format(r["run"][:31], "".join(
            "{:>12}".format(r["matchers"][h]["reach_over_quarter_page"]) for h in HOWS)))
    print("\nposition and content agree on")
    for r in out:
        print("  {:<32}{} of {} shared pairs".format(
            r["run"][:31], r["geometry_content_agree"], r["geometry_content_both"]))
    dest = ROOT / "scripts" / "match-real.json"
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("\nwrote {}".format(dest))


if __name__ == "__main__":
    main()
