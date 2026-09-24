"""How far content matching should be allowed to reach, measured on both kinds of page.

Content matching wins the built-to-know trial outright and then misbehaves on real
pages: with no limit on distance it pairs a label with its twin on the other side of the
canvas, because the two really do look alike. The two settings that govern that are the
price of distance and the cost at which a pair is refused outright.

Neither page alone can choose them. The synthetic pages say whether a setting still finds
elements that genuinely moved; the real pages say whether it invents pairs across a
canvas nobody rearranged. This runs both for every setting and prints them side by side,
so the choice is made against the pair of numbers that are in tension.

    python scripts/match_sweep.py
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
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
import match_trial as mt  # noqa: E402

FAR_W = (0.25, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0)
GATE = (10.0, 1.6, 1.4, 1.2, 1.0, 0.9, 0.7, 0.55)
BIGGEST = 6   # real runs to sweep, the largest first: small pages have no lookalikes


def synthetic_cases(tmp):
    """Render every scenario once; the settings sweep reuses the detections."""
    cases = []
    for name in mt.SCENARIOS:
        attempt_items = mt.SCENARIOS[name](mt.PAGE)
        a = mt.render(mt.PAGE, tmp / "{}-design.png".format(name))
        b = mt.render(attempt_items, tmp / "{}-attempt.png".format(name))
        g_ref = so._gray(np.asarray(a.convert("RGB"), dtype=np.float64))
        g_att = so._gray(np.asarray(b.convert("RGB"), dtype=np.float64))
        found_d, found_a = so._elements(g_ref), so._elements(g_att)
        own_d = mt.render_ids(mt.PAGE, tmp / "{}-design-ids.png".format(name))
        own_a = mt.render_ids(attempt_items, tmp / "{}-attempt-ids.png".format(name))
        alive = {i["id"] for i in attempt_items}
        theirs = {}
        for box in found_a:
            ident, rel = mt.owns(box, own_a, attempt_items)
            if ident is not None:
                theirs.setdefault(ident, []).append((rel, box))
        truth, taken = {}, set()
        for box in found_d:
            ident, rel = mt.owns(box, own_d, mt.PAGE)
            if ident is None:
                continue
            if ident not in alive:
                truth[mt.key(box)] = None
                continue
            free = [(r, x) for r, x in theirs.get(ident, []) if mt.key(x) not in taken]
            if not free:
                continue
            r, x = min(free, key=lambda rb: sum((p - q) ** 2 for p, q in zip(rb[0], rel)))
            taken.add(mt.key(x))
            truth[mt.key(box)] = mt.key(x)
        cases.append((name, found_d, found_a, g_ref, g_att, truth))
    return cases


def real_cases():
    """Whatever runs are on this machine, biggest first. Runs are personal and are not
    in the repository, so the result file records how many were swept, never which."""
    out = []
    runs = ROOT / "runs"
    if not runs.is_dir():
        return out
    for d in sorted(p for p in runs.iterdir() if p.is_dir()):
        try:
            run = so._load_run(d.name)
        except Exception:
            continue
        shot = d / "attempts" / "{:03d}.png".format(run.get("best_attempt") or 0)
        if not shot.exists() or not (d / "reference.png").exists():
            continue
        with Image.open(d / "reference.png") as a, Image.open(shot) as b:
            g_ref = so._gray(np.asarray(a.convert("RGB"), dtype=np.float64))
            g_att = so._gray(np.asarray(b.convert("RGB").resize(a.size, Image.LANCZOS),
                                        dtype=np.float64))
        out.append((so._elements(g_ref), so._elements(g_att), g_ref, g_att))
    out.sort(key=lambda c: -len(c[0]))
    return out[:BIGGEST]


def score_synthetic(cases, **kw):
    right = total = 0
    for name, fd, fa, g_ref, g_att, truth in cases:
        matched, missing = so._match_content(fd, fa, g_ref, g_att, **kw)
        got = {mt.key(d): mt.key(a) for d, a in matched}
        for d in missing:
            got[mt.key(d)] = None
        for k, want in truth.items():
            have = got.get(k)
            total += 1
            if want is None and have is None:
                right += 1
            elif want is not None and have is not None and mt.iou(
                    dict(zip("xywh", have)), dict(zip("xywh", want))) >= mt.IOU_SAME:
                right += 1
    return right, total


def score_real(cases, **kw):
    far = paired = designs = 0
    worst = 0.0
    for fd, fa, g_ref, g_att in cases:
        diag = math.hypot(*g_ref.shape[:2])
        matched, _ = so._match_content(fd, fa, g_ref, g_att, **kw)
        paired += len(matched)
        designs += len(fd)
        for d, a in matched:
            r = math.hypot(a["x"] + a["w"] / 2.0 - d["x"] - d["w"] / 2.0,
                           a["y"] + a["h"] / 2.0 - d["y"] - d["h"] / 2.0)
            worst = max(worst, r)
            if r > diag / 4:
                far += 1
    return far, paired, designs, worst


def main():
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="match-sweep-"))
    syn, real = synthetic_cases(tmp), real_cases()
    print("{:>7} {:>6} {:>10} {:>9} {:>9} {:>10}".format(
        "far_w", "gate", "known", "reaches", "paired", "furthest"))
    rows = []
    for fw in FAR_W:
        for gt in GATE:
            r, t = score_synthetic(syn, far_w=fw, gate=gt)
            far, paired, designs, worst = score_real(real, far_w=fw, gate=gt)
            rows.append({"far_w": fw, "gate": gt, "known_correct": r, "known_of": t,
                         "known_pct": round(100.0 * r / t, 1), "cross_page_pairs": far,
                         "real_paired": paired, "real_designs": designs,
                         "real_paired_pct": round(100.0 * paired / designs, 1),
                         "furthest_px": round(worst, 1)})
            print("{:>7} {:>6} {:>9.1f}% {:>9} {:>8.1f}% {:>10.0f}".format(
                fw, gt, rows[-1]["known_pct"], far, rows[-1]["real_paired_pct"], worst))
    # For reference, the two matchers this is competing with, on the same real pages.
    base = {}
    for how in ("geometry", "overlay"):
        paired = designs = far = 0
        for fd, fa, g_ref, g_att in real:
            diag = math.hypot(*g_ref.shape[:2])
            m, _ = so._match_elements(fd, fa, g_ref, g_att, how=how)
            paired += len(m)
            designs += len(fd)
            far += sum(1 for d, a in m if math.hypot(
                a["x"] + a["w"] / 2.0 - d["x"] - d["w"] / 2.0,
                a["y"] + a["h"] / 2.0 - d["y"] - d["h"] / 2.0) > diag / 4)
        base[how] = {"paired": paired, "designs": designs, "cross_page_pairs": far,
                     "paired_pct": round(100.0 * paired / designs, 1)}
        print("{:>7} {:>6} {:>10} {:>9} {:>8.1f}%".format(
            how, "", "", far, base[how]["paired_pct"]))
    dest = ROOT / "scripts" / "match-sweep.json"
    dest.write_text(json.dumps({"sweep": rows, "other_matchers_on_real": base,
                                "real_runs": len(real)}, indent=2), encoding="utf-8")
    print("\nwrote {}".format(dest))


if __name__ == "__main__":
    main()
