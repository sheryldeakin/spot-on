"""What the colour score's falloff should be, once distance is measured perceptually.

The colour score is `100 * exp(-distance / falloff)`. Distance used to be Euclidean in
RGB, on a 0 to 441 scale, and the falloff was 60. CIEDE2000 runs on a different scale
entirely: about 2.3 is the smallest difference a person can see and black against white
is a little over 100. Carrying the old 60 across would quietly turn every colour score
into a different number, and every score in every saved run with it.

So the falloff is fitted rather than picked: the value that leaves the colour scores of
real runs closest to where they already were. That makes the change what it is meant to
be, a re-ranking of which colour errors count as bad, rather than a rescaling of the
whole axis. Whatever moves after the fit is the perceptual correction doing its work,
and the script prints the elements that moved most so that can be read rather than
assumed.

    python scripts/fit_colour.py            # fit over every saved run
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

OLD_FALLOFF = 60.0        # what the RGB-distance score used
CANDIDATES = np.arange(5.0, 40.0, 0.1)


class AsRgb:
    """Score with the old colour metric, by putting the old functions back.

    The scores stored in a run were computed by whatever the tool was on the day, and
    on these runs that is months of other changes. Comparing against them would credit
    or blame the colour metric for all of it, so both sides of the comparison are
    computed here, now, with only the colour measure differing.
    """

    def __enter__(self):
        self.saved = (so._mean_delta_e, so._colour_gap, so.COLOUR_FALLOFF, so.DELTA_E_MAX)
        so._mean_delta_e = lambda r, a, sample=None: float(
            np.sqrt(((r - a) ** 2).sum(axis=1)).mean())
        so._colour_gap = lambda a, b: sum(
            (x - y) ** 2 for x, y in zip(a["rgb"], b["rgb"])) ** 0.5
        so.COLOUR_FALLOFF = OLD_FALLOFF
        so.DELTA_E_MAX = 441.7
        return self

    def __exit__(self, *e):
        so._mean_delta_e, so._colour_gap, so.COLOUR_FALLOFF, so.DELTA_E_MAX = self.saved


def old_rgb_distance(ref, att, m_ref, m_att):
    """The palette-and-background distance exactly as it was measured before."""
    ref_cols = so._top_colours(ref, m_ref)
    att_cols = so._top_colours(att, m_att)

    def gap(a, b):
        return sum((x - y) ** 2 for x, y in zip(a["rgb"], b["rgb"])) ** 0.5

    if not ref_cols or not att_cols:
        palette = 441.7
    else:
        fwd = sum(min(gap(rc, ac) for ac in att_cols) * rc["share"] for rc in ref_cols)
        fwd /= max(sum(rc["share"] for rc in ref_cols), 1e-6)
        back = sum(min(gap(ac, rc) for rc in ref_cols) * ac["share"] for ac in att_cols)
        back /= max(sum(ac["share"] for ac in att_cols), 1e-6)
        palette = 0.6 * fwd + 0.4 * back
    behind = ~np.logical_or(m_ref, m_att)
    background = (float(np.sqrt(((ref[behind] - att[behind]) ** 2).sum(axis=1)).mean())
                  if behind.sum() >= 64 else 0.0)
    return max(palette, background)


def pairs():
    """Every saved attempt, as (old distance, new distance)."""
    out = []
    runs = ROOT / "runs"
    if not runs.is_dir():
        return out
    for d in sorted(p for p in runs.iterdir() if p.is_dir()):
        try:
            so._load_run(d.name)
        except Exception:
            continue
        ref_path = d / "reference.png"
        if not ref_path.exists():
            continue
        with Image.open(ref_path) as r:
            ref_img = r.convert("RGB")
            ref = np.asarray(ref_img, dtype=np.float64)
        m_ref = so._ink_mask(ref)
        for shot in sorted((d / "attempts").glob("*.png")):
            if shot.stem.endswith("-diff"):
                continue
            with Image.open(shot) as a:
                att = np.asarray(a.convert("RGB").resize(ref_img.size, Image.LANCZOS),
                                 dtype=np.float64)
            m_att = so._ink_mask(att)
            old = old_rgb_distance(ref, att, m_ref, m_att)
            new_palette = so._palette_distance(so._top_colours(ref, m_ref),
                                               so._top_colours(att, m_att))
            behind = ~np.logical_or(m_ref, m_att)
            new_bg = (so._mean_delta_e(ref[behind], att[behind])
                      if behind.sum() >= 64 else 0.0)
            out.append({"run": d.name, "n": shot.stem,
                        "old": old, "new": max(new_palette, new_bg)})
    return out


def main():
    rows = pairs()
    if not rows:
        print("no saved runs to fit against")
        return
    old_scores = np.array([100.0 * math.exp(-r["old"] / OLD_FALLOFF) for r in rows])
    new_dist = np.array([r["new"] for r in rows])
    # Each run counts once, however many attempts it holds. One run of twenty near
    # identical rewrites would otherwise decide the constant for all of them.
    per_run = {}
    for r in rows:
        per_run[r["run"]] = per_run.get(r["run"], 0) + 1
    w = np.array([1.0 / per_run[r["run"]] for r in rows])
    w = w / w.sum()
    best, best_err = None, None
    for k in CANDIDATES:
        err = float(np.sqrt((w * ((100.0 * np.exp(-new_dist / k)) - old_scores) ** 2).sum()))
        if best_err is None or err < best_err:
            best, best_err = float(k), err
    new_scores = 100.0 * np.exp(-new_dist / best)
    moved = np.abs(new_scores - old_scores)
    print("fitted over {} attempts in {} runs".format(
        len(rows), len({r["run"] for r in rows})))
    print("falloff {:.1f}   root mean square change {:.2f} points   "
          "biggest change {:.1f}".format(best, best_err, moved.max()))
    print("\nwhere the perceptual measure disagrees most with distance in RGB")
    print("{:<34}{:>9}{:>9}{:>8}".format("run / attempt", "was", "now", "delta"))
    for i in np.argsort(-moved)[:10]:
        print("{:<34}{:>9.1f}{:>9.1f}{:>+8.1f}".format(
            (rows[i]["run"] + " " + rows[i]["n"])[:33],
            old_scores[i], new_scores[i], new_scores[i] - old_scores[i]))
    ab = compare_runs(best)
    dest = ROOT / "scripts" / "colour-fit.json"
    dest.write_text(json.dumps({
        "falloff": round(best, 1), "attempts": len(rows),
        "runs": len({r["run"] for r in rows}),
        "rms_change": round(best_err, 2), "biggest_change": round(float(moved.max()), 1),
        "match": ab,
    }, indent=2), encoding="utf-8")
    print("\nwrote {}".format(dest))


def compare_runs(falloff):
    """Score every saved attempt both ways and report what the change does to `match`."""
    so.COLOUR_FALLOFF = falloff
    deltas, flips, runs = [], [], 0
    for d in sorted(p for p in (ROOT / "runs").iterdir() if p.is_dir()):
        if not (d / "reference.png").exists():
            continue
        shots = [s for s in sorted((d / "attempts").glob("*.png"))
                 if not s.stem.endswith("-diff")]
        if len(shots) < 2:
            continue
        runs += 1
        with Image.open(d / "reference.png") as r:
            ref = r.convert("RGB")
            old_s, new_s = {}, {}
            for s in shots:
                with Image.open(s) as a:
                    att = a.convert("RGB")
                    with AsRgb():
                        old_s[s.stem] = so.score_images(ref, att, regions=False,
                                                        deep=False)[0]["match"]
                    new_s[s.stem] = so.score_images(ref, att, regions=False,
                                                    deep=False)[0]["match"]
        for k in old_s:
            deltas.append(new_s[k] - old_s[k])
        ob = max(old_s, key=lambda k: old_s[k])
        nb = max(new_s, key=lambda k: new_s[k])
        if ob != nb:
            flips.append({"run": d.name, "was": ob, "now": nb,
                          "gap_before": round(old_s[ob] - old_s[nb], 2),
                          "gap_after": round(new_s[nb] - new_s[ob], 2)})
    v = np.array(deltas)
    print("\nthe same {} attempts in {} runs, scored both ways with only the colour "
          "measure differing".format(len(v), runs))
    print("  match changes by {:+.2f} on average, sd {:.2f}, from {:+.1f} to {:+.1f}"
          .format(v.mean(), v.std(), v.min(), v.max()))
    print("  runs whose best attempt changes: {}".format(len(flips)))
    for f in flips:
        print("    {}: {} -> {}, ahead by {:.2f} before and {:.2f} after"
              .format(f["run"], f["was"], f["now"], f["gap_before"], f["gap_after"]))
    return {"attempts": len(v), "runs": runs, "mean": round(float(v.mean()), 2),
            "sd": round(float(v.std()), 2), "min": round(float(v.min()), 1),
            "max": round(float(v.max()), 1), "best_attempt_flips": flips}


if __name__ == "__main__":
    main()
