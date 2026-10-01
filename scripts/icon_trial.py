"""Can an icon drawn in a design be named, by matching it against the bundled set?

The report finds icon wells the attempt leaves empty, gives their positions, and asks
the round to look at the design, name what is drawn there, and search the set for that
name. Measured on one page, the design's own glyphs in those wells were worth 2.4
points of the per-element score and the loop's plausible substitutes were worth
nothing, so the gap is naming the right icon, not drawing one. If matching could name
it, the report could say "the glyph at x 272, y 694 is droplet" instead of asking.

This measures that against icons whose answer is known. Every distinct drawing in the
set is rendered the way the templates were built (scripts/build_icon_templates.py),
then rendered again the way a rebuild or a generated design draws it: at well size, a
different stroke, light on dark, nudged and scaled a little in its cell, and
optionally blurred and JPEG compressed. Each is then matched by the tool's own
glyph matcher, so what is measured here is what ships.

Every probe is also scored as if its icon were missing from the set, by leaving it and
its twins out of the running. That is the known answer for a glyph the set does not
have, which is what almost every glyph in a real design turns out to be
(scripts/icon_real.py), so it is the number that decides whether a name can be
printed at all.

A sanity arm renders the probes exactly like the templates. It has to come out at
100%; if it does not, the pipeline is broken and nothing below it means anything.

Two drawings can be different paths and the same picture (a circle, and a circle of a
slightly different radius). A probe whose best match is one of those is counted as a
twin, separately, not as correct and not as wrong.

    python scripts/icon_trial.py           # writes scripts/icon-trial.json
    python scripts/icon_trial.py --quick   # every 8th icon, for checking the harness
"""
import importlib.util
import io
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
from PIL import Image, ImageFilter  # noqa: E402

TWIN = 0.95          # template-to-template correlation at which two drawings are one picture
SCHEMES = {"dark-on-light": ("#0f172a", "#FFFFFF"), "light-on-dark": ("#7dd3fc", "#0b1220")}


def degrade(arr):
    """What a generated design does to a small glyph: soft edges and compression."""
    im = Image.fromarray(arr.clip(0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.6))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=70)
    return np.asarray(Image.open(buf).convert("L"), dtype=np.float32)


def probe(bodies, size, stroke, scheme, seed):
    ink, ground = SCHEMES[scheme]
    rng = np.random.default_rng(seed)
    return bt.render_pages(bodies, size, stroke, ink, ground, rng)


def score(probes, T, twins):
    ok = twin = wrong = blank = top5 = 0
    tops, right, absent, margins = [], [], [], []
    for i, v in enumerate(probes):
        if v is None:
            blank += 1
            continue
        sims = T @ v
        order = np.argsort(-sims)
        best = order[0]
        tops.append(float(sims[best]))
        # The gap to the nearest DIFFERENT picture: a runner-up that is the same
        # drawing is not a competitor, it is the same answer under another name.
        rival = next((j for j in order[1:] if j not in twins[i] and j != i), order[1])
        margins.append(float(sims[best] - sims[rival]))
        hit = best == i
        if hit:
            ok += 1
        elif best in twins[i]:
            twin += 1
        else:
            wrong += 1
        right.append(bool(hit or best in twins[i]))
        # The same probe as if its icon were not in the set at all, which is what a
        # brand logo or a hand drawn glyph is: the best score anything else gets.
        away = sims.copy()
        away[[i] + sorted(twins[i])] = -1
        absent.append(float(away.max()))
        if i in order[:5] or any(j in twins[i] for j in order[:5]):
            top5 += 1
    n = len(probes) - blank
    # A score gate: name only matches scoring at least this. named_pct is how many
    # in-set icons still get named and right_pct how many of those are right;
    # absent_named_pct is how often a glyph NOT in the set would be given a name,
    # which is the one that matters, because a real design is almost all of those.
    by_score = {}
    for g in (0.80, 0.85, 0.88, 0.90, 0.92):
        for mg in (0.0, 0.02, 0.03, 0.05):
            named = [r for r, t, m in zip(right, tops, margins) if t >= g and m >= mg]
            by_score["{:.2f}/{:.2f}".format(g, mg)] = {
                "named_pct": round(100.0 * len(named) / max(n, 1), 1),
                "right_pct": round(100.0 * sum(named) / max(len(named), 1), 1),
                "absent_named_pct": round(100.0 * sum(1 for a in absent if a >= g)
                                          / max(len(absent), 1), 1)}
    return {"probes": n, "blank": blank, "correct": ok, "twin": twin, "wrong": wrong,
            "top1_pct": round(100.0 * ok / max(n, 1), 1),
            "top1_or_twin_pct": round(100.0 * (ok + twin) / max(n, 1), 1),
            "top5_pct": round(100.0 * top5 / max(n, 1), 1),
            "median_best": round(float(np.median(tops)), 3) if tops else None,
            "median_best_if_absent": round(float(np.median(absent)), 3) if absent else None,
            "median_margin": round(float(np.median(margins)), 3) if margins else None,
            "by_score": by_score}


def main():
    quick = "--quick" in sys.argv
    icons = bt.unique_icons()
    if quick:
        icons = icons[::8]
    names = [n for n, _ in icons]
    bodies = [b for _, b in icons]
    print("icons", len(icons), flush=True)

    # The shipped templates, cut down to the same subset when running quick, so the
    # trial always matches against exactly the drawings it is probing.
    shipped_names, shipped = so._icon_templates()
    if shipped is None:
        raise SystemExit("no templates: run scripts/build_icon_templates.py first")
    index = {str(n): i for i, n in enumerate(shipped_names)}
    missing = [n for n in names if n not in index]
    if missing:
        raise SystemExit("the templates are out of date, rebuild them: {}".format(missing[:5]))
    T = np.stack([shipped[index[n]] for n in names])

    cross = T @ T.T
    np.fill_diagonal(cross, -1)
    twins = [set(np.nonzero(row >= TWIN)[0].tolist()) for row in cross]
    print("icons with a visual twin at {}: {}".format(TWIN, sum(1 for t in twins if t)),
          flush=True)

    results = {"icons": len(icons), "twin_threshold": TWIN,
               "icons_with_twin": sum(1 for t in twins if t),
               "present_gate": so.ICON_PRESENT, "name_gate": so.ICON_NAME,
               "name_margin": so.ICON_MARGIN,
               "readable_px": so.ICON_READABLE, "arms": []}

    # The sanity arm: rendered exactly as the templates were, through the browser a
    # second time, so it also checks that rendering is repeatable.
    sanity = score(bt.maps_of(bt.render_pages(bodies)), T, twins)
    results["sanity"] = sanity
    print("sanity", sanity["top1_or_twin_pct"], flush=True)
    if sanity["top1_or_twin_pct"] < 99.5:
        raise SystemExit("sanity arm below 99.5%: the matcher cannot find an icon in its own "
                         "rendering, so no other number here would mean anything")

    seed = 0
    for size in (16, 20, 24, 32):
        for stroke in (1.5, 2.0, 2.5):
            for scheme in SCHEMES:
                seed += 1
                pg = probe(bodies, size, stroke, scheme, seed)
                for degraded in (False, True):
                    use = [(degrade(a) if degraded else a, c) for a, c in pg]
                    s = score(bt.maps_of(use), T, twins)
                    s.update(size=size, stroke=stroke, scheme=scheme, degraded=degraded)
                    results["arms"].append(s)
                    print(size, stroke, scheme, degraded, s["top1_pct"], s["top1_or_twin_pct"],
                          s["top5_pct"], s["median_best"], s["median_best_if_absent"],
                          flush=True)

    out = HERE / ("icon-trial-quick.json" if quick else "icon-trial.json")
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print("wrote", out)


if __name__ == "__main__":
    main()
