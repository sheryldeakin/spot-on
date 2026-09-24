"""Which way of pairing design elements with attempt elements is right most often.

Per-element feedback is only as good as the pairing behind it. Get the pairing wrong
and the report invents faults: a moved element becomes one thing missing and one thing
unexpected, and the next round redraws something that was already correct.

Three ways to pair are measured here against pages built so the answer is known:
  geometry  the attempt element nearest in position and size
  content   the attempt element that looks most like it, position only breaking ties
  overlay   no pairing at all, compare each design element with whatever is in its box

Every page is built from a list of elements with known boxes, and the attempt is the
same list under a named mutation, so the true partner of each design element is known
before anything is detected. Ownership is read from a third render in which each
element is a flat unique colour: whichever element owns most of a detected box owns
that box. An element the detector splits into a frame and a label keeps both, paired
by where each sits inside the element; a box no element owns is dropped from the count
rather than charged to a matcher, and the dropped share is reported.

    python scripts/match_trial.py                 # all scenarios, writes match-trial.json
    python scripts/match_trial.py swap-two        # one scenario
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("spot_on", ROOT / "spot-on.py")
so = importlib.util.module_from_spec(spec)
sys.modules["spot_on"] = so
spec.loader.exec_module(so)

import numpy as np  # noqa: E402  (after the module load, which sets the import path)

W, H = 1000, 700
GROUND = "#FFFFFF"
IOU_SAME = 0.5      # overlap at which a detected box counts as one intended box

# A page with the shapes a real one has: a nav row of short labels, a heading, body
# copy, three cards, two buttons. Sizes and colours vary so content matching has
# something to tell them apart with, which is the whole question being asked.
PAGE = [
    dict(id="nav-home", x=40, y=28, w=70, h=22, bg="none", fg="#334155", fs=15, text="Home"),
    dict(id="nav-work", x=140, y=28, w=80, h=22, bg="none", fg="#334155", fs=15, text="Projects"),
    dict(id="nav-team", x=250, y=28, w=70, h=22, bg="none", fg="#334155", fs=15, text="Team"),
    dict(id="nav-cta", x=860, y=22, w=100, h=34, bg="#1d4ed8", fg="#FFFFFF", fs=14,
         text="Sign up"),
    dict(id="heading", x=40, y=120, w=560, h=56, bg="none", fg="#0f172a", fs=44,
         text="Measure the page"),
    dict(id="sub", x=40, y=196, w=520, h=48, bg="none", fg="#475569", fs=17,
         text="Score a rebuild against the design and say where the miss is."),
    dict(id="hero-btn", x=40, y=268, w=150, h=44, bg="#0f172a", fg="#FFFFFF", fs=16,
         text="Get started"),
    dict(id="hero-alt", x=214, y=268, w=130, h=44, bg="#E2E8F0", fg="#0f172a", fs=16,
         text="Read docs"),
    dict(id="card-one", x=40, y=380, w=280, h=200, bg="#F1F5F9", fg="#0f172a", fs=20,
         text="Structure"),
    dict(id="card-two", x=360, y=380, w=280, h=200, bg="#FEF3C7", fg="#0f172a", fs=20,
         text="Colour"),
    dict(id="card-three", x=680, y=380, w=280, h=200, bg="#DCFCE7", fg="#0f172a", fs=20,
         text="Detail"),
    dict(id="badge", x=680, y=120, w=180, h=60, bg="#312E81", fg="#E0E7FF", fs=30,
         text="98.4"),
    dict(id="note", x=680, y=210, w=280, h=40, bg="none", fg="#64748B", fs=14,
         text="Updated every round"),
]


def render(items, path):
    body = []
    for it in items:
        bg = "" if it["bg"] == "none" else "background:{};".format(it["bg"])
        body.append(
            "<div style=\"position:absolute;left:{x}px;top:{y}px;width:{w}px;height:{h}px;"
            "{bg}color:{fg};font:{fs}px/{lh}px 'Segoe UI',sans-serif;border-radius:8px;"
            "box-sizing:border-box;padding:6px 10px;overflow:hidden\">{text}</div>".format(
                **dict(it, bg=bg, lh=int(it["fs"] * 1.3))))
    return so.render_code("".join(body), "html", W, H, path, ground=GROUND)


def render_ids(items, path):
    """The same page with every element a flat unique colour: who owns which pixel."""
    body = []
    for n, it in enumerate(items, 1):
        body.append(
            "<div style=\"position:absolute;left:{x}px;top:{y}px;width:{w}px;height:{h}px;"
            "background:rgb({r},{g},{b});box-sizing:border-box\"></div>".format(
                x=it["x"], y=it["y"], w=it["w"], h=it["h"],
                r=(n * 37) % 256, g=(n * 91) % 256, b=(n * 143) % 256))
    img = so.render_code("".join(body), "html", W, H, path, ground="#000000")
    a = np.asarray(img.convert("RGB"), dtype=np.int32)
    owner = np.zeros(a.shape[:2], dtype=np.int32)
    for n, it in enumerate(items, 1):
        want = ((n * 37) % 256, (n * 91) % 256, (n * 143) % 256)
        owner[(np.abs(a - np.array(want)).max(axis=2) <= 6)] = n
    return owner


def owns(box, owner, ids):
    """Which element owns most of a detected box, and where the box sits inside it."""
    sub = owner[box["y"]:box["y"] + box["h"], box["x"]:box["x"] + box["w"]]
    if sub.size == 0:
        return None, None
    counts = np.bincount(sub.ravel(), minlength=len(ids) + 1)
    counts[0] = 0
    n = int(counts.argmax())
    if n == 0 or counts[n] < 0.30 * sub.size:
        return None, None
    it = ids[n - 1]
    rel = ((box["x"] - it["x"]) / float(it["w"]), (box["y"] - it["y"]) / float(it["h"]),
           box["w"] / float(it["w"]), box["h"] / float(it["h"]))
    return it["id"], rel


def move(items, ident, dx, dy):
    return [dict(i, x=i["x"] + dx, y=i["y"] + dy) if i["id"] == ident else i for i in items]


def grow(items, ident, fw, fh=1.0):
    return [dict(i, w=int(i["w"] * fw), h=int(i["h"] * fh)) if i["id"] == ident else i
            for i in items]


def drop(items, *idents):
    return [i for i in items if i["id"] not in idents]


def flip(items, ident):
    def f(i):
        if i["id"] != ident:
            return i
        bg = "#0f172a" if i["bg"] == "none" else "none"
        return dict(i, bg=bg, fg="#F8FAFC" if bg != "none" else "#0f172a")
    return [f(i) for i in items]


def swap(items, a, b):
    ia = next(i for i in items if i["id"] == a)
    ib = next(i for i in items if i["id"] == b)
    out = []
    for i in items:
        if i["id"] == a:
            out.append(dict(i, x=ib["x"], y=ib["y"]))
        elif i["id"] == b:
            out.append(dict(i, x=ia["x"], y=ia["y"]))
        else:
            out.append(i)
    return out


def shift_all(items, dx, dy):
    return [dict(i, x=i["x"] + dx, y=i["y"] + dy) for i in items]


def retext(items, ident, text):
    return [dict(i, text=text) if i["id"] == ident else i for i in items]


def rotate_positions(items, *idents):
    """Give each named element the position of the next one in the list."""
    slots = [(i["x"], i["y"]) for i in items if i["id"] in idents]
    order = [i["id"] for i in items if i["id"] in idents]
    put = dict(zip(order, slots[1:] + slots[:1]))
    return [dict(i, x=put[i["id"]][0], y=put[i["id"]][1]) if i["id"] in put else i
            for i in items]


def scale_all(items, f):
    return [dict(i, x=int(i["x"] * f), y=int(i["y"] * f), w=int(i["w"] * f),
                 h=int(i["h"] * f), fs=int(i["fs"] * f)) for i in items]


EXTRA = [
    dict(id="new-chip", x=380, y=268, w=110, h=44, bg="#FCE7F3", fg="#9D174D", fs=15,
         text="What's new"),
    dict(id="new-rule", x=40, y=340, w=920, h=20, bg="#E2E8F0", fg="#E2E8F0", fs=12,
         text="."),
]


SCENARIOS = {
    "identical": lambda p: list(p),
    "shift-all": lambda p: shift_all(p, 0, 8),
    "resize-some": lambda p: grow(grow(grow(p, "card-two", 1.25), "hero-btn", 1.3),
                                  "badge", 1.0, 1.4),
    "move-one-far": lambda p: move(p, "badge", -330, 300),
    "swap-two": lambda p: swap(p, "card-one", "card-three"),
    "delete-two": lambda p: drop(p, "nav-team", "card-two"),
    "recolour": lambda p: flip(flip(flip(p, "card-one"), "sub"), "hero-alt"),
    "mixed": lambda p: flip(drop(move(grow(shift_all(p, 3, 5), "card-three", 1.15),
                                      "badge", -120, 180), "nav-work"), "card-one"),
    # The cases that should punish content matching rather than position matching.
    "nav-shuffle": lambda p: rotate_positions(p, "nav-home", "nav-work", "nav-team"),
    "text-swap": lambda p: retext(retext(retext(p, "heading", "Rebuild the page"),
                                         "card-two", "Spacing"), "nav-work", "Archive"),
    "add-two": lambda p: p + EXTRA,
    "page-scale": lambda p: scale_all(p, 1.08),
    "move-three-far": lambda p: move(move(move(p, "badge", -330, 300),
                                          "hero-alt", 500, -180), "nav-cta", -700, 520),
    "siblings-swap": lambda p: swap(retext(retext(p, "card-one", "Spacing"),
                                           "card-three", "Spacing"),
                                    "card-one", "card-two"),
}


def iou(a, b):
    x0, y0 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x1 = min(a["x"] + a["w"], b["x"] + b["w"])
    y1 = min(a["y"] + a["h"], b["y"] + b["h"])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    return inter / float(a["w"] * a["h"] + b["w"] * b["h"] - inter)


def nearest(intended, found):
    """The detected box that is the intended one, or None when the detector merged it."""
    best, best_v = None, IOU_SAME
    for f in found:
        v = iou(intended, f)
        if v >= best_v:
            best, best_v = f, v
    return best


def key(box):
    return None if box is None else (box["x"], box["y"], box["w"], box["h"])


def run_scenario(name, tmp):
    attempt_items = SCENARIOS[name](PAGE)
    a = render(PAGE, tmp / "{}-design.png".format(name))
    b = render(attempt_items, tmp / "{}-attempt.png".format(name))
    g_ref = so._gray(np.asarray(a.convert("RGB"), dtype=np.float64))
    g_att = so._gray(np.asarray(b.convert("RGB"), dtype=np.float64))
    found_d, found_a = so._elements(g_ref), so._elements(g_att)
    own_d = render_ids(PAGE, tmp / "{}-design-ids.png".format(name))
    own_a = render_ids(attempt_items, tmp / "{}-attempt-ids.png".format(name))
    alive = {i["id"] for i in attempt_items}

    # Every detected attempt box, grouped by the element that owns it.
    theirs = {}
    for box in found_a:
        ident, rel = owns(box, own_a, attempt_items)
        if ident is not None:
            theirs.setdefault(ident, []).append((rel, box))

    # The ground truth, in detected boxes: for each design box the detector found, the
    # attempt box it became (the one sitting in the same place inside the same element),
    # or None when the element it belongs to was deleted.
    truth, dropped, taken = {}, 0, set()
    for box in found_d:
        ident, rel = owns(box, own_d, PAGE)
        if ident is None:
            dropped += 1
            continue
        if ident not in alive:
            truth[key(box)] = None
            continue
        free = [(r, b) for r, b in theirs.get(ident, []) if key(b) not in taken]
        if not free:
            dropped += 1
            continue
        r, b = min(free, key=lambda rb: sum((x - y) ** 2 for x, y in zip(rb[0], rel)))
        taken.add(key(b))
        truth[key(box)] = key(b)

    rows = {}
    for how in ("geometry", "content", "overlay"):
        matched, missing = so._match_elements(found_d, found_a, g_ref, g_att, how=how)
        got = {key(d): key(att) for d, att in matched}
        for d in missing:
            got[key(d)] = None
        right = wrong = absent_right = absent_wrong = 0
        for k, want in truth.items():
            have = got.get(k, None)
            if want is None:
                if have is None:
                    absent_right += 1
                else:
                    absent_wrong += 1
            elif have is not None and iou(dict(zip("xywh", have)), dict(zip("xywh", want))) >= IOU_SAME:
                right += 1
            elif have is None:
                absent_wrong += 1
            else:
                wrong += 1
        total = len(truth)
        rows[how] = {"correct": right + absent_right, "of": total,
                     "paired_wrong": wrong, "called_absent_wrongly": absent_wrong,
                     "accuracy": round(100.0 * (right + absent_right) / total, 1) if total else 0.0}
    return {"scenario": name, "evaluable": len(truth), "dropped": dropped,
            "design_boxes": len(found_d), "attempt_boxes": len(found_a), "matchers": rows}


def main():
    want = sys.argv[1:] or list(SCENARIOS)
    tmp = Path(tempfile.mkdtemp(prefix="match-trial-"))
    out = [run_scenario(n, tmp) for n in want]
    hows = ("geometry", "content", "overlay")
    print("{:<14} {:>5} {:>5} {}".format(
        "scenario", "eval", "drop", "".join("{:>11}".format(h) for h in hows)))
    for r in out:
        print("{:<14} {:>5} {:>5} {}".format(
            r["scenario"], r["evaluable"], r["dropped"],
            "".join("{:>10.1f}%".format(r["matchers"][h]["accuracy"]) for h in hows)))
    totals = {h: (sum(r["matchers"][h]["correct"] for r in out),
                  sum(r["matchers"][h]["of"] for r in out)) for h in hows}
    print("{:<14} {:>5} {:>5} {}".format(
        "ALL", sum(r["evaluable"] for r in out), sum(r["dropped"] for r in out),
        "".join("{:>10.1f}%".format(100.0 * c / max(1, n)) for c, n in totals.values())))
    print("")
    for h in hows:
        print("{:<9} paired wrong {:>3}   called absent wrongly {:>3}".format(
            h, sum(r["matchers"][h]["paired_wrong"] for r in out),
            sum(r["matchers"][h]["called_absent_wrongly"] for r in out)))
    dest = ROOT / "scripts" / "match-trial.json"
    dest.write_text(json.dumps({"scenarios": out, "totals": {
        h: {"correct": c, "of": n, "accuracy": round(100.0 * c / max(1, n), 1)}
        for h, (c, n) in totals.items()}}, indent=2), encoding="utf-8")
    print("\nwrote {}".format(dest))


if __name__ == "__main__":
    main()
