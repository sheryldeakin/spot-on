"""Spot On: pixel-perfect pages, measured.

Stop playing spot the difference with your AI. When a page is clearly off from
the design and the model keeps answering "fixed" without getting closer, Spot On
screenshots the page, scores it against the design on four axes, shows where
the miss is, and writes the feedback the model needs for the next round.

Run:
    python spot-on.py              # serve on http://127.0.0.1:7265
    python spot-on.py 7266         # serve on another port
    python spot-on.py score <design.png> <url-or-file> [--kind url|html|svg|canvas]
                            [--scale 1.5] [--diff out.png] [--json]

The tool itself serves on loopback only. Nothing is uploaded anywhere.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image

HOST = "127.0.0.1"
PORT = 7265

HERE = Path(__file__).resolve().parent
RUNS_DIR = HERE / "runs"
RUNS_DIR.mkdir(exist_ok=True)

CHROME_CANDIDATES = [
    # An unset override must not become Path("."), which exists and is not a browser.
    Path(p) for p in [
        os.environ.get("SPOT_ON_CHROME", "").strip(),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ] if p
]

# url: a running page, the usual web dev case. html: a pasted page or component.
# svg and canvas stay for icons and illustrations that live on the page.
KINDS = ("url", "html", "svg", "canvas")
SCALES = (1.0, 1.25, 1.5, 2.0)
MAX_PIXELS = 6_000_000   # designs bigger than this are scaled down before scoring
SSIM_WINDOW = 7          # odd; box window for the structural term
INK_THRESHOLD = 28       # colour distance from the page ground that counts as drawn


# ---------------------------------------------------------------- run storage

def _slugify(name):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", str(name).strip().lower()).strip("-")
    return s[:48] or "run"


def _run_dir(slug):
    """Resolve a run directory, refusing anything that escapes runs/."""
    d = (RUNS_DIR / _slugify(slug)).resolve()
    if RUNS_DIR.resolve() not in d.parents:
        raise ValueError("bad run name")
    return d


def _load_run(slug):
    p = _run_dir(slug) / "run.json"
    if not p.exists():
        raise ValueError("no run named {!r}".format(slug))
    return json.loads(p.read_text(encoding="utf-8"))


def _save_run(slug, data):
    d = _run_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def _list_runs():
    out = []
    for d in sorted(RUNS_DIR.glob("*/run.json")):
        try:
            r = json.loads(d.read_text(encoding="utf-8"))
            r["attempts"] = len(list((d.parent / "attempts").glob("*.json")))
            out.append(r)
        except Exception:
            continue
    out.sort(key=lambda r: r.get("created", 0), reverse=True)
    return out


def _attempts(slug):
    d = _run_dir(slug) / "attempts"
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    out.sort(key=lambda a: a.get("n", 0))
    return out


def _next_n(slug):
    return len(_attempts(slug)) + 1


# ------------------------------------------------------------------ rendering

def _find_chrome():
    for c in CHROME_CANDIDATES:
        if c.is_file():
            return c
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return Path(found)
    raise RuntimeError("no Chrome or Edge found; set SPOT_ON_CHROME to the browser executable")


def _check_url(url):
    u = (url or "").strip()
    if not re.match(r"^https?://[^\s]+$", u):
        raise ValueError("a running page needs a full http:// or https:// address, "
                         "for example http://localhost:5173/pricing")
    return u


def _wrap(code, kind, width, height, ground="#FFFFFF"):
    """Put an attempt inside a fixed-size page so every render is comparable."""
    shell = (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "html,body{{margin:0;padding:0;width:{w}px;height:{h}px;overflow:hidden;background:{g};}}"
        "svg,canvas{{display:block;}}"
        "</style></head><body>{body}</body></html>"
    )
    if kind == "svg":
        body = code
    elif kind == "canvas":
        body = (
            "<canvas id='c' width='{w}' height='{h}'></canvas><script>\n"
            "(function(ctx, W, H){{\n{code}\n}})"
            "(document.getElementById('c').getContext('2d'), {w}, {h});\n"
            "</script>"
        ).format(w=width, h=height, code=code)
    elif kind == "html":
        body = code
    else:
        raise ValueError("kind must be one of {}".format(", ".join(KINDS)))
    return shell.format(w=width, h=height, g=ground, body=body)


def render_code(code, kind, css_width, css_height, out_png, out_size=None, scale=1.0,
                ground="#FFFFFF", settle_ms=None):
    """Screenshot an attempt with headless Chrome.

    The browser window is css_width x css_height CSS pixels at the given device
    scale, which is what the design was captured at. A 2160px-wide screenshot
    taken on a 150% display is a 1440px page; rendering it 2160px wide would
    trigger a different layout, and the score would measure the wrong page.
    """
    chrome = _find_chrome()
    out_size = out_size or (round(css_width * scale), round(css_height * scale))
    tmp = Path(tempfile.mkdtemp(prefix="spot-on-"))
    try:
        if kind == "url":
            target = _check_url(code)
            settle_ms = settle_ms or 4000
        else:
            page = tmp / "attempt.html"
            page.write_text(_wrap(code, kind, css_width, css_height, ground), encoding="utf-8")
            target = page.as_uri()
            settle_ms = settle_ms or 1200
        shot = tmp / "shot.png"
        cmd = [
            str(chrome),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--disable-extensions",
            "--no-first-run",
            "--force-device-scale-factor={}".format(scale),
            "--user-data-dir={}".format(tmp / "profile"),
            "--window-size={},{}".format(css_width, css_height),
            "--virtual-time-budget={}".format(settle_ms),
            "--screenshot={}".format(shot),
            target,
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        if not shot.exists():
            err = (proc.stderr or b"").decode("utf-8", "replace")[-600:]
            raise RuntimeError("the browser produced no screenshot. {}".format(err.strip()))
        shot_img = Image.open(shot)
        if shot_img.mode in ("RGBA", "LA", "P"):
            # A page with no background colour is white in a real browser, not black.
            flat = Image.new("RGB", shot_img.size, "#FFFFFF")
            flat.paste(shot_img.convert("RGBA"), mask=shot_img.convert("RGBA").split()[3])
            img = flat
        else:
            img = shot_img.convert("RGB")
        if img.size != tuple(out_size):
            img = img.resize(tuple(out_size), Image.LANCZOS)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_png)
        return img
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -------------------------------------------------------------------- scoring

def _box_mean(a, k):
    """Mean over a k x k window, edge-padded, via an integral image."""
    pad = k // 2
    ap = np.pad(a, pad, mode="edge")
    c = np.cumsum(np.cumsum(ap, axis=0), axis=1)
    c = np.pad(c, ((1, 0), (1, 0)), mode="constant")
    h, w = a.shape
    s = c[k:k + h, k:k + w] - c[0:h, k:k + w] - c[k:k + h, 0:w] + c[0:h, 0:w]
    return s / float(k * k)


def _gray(rgb):
    return rgb[:, :, 0] * 0.299 + rgb[:, :, 1] * 0.587 + rgb[:, :, 2] * 0.114


def _ssim(ga, gb, k=SSIM_WINDOW):
    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2
    mu_a, mu_b = _box_mean(ga, k), _box_mean(gb, k)
    saa = _box_mean(ga * ga, k) - mu_a * mu_a
    sbb = _box_mean(gb * gb, k) - mu_b * mu_b
    sab = _box_mean(ga * gb, k) - mu_a * mu_b
    num = (2 * mu_a * mu_b + c1) * (2 * sab + c2)
    den = (mu_a ** 2 + mu_b ** 2 + c1) * (saa + sbb + c2)
    return np.clip(num / np.maximum(den, 1e-9), -1.0, 1.0)


def _ground_colour(rgb):
    """The page colour, read from a 3px border ring rather than assumed white."""
    h, w = rgb.shape[:2]
    ring = np.concatenate([
        rgb[:3, :, :].reshape(-1, 3), rgb[-3:, :, :].reshape(-1, 3),
        rgb[:, :3, :].reshape(-1, 3), rgb[:, -3:, :].reshape(-1, 3),
    ])
    return np.median(ring, axis=0)


def _ink_mask(rgb, ground):
    return np.sqrt(((rgb - ground) ** 2).sum(axis=2)) > INK_THRESHOLD


def _top_colours(rgb, mask, limit=6):
    """Dominant drawn colours, quantised to 32 levels per channel."""
    if mask.sum() < 16:
        return []
    px = rgb[mask].astype(np.int32) >> 3
    keys = (px[:, 0] << 10) | (px[:, 1] << 5) | px[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    order = np.argsort(counts)[::-1][:limit]
    total = float(mask.sum())
    out = []
    for i in order:
        k = int(vals[i])
        r, g, b = ((k >> 10) & 31) << 3, ((k >> 5) & 31) << 3, (k & 31) << 3
        out.append({"hex": "#{:02X}{:02X}{:02X}".format(r, g, b),
                    "rgb": [r, g, b],
                    "share": round(float(counts[i]) / total * 100.0, 1)})
    return out


def _colour_gap(a, b):
    return sum((x - y) ** 2 for x, y in zip(a["rgb"], b["rgb"])) ** 0.5


def _palette_distance(ref_cols, att_cols):
    """How far the attempt's palette is from the reference's, regardless of placement.

    Measured both ways: reference colours with no close match (something is missing)
    and attempt colours that appear nowhere in the reference (something is invented).
    """
    if not ref_cols and not att_cols:
        return 0.0
    if not ref_cols or not att_cols:
        return 441.7
    fwd = sum(min(_colour_gap(rc, ac) for ac in att_cols) * rc["share"] for rc in ref_cols)
    fwd /= max(sum(rc["share"] for rc in ref_cols), 1e-6)
    back = sum(min(_colour_gap(ac, rc) for rc in ref_cols) * ac["share"] for ac in att_cols)
    back /= max(sum(ac["share"] for ac in att_cols), 1e-6)
    return 0.6 * fwd + 0.4 * back


def _offsets(g_ref, g_att, bands=10, max_frac=0.15, min_px=4):
    """Find content that is shifted rather than wrong, and where the shift starts.

    On a web page one missing or extra element pushes everything below it, and a
    pixel comparison then reports the whole lower page as wrong. Each horizontal
    band of the design is slid up and down against the attempt (2D, on a softened
    grayscale copy so font differences do not dominate) to find the distance that
    lines it up best.

    A shift is reported only when it clearly beats no shift and several bands
    agree on the distance. A missing element or a small size change can make one
    band prefer some offset, and reporting that as a shift would send the fix to
    the wrong place.
    """
    h, w = g_ref.shape
    f = max(1, int(np.ceil(h / 600.0)), int(np.ceil(w / 800.0)))
    hh, ww = h // f, w // f
    r = g_ref[:hh * f, :ww * f].reshape(hh, f, ww, f).mean(axis=(1, 3))
    a = g_att[:hh * f, :ww * f].reshape(hh, f, ww, f).mean(axis=(1, 3))
    r, a = _box_mean(r, 5), _box_mean(a, 5)
    out = {"vertical": None, "horizontal": None}

    lag = max(2, int(hh * max_frac))
    found = []
    for b in range(bands):
        lo, hi = hh * b // bands, hh * (b + 1) // bands
        band = r[lo:hi]
        if band.std() < 2.0:
            continue  # a flat band has nothing to line up
        e0 = float(np.abs(band - a[lo:hi]).mean())
        best, best_e = 0, e0
        for d in range(-lag, lag + 1):
            s0, s1 = lo - d, hi - d
            if s0 < 0 or s1 > hh:
                continue
            e = float(np.abs(band - a[s0:s1]).mean())
            if e < best_e - 1e-9:
                best, best_e = d, e
        # 0.75: on a real pricing page the shifted heading bands line up at 0.69 to
        # 0.71, while the closest no-shift case in the tests sits at 0.78.
        if e0 > 0 and abs(best) * f >= min_px and best_e <= 0.75 * e0:
            found.append((lo * f, best * f))
    if found:
        tol = max(3, 2 * f)
        groups = [[x for x in found if abs(x[1] - anchor[1]) <= tol] for anchor in found]
        group = max(groups, key=len)
        if len(group) >= 2:
            out["vertical"] = {"from_y": group[0][0],
                               "shift": int(np.median([d for _, d in group])),
                               "bands": len(group)}

    lag_x = max(2, int(ww * max_frac))
    e0 = float(np.abs(r - a).mean())
    best, best_e = 0, e0
    for d in range(-lag_x, lag_x + 1):
        if d >= 0:
            e = float(np.abs(r[:, d:] - a[:, :ww - d]).mean()) if d < ww else None
        else:
            e = float(np.abs(r[:, :ww + d] - a[:, -d:]).mean())
        if e is not None and e < best_e - 1e-9:
            best, best_e = d, e
    if e0 > 0 and abs(best) * f >= min_px and best_e <= 0.75 * e0:
        out["horizontal"] = {"shift": best * f}
    return out


def _region_grid(diff_sq, rows=4, cols=4):
    h, w = diff_sq.shape
    cells = []
    for r in range(rows):
        for c in range(cols):
            y0, y1 = h * r // rows, h * (r + 1) // rows
            x0, x1 = w * c // cols, w * (c + 1) // cols
            block = diff_sq[y0:y1, x0:x1]
            cells.append({"row": r, "col": c,
                          "rmse": round(float(np.sqrt(block.mean())), 2)})
    return cells


_ROW_WORDS = ["top", "upper middle", "lower middle", "bottom"]
_COL_WORDS = ["left", "centre left", "centre right", "right"]


def _cell_name(cell):
    return "{}, {}".format(_ROW_WORDS[cell["row"]], _COL_WORDS[cell["col"]])


def score_images(ref_img, att_img, px_per_css=1.0):
    """Compare two same-size RGB images and return the full score report.

    px_per_css converts image pixels back to CSS pixels for the sentences in the
    report, since that is the unit the person or model will edit.
    """
    ref = np.asarray(ref_img.convert("RGB"), dtype=np.float64)
    att = np.asarray(att_img.convert("RGB").resize(ref_img.size, Image.LANCZOS), dtype=np.float64)

    d = ref - att
    per_px = (d ** 2).sum(axis=2) / 3.0          # the jelly-lab MSE, kept for continuity
    mse = float(per_px.mean())
    rmse = float(np.sqrt((d ** 2).mean()))

    g_ref, g_att = _gray(ref), _gray(att)
    ssim_map = _ssim(g_ref, g_att)

    ground = _ground_colour(ref)
    m_ref, m_att = _ink_mask(ref, ground), _ink_mask(att, ground)
    m_union = np.logical_or(m_ref, m_att)
    union = m_union.sum()
    inter = np.logical_and(m_ref, m_att).sum()
    iou = float(inter) / float(union) if union else 1.0
    coverage_ref = float(m_ref.mean())
    coverage_att = float(m_att.mean())

    # Coverage: the share of the design with something drawn within 6px of it.
    # Missing content caps the whole score. SSIM forgives flat regions and the
    # palette term forgives a colour that is barely present, so without this an
    # element that was never drawn could outscore one drawn three pixels off.
    att_near = _box_mean(m_att.astype(np.float64), 13) > 1e-9
    missed = np.logical_and(m_ref, ~att_near)
    ref_ink = float(m_ref.sum())
    coverage = 1.0 - float(missed.sum()) / ref_ink if ref_ink else 1.0
    missed_cells = _region_grid(missed.astype(np.float64))
    missed_where = _cell_name(max(missed_cells, key=lambda c: c["rmse"]))

    # Structure and colour are measured on the drawn content and its surroundings,
    # not on the empty page. Averaged over the whole canvas, an attempt that simply
    # leaves an element out scores better than one that draws it slightly wrong.
    near_ink = _box_mean(m_union.astype(np.float64), 9) > 0.02
    ssim = float(ssim_map[near_ink].mean()) if near_ink.sum() >= 64 else float(ssim_map.mean())

    ref_cols, att_cols = _top_colours(ref, m_ref), _top_colours(att, m_att)
    colour_dist = _palette_distance(ref_cols, att_cols)
    both = np.logical_and(m_ref, m_att)
    overlap_dist = (float(np.sqrt(((ref[both] - att[both]) ** 2).sum(axis=1)).mean())
                    if both.sum() >= 16 else colour_dist)

    def _mag(g):
        gx = np.abs(np.diff(g, axis=1, prepend=g[:, :1]))
        gy = np.abs(np.diff(g, axis=0, prepend=g[:1, :]))
        # Softened over a 9px window. This term is meant to answer "is there as much
        # detail here as there should be", not "is every edge in the exact right
        # place"; placement is what structure and shape already measure. Without the
        # blur, a three pixel offset reads as badly as missing detail entirely.
        return _box_mean(np.hypot(gx, gy), 9)

    sel = near_ink if near_ink.sum() >= 64 else np.ones_like(near_ink, dtype=bool)
    e_ref, e_att = _mag(g_ref)[sel], _mag(g_att)[sel]
    if e_ref.std() < 1e-6 or e_att.std() < 1e-6:
        edge_corr = 1.0 if e_ref.std() < 1e-6 and e_att.std() < 1e-6 else 0.0
    else:
        edge_corr = float(np.corrcoef(e_ref, e_att)[0, 1])

    structure = max(0.0, min(100.0, ssim * 100.0))
    shape = max(0.0, min(100.0, iou * 100.0))
    colour = max(0.0, min(100.0, 100.0 * float(np.exp(-colour_dist / 60.0))))
    detail = max(0.0, min(100.0, edge_corr * 100.0))
    weighted = 0.40 * structure + 0.25 * shape + 0.20 * colour + 0.15 * detail
    match = weighted * (0.6 + 0.4 * coverage)

    cells = _region_grid(per_px)
    worst = sorted(cells, key=lambda c: -c["rmse"])[:3]

    report = {
        "match": round(match, 1),
        "components": {
            "structure": round(structure, 1),
            "shape": round(shape, 1),
            "colour": round(colour, 1),
            "detail": round(detail, 1),
            "coverage": round(coverage * 100, 1),
        },
        "raw": {
            "mse": round(mse, 2),
            "rmse": round(rmse, 2),
            "ssim": round(ssim, 4),
            "shape_iou": round(iou, 4),
            "palette_distance": round(colour_dist, 2),
            "colour_distance_where_both_drew": round(overlap_dist, 2),
            "edge_correlation": round(edge_corr, 4),
            "ink_coverage_reference": round(coverage_ref * 100, 2),
            "ink_coverage_attempt": round(coverage_att * 100, 2),
            "design_not_drawn_pct": round((1 - coverage) * 100, 2),
            "design_not_drawn_where": missed_where,
        },
        "size": [int(ref.shape[1]), int(ref.shape[0])],
        "ground": ["#{:02X}{:02X}{:02X}".format(*[int(v) for v in ground])],
        "colours": {"reference": ref_cols, "attempt": att_cols},
        "regions": cells,
        "worst_regions": [{"where": _cell_name(c), "rmse": c["rmse"]} for c in worst],
    }
    off = _offsets(g_ref, g_att)
    if off["vertical"]:
        v = off["vertical"]
        v["from_css_y"] = int(round(v["from_y"] / px_per_css))
        v["css_shift"] = int(round(v["shift"] / px_per_css))
        v["where"] = _ROW_WORDS[min(3, v["from_y"] * 4 // max(1, ref.shape[0]))]
    if off["horizontal"]:
        off["horizontal"]["css_shift"] = int(round(off["horizontal"]["shift"] / px_per_css))
    report["offsets"] = off
    report["problems"] = _problems(report)
    return report, per_px, ground


def _problems(report):
    """Turn the numbers into the two or three sentences worth acting on."""
    out = []
    comp = report["components"]
    raw = report["raw"]
    ranked = sorted(((k, v) for k, v in comp.items() if k != "coverage"), key=lambda kv: kv[1])

    off = report.get("offsets") or {}
    if off.get("vertical"):
        v = off["vertical"]
        way = "higher" if v["css_shift"] > 0 else "lower"
        if v["from_y"] * 10 < report["size"][1]:
            out.append(
                "Nearly everything sits about {}px {} than in the design. Something at the very top "
                "is missing, extra or the wrong height: a label, a heading, padding or a margin. "
                "Fix that first: most of the error below follows from this one shift.".format(
                    abs(v["css_shift"]), way))
        else:
            out.append(
                "From about {}px down (the {} of the page), content sits about {}px {} than in the "
                "design. Something above that point is missing, extra or the wrong height. Fix that "
                "first: most of the error below it follows from this one shift.".format(
                    v["from_css_y"], v["where"], abs(v["css_shift"]), way))
    if off.get("horizontal"):
        hs = off["horizontal"]["css_shift"]
        out.append("Content sits about {}px further {} than in the design; check the container "
                   "width, side padding and centring.".format(abs(hs), "left" if hs > 0 else "right"))

    if comp.get("coverage", 100) < 97:
        if off.get("vertical") or off.get("horizontal"):
            tail = "Part of that is the shift; whatever is still uncovered after fixing it is missing."
        else:
            tail = "Something is missing there, not just misplaced."
        out.append("{:.0f}% of the design has nothing drawn near it, mostly in the {} of the page. "
                   "{}".format(raw["design_not_drawn_pct"], raw["design_not_drawn_where"], tail))

    wording = {
        "shape": "The silhouette is off: {:.0f}% of the drawn area overlaps the design. "
                 "The design covers {:.1f}% of the page, the attempt covers {:.1f}%. {}",
        "colour": "The palette is off by {:.0f} on a 0 to 441 scale: a design colour has "
                  "no close match in the attempt, or the attempt invented one.",
        "structure": "Local structure does not line up (SSIM {:.3f}). Edges and gradients sit in "
                     "the wrong places even where the colours are close.",
        "detail": "Detail density does not match (edge correlation {:.2f}). {}",
    }

    shifted = bool(off.get("vertical") or off.get("horizontal"))
    typeface = (comp["colour"] >= 90 and comp.get("coverage", 100) >= 90
                and comp["structure"] < 70 and not (shifted and abs(
                    (off.get("vertical") or {}).get("css_shift", 0)) > 20))
    if typeface:
        out.append("Colours and layout are close but edges do not line up. On a page that is "
                   "usually the text: check the font family first, then size, weight, line height "
                   "and letter spacing against the design, before moving any boxes.")

    for name, value in ranked[:2]:
        if typeface and name in ("structure", "detail"):
            continue
        if value >= 92:
            continue
        if name == "shape":
            cov_r, cov_a = raw["ink_coverage_reference"], raw["ink_coverage_attempt"]
            hint = ("The attempt draws too little." if cov_a < cov_r * 0.85 else
                    "The attempt draws too much." if cov_a > cov_r * 1.15 else
                    "Coverage is about right, so this is placement rather than size.")
            out.append(wording[name].format(comp[name], cov_r, cov_a, hint))
        elif name == "colour":
            out.append(wording[name].format(raw["palette_distance"]))
        elif name == "structure":
            out.append(wording[name].format(raw["ssim"]))
        else:
            hint = ("The attempt is smoother than the design; it is missing texture or edges."
                    if raw["edge_correlation"] < 0.5 else "Some strokes land in the wrong place.")
            out.append(wording[name].format(raw["edge_correlation"], hint))

    if not out:
        out.append("Close on every axis. What is left is antialiasing and sub-pixel placement, "
                   "which is not worth chasing.")

    if report["worst_regions"]:
        w = report["worst_regions"][0]
        out.append("The worst area is the {} of the page (RMSE {:.0f}).".format(w["where"], w["rmse"]))

    ref_cols = report["colours"]["reference"]
    att_cols = report["colours"]["attempt"]
    if ref_cols and att_cols:
        missing = []
        for rc in ref_cols[:4]:
            best = min(
                (sum((a - b) ** 2 for a, b in zip(rc["rgb"], ac["rgb"])) ** 0.5 for ac in att_cols),
                default=999.0,
            )
            if best > 70:
                missing.append("{} ({:.0f}% of the design)".format(rc["hex"], rc["share"]))
        if missing:
            out.append("Colours in the design with no close match in the attempt: "
                       + ", ".join(missing) + ".")
    return out


def diff_heatmap(per_px, out_png):
    """Red-orange heatmap, the same mapping the jelly lab used."""
    intensity = np.minimum(255.0, np.sqrt(per_px) * 3.0)
    h, w = intensity.shape
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:, :, 0] = intensity.astype(np.uint8)
    img[:, :, 1] = (intensity * 0.35).astype(np.uint8)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(out_png)


# ------------------------------------------------------------- feedback packet

def feedback_text(run, attempt, report):
    """The block a model can act on: what it built, how it scored, what to fix."""
    c = report["components"]
    lines = [
        "SPOT ON REPORT",
        "run: {}   attempt: {}   size: {}x{}   kind: {}".format(
            run.get("name", ""), attempt, run.get("width"), run.get("height"), run.get("kind")),
        "",
        "match score: {}/100".format(report["match"]),
        "  structure {:.1f}   shape {:.1f}   colour {:.1f}   detail {:.1f}   coverage {:.1f}".format(
            c["structure"], c["shape"], c["colour"], c["detail"], c.get("coverage", 100.0)),
        "  rmse {}   ssim {}   shape IoU {}".format(
            report["raw"]["rmse"], report["raw"]["ssim"], report["raw"]["shape_iou"]),
        "",
        "what to fix, in order:",
    ]
    for i, p in enumerate(report["problems"], 1):
        lines.append("  {}. {}".format(i, p))
    ref_cols = report["colours"]["reference"]
    if ref_cols:
        lines += ["", "dominant design colours: " + ", ".join(
            "{} {}%".format(c0["hex"], c0["share"]) for c0 in ref_cols)]
    lines += ["", "worst areas: " + ", ".join(
        "{} ({})".format(w["where"], w["rmse"]) for w in report["worst_regions"])]
    return "\n".join(lines)


ITERATE_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "description": "the full replacement source, no fences"},
        "changes": {"type": "string", "description": "one line on what was changed and why"},
    },
    "required": ["code", "changes"],
}


def iteration_base(history):
    """The attempt the next round builds on: the best so far, not the latest.

    A round that scores lower is a failed experiment. Building on it compounds the
    mistake, which is exactly what happens when a person keeps saying "closer".
    """
    best = max(history, key=lambda a: (a["match"], a["n"]))
    latest = history[-1]
    return best, (latest if latest["n"] != best["n"] else None)


def _iterate_prompt(run, n, code, report, extra, discarded=None):
    ref = "reference.png"
    att = "attempts/{:03d}.png".format(n)
    dif = "attempts/{:03d}-diff.png".format(n)
    parts = [
        "You are making {} code look exactly like a design.".format(run["kind"]),
        "",
        "Look at all three images before you change anything:",
        "  Read {} (the design)".format(ref),
        "  Read {} (your last attempt, rendered)".format(att),
        "  Read {} (the difference; bright red is where you missed)".format(dif),
        "",
        "The page is {}x{} CSS pixels on a {} ground.".format(
            run.get("css_width", run["width"]), run.get("css_height", run["height"]),
            run.get("ground", "#FFFFFF")),
        "",
        feedback_text(run, n, report),
        "",
    ]
    if discarded:
        parts += [
            "Your most recent attempt ({}) scored {:.1f}, below this one at {:.1f}, so it was "
            "discarded. Its change was: {}. Do not repeat it; try a different fix.".format(
                discarded["n"], discarded["match"], report["match"],
                discarded.get("changes") or "not recorded"),
            "",
        ]
    parts += [
        "The attempt to improve is:",
        "-----",
        code,
        "-----",
        "",
        "Return improved {} code for the same canvas.".format(run["kind"]),
        "Change the things the report names, keep what already scores well, and do not",
        "restructure working parts for their own sake. Return the complete source, not a patch.",
    ]
    if run["kind"] == "canvas":
        parts.append("The code is a function body with ctx, W and H already in scope.")
    if extra:
        parts += ["", "Extra instruction from the person running this: " + extra]
    return "\n".join(parts)


def run_iteration(slug, extra=""):
    """One round: ask headless Claude Code for a better attempt, render it, score it."""
    run = _load_run(slug)
    if run["kind"] == "url":
        # The code behind a running page lives in someone's repo, which this process
        # cannot and should not edit. That loop belongs to the session doing the work.
        raise ValueError("a running page is iterated by the session editing its code; "
                         "use the spot-on skill there")
    d = _run_dir(slug)
    history = _attempts(slug)
    if not history:
        raise ValueError("render a first attempt before iterating")
    base, discarded = iteration_base(history)
    n = base["n"]
    code = (d / "attempts" / "{:03d}.code".format(n)).read_text(encoding="utf-8")
    report = base["report"]

    prompt = _iterate_prompt(run, n, code, report, extra, discarded)
    cmd = ["claude", "-p", prompt, "--output-format", "json",
           "--model", os.environ.get("SPOT_ON_MODEL", "sonnet"),
           "--json-schema", json.dumps(ITERATE_SCHEMA)]
    # Explicit UTF-8: the Windows default codepage turns a model's arrows, quotes and
    # dashes into mojibake, in its notes and in the generated page alike.
    proc = subprocess.run(cmd, cwd=str(d), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600)
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError("claude exited {}: {}".format(proc.returncode, (proc.stderr or "")[-400:]))

    new_code, changes = _parse_iteration(proc.stdout)
    if not new_code:
        raise RuntimeError("no code came back from claude")
    if not _looks_like(new_code, run["kind"]):
        # Usage limits, refusals and errors all come back as ordinary prose. Rendering
        # that as if it were code silently poisons the run, so stop and show it instead.
        raise RuntimeError("claude did not return {} code. It said: {}".format(
            run["kind"], " ".join(new_code.split())[:240]))
    return record_attempt(slug, new_code, source="claude", changes=changes)


def _looks_like(code, kind):
    c = (code or "").strip()
    if not c:
        return False
    if kind == "svg":
        return "<svg" in c.lower()
    if kind == "canvas":
        return "ctx" in c and (";" in c or "\n" in c)
    if kind == "url":
        return bool(re.match(r"^https?://\S+$", c))
    return "<" in c and ">" in c


def _parse_iteration(stdout):
    """Pull the code out of headless output, whether structured or fenced."""
    try:
        payload = json.loads(stdout)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        so = payload.get("structured_output")
        if isinstance(so, dict) and so.get("code"):
            return so["code"], so.get("changes", "")
        if payload.get("is_error"):
            return (payload.get("result") or "claude reported an error").strip(), ""
        result = payload.get("result") or ""
    else:
        result = stdout
    try:
        obj = json.loads(result)
        if isinstance(obj, dict) and obj.get("code"):
            return obj["code"], obj.get("changes", "")
    except Exception:
        pass
    m = re.search(r"```(?:[a-zA-Z]+)?\n(.*?)```", result, re.S)
    if m:
        return m.group(1).strip(), ""
    return (result.strip(), "") if result.strip() else ("", "")


# ----------------------------------------------------------------- the attempt

def record_attempt(slug, code, source="manual", changes=""):
    """Render, score, store, and return the attempt record."""
    run = _load_run(slug)
    d = _run_dir(slug)
    ref_img = Image.open(d / "reference.png").convert("RGB")
    n = _next_n(slug)
    png = d / "attempts" / "{:03d}.png".format(n)

    t0 = time.time()
    att_img = render_code(code, run["kind"],
                          run.get("css_width", run["width"]), run.get("css_height", run["height"]),
                          png, out_size=(run["width"], run["height"]),
                          scale=run.get("scale", 1.0), ground=run.get("ground", "#FFFFFF"))
    px_per_css = run["width"] / float(run.get("css_width", run["width"]))
    report, per_px, _ = score_images(ref_img, att_img, px_per_css=px_per_css)
    diff_heatmap(per_px, d / "attempts" / "{:03d}-diff.png".format(n))
    (d / "attempts" / "{:03d}.code".format(n)).write_text(code, encoding="utf-8")

    record = {
        "n": n,
        "created": time.time(),
        "source": source,
        "changes": changes,
        "match": report["match"],
        "render_seconds": round(time.time() - t0, 2),
        "report": report,
    }
    (d / "attempts" / "{:03d}.json".format(n)).write_text(
        json.dumps(record, indent=2), encoding="utf-8")

    if report["match"] > run.get("best_match", -1):
        run["best_match"] = report["match"]
        run["best_attempt"] = n
        _save_run(slug, run)
    return record


def load_design(reference_bytes=None, reference_path=None):
    if reference_bytes is not None:
        import io
        img = Image.open(io.BytesIO(reference_bytes))
    elif reference_path:
        img = Image.open(reference_path)
    else:
        raise ValueError("a design image is required")
    if img.mode in ("RGBA", "LA", "P"):
        flat = Image.new("RGB", img.size, "#FFFFFF")
        rgba = img.convert("RGBA")
        flat.paste(rgba, mask=rgba.split()[3])
        return flat
    return img.convert("RGB")


def page_geometry(img, scale):
    """CSS page size the design was captured at, and the size scoring happens at."""
    scale = float(scale) if float(scale) in SCALES else 1.0
    css_w, css_h = max(1, round(img.width / scale)), max(1, round(img.height / scale))
    w, h = img.width, img.height
    if w * h > MAX_PIXELS:
        f = (MAX_PIXELS / float(w * h)) ** 0.5
        w, h = max(1, int(w * f)), max(1, int(h * f))
    return scale, css_w, css_h, w, h


def create_run(name, kind, reference_bytes=None, reference_path=None, scale=1.0):
    slug = _slugify(name)
    d = _run_dir(slug)
    (d / "attempts").mkdir(parents=True, exist_ok=True)

    img = load_design(reference_bytes, reference_path)
    scale, css_w, css_h, w, h = page_geometry(img, scale)
    if (w, h) != img.size:
        img = img.resize((w, h), Image.LANCZOS)
    img.save(d / "reference.png")

    ground = _ground_colour(np.asarray(img, dtype=np.float64))
    run = {
        "slug": slug,
        "name": name,
        "kind": kind if kind in KINDS else "url",
        "width": w,
        "height": h,
        "css_width": css_w,
        "css_height": css_h,
        "scale": scale,
        "ground": "#{:02X}{:02X}{:02X}".format(*[int(v) for v in ground]),
        "created": time.time(),
        "best_match": -1,
        "best_attempt": None,
    }
    _save_run(slug, run)
    return run


STARTERS = {
    "url": "http://localhost:5173/",
    "svg": '<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
           'viewBox="0 0 {w} {h}">\n  <rect width="{w}" height="{h}" fill="{g}"/>\n'
           '  <circle cx="{cx}" cy="{cy}" r="{r}" fill="#52796F"/>\n</svg>',
    "canvas": 'ctx.fillStyle = "{g}";\nctx.fillRect(0, 0, W, H);\n'
              'ctx.fillStyle = "#52796F";\nctx.beginPath();\n'
              'ctx.arc(W / 2, H / 2, Math.min(W, H) * 0.3, 0, Math.PI * 2);\nctx.fill();',
    "html": '<div style="width:{w}px;height:{h}px;background:{g};display:flex;'
            'align-items:center;justify-content:center">\n'
            '  <div style="width:{r}px;height:{r}px;border-radius:50%;background:#52796F"></div>\n'
            '</div>',
}


def starter_code(run):
    w, h = run.get("css_width", run["width"]), run.get("css_height", run["height"])
    return STARTERS[run["kind"]].format(
        w=w, h=h, g=run.get("ground", "#FFFFFF"),
        cx=w // 2, cy=h // 2, r=int(min(w, h) * 0.3))


PAGE_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Spot On</title>
<style>
  :root {
    --ink: #22372B;
    --muted: #7C827E;
    --faint: #A9AFAB;
    --accent: #52796F;
    --accent-hover: #456A61;
    --fill: #EDF4EF;
    --fill-hover: #E0EBE3;
    --border: #E7E9E5;
    --deep: #4C635A;
    color-scheme: light;
  }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  body {
    margin: 0;
    background-color: #FDFDFC;
    background-image:
      radial-gradient(720px 460px at 8% 2%, rgba(228,212,175,0.16), transparent 70%),
      radial-gradient(820px 520px at 96% 14%, rgba(132,169,140,0.11), transparent 70%),
      radial-gradient(900px 560px at 40% 98%, rgba(82,121,111,0.05), transparent 72%);
    background-attachment: fixed;
    -webkit-font-smoothing: antialiased;
    color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Inter', 'Segoe UI Variable', system-ui, sans-serif;
    font-size: 13px;
    line-height: 1.5;
    letter-spacing: -0.01em;
  }
  a { color: var(--accent); text-decoration: none; }
  input, textarea, button, select { font-family: inherit; letter-spacing: inherit; }
  input::placeholder, textarea::placeholder { color: var(--faint); }
  input:focus, textarea:focus, select:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(82,121,111,0.12); }
  button { cursor: pointer; }

  @keyframes mlBreathe { 0%, 100% { opacity: .45; } 50% { opacity: 1; } }
  @keyframes mlSpin { to { transform: rotate(360deg); } }
  @keyframes mlRise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
  *::-webkit-scrollbar { width: 10px; height: 10px; }
  *::-webkit-scrollbar-thumb { background: rgba(70,80,72,.18); border-radius: 99px; border: 3px solid transparent; background-clip: content-box; }

  input[type="range"] { -webkit-appearance: none; appearance: none; height: 6px; border-radius: 999px; margin: 0; cursor: pointer; background: rgba(120,120,128,0.16); }
  input[type="range"]::-webkit-slider-runnable-track { height: 6px; background: transparent; }
  input[type="range"]::-webkit-slider-thumb { -webkit-appearance: none; appearance: none; width: 16px; height: 16px; border-radius: 50%; background: #FFFFFF; border: none; box-shadow: 0 2px 5px rgb(0 0 0/0.25), 0 0.5px 1px rgb(0 0 0/0.12); margin-top: -5px; }

  .rule16 { height: 1px; background: linear-gradient(90deg, transparent, rgba(70,80,72,0.16) 3%, rgba(70,80,72,0.16) 97%, transparent); }
  .rule13 { height: 1px; background: linear-gradient(90deg, transparent, rgba(70,80,72,0.13) 2%, rgba(70,80,72,0.13) 98%, transparent); }
  .rule20 { height: 1px; background: linear-gradient(90deg, transparent, rgba(70,80,72,0.20) 5%, rgba(70,80,72,0.20) 95%, transparent); }
  .rule22 { height: 1px; background: linear-gradient(90deg, transparent, rgba(70,80,72,0.22) 8%, rgba(70,80,72,0.22) 88%, transparent); }
  .vrule { width: 1px; background: linear-gradient(180deg, transparent, rgba(70,80,72,0.11) 8%, rgba(70,80,72,0.11) 92%, transparent); }

  .wash { position: absolute; z-index: -1; top: 0; bottom: 0; left: -32px; right: -30px; pointer-events: none; }
  .wash-cream {
    background: radial-gradient(120% 150% at 50% 0%, rgba(228,212,175,0.12), rgba(228,212,175,0.045) 52%, rgba(228,212,175,0) 88%);
    -webkit-mask-image: linear-gradient(180deg, #000 0, #000 58%, transparent 99%), linear-gradient(90deg, transparent 0, #000 6%, #000 94%, transparent 100%);
    -webkit-mask-composite: source-in;
    mask-image: linear-gradient(180deg, #000 0, #000 58%, transparent 99%), linear-gradient(90deg, transparent 0, #000 6%, #000 94%, transparent 100%);
    mask-composite: intersect;
  }
  .wash-sage {
    background: radial-gradient(120% 150% at 50% 0%, rgba(132,169,140,0.20), rgba(132,169,140,0.08) 52%, rgba(132,169,140,0) 88%);
    -webkit-mask-image: linear-gradient(180deg, #000 0, #000 58%, transparent 99%), linear-gradient(90deg, transparent 0, #000 14%, #000 86%, transparent 100%);
    -webkit-mask-composite: source-in;
    mask-image: linear-gradient(180deg, #000 0, #000 58%, transparent 99%), linear-gradient(90deg, transparent 0, #000 14%, #000 86%, transparent 100%);
    mask-composite: intersect;
  }

  .sec-title { margin: 0; font-family: 'SF Pro Rounded', -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Inter', system-ui, sans-serif; font-size: 18px; font-weight: 600; letter-spacing: -0.015em; }
  .sec-tag { font-size: 13px; color: var(--muted); margin-top: 2px; }
  .mono { font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace; }
  .hint { color: var(--muted); font-size: 11px; margin-top: 6px; }

  .btn-primary { background: var(--accent); color: #FFFFFF; border: none; border-radius: 999px; padding: 0 16px; font-size: 13px; font-weight: 600; height: 32px; display: inline-flex; align-items: center; gap: 8px; box-shadow: 0 1px 2px rgb(31 37 33/0.14), 0 6px 16px rgba(82,121,111,0.22); }
  .btn-primary:hover { background: var(--accent-hover); }
  .btn-primary:disabled { background: var(--fill); color: var(--faint); box-shadow: none; cursor: default; }
  .btn-fill { background: var(--fill); border: none; color: var(--ink); font-size: 13px; font-weight: 500; padding: 0 14px; height: 32px; white-space: nowrap; border-radius: 999px; box-shadow: 0 1px 2px rgb(16 24 40/0.05); }
  .btn-fill:hover { background: var(--fill-hover); }
  .btn-fill.small { height: 28px; font-size: 12px; padding: 0 12px; }
  .btn-fill.row-btn { height: 26px; padding: 0 13px; font-size: 12px; }
  .btn-fill:disabled { color: var(--faint); cursor: default; }
  .btn-ghost { background: transparent; border: none; color: var(--deep); font-size: 13px; font-weight: 500; padding: 0 8px; height: 32px; border-radius: 999px; }
  .btn-ghost:hover { background: var(--fill); }
  .btn-danger { background: transparent; border: none; color: #FF3B30; font-size: 12px; font-weight: 500; padding: 0 6px; height: 26px; border-radius: 999px; }
  .btn-danger:hover { background: rgba(255,59,48,0.08); }

  .meta-pill { font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace; font-size: 12px; font-weight: 500; background: var(--fill); border-radius: 999px; padding: 2px 8px; white-space: nowrap; cursor: help; color: var(--deep); font-variant-numeric: tabular-nums; }
  .chip { height: 24px; padding: 0 11px; border-radius: 999px; border: none; font-size: 12px; font-weight: 500; background: var(--fill); color: var(--muted); }
  .chip.active { background: rgba(82,121,111,0.14); color: var(--accent); }
  .dark-chip { height: 22px; padding: 0 10px; border-radius: 999px; border: 1px solid rgba(255,255,255,0.16); background: rgba(255,255,255,0.08); color: #E2EAE3; font-size: 11px; font-weight: 500; }
  .dark-chip.active { background: rgba(237,244,239,0.20); border-color: rgba(237,244,239,0.46); color: #FFFFFF; }

  .guide-step { display: flex; align-items: baseline; gap: 9px; }
  .guide-num { font-family: ui-monospace, Menlo, monospace; font-size: 11px; color: var(--faint); }
  .guide-text { font-size: 12px; color: var(--muted); }

  .track-btn { background: transparent; border: none; padding: 0; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 9.5px; letter-spacing: 0.16em; text-transform: uppercase; color: var(--muted); font-weight: 400; }
  .track-btn.current { color: var(--accent); font-weight: 600; }
  .track-btn:disabled { color: #CFD4D0; cursor: default; }
  .track-link { width: 24px; height: 1px; background: rgba(70,80,72,0.18); margin: 0 10px; }

  .code-area { width: 100%; background: #FFFFFF; border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace; font-size: 12px; line-height: 1.65; color: var(--ink); resize: vertical; tab-size: 2; }
  .text-input { color: var(--ink); background: #FFFFFF; border: 1px solid var(--border); border-radius: 10px; height: 32px; padding: 0 12px; font-size: 13px; }

  .score-big { font-family: 'SF Pro Rounded', -apple-system, BlinkMacSystemFont, system-ui, sans-serif; font-size: 52px; font-weight: 600; letter-spacing: -0.03em; line-height: 1; font-variant-numeric: tabular-nums; background: linear-gradient(96deg, #22372B, #52796F); -webkit-background-clip: text; background-clip: text; color: transparent; -webkit-text-fill-color: transparent; }
  .bar-track { height: 6px; border-radius: 999px; background: rgba(120,120,128,0.16); overflow: hidden; }
  .bar-fill { height: 6px; border-radius: 999px; background: var(--accent); }
  .comp-row { display: grid; grid-template-columns: 96px 1fr 52px; gap: 12px; align-items: center; min-height: 30px; }
  .comp-name { font-size: 13px; font-weight: 500; }
  .comp-val { text-align: right; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12.5px; font-weight: 500; color: var(--deep); font-variant-numeric: tabular-nums; }

  .region-cell { aspect-ratio: 1; border-radius: 3px; cursor: help; }
  .problem { display: flex; align-items: baseline; gap: 10px; padding: 7px 0; font-size: 13px; color: var(--deep); line-height: 1.6; }
  .problem .n { font-family: ui-monospace, Menlo, monospace; font-size: 11px; color: var(--faint); min-width: 14px; }

  .att-row { display: flex; align-items: center; gap: 12px; min-height: 50px; padding-left: 10px; }
  .att-row.current { background: linear-gradient(90deg, rgba(82,121,111,0.13), rgba(82,121,111,0.05) 34%, rgba(82,121,111,0) 68%); }
  .att-n { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; color: var(--muted); min-width: 30px; font-variant-numeric: tabular-nums; }
  .att-score { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 15px; font-weight: 600; color: var(--ink); min-width: 52px; font-variant-numeric: tabular-nums; }
  .att-delta { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 11.5px; min-width: 52px; font-variant-numeric: tabular-nums; }
  .att-delta.up { color: var(--accent); }
  .att-delta.down { color: #C2703F; }
  .att-note { flex: 1; min-width: 0; font-size: 12.5px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .best-badge { height: 22px; padding: 0 9px; border-radius: 999px; background: rgba(82,121,111,0.14); color: var(--accent); font-family: ui-monospace, Menlo, monospace; font-size: 9.5px; font-weight: 600; letter-spacing: 0.14em; text-transform: uppercase; display: inline-flex; align-items: center; }

  .run-row { display: flex; align-items: center; gap: 12px; min-height: 44px; border-bottom: 1px solid rgba(70,80,72,0.08); }
  .drop-zone { border: 1px dashed rgba(70,80,72,0.22); border-radius: 12px; padding: 26px 20px; text-align: center; background: rgba(255,255,255,0.5); }
  .drop-zone.over { border-color: var(--accent); background: rgba(82,121,111,0.06); }
</style>
</head>
<body>
<div id="sheet" style="width: 1440px; max-width: 100%; min-width: 1240px; margin: 0 auto; min-height: 960px; position: relative; padding: 30px 52px 24px 78px;">

  <div aria-hidden="true" style="position: fixed; inset: 0; pointer-events: none; z-index: 30; opacity: 0.055; background-image: url('data:image/svg+xml,%3Csvg%20xmlns=%22http://www.w3.org/2000/svg%22%20width=%22160%22%20height=%22160%22%3E%3Cfilter%20id=%22g%22%3E%3CfeTurbulence%20type=%22fractalNoise%22%20baseFrequency=%220.82%22%20numOctaves=%223%22%20stitchTiles=%22stitch%22/%3E%3C/filter%3E%3Crect%20width=%22160%22%20height=%22160%22%20filter=%22url(%23g)%22/%3E%3C/svg%3E'); background-repeat: repeat; background-size: 160px 160px;"></div>

  <div aria-hidden="true" style="position: absolute; left: 0; top: 0; bottom: 0; width: 64px; pointer-events: none;">
    <div style="position: absolute; left: 0; top: 0; bottom: 0; width: 64px; background: linear-gradient(90deg, rgba(70,80,72,0.022) 0, rgba(70,80,72,0.046) 11px, rgba(70,80,72,0.022) 31px, rgba(70,80,72,0.008) 49px, rgba(70,80,72,0) 64px);"></div>
    <div style="position: absolute; left: 46px; top: 26px; bottom: 26px; width: 1px; background: linear-gradient(180deg, transparent, rgba(70,80,72,0.14) 8%, rgba(70,80,72,0.14) 92%, transparent);"></div>
    <div style="position: absolute; left: 0; right: 0; top: 34px; bottom: 34px; background-image: radial-gradient(circle at 21px calc(50% - 0.2px), #F0EEE8 0 4.1px, transparent 4.4px), radial-gradient(circle at 21px calc(50% - 1.2px), rgba(52,62,54,0.30) 0 4.7px, transparent 5.1px), radial-gradient(circle at 21px calc(50% + 1.4px), rgba(255,255,255,0.95) 0 5.05px, transparent 5.5px); background-size: 64px 70px; background-repeat: repeat-y;"></div>
  </div>

  <div aria-hidden="true" style="position: absolute; right: 0; top: 0; bottom: 0; width: 96px; pointer-events: none; background: linear-gradient(270deg, rgba(70,80,72,0) 0, rgba(70,80,72,0.03) 18px, rgba(70,80,72,0.015) 46px, rgba(70,80,72,0.005) 72px, rgba(70,80,72,0) 96px);"></div>
  <div aria-hidden="true" style="position: absolute; inset: 0; pointer-events: none; background-image: linear-gradient(180deg, rgba(70,80,72,0) 0, rgba(70,80,72,0.024) 16px, rgba(70,80,72,0.008) 64px, rgba(70,80,72,0) 130px), linear-gradient(0deg, rgba(70,80,72,0) 0, rgba(70,80,72,0.024) 16px, rgba(70,80,72,0.008) 64px, rgba(70,80,72,0) 130px);"></div>

  <nav style="display: flex; align-items: center; height: 20px;">
    <div style="display: flex; align-items: center; gap: 12px;">
      <button type="button" id="prev-att" class="track-btn" title="Show the previous attempt">prev</button>
      <span id="att-pos" class="mono" style="font-size: 9.5px; letter-spacing: 0.16em; text-transform: uppercase; color: var(--faint);">no attempts</span>
      <button type="button" id="next-att" class="track-btn" title="Show the next attempt">next</button>
    </div>
    <div id="track-nav" style="display: flex; align-items: center; justify-content: flex-end; height: 20px; margin-left: auto;"></div>
  </nav>
  <div style="height: 1px; margin-top: 9px; background: linear-gradient(90deg, rgba(70,80,72,0.16), rgba(70,80,72,0.16) 96%, transparent);"></div>

  <header style="position: relative; display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; padding: 26px 26px 18px; margin: 0 -26px;">
    <div class="wash wash-sage" aria-hidden="true"></div>
    <div>
      <h1 style="margin: 0; font-family: 'SF Pro Rounded', -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Inter', system-ui, sans-serif; font-size: 40px; font-weight: 600; line-height: 1.1; letter-spacing: -0.02em; background: linear-gradient(96deg, #22372B, #52796F); -webkit-background-clip: text; background-clip: text; color: transparent; -webkit-text-fill-color: transparent;">Spot On</h1>
      <p style="margin: 5px 0 0; font-size: 15px; color: var(--muted);">Pixel-perfect pages, measured.</p>
      <p style="margin: 2px 0 0; font-size: 12px; color: var(--faint);">Stop playing spot the difference with your AI.</p>
    </div>
    <div style="display: flex; align-items: center; gap: 10px; padding-top: 6px;">
      <span style="height: 28px; border-radius: 999px; border: 1px solid var(--border); background: #FFFFFF; padding: 0 12px; display: inline-flex; align-items: center; gap: 7px; box-shadow: 0 1px 2px rgb(16 24 40/0.05);">
        <span style="width: 5px; height: 5px; border-radius: 50%; background: var(--accent);"></span>
        <span id="active-chip" style="font-size: 13px; font-weight: 500;">no design</span>
      </span>
      <span style="height: 28px; border-radius: 999px; background: rgba(255,255,255,0.62); border: 1px solid rgba(231,233,229,0.9); padding: 0 11px 0 9px; display: inline-flex; align-items: center; gap: 6px;">
        <span style="width: 6px; height: 6px; border-radius: 50%; background: var(--accent); animation: mlBreathe 3.4s ease-in-out infinite;"></span>
        <span class="mono" style="font-size: 11px; color: var(--muted);">Chrome</span>
      </span>
    </div>
  </header>
  <div class="rule16" style="animation: mlBreathe 6s ease-in-out infinite;"></div>

  <section style="margin-top: 6px;">

    <div id="sec-ref" style="position: relative; padding: 33px 26px 30px; margin: -7px -26px 0;">
      <div class="wash wash-cream" aria-hidden="true"></div>
      <div style="display: flex; align-items: flex-end; justify-content: space-between; padding: 0 0 12px;">
        <div>
          <h2 class="sec-title">Design</h2>
          <div class="sec-tag">The mockup or screenshot the page should match. Everything is scored against this.</div>
        </div>
        <div id="ref-meta" style="display: flex; align-items: center; gap: 7px;"></div>
      </div>
      <div class="rule16" style="margin-bottom: 16px;"></div>

      <div style="display: grid; grid-template-columns: 300px 1px minmax(0,1fr); gap: 0 26px; align-items: start;">
        <div>
          <div id="ref-shell" style="width: 300px; height: 200px; border-radius: 10px; border: 1px solid var(--border); background: #FFFFFF; display: flex; align-items: center; justify-content: center; overflow: hidden;">
            <img id="ref-img" alt="" hidden style="max-width: 100%; max-height: 100%; display: block;">
            <span id="ref-empty" class="mono" style="font-size: 11px; color: var(--faint);">no design loaded</span>
          </div>
          <div style="display: flex; align-items: center; gap: 8px; margin-top: 10px;">
            <button type="button" id="pick-file" class="btn-fill small">Choose image</button>
            <input type="file" id="file-input" accept="image/*" hidden>
            <button type="button" id="new-run" class="btn-ghost" style="height: 28px; font-size: 12px;">New run</button>
          </div>
        </div>

        <div class="vrule" style="height: 100%;"></div>

        <div style="min-width: 0;">
          <div id="new-panel">
            <div id="drop" class="drop-zone">
              <div style="font-size: 13px; color: var(--deep);">Drop the design here: a Figma export or a screenshot.</div>
              <div class="hint" style="margin-top: 5px;">PNG, JPG, WEBP. Full-page screenshots are fine; very large ones are scaled down for scoring only.</div>
            </div>
            <div style="display: flex; align-items: center; gap: 10px; margin-top: 14px;">
              <input type="text" id="run-name" class="text-input" placeholder="Name this run, for example pricing page" style="flex: 1; min-width: 0;">
              <select id="run-kind" class="text-input" style="width: 150px;" title="What gets scored. A running page is the usual case: point it at your dev server. Pasted HTML suits a single component; SVG and canvas suit icons and illustrations.">
                <option value="url">Running page</option>
                <option value="html">Pasted HTML</option>
                <option value="svg">SVG</option>
                <option value="canvas">Canvas 2D</option>
              </select>
              <select id="run-scale" class="text-input" style="width: 126px;" title="The display scale the design screenshot was taken at. A screenshot from a 150% Windows display or a Retina Mac is bigger than the page it shows; pick the matching scale so the page is rendered at its real width.">
                <option value="1">1x capture</option>
                <option value="1.25">1.25x capture</option>
                <option value="1.5">1.5x capture</option>
                <option value="2">2x capture</option>
              </select>
              <button type="button" id="create-run" class="btn-primary" disabled>Start run</button>
            </div>
            <div id="guide" style="padding: 20px 0 4px; display: flex; flex-direction: column; gap: 7px;">
              <div class="guide-step"><span class="guide-num">01</span><span class="guide-text">Drop in the design and name the run.</span></div>
              <div class="guide-step"><span class="guide-num">02</span><span class="guide-text">Point it at the running page, or paste HTML, and score it.</span></div>
              <div class="guide-step"><span class="guide-num">03</span><span class="guide-text">Hand the report to the model and watch the score climb instead of guessing.</span></div>
            </div>
          </div>

          <div id="runs-panel" hidden>
            <div style="font-size: 12px; color: var(--muted); padding-bottom: 6px;">Saved runs. Opening one restores its design, its history and its best attempt.</div>
            <div class="rule13"></div>
            <div id="runs-list"></div>
          </div>
        </div>
      </div>
    </div>

    <div class="rule20"></div>

    <div style="position: relative; display: grid; grid-template-columns: minmax(0,1fr) 1px 560px; align-items: stretch;">

      <div id="sec-attempt" style="grid-column: 1; grid-row: 1; padding: 26px 34px 28px 0; min-width: 0; position: relative;">
        <div class="wash wash-cream" aria-hidden="true" style="left: -32px; right: -10px;"></div>
        <div style="display: flex; align-items: flex-end; justify-content: space-between; padding: 0 0 12px;">
          <div>
            <h2 class="sec-title">Attempt</h2>
            <div class="sec-tag">The page the model built, screenshot at the design's size.</div>
          </div>
          <span id="attempt-meta" class="mono" style="font-size: 11px; color: var(--muted);"></span>
        </div>
        <div class="rule16" style="margin-bottom: 12px;"></div>
        <div id="url-wrap" hidden>
          <input type="text" id="url-input" class="text-input mono" spellcheck="false" placeholder="http://localhost:5173/pricing" style="width: 100%; height: 38px; font-size: 13px;">
          <div class="hint">Your dev server, at the page that should match the design. Change the code in your editor, then screenshot again; hot reload keeps it to one click.</div>
        </div>
        <textarea id="code" class="code-area" rows="16" spellcheck="false" placeholder="Paste the page's HTML, or SVG or canvas code."></textarea>
        <div class="hint">Screenshot by headless Chrome at the design's page size and display scale, so the score reflects the page and nothing else.</div>

        <div style="display: flex; align-items: center; gap: 10px; margin-top: 14px; flex-wrap: wrap;">
          <button type="button" id="render" class="btn-primary" disabled><span id="render-spin" hidden style="width: 11px; height: 11px; border-radius: 50%; border: 1.5px solid rgba(255,255,255,0.4); border-top-color: #FFFFFF; animation: mlSpin .7s linear infinite;"></span><span id="render-label">Screenshot and score</span></button>
          <button type="button" id="starter" class="btn-fill" disabled>Insert starter</button>
          <span id="status-line" style="margin-left: 4px; font-size: 12px; color: var(--muted);" aria-live="polite">Drop in a design to begin.</span>
        </div>

        <div class="rule22" style="margin-top: 20px;"></div>
        <div id="url-loop-note" hidden style="padding-top: 14px; max-width: 580px;">
          <div style="font-size: 13px; font-weight: 500;">Letting the model iterate</div>
          <div style="font-size: 12px; color: var(--muted); margin-top: 2px;">A running page's code lives in your repo, so the loop runs in the session that edits it. Ask that session to use <span class="mono">/spot-on</span>: it scores the page after each change, reads the difference map, and its attempts land in this history.</div>
        </div>
        <div id="iterate-wrap" style="padding-top: 14px;">
          <div style="font-size: 13px; font-weight: 500;">Let Claude iterate</div>
          <div style="font-size: 12px; color: var(--muted); margin-top: 2px; max-width: 560px;">Each round shows the model the design, its own render and the difference map, hands it the report below, and scores whatever comes back.</div>
          <div style="display: flex; align-items: center; gap: 10px; margin-top: 12px; flex-wrap: wrap;">
            <button type="button" id="iterate" class="btn-fill" disabled><span id="iter-spin" hidden style="width: 11px; height: 11px; border-radius: 50%; border: 1.5px solid rgba(82,121,111,0.3); border-top-color: var(--accent); animation: mlSpin .7s linear infinite; display: inline-block; margin-right: 7px; vertical-align: -1px;"></span><span id="iter-label">Run 3 rounds</span></button>
            <select id="rounds" class="text-input" style="width: 104px; height: 32px;">
              <option value="1">1 round</option>
              <option value="3" selected>3 rounds</option>
              <option value="5">5 rounds</option>
            </select>
            <input type="text" id="iter-note" class="text-input" placeholder="Optional steer, for example the font is Inter, keep the card colours" style="flex: 1; min-width: 220px;">
            <button type="button" id="iter-stop" class="btn-ghost" hidden style="height: 32px;">Stop</button>
          </div>
        </div>
      </div>

      <div class="vrule" style="grid-column: 2; grid-row: 1 / 3;"></div>

      <div id="sec-compare" style="grid-column: 3; grid-row: 1 / 3; display: flex; flex-direction: column; padding: 11px 18px 18px; margin: 14px 0 18px 30px; min-width: 0; background: linear-gradient(180deg, #52796F 0%, #3E5D53 40%, #2A4237 74%, #1F3029 100%); border-radius: 14px; color: #D6E1D8; -webkit-mask-image: radial-gradient(126% 118% at 50% 42%, #000 62%, rgba(0,0,0,0.55) 84%, rgba(0,0,0,0) 100%); mask-image: radial-gradient(126% 118% at 50% 42%, #000 62%, rgba(0,0,0,0.55) 84%, rgba(0,0,0,0) 100%);">
        <div style="display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 0 0 12px;">
          <div style="flex: 1; min-width: 0;">
            <h2 class="sec-title" style="color: #EDF2ED;">Comparison</h2>
            <div style="font-size: 13px; color: #E2EAE3; margin-top: 2px;">Where the page and the design disagree.</div>
          </div>
          <span class="mono" style="font-size: 10px; color: #DCEBE0; letter-spacing: 0.16em; text-transform: uppercase;">live</span>
        </div>
        <div style="height: 1px; background: linear-gradient(90deg, rgba(255,255,255,0.13), rgba(255,255,255,0.13) 94%, transparent); margin-bottom: 12px;"></div>

        <div style="display: flex; align-items: center; gap: 7px; margin-bottom: 11px; flex-wrap: wrap;">
          <button type="button" class="dark-chip" data-view="reference">design</button>
          <button type="button" class="dark-chip active" data-view="attempt">attempt</button>
          <button type="button" class="dark-chip" data-view="difference">difference</button>
          <button type="button" class="dark-chip" data-view="overlay">overlay</button>
        </div>

        <div id="stage" style="position: relative; flex: 1; min-height: 340px; border-radius: 8px; overflow: hidden; background: rgba(10,18,13,0.30); display: flex; align-items: center; justify-content: center;">
          <img id="stage-base" alt="" hidden style="position: absolute; inset: 8px; width: calc(100% - 16px); height: calc(100% - 16px); object-fit: contain;">
          <img id="stage-over" alt="" hidden style="position: absolute; inset: 8px; width: calc(100% - 16px); height: calc(100% - 16px); object-fit: contain;">
          <span id="stage-empty" class="mono" style="font-size: 11px; color: #A9AFAB;">Nothing rendered yet.</span>
        </div>

        <div id="onion-row" hidden style="display: flex; align-items: center; gap: 10px; margin-top: 11px;">
          <span class="mono" style="font-size: 10px; color: #DCEBE0; letter-spacing: 0.14em; text-transform: uppercase;">design</span>
          <input type="range" id="onion" min="0" max="100" value="50" style="flex: 1; min-width: 0;">
          <span class="mono" style="font-size: 10px; color: #DCEBE0; letter-spacing: 0.14em; text-transform: uppercase;">page</span>
        </div>
        <div style="color: #E2EAE3; font-size: 11px; margin-top: 9px;">In the difference view, bright red is a big miss and black is an exact match. Antialiasing always leaves a faint outline.</div>
      </div>

      <div id="sec-score" style="grid-column: 1; grid-row: 2; padding: 26px 34px 30px 0; min-width: 0; position: relative;">
        <div class="wash wash-cream" aria-hidden="true" style="left: -32px; right: -10px;"></div>
        <div style="display: flex; align-items: flex-end; justify-content: space-between; padding: 0 0 12px;">
          <div>
            <h2 class="sec-title">Score</h2>
            <div class="sec-tag">One number to chase, five to explain it.</div>
          </div>
          <span id="score-raw" class="mono" style="font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums;"></span>
        </div>
        <div class="rule16" style="margin-bottom: 16px;"></div>

        <div id="score-empty" style="padding: 16px 0 6px; font-size: 13px; color: var(--muted);">Render an attempt and the score appears here.</div>

        <div id="score-body" hidden>
          <div style="display: grid; grid-template-columns: 150px 1px minmax(0,1fr) 1px 150px; gap: 0 22px; align-items: center;">
            <div>
              <div id="score-num" class="score-big">0</div>
              <div class="mono" style="font-size: 10px; color: var(--muted); letter-spacing: 0.16em; text-transform: uppercase; margin-top: 6px;">match / 100</div>
              <div id="score-delta" class="mono" style="font-size: 12px; margin-top: 6px; font-variant-numeric: tabular-nums;"></div>
            </div>
            <div class="vrule" style="height: 108px;"></div>
            <div id="components" style="min-width: 0;"></div>
            <div class="vrule" style="height: 108px;"></div>
            <div>
              <div id="region-grid" style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 3px; width: 120px;"></div>
              <div class="mono" style="font-size: 10px; color: var(--muted); letter-spacing: 0.14em; text-transform: uppercase; margin-top: 8px;">where the miss is</div>
            </div>
          </div>

          <div class="rule13" style="margin: 20px 0 8px;"></div>
          <div id="problems"></div>
          <div id="palette-row" style="display: flex; align-items: center; gap: 14px; margin-top: 12px; flex-wrap: wrap;"></div>
        </div>
      </div>
    </div>

    <div class="rule20"></div>

    <div id="sec-history" style="position: relative; padding: 30px 26px 26px; margin: 0 -26px;">
      <div class="wash wash-cream" aria-hidden="true"></div>
      <div style="display: flex; align-items: flex-end; justify-content: space-between; padding: 0 0 12px;">
        <div>
          <h2 class="sec-title">History</h2>
          <div class="sec-tag">Every attempt, kept. Open one to put its code back in the editor.</div>
        </div>
        <canvas id="spark" width="360" height="44" style="width: 360px; height: 44px;"></canvas>
      </div>
      <div class="rule16"></div>
      <div id="history-list"></div>
      <div id="history-empty" style="padding: 20px 0 6px; font-size: 13px; color: var(--muted);">No attempts yet. The first render starts the history.</div>
    </div>
  </section>

  <section id="sec-packet" style="position: relative; margin-top: 38px; padding: 0 0 20px;">
    <div class="wash wash-cream" aria-hidden="true"></div>
    <div class="rule16"></div>
    <div style="height: 26px;"></div>
    <div style="display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; padding: 0 0 12px;">
      <div>
        <h2 class="sec-title">Feedback packet</h2>
        <div class="sec-tag">What you paste back to the model when you are driving the loop yourself.</div>
      </div>
      <div style="display: flex; align-items: center; gap: 8px;">
        <button type="button" id="packet-toggle" class="btn-fill">Expand</button>
        <button type="button" id="packet-copy" class="btn-primary" style="box-shadow: 0 1px 2px rgb(31 37 33/0.14), 0 6px 16px rgba(82,121,111,0.18);">Copy</button>
      </div>
    </div>
    <div class="rule13"></div>
    <div style="display: flex; align-items: center; gap: 10px; min-height: 52px;">
      <span class="mono" style="font-size: 9.5px; letter-spacing: 0.16em; text-transform: uppercase; color: var(--muted); white-space: nowrap;">report</span>
      <span id="packet-preview" class="mono" style="flex: 1; min-width: 0; font-size: 12px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">nothing scored yet</span>
    </div>
    <div id="packet-block" hidden style="position: relative;">
      <div class="rule13" style="position: absolute; top: 0; left: 0; right: 0;"></div>
      <pre id="packet-full" class="mono" style="margin: 0; max-height: 360px; overflow: auto; background: transparent; border: none; padding: 16px 0 4px; font-size: 12px; line-height: 1.6; color: var(--ink); white-space: pre-wrap;"></pre>
    </div>
  </section>

  <div class="rule16" style="margin-top: 46px;"></div>

  <div style="display: flex; align-items: flex-start; justify-content: space-between; gap: 40px; margin-top: 20px;">
    <div class="mono" style="display: flex; align-items: center; gap: 9px; font-size: 10.5px; color: var(--muted); line-height: 1.65;">
      <span style="width: 6px; height: 6px; border-radius: 50%; background: var(--accent); animation: mlBreathe 3.4s ease-in-out infinite;"></span>
      <span style="color: var(--deep);">spot-on</span><span>v1</span>
      <span style="width: 1px; height: 10px; background: rgba(70,80,72,0.18);"></span><span>127.0.0.1:7265</span>
      <span style="width: 1px; height: 10px; background: rgba(70,80,72,0.18);"></span><span>chrome + numpy</span>
    </div>
    <div style="display: flex; flex-direction: column; align-items: flex-end; gap: 8px; max-width: 720px;">
      <p style="margin: 0; font-size: 13px; line-height: 1.65; color: var(--muted); text-wrap: pretty;">Thanks for using Spot On. I build tools and projects around human-centered AI, aiming to make these systems more accessible and to have a positive impact on the people they are built for.</p>
      <img src="/signature.svg" alt="" onerror="this.style.display='none'" style="display: block; margin-top: -10px; width: 124px; height: 36px; object-fit: contain; object-position: right bottom; opacity: 0.5;">
    </div>
  </div>
</div>

<script>
var $ = function (id) { return document.getElementById(id); };
var state = {
  run: null, attempts: [], sel: null, view: "attempt",
  pending: null, packet: "", track: "sec-ref", stopping: false
};

function setStatus(msg) { $("status-line").textContent = msg; }
function imgUrl(file) {
  if (!state.run) return "";
  return "/img?run=" + encodeURIComponent(state.run.slug) + "&file=" + encodeURIComponent(file) + "&t=" + Date.now();
}
function api(path, body) {
  var opts = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  return fetch(path, opts).then(function (r) { return r.json(); }).then(function (d) {
    if (d && d.error) throw new Error(d.error);
    return d;
  });
}

// ---- attempt input: a URL for running pages, source for everything else ----
function isUrlKind() { return !!state.run ? state.run.kind === "url" : $("run-kind").value === "url"; }
function getCode() { return isUrlKind() ? $("url-input").value.trim() : $("code").value; }
function setCode(v) { if (isUrlKind()) $("url-input").value = v || ""; else $("code").value = v || ""; }
function syncKindUi() {
  var url = isUrlKind();
  $("url-wrap").hidden = !url;
  $("code").hidden = url;
  $("url-loop-note").hidden = !url;
  $("iterate-wrap").hidden = url;
  $("starter").textContent = url ? "Use localhost" : "Insert starter";
}
$("url-input").addEventListener("keydown", function (e) {
  if (e.key === "Enter") $("render").click();
});

// ---- reference and runs ----
function loadRuns() {
  return api("/runs").then(function (runs) {
    var list = $("runs-list");
    list.innerHTML = "";
    runs.forEach(function (r) {
      var row = document.createElement("div");
      row.className = "run-row";
      var best = r.best_match >= 0 ? r.best_match.toFixed(1) : "-";
      row.innerHTML =
        '<span style="font-size: 14px; font-weight: 500; min-width: 150px;"></span>' +
        '<span class="meta-pill">' + r.kind + '</span>' +
        '<span class="meta-pill">' + (r.css_width || r.width) + ' x ' + (r.css_height || r.height) + '</span>' +
        '<span class="mono" style="font-size: 12px; color: var(--muted); flex: 1;">' + r.attempts + ' attempts, best ' + best + '</span>';
      row.firstChild.textContent = r.name;
      var open = document.createElement("button");
      open.type = "button";
      open.className = "btn-fill row-btn";
      open.textContent = "Open";
      open.addEventListener("click", function () { openRun(r.slug); });
      var del = document.createElement("button");
      del.type = "button";
      del.className = "btn-danger";
      del.textContent = "Delete";
      del.addEventListener("click", function () {
        api("/runs/delete", { run: r.slug }).then(loadRuns);
      });
      row.appendChild(open);
      row.appendChild(del);
      list.appendChild(row);
    });
    $("runs-panel").hidden = runs.length === 0;
    $("new-panel").hidden = runs.length > 0 && state.run !== null;
    return runs;
  });
}

function openRun(slug) {
  return api("/run?run=" + encodeURIComponent(slug)).then(function (run) {
    state.run = run;
    state.attempts = run.attempt_list || [];
    state.sel = state.attempts.length ? state.attempts[state.attempts.length - 1].n : null;
    $("ref-img").src = imgUrl("reference.png");
    $("ref-img").hidden = false;
    $("ref-empty").hidden = true;
    $("active-chip").textContent = run.name;
    $("run-kind").value = run.kind;
    syncKindUi();
    $("new-panel").hidden = true;
    $("runs-panel").hidden = false;
    $("render").disabled = false;
    $("starter").disabled = false;
    renderRefMeta();
    if (state.sel !== null) {
      loadAttemptCode(state.sel);
      setStatus("Attempt " + state.sel + " of " + state.attempts.length + ", best " + run.best_match.toFixed(1) + ".");
    } else {
      setCode("");
      setStatus("Ready. Paste an attempt or insert the starter.");
    }
    renderAll();
    return run;
  });
}

function renderRefMeta() {
  var r = state.run;
  if (!r) { $("ref-meta").innerHTML = ""; return; }
  $("ref-meta").innerHTML =
    '<span class="meta-pill" title="Page size in CSS pixels that every attempt is screenshot at">' + (r.css_width || r.width) + ' x ' + (r.css_height || r.height) + '</span>' +
    '<span class="meta-pill" title="Display scale the design was captured at">' + (r.scale || 1) + 'x</span>' +
    '<span class="meta-pill" title="Page colour read from the border of the design, used as the render ground">' + r.ground + '</span>' +
    '<span class="meta-pill" title="What the model writes for this run">' + r.kind + '</span>';
}

function handleFile(file) {
  var reader = new FileReader();
  reader.onload = function () {
    state.pending = reader.result;
    $("ref-img").src = reader.result;
    $("ref-img").hidden = false;
    $("ref-empty").hidden = true;
    if (!$("run-name").value) $("run-name").value = file.name.replace(/\.[^.]+$/, "");
    $("create-run").disabled = false;
    setStatus("Design loaded. Name the run and start it.");
  };
  reader.readAsDataURL(file);
}

$("pick-file").addEventListener("click", function () { $("file-input").click(); });
$("file-input").addEventListener("change", function (e) {
  if (e.target.files && e.target.files[0]) handleFile(e.target.files[0]);
});
$("new-run").addEventListener("click", function () {
  state.run = null; state.attempts = []; state.sel = null; state.pending = null;
  $("new-panel").hidden = false;
  $("ref-img").hidden = true; $("ref-empty").hidden = false;
  $("run-name").value = ""; $("create-run").disabled = true;
  $("active-chip").textContent = "no design";
  syncKindUi();
  renderAll();
});
var drop = $("drop");
["dragenter", "dragover"].forEach(function (ev) {
  drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add("over"); });
});
["dragleave", "drop"].forEach(function (ev) {
  drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove("over"); });
});
drop.addEventListener("drop", function (e) {
  var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if (f) handleFile(f);
});
drop.addEventListener("click", function () { $("file-input").click(); });

$("create-run").addEventListener("click", function () {
  var name = $("run-name").value.trim();
  if (!name || !state.pending) return;
  setStatus("Creating run...");
  api("/runs", { name: name, kind: $("run-kind").value, scale: parseFloat($("run-scale").value), data_url: state.pending })
    .then(function (run) { state.pending = null; return loadRuns().then(function () { return openRun(run.slug); }); })
    .then(function () { setStatus("Run created. Paste an attempt or insert the starter."); })
    .catch(function (e) { setStatus("Could not create the run: " + e.message); });
});

$("run-kind").addEventListener("change", function () {
  if (!state.run) { syncKindUi(); return; }
  api("/kind", { run: state.run.slug, kind: $("run-kind").value }).then(function (run) {
    state.run.kind = run.kind;
    state.run.starter = null;
    syncKindUi();
    renderRefMeta();
  });
});

$("starter").addEventListener("click", function () {
  if (!state.run) return;
  api("/run?run=" + encodeURIComponent(state.run.slug)).then(function (run) { setCode(run.starter); });
});

// ---- render and score ----
function busy(on, label) {
  $("render").disabled = on || !state.run;
  $("render-spin").hidden = !on;
  $("render-label").textContent = on ? (label || "Rendering") : "Screenshot and score";
  $("iterate").disabled = on || !state.attempts.length;
}

$("render").addEventListener("click", function () {
  var code = getCode();
  if (!state.run || !code.trim()) { setStatus("Nothing to render yet."); return; }
  busy(true);
  setStatus("Taking the screenshot...");
  api("/attempt", { run: state.run.slug, code: code })
    .then(function (rec) {
      state.attempts.push(rec);
      state.sel = rec.n;
      if (rec.match > (state.run.best_match || -1)) {
        state.run.best_match = rec.match;
        state.run.best_attempt = rec.n;
      }
      setStatus("Attempt " + rec.n + " scored " + rec.match.toFixed(1) + " out of 100.");
      renderAll();
      loadRuns();
    })
    .catch(function (e) { setStatus("Render failed: " + e.message); })
    .then(function () { busy(false); });
});

// ---- iterate ----
function iterateRounds(left, note) {
  if (left <= 0 || state.stopping) {
    $("iter-spin").hidden = true;
    $("iter-stop").hidden = true;
    $("iter-label").textContent = "Run " + $("rounds").value + " rounds";
    $("iterate").disabled = !state.attempts.length;
    busy(false);
    setStatus(state.stopping ? "Stopped." : "Iteration finished. Best so far: " + (state.run.best_match || 0).toFixed(1) + ".");
    state.stopping = false;
    return;
  }
  $("iter-label").textContent = left + " to go";
  setStatus("Claude is looking at the design and the difference map...");
  api("/iterate", { run: state.run.slug, instructions: note })
    .then(function (rec) {
      state.attempts.push(rec);
      state.sel = rec.n;
      if (rec.match > (state.run.best_match || -1)) {
        state.run.best_match = rec.match;
        state.run.best_attempt = rec.n;
      }
      loadAttemptCode(rec.n);
      renderAll();
      iterateRounds(left - 1, note);
    })
    .catch(function (e) {
      setStatus("Iteration stopped: " + e.message);
      state.stopping = true;
      iterateRounds(0, note);
    });
}

$("iterate").addEventListener("click", function () {
  if (!state.run || !state.attempts.length) return;
  state.stopping = false;
  $("iter-spin").hidden = false;
  $("iter-stop").hidden = false;
  $("iterate").disabled = true;
  busy(true, "Iterating");
  iterateRounds(parseInt($("rounds").value, 10), $("iter-note").value.trim());
});
$("iter-stop").addEventListener("click", function () {
  state.stopping = true;
  setStatus("Stopping after this round...");
});
$("rounds").addEventListener("change", function () {
  if ($("iter-spin").hidden) $("iter-label").textContent = "Run " + $("rounds").value + " rounds";
});

function loadAttemptCode(n) {
  return api("/code?run=" + encodeURIComponent(state.run.slug) + "&n=" + n).then(function (d) {
    setCode(d.code);
  });
}

// ---- the comparison stage ----
function current() {
  if (state.sel === null) return null;
  for (var i = 0; i < state.attempts.length; i++) {
    if (state.attempts[i].n === state.sel) return state.attempts[i];
  }
  return null;
}

function renderStage() {
  var rec = current();
  var base = $("stage-base"), over = $("stage-over");
  if (!state.run) { base.hidden = true; over.hidden = true; $("stage-empty").hidden = false; $("onion-row").hidden = true; return; }
  var pad = function (n) { return ("00" + n).slice(-3); };
  $("onion-row").hidden = state.view !== "overlay";
  if (state.view === "reference") {
    base.src = imgUrl("reference.png"); base.style.opacity = 1; base.hidden = false; over.hidden = true;
    $("stage-empty").hidden = true;
    return;
  }
  if (!rec) { base.hidden = true; over.hidden = true; $("stage-empty").hidden = false; return; }
  $("stage-empty").hidden = true;
  if (state.view === "attempt") {
    base.src = imgUrl("attempts/" + pad(rec.n) + ".png"); base.style.opacity = 1; base.hidden = false; over.hidden = true;
  } else if (state.view === "difference") {
    base.src = imgUrl("attempts/" + pad(rec.n) + "-diff.png"); base.style.opacity = 1; base.hidden = false; over.hidden = true;
  } else {
    base.src = imgUrl("reference.png"); base.style.opacity = 1; base.hidden = false;
    over.src = imgUrl("attempts/" + pad(rec.n) + ".png"); over.hidden = false;
    over.style.opacity = parseInt($("onion").value, 10) / 100;
  }
}
Array.prototype.forEach.call(document.querySelectorAll("[data-view]"), function (b) {
  b.addEventListener("click", function () {
    state.view = b.dataset.view;
    Array.prototype.forEach.call(document.querySelectorAll("[data-view]"), function (o) {
      o.classList.toggle("active", o.dataset.view === state.view);
    });
    renderStage();
  });
});
$("onion").addEventListener("input", function () {
  if (state.view === "overlay") $("stage-over").style.opacity = parseInt($("onion").value, 10) / 100;
});

// ---- score panel ----
var COMPONENT_HELP = {
  structure: "SSIM over 7px windows. Whether edges and gradients sit in the same places.",
  shape: "Overlap of the drawn area with the design's drawn area, as intersection over union.",
  colour: "How far the colours are where both images have drawn something.",
  detail: "Correlation of edge density. Low means the attempt is smoother or busier than the design.",
  coverage: "Share of the design with something drawn within 6px of it. Missing content caps the whole score."
};

function renderScore() {
  var rec = current();
  if (!rec) { $("score-body").hidden = true; $("score-empty").hidden = false; $("score-raw").textContent = ""; return; }
  $("score-body").hidden = false;
  $("score-empty").hidden = true;
  var rep = rec.report;
  $("score-num").textContent = rep.match.toFixed(1);
  $("score-raw").textContent = "rmse " + rep.raw.rmse + "  ssim " + rep.raw.ssim + "  iou " + rep.raw.shape_iou;

  var prevRec = null;
  for (var i = 0; i < state.attempts.length; i++) {
    if (state.attempts[i].n === rec.n - 1) prevRec = state.attempts[i];
  }
  var d = $("score-delta");
  if (prevRec) {
    var diff = rep.match - prevRec.report.match;
    d.textContent = (diff >= 0 ? "+" : "") + diff.toFixed(1) + " from " + prevRec.n;
    d.style.color = diff >= 0 ? "var(--accent)" : "#C2703F";
  } else { d.textContent = "first attempt"; d.style.color = "var(--faint)"; }

  var comp = $("components");
  comp.innerHTML = "";
  ["structure", "shape", "colour", "detail", "coverage"].forEach(function (k) {
    if (rep.components[k] === undefined) return;
    var v = rep.components[k];
    var row = document.createElement("div");
    row.className = "comp-row";
    row.title = COMPONENT_HELP[k];
    row.style.cursor = "help";
    row.innerHTML =
      '<span class="comp-name">' + k + '</span>' +
      '<span class="bar-track"><span class="bar-fill" style="width:' + Math.max(0, Math.min(100, v)) + '%"></span></span>' +
      '<span class="comp-val">' + v.toFixed(1) + '</span>';
    comp.appendChild(row);
  });

  var grid = $("region-grid");
  grid.innerHTML = "";
  var worst = Math.max.apply(null, rep.regions.map(function (c) { return c.rmse; })) || 1;
  rep.regions.forEach(function (c) {
    var cell = document.createElement("div");
    cell.className = "region-cell";
    var t = Math.min(1, c.rmse / worst);
    cell.style.background = "rgba(194,112,63," + (0.08 + t * 0.82).toFixed(3) + ")";
    cell.title = "RMSE " + c.rmse;
    grid.appendChild(cell);
  });

  var pr = $("problems");
  pr.innerHTML = "";
  rep.problems.forEach(function (p, i) {
    var row = document.createElement("div");
    row.className = "problem";
    row.innerHTML = '<span class="n">' + ("0" + (i + 1)).slice(-2) + '</span><span></span>';
    row.lastChild.textContent = p;
    pr.appendChild(row);
  });

  var pal = $("palette-row");
  pal.innerHTML = "";
  [["design", rep.colours.reference], ["attempt", rep.colours.attempt]].forEach(function (pair) {
    if (!pair[1] || !pair[1].length) return;
    var wrap = document.createElement("div");
    wrap.style.cssText = "display:flex;align-items:center;gap:7px;";
    var label = document.createElement("span");
    label.className = "mono";
    label.style.cssText = "font-size:10px;letter-spacing:0.14em;text-transform:uppercase;color:var(--muted);";
    label.textContent = pair[0];
    wrap.appendChild(label);
    pair[1].forEach(function (c) {
      var sw = document.createElement("span");
      sw.title = c.hex + ", " + c.share + "% of the drawn area";
      sw.style.cssText = "width:18px;height:18px;border-radius:5px;border:1px solid rgba(70,80,72,0.16);cursor:help;background:" + c.hex;
      wrap.appendChild(sw);
    });
    pal.appendChild(wrap);
  });
}

// ---- history ----
function renderHistory() {
  var list = $("history-list");
  list.innerHTML = "";
  $("history-empty").hidden = state.attempts.length > 0;
  state.attempts.slice().reverse().forEach(function (rec) {
    var row = document.createElement("div");
    row.className = "att-row" + (rec.n === state.sel ? " current" : "");
    var prev = null;
    for (var i = 0; i < state.attempts.length; i++) {
      if (state.attempts[i].n === rec.n - 1) prev = state.attempts[i];
    }
    var deltaHtml = "";
    if (prev) {
      var diff = rec.match - prev.report.match;
      deltaHtml = '<span class="att-delta ' + (diff >= 0 ? "up" : "down") + '">' + (diff >= 0 ? "+" : "") + diff.toFixed(1) + '</span>';
    } else {
      deltaHtml = '<span class="att-delta" style="color: var(--faint);">first</span>';
    }
    row.innerHTML =
      '<span class="att-n">' + ("00" + rec.n).slice(-3) + '</span>' +
      '<span class="att-score">' + rec.match.toFixed(1) + '</span>' +
      deltaHtml +
      '<span class="meta-pill" title="Who wrote this attempt">' + rec.source + '</span>' +
      '<span class="att-note"></span>';
    row.querySelector(".att-note").textContent = rec.changes || "";
    if (state.run && rec.n === state.run.best_attempt) {
      var badge = document.createElement("span");
      badge.className = "best-badge";
      badge.textContent = "best";
      row.appendChild(badge);
    }
    var open = document.createElement("button");
    open.type = "button";
    open.className = "btn-fill row-btn";
    open.textContent = "Open";
    open.title = "Put this attempt's code back in the editor";
    open.addEventListener("click", function () {
      state.sel = rec.n;
      loadAttemptCode(rec.n);
      renderAll();
    });
    row.appendChild(open);
    list.appendChild(row);
    var hair = document.createElement("div");
    hair.className = "rule13";
    list.appendChild(hair);
  });
  drawSpark();
}

function drawSpark() {
  var c = $("spark"), ctx = c.getContext("2d");
  var dpr = window.devicePixelRatio || 1;
  if (c.width !== 360 * dpr) { c.width = 360 * dpr; c.height = 44 * dpr; }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, 360, 44);
  if (state.attempts.length < 2) return;
  var vals = state.attempts.map(function (a) { return a.match; });
  var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
  if (hi - lo < 1) { lo = Math.max(0, lo - 1); hi = lo + 2; }
  var x = function (i) { return 2 + i * (356 / (vals.length - 1)); };
  var y = function (v) { return 38 - ((v - lo) / (hi - lo)) * 32; };
  ctx.beginPath();
  ctx.moveTo(x(0), y(vals[0]));
  vals.forEach(function (v, i) { ctx.lineTo(x(i), y(v)); });
  ctx.strokeStyle = "#52796F";
  ctx.lineWidth = 1.5;
  ctx.lineJoin = "round";
  ctx.stroke();
  ctx.lineTo(x(vals.length - 1), 44);
  ctx.lineTo(x(0), 44);
  ctx.closePath();
  ctx.fillStyle = "rgba(82,121,111,0.10)";
  ctx.fill();
  vals.forEach(function (v, i) {
    ctx.beginPath();
    ctx.arc(x(i), y(v), v === hi ? 3 : 1.8, 0, Math.PI * 2);
    ctx.fillStyle = v === hi ? "#52796F" : "rgba(82,121,111,0.45)";
    ctx.fill();
  });
}

// ---- packet ----
function renderPacket() {
  var rec = current();
  if (!rec || !state.run) {
    state.packet = "";
    $("packet-preview").textContent = "nothing scored yet";
    $("packet-full").textContent = "";
    return;
  }
  api("/feedback?run=" + encodeURIComponent(state.run.slug) + "&n=" + rec.n).then(function (d) {
    state.packet = d.text;
    $("packet-full").textContent = d.text;
    $("packet-preview").textContent = "match " + rec.report.match.toFixed(1) + " / 100, " +
      rec.report.problems.length + " things to fix, worst area " +
      (rec.report.worst_regions[0] ? rec.report.worst_regions[0].where : "none");
  });
}
$("packet-toggle").addEventListener("click", function () {
  var b = $("packet-block");
  b.hidden = !b.hidden;
  $("packet-toggle").textContent = b.hidden ? "Expand" : "Collapse";
});
var copyTimer = null;
$("packet-copy").addEventListener("click", function () {
  if (!state.packet) return;
  navigator.clipboard.writeText(state.packet);
  var btn = $("packet-copy");
  btn.textContent = "Copied";
  clearTimeout(copyTimer);
  copyTimer = setTimeout(function () { btn.textContent = "Copy"; }, 1600);
});

// ---- attempt stepper in the track header ----
function renderStepper() {
  var n = state.attempts.length;
  $("att-pos").textContent = n === 0 ? "no attempts" :
    "attempt " + state.sel + " of " + state.attempts[n - 1].n;
  $("prev-att").disabled = !n || state.sel <= 1;
  $("next-att").disabled = !n || state.sel >= state.attempts[n - 1].n;
}
$("prev-att").addEventListener("click", function () {
  if (state.sel > 1) { state.sel -= 1; loadAttemptCode(state.sel); renderAll(); }
});
$("next-att").addEventListener("click", function () {
  if (state.attempts.length && state.sel < state.attempts[state.attempts.length - 1].n) {
    state.sel += 1; loadAttemptCode(state.sel); renderAll();
  }
});

// ---- track header ----
var TRACK_SECTIONS = [
  { id: "sec-ref", label: "design" },
  { id: "sec-attempt", label: "attempt" },
  { id: "sec-score", label: "score" },
  { id: "sec-history", label: "history" },
  { id: "sec-packet", label: "packet" }
];
function renderTrack() {
  var nav = $("track-nav");
  nav.innerHTML = "";
  TRACK_SECTIONS.forEach(function (t, i) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "track-btn" + (state.track === t.id ? " current" : "");
    b.textContent = t.label;
    b.addEventListener("click", function () {
      state.track = t.id;
      renderTrack();
      var el = document.getElementById(t.id);
      if (el) window.scrollTo({ top: Math.max(0, el.getBoundingClientRect().top + window.scrollY - 70), behavior: "smooth" });
    });
    nav.appendChild(b);
    if (i < TRACK_SECTIONS.length - 1) {
      var link = document.createElement("span");
      link.className = "track-link";
      nav.appendChild(link);
    }
  });
}
window.addEventListener("scroll", function () {
  var cur = TRACK_SECTIONS[0].id;
  TRACK_SECTIONS.forEach(function (t) {
    var el = document.getElementById(t.id);
    if (el && el.getBoundingClientRect().top <= 120) cur = t.id;
  });
  if (cur !== state.track) { state.track = cur; renderTrack(); }
}, { passive: true });

function renderAll() {
  $("attempt-meta").textContent = state.run
    ? state.attempts.length + " attempts, best " + (state.run.best_match >= 0 ? state.run.best_match.toFixed(1) : "-")
    : "";
  $("iterate").disabled = !state.attempts.length;
  renderStepper();
  renderStage();
  renderScore();
  renderHistory();
  renderPacket();
}

renderTrack();
syncKindUi();
renderAll();
loadRuns().then(function (runs) {
  if (runs.length) openRun(runs[0].slug);
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message):
        self._send_json(400, {"error": str(message)})

    def _send_bytes(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length))

    def _query(self):
        from urllib.parse import urlparse, parse_qs
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/":
                body = PAGE_HTML.replace("127.0.0.1:7265", "127.0.0.1:{}".format(PORT))
                self._send_bytes(body.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/runs":
                self._send_json(200, _list_runs())
            elif path == "/run":
                q = self._query()
                run = _load_run(q["run"])
                run["attempt_list"] = _attempts(q["run"])
                run["starter"] = starter_code(run)
                self._send_json(200, run)
            elif path == "/code":
                q = self._query()
                p = _run_dir(q["run"]) / "attempts" / "{:03d}.code".format(int(q["n"]))
                self._send_json(200, {"code": p.read_text(encoding="utf-8")})
            elif path == "/feedback":
                q = self._query()
                run = _load_run(q["run"])
                rec = [a for a in _attempts(q["run"]) if a["n"] == int(q["n"])][0]
                self._send_json(200, {"text": feedback_text(run, rec["n"], rec["report"]),
                                      "json": rec["report"]})
            elif path == "/img":
                q = self._query()
                d = _run_dir(q["run"])
                target = (d / q["file"]).resolve()
                if d.resolve() not in target.parents and target.parent != d.resolve():
                    raise ValueError("bad image path")
                self._send_bytes(target.read_bytes(), "image/png")
            elif path == "/signature.svg":
                sig = HERE / "assets" / "signature.svg"
                if sig.exists():
                    self._send_bytes(sig.read_bytes(), "image/svg+xml")
                else:
                    self.send_response(404)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            self._send_error_json(e)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            payload = self._read_json_body()
            if path == "/runs":
                data = payload.get("data_url")
                raw = base64.b64decode(data.split(",", 1)[1]) if data else None
                run = create_run(payload["name"], payload.get("kind", "url"),
                                 reference_bytes=raw, reference_path=payload.get("path"),
                                 scale=payload.get("scale", 1.0))
                run["starter"] = starter_code(run)
                self._send_json(200, run)
            elif path == "/runs/delete":
                shutil.rmtree(_run_dir(payload["run"]), ignore_errors=True)
                self._send_json(200, {"ok": True})
            elif path == "/attempt":
                self._send_json(200, record_attempt(payload["run"], payload["code"],
                                                    source=payload.get("source", "manual")))
            elif path == "/iterate":
                self._send_json(200, run_iteration(payload["run"], payload.get("instructions", "")))
            elif path == "/kind":
                run = _load_run(payload["run"])
                run["kind"] = payload["kind"] if payload["kind"] in KINDS else run["kind"]
                _save_run(payload["run"], run)
                self._send_json(200, run)
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            self._send_error_json(e)


def _guess_kind(target):
    if re.match(r"^https?://", target):
        return "url"
    suffix = Path(target).suffix.lower()
    return {".svg": "svg", ".js": "canvas"}.get(suffix, "html")


def cli_score(argv):
    """Score one attempt from the command line, as a step in someone else's loop.

    Each call is recorded as the next attempt of a named run, the same runs the
    page shows, so a session iterating on a real site builds a visible history
    and can see whether its last change helped.
    """
    import argparse
    import hashlib
    ap = argparse.ArgumentParser(prog="spot-on.py score")
    ap.add_argument("design", help="the design: a mockup export or screenshot")
    ap.add_argument("target", help="a running page URL, or a .html / .svg / .js file")
    ap.add_argument("--kind", choices=KINDS, help="what the target is; guessed if omitted")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="display scale the design was captured at: 1, 1.25, 1.5 or 2")
    ap.add_argument("--run", help="run name; defaults to the design's file name")
    ap.add_argument("--note", default="", help="one line on what changed since the last attempt")
    ap.add_argument("--json", action="store_true", help="print the record as JSON")
    a = ap.parse_args(argv)

    design = Path(a.design)
    kind = a.kind or _guess_kind(a.target)
    code = a.target if kind == "url" else Path(a.target).read_text(encoding="utf-8")
    slug = _slugify(a.run or design.stem)
    sha = hashlib.sha256(design.read_bytes()).hexdigest()[:16]

    if (_run_dir(slug) / "run.json").exists():
        run = _load_run(slug)
        if run.get("design_sha") and run["design_sha"] != sha:
            raise SystemExit("run {!r} was started with a different design image; "
                             "pass --run with a new name".format(slug))
        if run["kind"] != kind:
            run["kind"] = kind
            _save_run(slug, run)
    else:
        run = create_run(a.run or design.stem, kind, reference_path=design, scale=a.scale)
        run["design_sha"] = sha
        _save_run(slug, run)

    history = _attempts(slug)
    record = record_attempt(slug, code, source="session", changes=a.note)
    d = _run_dir(slug) / "attempts"
    record["files"] = {
        "design": str(_run_dir(slug) / "reference.png"),
        "attempt": str(d / "{:03d}.png".format(record["n"])),
        "difference": str(d / "{:03d}-diff.png".format(record["n"])),
    }
    prev = history[-1]["match"] if history else None
    best = max([h["match"] for h in history] + [record["match"]])

    if a.json:
        record["previous_match"] = prev
        record["best_match"] = best
        print(json.dumps(record, indent=2))
        return
    run = _load_run(slug)
    print(feedback_text(run, record["n"], record["report"]))
    print("")
    if prev is None:
        print("first attempt in run {!r}".format(slug))
    else:
        print("change from attempt {}: {:+.1f}   best so far: {:.1f}".format(
            record["n"] - 1, record["match"] - prev, best))
    print("design:     " + record["files"]["design"])
    print("attempt:    " + record["files"]["attempt"])
    print("difference: " + record["files"]["difference"])


def main():
    global PORT
    args = sys.argv[1:]
    if args and args[0] == "score":
        cli_score(args[1:])
        return
    if args:
        try:
            PORT = int(args[0])
        except ValueError:
            print("ignoring invalid port argument: {!r}".format(args[0]))
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("ready on http://{}:{}".format(HOST, PORT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
