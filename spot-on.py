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
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

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
INK_EDGE = 6.0           # local contrast that counts as the edge of drawn content
PRESS_LIMIT = 3          # stuck faults one round is asked to fix; ten is the same as none
# Two questions, two pairings. "What did the attempt put in this slot?" is a question
# about position, and the detectors built on it (empty containers, emphasis, type,
# spacing) exist precisely to compare things that look different, so pairing them by
# appearance refuses the pairs they need. "Which element is this?" is a question about
# identity, and there position is the thing that changed. Measured on pages built so the
# answer is known, identity by appearance is right 98.4% of the time against 92.8% for
# position and 80.4% for not pairing at all: scripts/match_trial.py.
MATCH_DEFAULT = "geometry"   # for slot questions: what is in this part of the page
IDENTITY_MATCH = "content"   # for identity questions: where did this element end up
CONTENT_ACCEPT = 0.35    # thumbnail correlation below which two elements are not the same thing
CONTENT_FAR_W = 2.0      # what crossing the whole page costs a pair, in correlation
CONTENT_GATE = 1.0       # total cost above which a pair is refused
LAYOUT_TRUST = 0.8       # how far the ink profile is trusted against the pixels; 0 is off
ELEMENT_SCORES = 8       # elements kept in the report, worst first
ELEMENT_MIN = 16         # px; below this a box is a glyph, not something to be told about
ELEMENT_BEHIND = 10      # points below the page score at which an element is worth naming
ELEMENT_MOVED = 6        # css px of displacement before "move it" is the instruction
ELEMENT_BUILT = 70       # like-for-like score at which an element counts as built right


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


_RUN_LOCKS = {}
_RUN_LOCKS_GUARD = threading.Lock()


def _run_lock(slug):
    """One lock per run, for the read-modify-write on its run.json."""
    with _RUN_LOCKS_GUARD:
        return _RUN_LOCKS.setdefault(_slugify(slug), threading.Lock())


def _clean_source(source):
    """Only a name the tool itself uses. Anything else is recorded as manual."""
    return source if source in SOURCES else "manual"


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
    """Take the next attempt number, atomically.

    Counting the files and adding one is a read-modify-write. Two attempts recorded
    at the same moment (the server while a command-line score runs, or two candidates
    of one round) both saw the same count, and the second quietly overwrote the first.
    Creating the record file exclusively makes claiming the number the same act as
    counting, across threads and across processes. A half-written placeholder does not
    parse, and _attempts already skips what it cannot read.
    """
    d = _run_dir(slug) / "attempts"
    d.mkdir(parents=True, exist_ok=True)
    n = len(_attempts(slug)) + 1
    while True:
        try:
            fd = os.open(str(d / "{:03d}.json".format(n)),
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            n += 1
            continue
        os.close(fd)
        return n


# ------------------------------------------------------------------ rendering

def _kill_tree(proc):
    """Kill a process and everything it started."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        import signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _run_tree(cmd, timeout, **kw):
    """Run a command; on timeout kill its whole process tree, not just the top.

    subprocess.run kills only the process it started. Chrome and every agent CLI
    start children, so a timeout used to leave those children alive: they hold the
    temporary profile open, which makes the cleanup fail silently, and an agent left
    running goes on spending the account after the round that wanted it has gone.
    """
    kw.setdefault("stdout", subprocess.PIPE)
    kw.setdefault("stderr", subprocess.PIPE)
    if os.name != "nt":
        kw.setdefault("start_new_session", True)
    proc = subprocess.Popen(cmd, **kw)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = None, None
        raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


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
        flags = []
        if kind == "url":
            target = _check_url(code)
            settle_ms = settle_ms or 4000
            # Apps that honour prefers-reduced-motion hold still for the screenshot.
            flags.append("--force-prefers-reduced-motion")
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
        ]
        cmd += flags + shlex.split(os.environ.get("SPOT_ON_CHROME_FLAGS", ""))
        cmd.append(target)
        proc = _run_tree(cmd, timeout=120)
        if not shot.exists():
            err = (proc.stderr or b"").decode("utf-8", "replace")[-600:]
            raise RuntimeError("the browser produced no screenshot. {}".format(err.strip()))
        # Closed before the cleanup below: an open handle on Windows keeps the whole
        # temporary directory undeletable, and the cleanup ignores errors, so it leaks.
        with Image.open(shot) as shot_img:
            if shot_img.mode in ("RGBA", "LA", "P"):
                # Flattened onto the page's own colour, not white. A page with no
                # background of its own is white in a real browser, which is why white
                # was the default, but this run knows what the page sits on: on a dark
                # design every transparent area was coming out white, so a translucent
                # panel over a dark ground scored as a white box.
                rgba = shot_img.convert("RGBA")
                flat = Image.new("RGB", shot_img.size, ground)
                flat.paste(rgba, mask=rgba.split()[3])
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


def _ink_mask(rgb):
    """The drawn content: what has local contrast, not what is far from one colour.

    Measuring distance from a single page colour fails in both directions on a real
    page, and the calibration cases show each one. A full-bleed gradient that is only
    a little too saturated crosses the threshold everywhere, so a background nobody
    would call content is measured as content: on one rebuilt page the design read as
    8.6% drawn and the attempt as 70.3%, which pinned shape at 10.7 for a whole run
    and sent the model to fix box geometry that was already right. In the other
    direction a white card on an off-white page sits 19.5 from the ground, under the
    threshold, so leaving out the largest element on the page cost 0.1 points.

    Local contrast answers both. A smooth gradient has almost none however saturated
    it is, so it never registers, and a pale panel still has an edge against whatever
    it sits on, so filling what those edges enclose recovers the panel. A gradient
    that is genuinely wrong is still reported, by colour, which is the part whose job
    that is; shape no longer charges for it a second time.
    """
    from scipy import ndimage

    im = Image.fromarray(rgb.astype(np.uint8)).convert("L")
    g = np.asarray(im.filter(ImageFilter.GaussianBlur(0.6)), dtype=np.float64)
    gx = np.zeros_like(g)
    gy = np.zeros_like(g)
    gx[:, 1:-1] = g[:, 2:] - g[:, :-2]
    gy[1:-1, :] = g[2:, :] - g[:-2, :]
    edges = np.hypot(gx, gy) > INK_EDGE
    edges = ndimage.binary_closing(edges, structure=np.ones((3, 3)), iterations=2)
    return ndimage.binary_fill_holes(edges)


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


def _elements(g):
    """Text lines, buttons and boxes, found from edges, as bounding boxes in image pixels.

    Edges rather than an ink mask, so light text on a dark card is found too.
    Letters are joined into words and lines by a wide, short dilation.
    """
    from scipy import ndimage
    e = np.maximum(np.abs(np.diff(g, axis=1, prepend=g[:, :1])),
                   np.abs(np.diff(g, axis=0, prepend=g[:1, :])))
    m = ndimage.binary_dilation(e > 24, structure=np.ones((3, 9), dtype=bool))
    lab, _ = ndimage.label(m, structure=np.ones((3, 3), dtype=bool))
    out = []
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        if sl is None:
            continue
        ys, xs = sl
        h, w = ys.stop - ys.start, xs.stop - xs.start
        if h * w < 60:
            continue
        if min(h, w) <= 12 and max(h, w) >= 60:
            continue  # a border or rule: these split into fragments unpredictably
        fill = float((lab[sl] == i).mean())
        kind = "box" if (h > 30 and w > 60 and fill < 0.35) else "text"
        out.append({"x": int(xs.start), "y": int(ys.start), "w": int(w), "h": int(h), "kind": kind})
    return out


def _greedy(pairs, design, attempt):
    """Take the cheapest pairing first, one design element to one attempt element."""
    pairs.sort(key=lambda p: p[0])
    used_d, used_a, matched = set(), set(), []
    for cost, i, j in pairs:
        if i in used_d or j in used_a:
            continue
        used_d.add(i)
        used_a.add(j)
        matched.append((design[i], attempt[j]))
    missing = [d for i, d in enumerate(design) if i not in used_d]
    return matched, missing


def _match_geometry(design, attempt, g_ref=None, g_att=None):
    """Pair each design element with the attempt element nearest in position and size."""
    pairs = []
    for i, d in enumerate(design):
        for j, a in enumerate(attempt):
            cost = (abs(a["x"] - d["x"]) + abs(a["y"] - d["y"]) + abs(a["w"] - d["w"])
                    + abs(a["h"] - d["h"]) + (40 if a["kind"] != d["kind"] else 0))
            if cost <= 0.6 * (d["w"] + d["h"]) + 12:
                pairs.append((cost, i, j))
    return _greedy(pairs, design, attempt)


def _thumb(g, el, n=12):
    """One element reduced to a small contrast-normalised thumbnail.

    Normalising away the mean and the scale is what lets this recognise the same
    element after it has been recoloured or resized, which is exactly where matching
    on position and size gives up.
    """
    crop = g[el["y"]:el["y"] + el["h"], el["x"]:el["x"] + el["w"]]
    if crop.size < 4:
        return None
    t = np.asarray(Image.fromarray(crop.astype(np.uint8)).resize((n, n), Image.BILINEAR),
                   dtype=np.float64)
    t -= t.mean()
    s = float(np.sqrt((t * t).sum()))
    return (t / s).ravel() if s > 1e-9 else None


def _ink_profile(g, el, rows=10, cols=4):
    """Where the ink sits inside one element, rather than what it spells.

    A thumbnail of the pixels is the sharper way to recognise an element, and it has
    one blind spot: change the words in a box and the pixels change, so the box reads
    as a different thing and a reworded label is reported missing. What a reword does
    not change is the layout of the ink: how many rows of it there are, how tall they
    are, how dense. Reading that as well gives the match something to hold on to when
    the glyphs have all moved. Deliberately coarse across the page, where words live,
    and finer down it, where the type metrics do.
    """
    crop = g[el["y"]:el["y"] + el["h"], el["x"]:el["x"] + el["w"]]
    if crop.size < 16:
        return None
    ink = (np.abs(crop - np.median(crop)) > 24).astype(np.float64)
    if ink.sum() < 4:
        return None

    def bins(profile, n):
        src = np.linspace(0.0, 1.0, num=len(profile))
        return np.interp(np.linspace(0.0, 1.0, num=n), src, profile)

    v = np.concatenate([bins(ink.mean(axis=1), rows), bins(ink.mean(axis=0), cols),
                        [ink.mean()]])
    v -= v.mean()
    s = float(np.sqrt((v * v).sum()))
    return v / s if s > 1e-9 else None


def _match_content(design, attempt, g_ref, g_att, accept=CONTENT_ACCEPT,
                   far_w=CONTENT_FAR_W, gate=CONTENT_GATE, layout=LAYOUT_TRUST):
    """Pair elements by what they look like, with position only as a tie-breaker.

    Matching on position cannot tell a moved element from a missing one and a new
    one: it reports two faults where there is one, and the sentence it writes sends
    the next round to redraw something that is already correct.
    """
    td = [_thumb(g_ref, d) for d in design]
    ta = [_thumb(g_att, a) for a in attempt]
    pd = [_ink_profile(g_ref, d) for d in design] if layout else [None] * len(design)
    pa = [_ink_profile(g_att, a) for a in attempt] if layout else [None] * len(attempt)
    diag = float(np.hypot(g_ref.shape[1], g_ref.shape[0])) or 1.0
    pairs = []
    for i, d in enumerate(design):
        if td[i] is None:
            continue
        for j, a in enumerate(attempt):
            if ta[j] is None:
                continue
            # Absolute correlation, so a light-on-dark element still matches its
            # dark-on-light twin. A rebuild that inverts a card has moved nothing and
            # lost nothing, and saying "the card is missing, and here is one you did not
            # ask for" sends the next round to redraw geometry that is already right.
            sim = abs(float(td[i] @ ta[j]))
            # Whichever way recognises it, discounting the coarser one so that where the
            # pixels agree they decide. The profile is there to rescue an element the
            # pixels have lost, not to overrule them.
            if pd[i] is not None and pa[j] is not None:
                sim = max(sim, layout * abs(float(pd[i] @ pa[j])))
            if sim < accept:
                continue
            shape = abs(math.log((a["w"] * d["h"] + 1.0) / (a["h"] * d["w"] + 1.0)))
            size = abs(math.log((a["w"] * a["h"] + 1.0) / (d["w"] * d["h"] + 1.0)))
            far = math.hypot(a["x"] + a["w"] / 2.0 - d["x"] - d["w"] / 2.0,
                             a["y"] + a["h"] / 2.0 - d["y"] - d["h"] / 2.0) / diag
            cost = (1.0 - sim) + 0.6 * shape + 0.3 * min(size, 2.0) + far_w * far
            if cost <= gate:
                pairs.append((cost, i, j))
    return _greedy(pairs, design, attempt)


def _match_overlay(design, attempt, g_ref=None, g_att=None):
    """Do not match at all: compare each design element with whatever sits in its box."""
    return [(d, dict(d, kind=d["kind"])) for d in design], []


MATCHERS = {"geometry": _match_geometry, "content": _match_content, "overlay": _match_overlay}


def _match_elements(design, attempt, g_ref=None, g_att=None, how=None):
    """Pair design elements with attempt elements by the chosen strategy."""
    how = how or os.environ.get("SPOT_ON_MATCH") or MATCH_DEFAULT
    return MATCHERS[how](design, attempt, g_ref, g_att)


def _where(el, size):
    cx, cy = el["x"] + el["w"] / 2.0, el["y"] + el["h"] / 2.0
    return _cell_name({"row": min(3, int(cy * 4 // max(1, size[1]))),
                       "col": min(3, int(cx * 4 // max(1, size[0])))})


def _ncc(a, b):
    a, b = a - a.mean(), b - b.mean()
    den = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / den) if den > 1e-9 else 0.0


def _glyph_check(g_ref, g_att, matched):
    """Compare letter shapes on text lines that already sit in the right place.

    Width alone does not reveal a wrong typeface: Arial and Segoe UI set the same
    string to nearly the same width. Lining up a text line and comparing its pixels
    does. Only well-aligned lines are used, so a broken layout cannot masquerade as
    a font problem. Measured on a pricing page: right font, no line below 0.75;
    wrong font, a fifth of lines below it.
    """
    scores = []
    for d, a in matched:
        if d["kind"] != "text" or d["h"] < 8:
            continue
        if (abs(a["w"] - d["w"]) > 0.08 * d["w"] + 2 or abs(a["h"] - d["h"]) > 0.25 * d["h"] + 2
                or abs(a["x"] - d["x"]) > 8 or abs(a["y"] - d["y"]) > 8):
            continue
        cd = g_ref[d["y"]:d["y"] + d["h"], d["x"]:d["x"] + d["w"]]
        ca = g_att[a["y"]:a["y"] + a["h"], a["x"]:a["x"] + a["w"]]
        if cd.size == 0 or ca.size == 0:
            continue
        ca = np.asarray(Image.fromarray(ca.astype(np.uint8)).resize(
            (cd.shape[1], cd.shape[0]), Image.BILINEAR), dtype=np.float64)
        scores.append(_ncc(cd, ca))
    if len(scores) < 8:
        return {"lines": len(scores), "weak_share": 0.0, "median": None}
    return {"lines": len(scores),
            "weak_share": round(float(np.mean([v < 0.75 for v in scores])), 3),
            "median": round(float(np.median(scores)), 3)}


def compare_elements(g_ref, g_att, px_per_css=1.0, shift_css=0, how=None):
    """Element-level differences in CSS pixels, grouped and ordered by how much they matter.

    Page-wide numbers stop helping once a page is close: they say the structure is
    off but not where, so a model makes sweeping edits that break what already
    works. This names the element, the direction and the size of each miss.
    """
    design, attempt = _elements(g_ref), _elements(g_att)
    matched, missing = _match_elements(design, attempt, g_ref, g_att, how=how)
    size = (g_ref.shape[1], g_ref.shape[0])

    def css(v):
        return int(round(v / px_per_css))

    items = []
    for d, a in matched:
        base = {"kind": d["kind"], "x": css(d["x"]), "y": css(d["y"]), "w": css(d["w"]),
                "h": css(d["h"]), "aw": css(a["w"]), "where": _where(d, size),
                "area": d["w"] * d["h"], "aspects": {}}
        dw_pct = (a["w"] - d["w"]) * 100.0 / max(1, d["w"])
        dh, dx, dy = css(a["h"] - d["h"]), css(a["x"] - d["x"]), css(a["y"] - d["y"])
        if abs(dw_pct) >= 4 and abs(css(a["w"] - d["w"])) >= 3:
            base["aspects"]["width"] = round(dw_pct, 1)
        if abs(dh) >= 3:
            base["aspects"]["height"] = dh
        # A page-wide shift is reported once, on its own; do not repeat it per element.
        tol = max(3, int(abs(shift_css) * 0.25))
        covered = shift_css and abs(dy + shift_css) <= tol and abs(dx) < 4
        if max(abs(dx), abs(dy)) >= 4 and not covered:
            base["aspects"]["position"] = (dx, dy)
        if base["aspects"]:
            items.append(base)
    for d in missing:
        if d["w"] * d["h"] >= 150:
            items.append({"kind": d["kind"], "x": css(d["x"]), "y": css(d["y"]), "w": css(d["w"]),
                          "h": css(d["h"]), "where": _where(d, size), "area": d["w"] * d["h"],
                          "aspects": {"missing": True}})

    # Group the same miss repeated across siblings (three card titles, three buttons).
    def alike(a, b):
        if a["kind"] != b["kind"] or abs(a["y"] - b["y"]) > 8:
            return False
        if set(a["aspects"]) != set(b["aspects"]):
            return False
        for k, va in a["aspects"].items():
            vb = b["aspects"][k]
            if k == "width" and (va * vb <= 0 or abs(va - vb) > 5):
                return False
            if k == "height" and (va * vb <= 0 or abs(va - vb) > 3):
                return False
            if k == "position" and (abs(va[0] - vb[0]) > 3 or abs(va[1] - vb[1]) > 3):
                return False
        return True

    groups = []
    for it in items:
        for g in groups:
            if alike(g[0], it):
                g.append(it)
                break
        else:
            groups.append([it])

    def weight(g):
        asp = g[0]["aspects"]
        mag = 0.0
        if "width" in asp:
            mag += abs(asp["width"]) / 10.0
        if "height" in asp:
            mag += abs(asp["height"]) / 6.0
        if "position" in asp:
            mag += max(abs(asp["position"][0]), abs(asp["position"][1])) / 6.0
        if "missing" in asp:
            mag += 1.5
        return sum(x["area"] for x in g) * mag

    groups.sort(key=weight, reverse=True)
    for d, a in matched:
        if d["kind"] == "text":
            d["type"], a["type"] = _type_metrics(g_ref, d), _type_metrics(g_att, a)
            d["emphasis"] = _emphasis_metrics(g_ref, d)
            a["emphasis"] = _emphasis_metrics(g_att, a)
        d["inside"] = _interior_detail(g_ref, d)
        a["inside"] = _interior_detail(g_att, a)

    return {"design_count": len(design), "attempt_count": len(attempt), "matched": len(matched),
            "groups": groups, "glyph": _glyph_check(g_ref, g_att, matched),
            "spacing": _spacing_gaps(matched, css, size),
            "row_spacing": _row_gaps(matched, css, size),
            "type": _type_findings(matched, css, size),
            "alignment": _alignment(matched, css),
            "emphasis": _emphasis_findings(matched, css, size),
            "hollow": _hollow(matched, css, size, limit=8)}


def _type_metrics(g, el):
    """Measure the type inside one element box: how tall, how heavy, how far apart.

    The report could only say a text element was "about 17% wider" and then hedge
    between font size, weight and letter spacing, leaving the model to pick. These are
    the three it can set directly, read off the rows of ink inside the box: the height
    of a row band is the size, how much of the box is ink is the weight, and the step
    from one band to the next is the line height.
    """
    crop = g[el["y"]:el["y"] + el["h"], el["x"]:el["x"] + el["w"]]
    if crop.size < 60:
        return None
    ink = np.abs(crop - np.median(crop)) > 24
    rows = ink.mean(axis=1) > 0.02
    if not rows.any():
        return None

    bands, start = [], None
    for i, on in enumerate(rows):
        if on and start is None:
            start = i
        elif not on and start is not None:
            bands.append((start, i))
            start = None
    if start is not None:
        bands.append((start, len(rows)))
    bands = [b for b in bands if b[1] - b[0] >= 3]
    if not bands:
        return None

    heights = sorted(b[1] - b[0] for b in bands)
    spacing = None
    if len(bands) >= 2:
        steps = [bands[i + 1][0] - bands[i][0] for i in range(len(bands) - 1)]
        spacing = float(np.median(steps))
    return {"height": float(heights[len(heights) // 2]), "density": float(ink.mean()),
            "spacing": spacing, "lines": len(bands)}


def _interior_detail(g, el, inset=0.22):
    """How much is drawn inside an element, ignoring its own edge."""
    iy, ix = int(el["h"] * inset), int(el["w"] * inset)
    crop = g[el["y"] + iy:el["y"] + el["h"] - iy, el["x"] + ix:el["x"] + el["w"] - ix]
    if crop.size < 40 or crop.shape[0] < 3 or crop.shape[1] < 3:
        return None
    return float(np.abs(np.diff(crop, axis=1)).mean() + np.abs(np.diff(crop, axis=0)).mean())


def _hollow(matched, css, size, limit=2):
    """Elements drawn as an empty container where the design has something inside.

    A button, a nav chip or an icon well matches on geometry because the container is
    the right size in the right place, so every geometric check passes and nothing is
    reported, while the glyph that belongs inside it is simply absent. On one rebuild
    the whole top bar came out as empty circles and the report said nothing about it.
    """
    out = []
    for d, a in matched:
        if min(d["w"], d["h"]) < 12:
            continue
        di, ai = d.get("inside"), a.get("inside")
        if di is None or ai is None:
            continue
        # Something clearly drawn in the design, and clearly less in the attempt.
        if di >= 8.0 and ai < 0.45 * di:
            out.append({"x": css(d["x"]), "y": css(d["y"]), "w": css(d["w"]),
                        "h": css(d["h"]), "where": _where(d, size), "kind": d["kind"],
                        "cx": d["x"] + d["w"] // 2, "cy": d["y"] + d["h"] // 2,
                        "score": di * d["w"] * d["h"]})

    groups = []
    for f in sorted(out, key=lambda g: (g["y"], g["x"])):
        for g in groups:
            like = (abs(g[0]["y"] - f["y"]) <= 12
                    and abs(g[0]["w"] - f["w"]) <= max(6, 0.25 * g[0]["w"])
                    and abs(g[0]["h"] - f["h"]) <= max(6, 0.25 * g[0]["h"]))
            if like:
                g.append(f)
                break
        else:
            groups.append([f])
    merged = []
    for g in groups:
        first = dict(g[0])
        first["n"] = len(g)
        first["score"] = sum(x["score"] for x in g)
        merged.append(first)
    merged.sort(key=lambda f: -f["score"])
    return merged[:limit]


def _hollow_summary(found):
    """One sentence when empties are everywhere, rather than two arbitrary examples.

    A rebuild that leaves out its icons leaves out all of them: on one page the top
    bar, the quick-access grid and three bottom panels were every one of them an empty
    container. Naming two of those sets describes the symptom; naming the pattern is
    what a person or a model can act on in a single pass.
    """
    if len(found) < 3:
        return None
    boxes = sum(f.get("n", 1) for f in found)
    biggest = max(found, key=lambda f: f["w"] * f["h"])
    return ("{} places on the page draw a container the right size in the right place and "
            "leave it empty, {} boxes in all, from {}x{}px down to {}x{}px. The design puts an "
            "icon or a glyph in each. This is one job, not {} separate ones: pick an icon set or "
            "draw them as inline SVG and fill them all.".format(
                len(found), boxes, biggest["w"], biggest["h"],
                min(f["w"] for f in found), min(f["h"] for f in found), boxes))


def _hollow_sentence(f):
    if f.get("n", 1) > 1:
        return ("{} boxes about {}x{}px around y {}px (the {} of the page) are the right size in "
                "the right place but empty: the design draws an icon or a glyph inside each one "
                "and the attempt has the containers only. They are one set, so draw them "
                "together.".format(f["n"], f["w"], f["h"], f["y"], f["where"]))
    return ("The {} at x {}, y {} ({}x{}px, the {} of the page) is the right size in the right "
            "place but empty: the design has something drawn inside it, an icon, a glyph or a "
            "small chart, and the attempt has the container only. Draw the contents; the box "
            "itself already matches.".format(
                "box" if f["kind"] != "text" else "element", f["x"], f["y"], f["w"], f["h"],
                f["where"]))


def _emphasis_metrics(g, el):
    """Underline, slant and the weight along the line, for one text element.

    Emphasis is a real design instruction that the measurements so far walked past: a
    word set in bold, a link underlined, a phrase in italic. The score feels it, as a
    little less ink or a slightly different edge, and no sentence ever said what it was.
    """
    crop = g[el["y"]:el["y"] + el["h"], el["x"]:el["x"] + el["w"]]
    if crop.size < 200:
        return None
    ink = np.abs(crop - np.median(crop)) > 24
    rows = np.where(ink.mean(axis=1) > 0.005)[0]
    cols = np.where(ink.any(axis=0))[0]
    if rows.size < 6 or cols.size < 24:
        return None
    band = ink[rows.min():rows.max() + 1, cols.min():cols.max() + 1]
    width = band.shape[1]

    # A row inked across nearly the whole width of the text is a rule, not letters.
    bottom = band[int(band.shape[0] * 0.78):]
    underline = float(max((r.sum() / float(width) for r in bottom), default=0.0))

    # Stroke angle from the gradients, with any underline row dropped first: a long
    # horizontal rule swamps the statistics and makes underlined text read as italic.
    keep = band[:int(band.shape[0] * 0.78)] if underline > 0.9 else band
    patch = crop[rows.min():rows.min() + keep.shape[0], cols.min():cols.max() + 1]
    gx = np.zeros_like(patch); gy = np.zeros_like(patch)
    gx[:, 1:-1] = patch[:, 2:] - patch[:, :-2]
    gy[1:-1, :] = patch[2:, :] - patch[:-2, :]
    mag = np.hypot(gx, gy)
    strong = mag > 40
    if strong.sum() >= 50:
        ang = np.arctan2(gy[strong], gx[strong])
        slant = float(np.degrees(0.5 * np.arctan2(np.sin(2 * ang).mean(),
                                                  np.cos(2 * ang).mean())))
    else:
        slant = 0.0

    cells = 12
    profile = [float(c.mean()) for c in np.array_split(keep, cells, axis=1)]
    return {"underline": underline, "slant": slant, "profile": profile,
            "density": float(band.mean()), "x": el["x"], "w": el["w"]}


def _emphasis_findings(matched, css, size, limit=2):
    """Where the design emphasises text and the attempt does not, or the reverse."""
    out = []
    for d, a in matched:
        if d["kind"] != "text":
            continue
        md, ma = d.get("emphasis"), a.get("emphasis")
        if not md or not ma:
            continue
        # Only lines that already sit in the right place and hold the same amount of
        # text. A one-line box in the design matched against a box holding three
        # wrapped lines in the attempt compared the design's only line against the
        # attempt's last one, and reported an underline and an italic that were
        # neither. The test is a ratio, not a percentage, because an underline
        # legitimately makes a line about a quarter taller while a wrapped paragraph
        # is a multiple of it.
        taller = max(d["h"], a["h"]) / float(max(min(d["h"], a["h"]), 1))
        if (abs(d["w"] - a["w"]) > 0.08 * max(d["w"], 1) or taller > 1.6
                or abs(d["x"] - a["x"]) > 6):
            continue
        # A mostly-filled box is a shape, not a line of type. A pill button's rounded
        # ends tilt the measured stroke angle (-8.6 degrees on one, which read as
        # italic), and a solid rectangle's bottom row is a perfect underline. Neither
        # has any emphasis to report, so shapes are left out of all three checks.
        if md["density"] > 0.6 or ma["density"] > 0.6:
            continue
        # And only on something shaped like a line of text. The element finder calls a
        # 57x49 logo "text", and when the attempt drew a plain dark circle in its place
        # the circle's fully inked bottom rows scored a perfect underline. A line of
        # type is several times wider than it is tall; a glyph or an icon is not.
        if min(d["w"], a["w"]) < 2.5 * max(d["h"], a["h"]):
            continue
        common = {"x": css(d["x"]), "y": css(d["y"]), "where": _where(d, size),
                  "area": d["w"] * d["h"]}
        if md["underline"] > 0.9 and ma["underline"] < 0.7:
            out.append(dict(common, prop="underline", design="underlined",
                            attempt="not underlined"))
        elif ma["underline"] > 0.9 and md["underline"] < 0.7:
            out.append(dict(common, prop="underline", design="not underlined",
                            attempt="underlined"))
        # 12 degrees, not 8: a genuine italic measures about 18 against an upright 0,
        # while curves and antialiasing on ordinary text drift a few degrees on their own.
        if abs(md["slant"] - ma["slant"]) >= 12:
            leaning = md["slant"] > ma["slant"]
            out.append(dict(common, prop="italic",
                            design="italic" if leaning else "upright",
                            attempt="upright" if leaning else "italic"))
        gaps = [x - y for x, y in zip(md["profile"], ma["profile"])]
        if gaps:
            spread = sorted(abs(g) for g in gaps)[len(gaps) // 2]
            worst = max(gaps)
            # A spike in one part of the line is a word set differently. A rise across
            # the whole line is the weight of the line, which font-weight already says.
            if worst >= 0.04 and worst >= 3 * max(spread, 0.008):
                at = gaps.index(worst)
                out.append(dict(common, prop="part-weight",
                                part=int(round(100.0 * at / len(gaps)))))
    out.sort(key=lambda f: -f["area"])
    seen, kept = set(), []
    for f in out:
        if f["prop"] in seen:
            continue
        seen.add(f["prop"])
        kept.append(f)
    return kept[:limit]


_EMPHASIS_WORDING = {
    "underline": ("The text at x {x}, y {y} (the {where} of the page) is {design} in the design "
                  "and {attempt} in the attempt. That is text-decoration."),
    "italic": ("The text at x {x}, y {y} (the {where} of the page) is {design} in the design and "
               "{attempt} in the attempt. That is font-style."),
    "part-weight": ("Part of the text at x {x}, y {y} (the {where} of the page) is set heavier in "
                    "the design than in the attempt, starting about {part}% along the line. A word "
                    "or phrase there is bold in the design and is not here. That is a span, not "
                    "the weight of the whole line."),
}


def _emphasis_sentence(f):
    return _EMPHASIS_WORDING[f["prop"]].format(**f)


def _type_findings(matched, css, size, limit=2):
    """Where the type itself differs, named as the property that sets it."""
    out = []
    for d, a in matched:
        if d["kind"] != "text":
            continue
        md, ma = d.get("type"), a.get("type")
        if not md or not ma:
            continue
        common = {"x": css(d["x"]), "y": css(d["y"]), "where": _where(d, size),
                  "area": d["w"] * d["h"]}
        # Deliberately hard to trigger. The old typeface hint fired on pages whose font
        # was already right and cost a round every time, so the bar is set above what
        # antialiasing and band-edge rounding can produce on their own: a 2px difference
        # on a 23px line is noise, and every real miss measured on actual rebuilds was
        # 4px or more. Silence on a page that is already right is worth more here than
        # catching the smallest true difference.
        dh = ma["height"] - md["height"]
        if abs(dh) >= 3 and abs(dh) >= 0.12 * md["height"]:
            out.append(dict(common, prop="size", design=css(md["height"]),
                            attempt=css(ma["height"])))
        dd = ma["density"] - md["density"]
        if abs(dd) >= 0.08 and abs(dd) >= 0.25 * md["density"]:
            out.append(dict(common, prop="weight", design=int(round(md["density"] * 100)),
                            attempt=int(round(ma["density"] * 100))))
        # Line height is deliberately not measured here. The element finder already
        # splits a paragraph into one element per line, so every text box holds a single
        # band and this is the wrong level to look at it. It is the gap between
        # consecutive lines of a block instead, which _spacing_gaps reports.
    out.sort(key=lambda f: -f["area"])
    seen, kept = set(), []
    for f in out:
        # One sentence per property, on the biggest element that shows it. Three
        # headings all a size too small is one mistake, not three.
        if f["prop"] in seen:
            continue
        seen.add(f["prop"])
        kept.append(f)
    return kept[:limit]


_TYPE_WORDING = {
    "size": ("The text at x {x}, y {y} (the {where} of the page) is {attempt}px tall; the design's "
             "is {design}px. That is font-size, and getting it wrong also makes the width and the "
             "spacing around it read as wrong."),
    "weight": ("The text at x {x}, y {y} (the {where} of the page) is {heavier} than the design: "
               "{attempt}% of its box is ink against {design}%. That is font-weight."),
    "leading": ("The lines of text at x {x}, y {y} (the {where} of the page) sit {attempt}px apart; "
                "the design has {design}px. That is line-height on that block alone, never on "
                "every text element."),
}


def _type_sentence(f):
    return _TYPE_WORDING[f["prop"]].format(
        heavier="heavier" if f["attempt"] > f["design"] else "lighter", **f)


def problem_keys(report):
    """A stable name for each thing the report is complaining about.

    Sentences carry measurements, so their wording changes every round even when the
    fault itself has not moved. These keys are what stays the same, so a problem can be
    recognised as one that was already named and not fixed. Positions are bucketed,
    because an element that shifted a few pixels is still the same element.
    """
    keys = set()
    comp = report.get("components") or {}
    off = report.get("offsets") or {}
    for way in ("vertical", "horizontal"):
        if off.get(way):
            keys.add(("shift", way))
    if comp.get("coverage", 100) < 97:
        keys.add(("coverage",))
    raw = report.get("raw") or {}
    if comp.get("colour", 100) < 90:
        keys.add(("colour", "background"
                  if raw.get("background_distance", 0) >= raw.get("palette_only_distance", 0)
                  else "palette"))
    els = report.get("elements") or {}
    glyph = els.get("glyph") or {}
    if glyph.get("lines", 0) >= 8 and glyph.get("weak_share", 0) >= 0.15:
        keys.add(("type", "family"))
    for f in els.get("type") or []:
        keys.add(("type", f["prop"], f["y"] // 50))
    for f in els.get("alignment") or []:
        keys.add(("align", f["design"] // 20))
    for s in els.get("spacing") or []:
        keys.add(("spacing", s["y"] // 50))
    for g in (els.get("groups") or [])[:4]:
        it = g[0]
        keys.add(("element", it["kind"], it["y"] // 50, tuple(sorted(it["aspects"]))))
    return keys


def stuck_problems(history, base):
    """Faults on the best attempt that the best attempts before it also had.

    A round that leaves the first problem exactly where it was has spent its money for
    nothing, and the next round gets the same list and often makes the same choice.
    Counting how many rounds each fault has survived lets the prompt say which ones
    have already been asked for and skipped.
    """
    climb, best = [], -1.0
    for rec in sorted(history, key=lambda r: r["n"]):
        if rec["match"] > best:
            best = rec["match"]
            climb.append(rec)
    chain = [r for r in climb if r["n"] <= base["n"]]
    if len(chain) < 2:
        return []
    earlier = [problem_keys(r["report"]) for r in chain[:-1]]
    out = []
    for key in problem_keys(base["report"]):
        rounds = 0
        for keys in reversed(earlier):
            if key in keys:
                rounds += 1
            else:
                break
        if rounds:
            out.append({"key": list(key), "rounds": rounds})
    out.sort(key=lambda s: -s["rounds"])
    return out


def stuck_phrase(key):
    """Name a stuck problem in the words the report used for it."""
    kind = key[0]
    if kind == "shift":
        return "the page-wide {} shift".format(key[1])
    if kind == "coverage":
        return "the part of the design with nothing drawn near it"
    if kind == "colour":
        return ("the page background colour" if key[1] == "background"
                else "the colours the design uses")
    if kind == "type":
        if key[1] == "family":
            return "the typeface"
        return "the font {} around y {}px".format(key[1], key[2] * 50)
    if kind == "align":
        return "the left edge alignment near x {}px".format(key[1] * 20)
    if kind == "spacing":
        return "the spacing around y {}px".format(key[1] * 50)
    if kind == "element":
        what = ", ".join(key[3]) or "position"
        return "the {} around y {}px ({})".format(key[1], key[2] * 50, what)
    return str(key)


def _alignment(matched, css, limit=2):
    """Left edges that line up in the design, checked in the attempt.

    Worth its own sentence because one container fixes all of it. A column of elements
    that share a left edge is a single padding or margin, so when they come out ragged
    the report would otherwise say the same thing about each element separately and the
    model would move each one on its own.
    """
    by_edge = {}
    for d, a in matched:
        by_edge.setdefault(round(d["x"] / 4.0), []).append((d, a))
    out = []
    for group in by_edge.values():
        if len(group) < 3:
            continue
        design_edges = [d["x"] for d, _ in group]
        if max(design_edges) - min(design_edges) > 4:
            continue                      # not actually aligned in the design
        attempt_edges = [a["x"] for _, a in group]
        spread = max(attempt_edges) - min(attempt_edges)
        offset = int(round(sum(attempt_edges) / len(attempt_edges))) - design_edges[0]
        if spread >= 8:
            out.append({"kind": "ragged", "n": len(group), "design": css(design_edges[0]),
                        "low": css(min(attempt_edges)), "high": css(max(attempt_edges)),
                        "y": css(min(d["y"] for d, _ in group)), "score": spread * len(group)})
        elif abs(offset) >= 6:
            out.append({"kind": "shifted", "n": len(group), "design": css(design_edges[0]),
                        "attempt": css(design_edges[0] + offset), "offset": css(offset),
                        "y": css(min(d["y"] for d, _ in group)), "score": abs(offset) * len(group)})
    out.sort(key=lambda f: -f["score"])
    return out[:limit]


def _alignment_sentence(f):
    if f["kind"] == "ragged":
        return ("{} elements share a left edge at x {}px in the design; in the attempt they start "
                "anywhere between x {}px and x {}px. They belong to one container, so align them "
                "there rather than moving each one.".format(
                    f["n"], f["design"], f["low"], f["high"]))
    return ("{} elements share a left edge in the design at x {}px, but sit at x {}px in the "
            "attempt, {}px to the {}. That is one container's padding or margin, not {} separate "
            "moves.".format(f["n"], f["design"], f["attempt"], abs(f["offset"]),
                            "right" if f["offset"] > 0 else "left", f["n"]))


def _spacing_gaps(matched, css, size, limit=3):
    """Vertical gaps between stacked elements, design against attempt.

    The report could say where every element sits and how big it is, and still never
    say the one thing a person looking at the page says first: the spacing is wrong.
    A gap is the distance between the bottom of one element and the top of the next,
    so unlike a position it does not move when the whole page shifts, and it is the
    number that maps onto the margin or padding actually being edited.
    """
    stacked = sorted(matched, key=lambda pair: pair[0]["y"])
    out, seen = [], set()
    for i, (d0, a0) in enumerate(stacked):
        # The nearest element below this one that shares a column, not simply the next
        # one by y. Three cards side by side interleave when sorted, so comparing only
        # neighbours in that order skips most of the gaps a person would actually see.
        best = None
        for d1, a1 in stacked[i + 1:]:
            if d1["y"] < d0["y"] + d0["h"]:
                continue
            overlap = min(d0["x"] + d0["w"], d1["x"] + d1["w"]) - max(d0["x"], d1["x"])
            if overlap < 0.4 * min(d0["w"], d1["w"]):
                continue
            if best is None or d1["y"] < best[0]["y"]:
                best = (d1, a1)
        if best is None:
            continue
        d1, a1 = best
        gap_ref, gap_att = d1["y"] - (d0["y"] + d0["h"]), a1["y"] - (a0["y"] + a0["h"])
        if gap_ref < 0 or gap_att < 0:
            continue
        diff = css(gap_att - gap_ref)
        if abs(diff) < 5:
            continue
        # Siblings on one row (three cards) each find the same element below them and
        # would each report the same gap. Report it once.
        key = (css(d0["y"] + d0["h"]), css(gap_ref), css(gap_att))
        if key in seen:
            continue
        seen.add(key)
        # Two text lines that start at the same left edge and sit close together are
        # consecutive lines of one block, so their gap is line-height rather than a
        # margin. Worth saying, because it is the one property a model reaches for
        # globally, and doing that has repeatedly broken pages that already matched.
        # Both gaps have to look like line spacing, not just the design's. A design gap
        # of 22px that became 113px is not a line height five times too big, it is
        # something inserted between the lines or a pair that should not have matched,
        # and calling it line-height would send the next round to set exactly the wrong
        # property. That falls through to the plain gap sentence, which stays true.
        line_h = min(d0["h"], d1["h"])
        leading = (d0["kind"] == "text" and d1["kind"] == "text"
                   and abs(d0["x"] - d1["x"]) <= 8
                   and gap_ref <= 1.5 * line_h and gap_att <= 2.5 * line_h)
        out.append({"y": css(d0["y"] + d0["h"]), "where": _where(d0, size),
                    "design": css(gap_ref), "attempt": css(gap_att), "diff": diff,
                    "kind": d1["kind"], "leading": leading})
    out.sort(key=lambda s: -abs(s["diff"]))
    return out[:limit]


def _row_gaps(matched, css, size, limit=2):
    """Horizontal gaps between elements sitting in a row, design against attempt.

    The vertical version above catches stacked content, which is most of a page, but a
    row of navigation links is spaced along the other axis and nothing measured it. The
    report could only say each link "sits 43px to the right", once per link, which reads
    as five separate faults when it is one: the gap between them, or where the row
    starts. Same shape as the vertical pass, x and y exchanged.
    """
    rows = {}
    for d, a in matched:
        rows.setdefault(round((d["y"] + d["h"] / 2.0) / 12.0), []).append((d, a))
    out = []
    for group in rows.values():
        if len(group) < 3:
            continue
        group.sort(key=lambda pair: pair[0]["x"])
        design_gaps, attempt_gaps = [], []
        for i in range(len(group) - 1):
            (d0, a0), (d1, a1) = group[i], group[i + 1]
            gd = d1["x"] - (d0["x"] + d0["w"])
            ga = a1["x"] - (a0["x"] + a0["w"])
            if gd < 0 or ga < 0:
                continue
            design_gaps.append(gd)
            attempt_gaps.append(ga)
        if len(design_gaps) < 2:
            continue
        med_d = sorted(design_gaps)[len(design_gaps) // 2]
        med_a = sorted(attempt_gaps)[len(attempt_gaps) // 2]
        diff = css(med_a - med_d)
        if abs(diff) < 5:
            continue
        out.append({"n": len(group), "y": css(group[0][0]["y"]),
                    "where": _where(group[0][0], size), "design": css(med_d),
                    "attempt": css(med_a), "diff": diff,
                    "score": abs(diff) * len(group)})
    out.sort(key=lambda f: -f["score"])
    return out[:limit]


def _row_gap_sentence(f):
    return ("The {} items in the row at y {}px (the {} of the page) sit {}px apart; the design "
            "spaces them {}px apart, so the row is {} than it should be. That is the gap or "
            "padding in their container, not {} separate moves.".format(
                f["n"], f["y"], f["where"], f["attempt"], f["design"],
                "more spread out" if f["diff"] > 0 else "more tightly packed", f["n"]))


def _spacing_sentence(s):
    if s.get("leading"):
        return ("The lines of text at y {}px (the {} of the page) sit {}px apart; the design has "
                "{}px. That is line-height on that block alone, never on every text "
                "element.".format(s["y"], s["where"], s["attempt"], s["design"]))
    return ("The gap above the {} at y {}px (the {} of the page) is {}px; the design has {}px, "
            "so it is {}px too {}. Change the margin or padding there, not the element "
            "itself.".format("text" if s["kind"] == "text" else "box", s["y"], s["where"],
                             s["attempt"], s["design"], abs(s["diff"]),
                             "big" if s["diff"] > 0 else "small"))


def _element_sentence(g):
    """One plain sentence per group of elements that miss in the same way."""
    it, n = g[0], len(g)
    asp = it["aspects"]
    if n > 1:
        lead = "{} {}".format(n, "text elements" if it["kind"] == "text" else "boxes")
        at = "around y {}px (the {} of the page)".format(it["y"], it["where"])
        verb, verb_sit, them = "are", "sit", "them"
    else:
        lead = "The text" if it["kind"] == "text" else "The box"
        at = "at x {}, y {} ({}x{}px, the {} of the page)".format(
            it["x"], it["y"], it["w"], it["h"], it["where"])
        verb, verb_sit, them = "is", "sits", "it"

    if "missing" in asp:
        return "Nothing in the attempt matches {} {}.".format(
            "these elements" if n > 1 else lead[0].lower() + lead[1:], at)

    clauses = []
    if "width" in asp:
        clauses.append("{} about {:.0f}% {} ({}px against {}px)".format(
            verb, abs(asp["width"]), "wider" if asp["width"] > 0 else "narrower",
            it["aw"], it["w"]))
    if "height" in asp:
        clauses.append("{} {}px {}".format(
            "" if clauses else verb, abs(asp["height"]),
            "taller" if asp["height"] > 0 else "shorter").strip())
    if "position" in asp:
        dx, dy = asp["position"]
        moves = []
        if abs(dx) >= 4:
            moves.append("{}px to the {}".format(abs(dx), "right" if dx > 0 else "left"))
        if abs(dy) >= 4:
            moves.append("{}px {}".format(abs(dy), "lower" if dy > 0 else "higher"))
        clauses.append("{} {}".format(verb_sit, " and ".join(moves)))

    if "position" in asp:
        sentence = "{} {} {}, compared with the design.".format(lead, at, ", ".join(clauses))
    else:
        sentence = "{} {} {} than in the design.".format(lead, at, ", ".join(clauses))
    if "width" in asp:
        sentence += " Check font size, weight and letter spacing there."
    elif "height" in asp:
        sentence += " Check padding, line height and font size there."
    return " ".join(sentence.split())


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


def score_images(ref_img, att_img, px_per_css=1.0, ignore=None, design_fonts=None,
                 regions=True, deep=True, how=None):
    """Compare two same-size RGB images and return the full score report.

    px_per_css converts image pixels back to CSS pixels for the sentences in the
    report, since that is the unit the person or model will edit.

    `ignore` is a mask of pixels that cannot be measured because the page moves
    there. Both images are flattened to the page colour inside it, so the area
    holds no content, no edges and no difference, and every part of the score is
    computed on what is left. Scoring motion would otherwise dominate: on one real
    page only 6.5% of the pixels were drawn at all, and a quarter of those were the
    animation, which read as a 22 point gap that no change could ever close.
    """
    ref = np.asarray(ref_img.convert("RGB"), dtype=np.float64)
    att = np.asarray(att_img.convert("RGB").resize(ref_img.size, Image.LANCZOS), dtype=np.float64)
    ignored_share = 0.0
    if ignore is not None and ignore.any():
        ground_rgb = _ground_colour(ref)
        ref, att = ref.copy(), att.copy()
        ref[ignore] = ground_rgb
        att[ignore] = ground_rgb
        ignored_share = float(ignore.mean() * 100)

    d = ref - att
    per_px = (d ** 2).sum(axis=2) / 3.0          # the jelly-lab MSE, kept for continuity
    mse = float(per_px.mean())
    rmse = float(np.sqrt((d ** 2).mean()))

    g_ref, g_att = _gray(ref), _gray(att)
    ssim_map = _ssim(g_ref, g_att)

    ground = _ground_colour(ref)
    m_ref, m_att = _ink_mask(ref), _ink_mask(att)
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
    palette_dist = _palette_distance(ref_cols, att_cols)

    # The page behind the content is compared too, and separately. The palette is
    # sampled inside the drawn content, and the content no longer includes the
    # background now that the mask reads local contrast, so without this a rebuild
    # could put the whole page on the wrong colour and pay nothing for it: a clearly
    # over-saturated gradient scored 99.2. Whichever is worse governs, because a right
    # background does not excuse wrong text and right text does not excuse a wrong page.
    behind = ~m_union
    background_dist = (float(np.sqrt(((ref[behind] - att[behind]) ** 2).sum(axis=1)).mean())
                       if behind.sum() >= 64 else 0.0)
    colour_dist = max(palette_dist, background_dist)

    # Where the background is most wrong, and what colour each page is there. The two
    # border colours are not enough on their own: a gradient can match at the edge the
    # ground is read from and be far off in the middle, and naming two near-identical
    # hexes would send the next round chasing a difference of two.
    background_where, background_ref_hex, background_att_hex = None, None, None
    if behind.sum() >= 256:
        per_px_rgb = np.sqrt(((ref - att) ** 2).sum(axis=2))
        h, w = behind.shape
        worst, worst_cell = -1.0, None
        for r in range(4):
            for c in range(4):
                y0, y1 = h * r // 4, h * (r + 1) // 4
                x0, x1 = w * c // 4, w * (c + 1) // 4
                sel = behind[y0:y1, x0:x1]
                if sel.sum() < 64:
                    continue
                mean = float(per_px_rgb[y0:y1, x0:x1][sel].mean())
                if mean > worst:
                    worst, worst_cell = mean, (r, c, y0, y1, x0, x1, sel)
        if worst_cell is not None:
            r, c, y0, y1, x0, x1, sel = worst_cell
            background_where = _cell_name({"row": r, "col": c})
            background_ref_hex = "#{:02X}{:02X}{:02X}".format(
                *[int(v) for v in ref[y0:y1, x0:x1][sel].mean(axis=0)])
            background_att_hex = "#{:02X}{:02X}{:02X}".format(
                *[int(v) for v in att[y0:y1, x0:x1][sel].mean(axis=0)])
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
            # Kept apart so the report can say which of the two is wrong. A page whose
            # content colours are right but whose background is off needs a different
            # sentence from one that invented a colour, and the model can act on both.
            "palette_only_distance": round(palette_dist, 2),
            "background_distance": round(background_dist, 2),
            "background_where": background_where,
            "background_reference_hex": background_ref_hex,
            "background_attempt_hex": background_att_hex,
            "colour_distance_where_both_drew": round(overlap_dist, 2),
            "edge_correlation": round(edge_corr, 4),
            "ink_coverage_reference": round(coverage_ref * 100, 2),
            "ink_coverage_attempt": round(coverage_att * 100, 2),
            "design_not_drawn_pct": round((1 - coverage) * 100, 2),
            "design_not_drawn_where": missed_where,
        },
        "size": [int(ref.shape[1]), int(ref.shape[0])],
        "ignored_pct": round(ignored_share, 2),
        "ground": ["#{:02X}{:02X}{:02X}".format(*[int(v) for v in ground])],
        "ground_attempt": "#{:02X}{:02X}{:02X}".format(
            *[int(v) for v in _ground_colour(att)]),
        "colours": {"reference": ref_cols, "attempt": att_cols},
        "regions": cells,
        "worst_regions": [{"where": _cell_name(c), "rmse": c["rmse"]} for c in worst],
    }
    # A crop being scored to rank one element or one region needs the four numbers and
    # nothing else. Everything below writes sentences about a whole page, and running it
    # per crop cost more than the scores it was ranking.
    if not deep:
        report["problems"] = []
        return report, per_px, ground

    off = _offsets(g_ref, g_att)
    if off["vertical"]:
        v = off["vertical"]
        v["from_css_y"] = int(round(v["from_y"] / px_per_css))
        v["css_shift"] = int(round(v["shift"] / px_per_css))
        v["where"] = _ROW_WORDS[min(3, v["from_y"] * 4 // max(1, ref.shape[0]))]
    if off["horizontal"]:
        off["horizontal"]["css_shift"] = int(round(off["horizontal"]["shift"] / px_per_css))
    report["offsets"] = off
    shift_css = (off["vertical"] or {}).get("css_shift", 0)
    try:
        report["elements"] = compare_elements(g_ref, g_att, px_per_css, shift_css, how=how)
        report["element_scores"] = _element_scores(
            ref_img, att_img, g_ref, g_att, px_per_css, how=how)
    except ImportError:
        report["elements"] = None  # scipy missing: page-wide feedback only
        report["element_scores"] = []
    # Known only when the design is a live page, and only used to name the typeface.
    report["design_fonts"] = list(design_fonts or [])
    report["artwork"] = _artwork(ref, att)
    report["region_scores"] = _region_scores(ref_img, att_img) if regions else []
    report["problems"] = _problems(report)
    return report, per_px, ground


_REGION_NAMES = [["top left", "top centre", "top right"],
                 ["middle left", "middle centre", "middle right"],
                 ["bottom left", "bottom centre", "bottom right"]]


def _element_score_sentences(report, behind=ELEMENT_BEHIND, moved_by=ELEMENT_MOVED,
                             already=()):
    """Name the weakest elements, and say whether each needs moving or rebuilding.

    `already` is the elements the geometry lines above have named. Those lines are
    paired by position and these by appearance, so on an element the two pairings
    disagree about, the report would otherwise carry both answers: one saying the box
    is 22% narrower, the next saying nothing in the attempt resembles it. The specific
    line wins, and this adds the ranking and the diagnosis for everything else.
    """
    scored = report.get("element_scores") or []
    page = report.get("match", 0.0)
    seen = {(int(x), int(y)) for x, y in already}
    out = []
    for e in scored[:4]:
        if len(out) >= 2:
            break
        if e["in_place"] > page - behind:
            continue
        if any(abs(e["x"] - sx) <= 4 and abs(e["y"] - sy) <= 4 for sx, sy in seen):
            continue
        what = "{}x{}px {} at x {}, y {} ({} of the page)".format(
            e["w"], e["h"], "text" if e["kind"] == "text" else "box", e["x"], e["y"],
            e["where"])
        if e["as_built"] is None:
            out.append(
                "The {} scores {:.1f} on its own against {:.1f} for the page, and nothing in "
                "the attempt was recognisable as it. Draw it first."
                .format(what, e["in_place"], page))
            continue
        # Scoring it against what it was paired to takes position and size out, so a
        # large gap between the two says the element itself is right and only its
        # geometry is wrong. Which part of the geometry is a separate question: an
        # element can be the wrong size without having moved, and saying "the miss is
        # inside it" about something that scores 90 like for like is simply false.
        # The gap between the two scores decides the diagnosis; the measurements below
        # only explain it. So once the gap says geometry, every displacement counts,
        # however small: a 4px drop is a quarter of the height of a line of text, and
        # holding it to the same threshold as a hero panel reported an element that
        # scores 92 against its own pair as wrong on the inside.
        gap = e["as_built"] - e["in_place"]
        near = moved_by if gap < behind else 1
        geometry, sized = [], []
        if max(abs(e["moved"][0]), abs(e["moved"][1])) >= near:
            geometry.append("sits {} where the design has it".format(_by(e["moved"])))
        if abs(e["sized"][0]) >= (6 if gap < behind else 2):
            sized.append("{:.0f}% {}".format(
                abs(e["sized"][0]), "narrower" if e["sized"][0] > 0 else "wider"))
        if abs(e["sized"][1]) >= (6 if gap < behind else 2):
            sized.append("{:.0f}% {}".format(
                abs(e["sized"][1]), "shorter" if e["sized"][1] > 0 else "taller"))
        if sized:
            geometry.append("is drawn " + " and ".join(sized))
        if e["as_built"] >= ELEMENT_BUILT and gap >= behind and geometry:
            out.append(
                "The {} scores {:.1f} where the design puts it but {:.1f} against what it was "
                "paired to, so the element itself is right and its geometry is not: it {}. "
                "Fix that and leave the inside of it alone."
                .format(what, e["in_place"], e["as_built"], " and ".join(geometry)))
        elif gap >= behind and geometry:
            out.append(
                "The {} scores {:.1f} where the design puts it and {:.1f} against what it was "
                "paired to, so part of the miss is geometry and part is the element itself. It "
                "{}. Both need work."
                .format(what, e["in_place"], e["as_built"], " and ".join(geometry)))
        else:
            out.append(
                "The {} scores {:.1f} on its own against {:.1f} for the page, and {:.1f} even "
                "compared like for like, so the miss is inside it rather than in where it sits."
                .format(what, e["in_place"], page, e["as_built"]))
    if out:
        # Attached to the last sentence rather than numbered on its own: the loop is told
        # to work the numbered list in order, and a caveat is not a thing to fix.
        out[-1] += (
            " Element scores are comparable with each other and not with the page total, "
            "which is measured over the whole page at once, so expect the total to move by a "
            "fraction of an element's share of the pixels even when the element is fixed "
            "outright.")
    return out


def _by(moved):
    """How far the attempt put an element from where the design has it, in words.

    The sign is the direction the attempt went, so the instruction is the opposite of it.
    """
    dx, dy = moved
    parts = []
    if dx:
        parts.append("{}px {}".format(abs(dx), "right" if dx > 0 else "left"))
    if dy:
        parts.append("{}px {}".format(abs(dy), "below" if dy > 0 else "above"))
    return " and ".join(parts) or "exactly on top"


def _element_scores(ref_img, att_img, g_ref, g_att, px_per_css=1.0, how=None,
                    limit=ELEMENT_SCORES):
    """Score each element of the design on its own, twice: in place, and as built.

    A ninth of the page is a boundary nobody drew: it cuts through a card and averages
    its title with the gap beside it, so the weakest ninth names a part of the canvas
    rather than a thing to fix. An element is the thing to fix.

    Two scores, because they come apart and the difference is the instruction. `in place`
    compares the design's own rectangle in both images, so it falls when anything there
    is wrong, including the element having gone somewhere else. `as built` compares the
    element with whatever it was paired to, scaled to the same size, so it says whether
    the thing itself is right. Built right and in the wrong place is a move; built wrong
    is a redraw; and until now both read as one low number.
    """
    design = _elements(g_ref)
    matched, missing = _match_elements(design, _elements(g_att), g_ref, g_att,
                                       how=how or IDENTITY_MATCH)
    partner = {(d["x"], d["y"], d["w"], d["h"]): a for d, a in matched}
    size = (g_ref.shape[1], g_ref.shape[0])
    out = []
    for d in design:
        if d["w"] < ELEMENT_MIN or d["h"] < ELEMENT_MIN:
            continue
        box = (d["x"], d["y"], d["x"] + d["w"], d["y"] + d["h"])
        crop = ref_img.crop(box)
        place = score_images(crop, att_img.crop(box), regions=False, deep=False)[0]["match"]
        a = partner.get((d["x"], d["y"], d["w"], d["h"]))
        built = None
        if a is not None:
            shot = att_img.crop((a["x"], a["y"], a["x"] + a["w"], a["y"] + a["h"]))
            if shot.size[0] >= 2 and shot.size[1] >= 2:
                built = score_images(crop, shot.resize(crop.size, Image.LANCZOS),
                                     regions=False, deep=False)[0]["match"]
        out.append({
            "kind": d["kind"], "where": _where(d, size),
            "x": int(round(d["x"] / px_per_css)), "y": int(round(d["y"] / px_per_css)),
            "w": int(round(d["w"] / px_per_css)), "h": int(round(d["h"] / px_per_css)),
            "in_place": place, "as_built": built,
            "moved": None if a is None else [int(round((a["x"] - d["x"]) / px_per_css)),
                                             int(round((a["y"] - d["y"]) / px_per_css))],
            # Positive means the attempt drew it smaller than the design.
            "sized": None if a is None else [
                round(100.0 * (d["w"] - a["w"]) / max(1, d["w"]), 1),
                round(100.0 * (d["h"] - a["h"]) / max(1, d["h"]), 1)],
            "area": d["w"] * d["h"],
        })
    # Worst first, but weighted by size: a 20px label scoring 40 is not the page's
    # problem when a card the size of a quarter of it scores 55.
    out.sort(key=lambda e: (100.0 - e["in_place"]) * math.sqrt(e["area"]), reverse=True)
    return out[:limit]


def _region_scores(ref_img, att_img, n=3):
    """Score each ninth of the page on its own.

    One number over a whole page is an average, and an average hides where the work is.
    On a large rebuild the page scored 70.7 while its own regions ran from 53.7 to
    79.6: a quarter of the range invisible in the headline. It also explains why rounds
    stall on a big page, because a region worth eleven percent of the pixels can be
    fixed completely and move the total by under three points.
    """
    W, H = ref_img.size
    out = []
    for r in range(n):
        for c in range(n):
            box = (W * c // n, H * r // n, W * (c + 1) // n, H * (r + 1) // n)
            if box[2] - box[0] < 24 or box[3] - box[1] < 24:
                continue
            sub, _, _ = score_images(ref_img.crop(box), att_img.crop(box), regions=False,
                                     deep=False)
            out.append({"row": r, "col": c, "where": _REGION_NAMES[r][c],
                        "match": sub["match"]})
    return out


def _artwork(ref, att, cells=6):
    """Parts of the design that are pictures rather than layout.

    A photograph, a rendered globe, a gradient bloom: code does not reproduce these,
    and a loop that is not told so spends every round closing a gap that only an
    exported asset closes. Artwork reads as many distinct colours and dense detail at
    once, which flat UI never has: a card is two or three colours and clean edges, and
    text is dense but nearly monochrome.
    """
    h, w = ref.shape[:2]
    found = []
    for r in range(cells):
        for c in range(cells):
            y0, y1 = h * r // cells, h * (r + 1) // cells
            x0, x1 = w * c // cells, w * (c + 1) // cells
            dcell, acell = ref[y0:y1, x0:x1], att[y0:y1, x0:x1]
            if dcell.size < 300:
                continue

            def richness(block):
                q = (block.astype(np.int32) >> 4)
                keys = (q[:, :, 0] << 8) | (q[:, :, 1] << 4) | q[:, :, 2]
                colours = len(np.unique(keys))
                g = _gray(block)
                edge = float(np.abs(np.diff(g, axis=1)).mean() + np.abs(np.diff(g, axis=0)).mean())
                return colours, edge

            dcol, dedge = richness(dcell)
            acol, aedge = richness(acell)
            # Many colours and busy at the same time, and the attempt nowhere near it.
            # Measured across the designs on hand: ordinary UI tops out around 220
            # colours with a median edge near 5, while a rendered HUD runs 269 colours
            # at 13.9. Both conditions have to hold at once, because a single gradient
            # panel is colourful without being busy and dense text is busy without
            # being colourful.
            if dcol >= 250 and dedge >= 9.0 and (acol < dcol * 0.5 or aedge < dedge * 0.5):
                found.append({"row": r, "col": c, "colours": int(dcol)})
    if not found:
        return None
    share = 100.0 * len(found) / float(cells * cells)
    rows = sorted({f["row"] for f in found})
    cols = sorted({f["col"] for f in found})
    where = "{}, {}".format(_ROW_WORDS[min(3, rows[len(rows) // 2] * 4 // cells)],
                            _COL_WORDS[min(3, cols[len(cols) // 2] * 4 // cells)])
    return {"share": round(share, 1), "where": where,
            "colours": max(f["colours"] for f in found),
            "cells": [[f["row"], f["col"]] for f in found], "grid": cells,
            "box": [int(min(f["col"] for f in found) * w / cells),
                    int(min(f["row"] for f in found) * h / cells),
                    int((max(f["col"] for f in found) + 1) * w / cells),
                    int((max(f["row"] for f in found) + 1) * h / cells)]}


def _problems(report):
    """Turn the numbers into the two or three sentences worth acting on."""
    out = []
    said = set()          # the kinds of fault actually named, for the coverage check below
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
            said.add("shift")
        else:
            out.append(
                "From about {}px down (the {} of the page), content sits about {}px {} than in the "
                "design. Something above that point is missing, extra or the wrong height. Fix that "
                "first: most of the error below it follows from this one shift.".format(
                    v["from_css_y"], v["where"], abs(v["css_shift"]), way))
            said.add("shift")
    if off.get("horizontal"):
        hs = off["horizontal"]["css_shift"]
        out.append("Content sits about {}px further {} than in the design; check the container "
                   "width, side padding and centring.".format(abs(hs), "left" if hs > 0 else "right"))
        said.add("shift")

    if comp.get("coverage", 100) < 97:
        if off.get("vertical") or off.get("horizontal"):
            tail = "Part of that is the shift; whatever is still uncovered after fixing it is missing."
        else:
            tail = "Something is missing there, not just misplaced."
        out.append("{:.0f}% of the design has nothing drawn near it, mostly in the {} of the page. "
                   "{}".format(raw["design_not_drawn_pct"], raw["design_not_drawn_where"], tail))
        said.add("coverage")

    wording = {
        "shape": "The silhouette is off: {:.0f}% of the drawn area overlaps the design. "
                 "The design covers {:.1f}% of the page, the attempt covers {:.1f}%. {}",
        "colour": "The palette is off by {:.0f} on a 0 to 441 scale: a design colour has "
                  "no close match in the attempt, or the attempt invented one.",
        "structure": "Local structure does not line up (SSIM {:.3f}). Edges and gradients sit in "
                     "the wrong places even where the colours are close.",
        "detail": "Detail density does not match (edge correlation {:.2f}). {}",
    }

    # Colour is reported whatever else is wrong. It used to be reached only through the
    # ranked loop below, which breaks as soon as there are element lines, and on a real
    # page there are always element lines. So a page could score 64.5 on colour and be
    # told nothing about colour at all, round after round, while the loop nudged text.
    # A wrong page colour is also one of the cheapest things to fix, so it goes early.
    if comp.get("colour", 100) < 90:
        bg = raw.get("background_distance", 0.0)
        pal = raw.get("palette_only_distance", 0.0)
        if bg >= max(pal, 8.0) and raw.get("background_where"):
            out.append(
                "The page behind the content is the wrong colour. Where it is furthest off, the {} "
                "of the page, the design is {} and the attempt is {}. Fix the page background, "
                "gradient or body colour before the elements sitting on it.".format(
                    raw["background_where"], raw["background_reference_hex"],
                    raw["background_attempt_hex"]))
            said.add("colour")
        elif pal >= 8.0:
            out.append(wording["colour"].format(pal))
            said.add("colour")

    els = report.get("elements") or {}
    glyph = els.get("glyph") or {}
    typeface = glyph.get("lines", 0) >= 8 and glyph.get("weak_share", 0) >= 0.15
    if typeface:
        out.append("The letters themselves do not match: {:.0f}% of the text lines that are in the "
                   "right place still differ in shape, so the font family or weight is wrong. Fix "
                   "the font before moving any boxes.".format(glyph["weak_share"] * 100))
        said.add("type")
        fonts = report.get("design_fonts") or []
        if fonts:
            out.append("The design's own source asks for {}. Use that rather than guessing from "
                       "the letter shapes; if it is not installed, say so instead of "
                       "substituting.".format(", ".join(fonts)))

    # Type before the element boxes: a wrong font-size is reported again by every box
    # around it as a wrong width and a wrong height, so fixing it first removes several
    # of the complaints below rather than adding to them.
    type_findings = els.get("type") or []
    out.extend(_type_sentence(f) for f in type_findings)
    emphasis_findings = els.get("emphasis") or []
    out.extend(_emphasis_sentence(f) for f in emphasis_findings)
    if type_findings or emphasis_findings:
        said.add("type")
    # Alignment before the individual elements too: a ragged column is one container,
    # and naming it here stops the list below repeating it once per element.
    align_findings = els.get("alignment") or []
    out.extend(_alignment_sentence(f) for f in align_findings)
    if align_findings:
        said.add("align")

    groups = (els.get("groups") or [])[:4]
    element_lines = [_element_sentence(g) for g in groups]
    out.extend(element_lines)
    # Ranked elements sit with the named ones, because a report is read in order and a
    # weakest-element line at number sixteen is a line nobody acts on.
    element_score_lines = _element_score_sentences(
        report, already=[(i["x"], i["y"]) for g in groups for i in g])
    out.extend(element_score_lines)
    hollow_findings = els.get("hollow") or []
    art_cells = set()
    if report.get("artwork"):
        art_cells = {tuple(c) for c in report["artwork"].get("cells", [])}
    if art_cells:
        # Only a large element sitting in artwork defers to the artwork line. The cells
        # are coarse, so a photographic background puts half the page inside one, and
        # filtering on the cell alone silenced a row of empty icon wells that had
        # nothing to do with the artwork.
        grid = report["artwork"].get("grid", 6)
        w_img, h_img = report["size"]
        page_area = float(max(w_img * h_img, 1))
        hollow_findings = [
            f for f in hollow_findings
            if not ((int(f["cy"] * grid / max(h_img, 1)),
                     int(f["cx"] * grid / max(w_img, 1))) in art_cells
                    and (f["w"] * f["h"]) / page_area > 0.06)]
    summary = _hollow_summary(hollow_findings)
    if summary:
        out.append(summary)
    else:
        out.extend(_hollow_sentence(f) for f in hollow_findings[:2])
    if element_lines or hollow_findings or element_score_lines:
        said.add("element")
    # Spacing after the elements themselves: a gap is only worth changing once the
    # things on either side of it are the right size.
    spacing_findings = (els.get("spacing") or [])[:2]
    out.extend(_spacing_sentence(s) for s in spacing_findings)
    row_findings = (els.get("row_spacing") or [])[:2]
    out.extend(_row_gap_sentence(f) for f in row_findings)
    if spacing_findings or row_findings:
        said.add("spacing")

    for name, value in ranked[:2]:
        if element_lines or typeface:
            break
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

    if report["worst_regions"] and not element_lines:
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

    regions = report.get("region_scores") or []
    # Elements first: a ninth is a boundary nobody drew, and saying both crowds the
    # report with two answers to the same question.
    if len(regions) >= 4 and not element_score_lines:
        # Artwork regions are left out of the recommendation. The globe scored worst on
        # one page, and sending the next round there would contradict the artwork line
        # telling it not to chase a render.
        art = report.get("artwork") or {}
        art_cells = {tuple(c) for c in art.get("cells", [])}
        grid = art.get("grid", 6)
        def mostly_artwork(g):
            if not art_cells:
                return False
            lo_r, hi_r = g["row"] * grid // 3, ((g["row"] + 1) * grid - 1) // 3
            lo_c, hi_c = g["col"] * grid // 3, ((g["col"] + 1) * grid - 1) // 3
            spans = [(r, c) for r in range(lo_r, hi_r + 1) for c in range(lo_c, hi_c + 1)]
            hit = sum(1 for cell in spans if cell in art_cells)
            return spans and hit >= 0.6 * len(spans)

        workable = [g for g in regions if not mostly_artwork(g)]
        if len(workable) >= 2:
            worst = min(workable, key=lambda g: g["match"])
            best = max(workable, key=lambda g: g["match"])
            # Only when the page is genuinely uneven; on a level page this says nothing
            # and would crowd out the specific findings.
            if best["match"] - worst["match"] >= 15:
                out.append(
                    "The page is uneven. Scored ninth by ninth, the {} is the weakest part you "
                    "can act on at {:.1f}, against {:.1f} for the {}. Work there first. These "
                    "are comparable with each other and not with the page total, which is "
                    "measured over the whole page at once. Expect the total to crawl even when "
                    "a region improves a lot, because a region is only a fraction of the "
                    "page.".format(worst["where"], worst["match"], best["match"], best["where"]))

    art = report.get("artwork")
    if art:
        out.append(
            "About {:.0f}% of the design is artwork rather than layout, around the {} of the "
            "page: {} distinct colours and dense detail. If it is a 3D object, a globe, a device, "
            "a product, build it as one with three.js rather than faking it flat: it will read "
            "correctly even though the score may barely move, because what separates a render "
            "from a picture of one is fine low-contrast detail, and that is the part this "
            "measurement sees least well. If it is a photograph or an illustration, no code "
            "reaches it: export it and place it as an image. It covers roughly x {} to {}, y {} "
            "to {}, so that is the region to crop. Match its brightness to the design rather "
            "than assuming detailed means bright, and either way do not spend round after round "
            "nudging it.".format(art["share"], art["where"], art["colours"],
                           art["box"][0], art["box"][2], art["box"][1], art["box"][3]))
        said.add("artwork")

    out.extend(_unexplained(report, said))
    return out


# Which kinds of finding count as explaining a low component. A component scoring badly
# with none of these present means the report can see the fault but cannot say what it is.
EXPLAINED_BY = {
    "colour": {"colour"},
    "coverage": {"coverage", "element"},
    "shape": {"element", "align", "spacing", "coverage"},
    "structure": {"element", "type", "shift", "align"},
    "detail": {"type", "element", "coverage", "artwork"},
    "structure": {"element", "type", "shift", "align", "artwork"},
}
UNEXPLAINED_BELOW = 80.0


def _unexplained(report, said):
    """Say so when a component scores badly and nothing above accounts for it.

    This is the check that would have caught the colour blind spot on its own. Colour
    scored 64.5 for six rounds while every sentence in the report was about text, so a
    quarter of the loss had no sentence attached and nobody noticed, because nothing was
    watching for the absence. A score with no explanation is a fault the report cannot
    name yet, and saying that out loud is more use than silence: it tells the person to
    look at the difference map, and it stops the next round believing the list is
    complete.
    """
    # What was actually said, not what could be inferred. Deriving the kinds from the
    # report would defeat the point: the colour key is inferred from the colour score,
    # so colour would always look explained precisely when it is low.
    weak = []
    for name, value in (report.get("components") or {}).items():
        if value >= UNEXPLAINED_BELOW:
            continue
        if said & EXPLAINED_BY.get(name, set()):
            continue
        weak.append((name, value))
    if not weak:
        return []
    weak.sort(key=lambda p: p[1])
    named = ", ".join("{} at {:.1f}".format(n, v) for n, v in weak)
    return ["Scoring badly with nothing above to explain it: {}. The difference is real "
            "and the report cannot name it yet, so read the difference map for this one "
            "rather than trusting the list.".format(named)]


def diff_heatmap(per_px, out_png, ignore=None):
    """Red-orange heatmap, the same mapping the jelly lab used.

    Areas excluded for moving are painted slate blue, so they read as "not
    measured" rather than as a perfect match.
    """
    intensity = np.minimum(255.0, np.sqrt(per_px) * 3.0)
    h, w = intensity.shape
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:, :, 0] = intensity.astype(np.uint8)
    img[:, :, 1] = (intensity * 0.35).astype(np.uint8)
    if ignore is not None and ignore.any():
        img[ignore] = (38, 58, 92)
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
    ]
    st = run.get("stability") or {}
    if st.get("unscoreable"):
        lines += [
            "{:.0f}% of this page moves between screenshots, which is too much to score.".format(
                st["ignored_pct"]),
            "Freeze the animation or the video, or point at a section that holds still;",
            "until then the number below is mostly measuring motion.",
            "",
        ]
    elif st.get("ignored_pct"):
        lines += [
            "{:.1f}% of this page moves between screenshots (an animation, a carousel or live".format(
                st["ignored_pct"]),
            "data). That area is excluded from the score and shown slate blue in the difference",
            "map, so do not try to fix anything there. With it excluded, two screenshots of the",
            "page score {:.1f} against each other, which is the ceiling here.".format(st["match"]),
            "",
        ]
    lines += [
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


# ----------------------------------------------------------------- the agent
#
# The loop needs some model to rewrite the page. Any of these will do, and the
# first one that is actually available is used unless SPOT_ON_AGENT names one.
# CLI agents can open the three images themselves; the API ones are sent the
# images inline. Raw HTTP on purpose: this tool stays installable with numpy,
# pillow and scipy, rather than requiring every provider's SDK.

AGENT_ORDER = ("claude", "codex", "gemini", "anthropic-api", "openai-api", "ollama")

AGENT_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex CLI",
    "gemini": "Gemini CLI",
    "anthropic-api": "Anthropic API",
    "openai-api": "OpenAI API",
    "ollama": "Ollama (local)",
}

# Who an attempt may claim to be from. The page shows this, and an endpoint that took
# any string let a page in the browser store one through the loopback server.
SOURCES = ("manual", "session", "starter", "chrome-builtin") + tuple(AGENT_LABELS)

DEFAULT_MODELS = {
    "claude": "sonnet",
    "anthropic-api": "claude-sonnet-5",
    "openai-api": "gpt-4o",
    "ollama": "llava",
}

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


def _cli_for(agent):
    return {"claude": "claude", "codex": "codex", "gemini": "agy"}.get(agent)


# Some CLIs reach for a tool unprompted and stall when headless mode auto-denies the
# permission, answering with nothing at all and an explanation only on stderr. Asking
# them to answer directly is enough, and it does not weaken anyone's permissions: the
# alternative the CLI suggests is --dangerously-skip-permissions, which auto-approves
# every tool on the machine to make one page of HTML come back.
NO_TOOLS_PREFACE = {
    "gemini": ("Answer directly from the text below. Do not use any tools, do not run any "
               "commands, and do not read or write any files. Reply with the code only.\n\n"),
}


# Whether a CLI can actually open the three images in headless mode. Gemini's cannot
# without a permission rule it has no way to ask for: it auto-denies the read and
# returns nothing at all, so telling it to look at reference.png produced an empty
# answer every round while the other two worked. Claiming a capability an agent does
# not have costs the whole draft, so this is stated rather than assumed.
CLI_READS_FILES = {"claude": True, "codex": True, "gemini": False}


def image_mode(agent):
    """How this agent gets the pictures: opens them, is sent them, or gets none."""
    if _cli_for(agent):
        return "read" if CLI_READS_FILES.get(agent) else "none"
    return "attached"


def _agent_available(agent):
    """Is this agent usable on this machine right now?"""
    cli = _cli_for(agent)
    if cli:
        if agent == "gemini":
            local = Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe"
            if local.is_file():
                return True
        return shutil.which(cli) is not None
    if agent == "anthropic-api":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if agent == "openai-api":
        return bool(os.environ.get("OPENAI_API_KEY"))
    if agent == "ollama":
        try:
            urllib.request.urlopen(OLLAMA_HOST + "/api/tags", timeout=1.5).read()
            return True
        except Exception:
            return False
    return False


def available_agents():
    return [{"id": a, "label": AGENT_LABELS[a], "available": _agent_available(a)}
            for a in AGENT_ORDER]


def pick_agent(preferred=None):
    wanted = preferred or os.environ.get("SPOT_ON_AGENT")
    if wanted:
        if wanted not in AGENT_ORDER:
            raise ValueError("unknown agent {!r}; pick one of {}".format(
                wanted, ", ".join(AGENT_ORDER)))
        if not _agent_available(wanted):
            raise ValueError("{} is not available here".format(AGENT_LABELS[wanted]))
        return wanted
    for a in AGENT_ORDER:
        if _agent_available(a):
            return a
    raise ValueError(
        "no AI is set up for the loop on this machine. Install a CLI (Claude Code, Codex or "
        "Gemini), set ANTHROPIC_API_KEY or OPENAI_API_KEY, or run Ollama. Scoring and the "
        "feedback packet work without any of them: copy the packet to whatever you use.")


def _model_for(agent):
    return os.environ.get("SPOT_ON_MODEL") or DEFAULT_MODELS.get(agent)


def _post_json(url, payload, headers, timeout=600):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=dict(headers, **{
        "Content-Type": "application/json"}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError("{} returned {}: {}".format(url, e.code, body))


def _b64(path):
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


# Windows passes a command line through cmd.exe when the executable is a .cmd or .bat
# shim, and cmd.exe truncates at 8191 characters. Both the Codex and Gemini CLIs are
# installed as shims here, and the prompt carries the whole page source, so a real
# rebuild goes over and the CLI dies with "The command line is too long." A trivial
# prompt works, which is why this only showed up on a real page.
ARG_SAFE_CHARS = 6000


def _prompt_on_disk(prompt, cwd):
    """Hand a long prompt over as a file, with a short argument pointing at it."""
    path = Path(cwd) / "prompt.txt"
    path.write_text(prompt, encoding="utf-8")
    return ("Read the file prompt.txt in this directory. It contains your full "
            "instructions, including the report you must act on. Follow it exactly and "
            "answer in the format it asks for.")


def _run_cli_agent(agent, prompt, cwd, timeout=600):
    cli = _cli_for(agent)
    exe = shutil.which(cli)
    if exe is None and agent == "gemini":
        exe = str(Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe")
    prompt = NO_TOOLS_PREFACE.get(agent, "") + prompt
    if len(prompt) > ARG_SAFE_CHARS and str(exe or "").lower().endswith((".cmd", ".bat")):
        prompt = _prompt_on_disk(prompt, cwd)
    if agent == "claude":
        cmd = [exe, "-p", prompt, "--output-format", "json",
               "--model", _model_for("claude"),
               "--json-schema", json.dumps(ITERATE_SCHEMA)]
    elif agent == "codex":
        cmd = [exe, "exec", "--skip-git-repo-check", prompt]
    else:
        cmd = [exe, "-p", prompt, "--print-timeout", "300s"]
    # A CLI with an open stdin can wait forever for input that never comes.
    with open(os.devnull, "rb") as devnull:
        proc = _run_tree(cmd, timeout, cwd=str(cwd), stdin=devnull,
                         text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        raise RuntimeError("{} exited {}: {}".format(
            AGENT_LABELS[agent], proc.returncode, (proc.stderr or "")[-400:]))
    return proc.stdout


def _run_api_agent(agent, prompt, images):
    """Send the prompt and the three images to a provider that takes them inline."""
    model = _model_for(agent)
    if agent == "anthropic-api":
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                "data": _b64(f)}} for f in images]
        content.append({"type": "text", "text": prompt})
        out = _post_json("https://api.anthropic.com/v1/messages",
                         {"model": model, "max_tokens": 16000,
                          "messages": [{"role": "user", "content": content}]},
                         {"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                          "anthropic-version": "2023-06-01"})
        return "".join(b.get("text", "") for b in out.get("content", []))
    if agent == "openai-api":
        content = [{"type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + _b64(f)}} for f in images]
        content.append({"type": "text", "text": prompt})
        out = _post_json("https://api.openai.com/v1/chat/completions",
                         {"model": model, "messages": [{"role": "user", "content": content}]},
                         {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]})
        return out["choices"][0]["message"]["content"]
    out = _post_json(OLLAMA_HOST + "/api/chat",
                     {"model": model, "stream": False,
                      "messages": [{"role": "user", "content": prompt,
                                    "images": [_b64(f) for f in images]}]}, {})
    return out.get("message", {}).get("content", "")


def run_agent(agent, prompt, cwd, images):
    if _cli_for(agent):
        return _run_cli_agent(agent, prompt, cwd)
    return _run_api_agent(agent, prompt, images)


ITERATE_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "description": "the full replacement source, no fences"},
        "changes": {"type": "string", "description": "one line on what was changed and why"},
    },
    "required": ["code", "changes"],
}


def rejected_changes(history, base, limit=6):
    """Rounds that scored below the best, newest first, with what they changed.

    Without this a round only knows about the attempt just discarded, so the same
    losing idea comes back every few rounds.
    """
    out = []
    for rec in reversed(history):
        if rec["n"] == base["n"] or rec["match"] >= base["match"] - 0.05:
            continue
        if not (rec.get("changes") or "").strip():
            continue
        out.append({"n": rec["n"], "match": rec["match"], "changes": rec["changes"]})
        if len(out) >= limit:
            break
    return out


def iteration_base(history):
    """The attempt the next round builds on: the best so far, not the latest.

    A round that scores lower is a failed experiment. Building on it compounds the
    mistake, which is exactly what happens when a person keeps saying "closer".
    """
    best = max(history, key=lambda a: (a["match"], a["n"]))
    latest = history[-1]
    return best, (latest if latest["n"] != best["n"] else None)


def _stuck_section(stuck):
    """Tell the round which faults it has already been asked to fix and has not.

    Without it a stuck problem is handed over in the same words every round, and the
    round keeps making the same choice: the first problem stays first, untouched, while
    smaller things get tidied around it. The escape hatch matters as much as the
    demand. Some faults genuinely cannot be closed in code, and a loop that insists on
    those forever is worse than one that moves on, so saying so is an allowed answer
    and the counting stops asking once it has been said.
    """
    if not stuck:
        return []
    lines = ["Already named in an earlier round and still not fixed:"]
    for s in stuck[:PRESS_LIMIT]:
        lines.append("  {} (asked for {} round{} ago and unchanged)".format(
            stuck_phrase(tuple(s["key"])), s["rounds"], "" if s["rounds"] == 1 else "s"))
    lines += [
        "Start with these. If one of them cannot be fixed in code, a typeface that is not",
        "installed, a logo or photograph you do not have, or real data that differs from the",
        "design's, say exactly that in your note and spend the round on something else.",
        "",
    ]
    return lines


def _iterate_prompt(run, n, code, report, extra, discarded=None, rejected=(),
                    images="read", stuck=()):
    ref = "reference.png"
    att = "attempts/{:03d}.png".format(n)
    dif = "attempts/{:03d}-diff.png".format(n)
    parts = [
        "You are making {} code look exactly like a design.".format(run["kind"]),
        "",
    ]
    if images == "read":
        parts += [
            "Look at all three images before you change anything:",
            "  Read {} (the design)".format(ref),
            "  Read {} (your last attempt, rendered)".format(att),
            "  Read {} (the difference; bright red is where you missed)".format(dif),
            "",
        ]
    elif images == "attached":
        parts += [
            "Three images are attached, in this order:",
            "  {} the design".format(ref),
            "  {} your last attempt, rendered".format(att),
            "  {} the difference; bright red is where you missed".format(dif),
            "",
        ]
    else:
        # No pictures at all. Saying so is the point: an agent told to look at images
        # it cannot open either stalls or invents what it saw.
        parts += [
            "You cannot see the page or the design here, so work only from the measured",
            "report below. Do not describe or guess at anything visual that it does not state.",
            "",
        ]
    parts += [
        "The page is {}x{} CSS pixels on a {} ground.".format(
            run.get("css_width", run["width"]), run.get("css_height", run["height"]),
            run.get("ground", "#FFFFFF")),
        "",
        feedback_text(run, n, report),
        "",
    ]
    parts += _stuck_section(stuck)
    if discarded:
        parts += [
            "Your most recent attempt ({}) scored {:.1f}, below this one at {:.1f}, so it was "
            "discarded. Its change was: {}. Do not repeat it; try a different fix.".format(
                discarded["n"], discarded["match"], report["match"],
                discarded.get("changes") or "not recorded"),
            "",
        ]
    if rejected:
        parts.append("Changes already tried that lowered the score. Do not try any of these again:")
        for r in rejected:
            parts.append("  attempt {} scored {:.1f}: {}".format(
                r["n"], r["match"], " ".join(r["changes"].split())[:400]))
        parts.append("")
    parts += [
        "The attempt to improve is:",
        "-----",
        code,
        "-----",
        "",
        "Return improved {} code for the same canvas.".format(run["kind"]),
        "Change the things the report names, keep what already scores well, and do not",
        "restructure working parts for their own sake. Return the complete source, not a patch.",
        "",
    ]
    if report["match"] < 60:
        parts += [
            "The page is still far from the design, so fix everything the report names this round,",
            "including whole elements that are missing or built the wrong way.",
        ]
    else:
        parts += [
            "The page is close now, so change at most three things, each on a specific element the",
            "report names{}. Never apply one rule to every element (for".format(
                "" if images == "none" else " or the difference map shows"),
            "example a line height on all text): at this distance that breaks what already matches.",
        ]
    if run["kind"] == "canvas":
        parts.append("The code is a function body with ctx, W and H already in scope.")
    # What the rebuild is allowed to reach for. A model left to guess writes plain
    # boxes, so a gauge comes out square and an icon comes out missing, and the report
    # then reports the square box rather than the reason for it.
    if (run.get("materials") or "").strip():
        parts += ["", "What you may build with: " + run["materials"].strip()]
    if extra:
        parts += ["", "Extra instruction from the person running this: " + extra]
    return "\n".join(parts)


def panel_agents(preferred, count, panel=False):
    """Which agent writes each rewrite in a round.

    Three draws from one model are three samples of the same habits. Three draws from
    three different models fail differently, which is the whole argument behind running
    a council rather than asking once. Here there is already an objective judge, so the
    diversity buys better drafts rather than better judgement, and the scorer still
    picks the winner.

    Only agents actually available on this machine are used, the preferred one first,
    and the list wraps if fewer are installed than the round asks for, so a machine
    with one agent behaves exactly as before.
    """
    if not (panel or os.environ.get("SPOT_ON_PANEL")):
        return [preferred] * count
    others = [a for a in AGENT_ORDER if a != preferred and _agent_available(a)]
    order = [preferred] + others
    return [order[i % len(order)] for i in range(count)]


def gather_candidates(agent, prompt, cwd, images, count, kind, panel=False):
    """Ask for `count` independent rewrites at once and return the usable ones.

    Each round is a fresh sample, so the spread between draws is wide: keeping the
    best of several is the same trick as building on the best attempt, applied
    inside one round. They run in parallel, so the round still takes about as long
    as a single draw, and costs `count` times as much. With SPOT_ON_PANEL set the
    draws are spread across whichever models the machine has.
    """
    import concurrent.futures

    chosen_panel = panel_agents(agent, count, panel)

    def one(which):
        raw = run_agent(which, prompt, cwd, images)
        code, changes = _parse_iteration(raw)
        if not code or not _looks_like(code, kind):
            return which, None, " ".join((code or "").split())[:240]
        return which, code, changes

    out, refusals = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
        for which, code, changes in pool.map(one, chosen_panel):
            if code:
                out.append((code, changes, which))
            else:
                refusals.append("{}: {}".format(AGENT_LABELS.get(which, which), changes))
    return out, refusals


GIVE_UP_AFTER = 3


def run_iteration(slug, extra="", agent=None, candidates=None, insist=True, panel=False):
    """One round: ask headless Claude Code for a better attempt, render it, score it.

    The round is also told which faults it has already been asked to fix and has not,
    so a problem that survives is pressed rather than restated. It stops pressing after
    GIVE_UP_AFTER rounds: past that the honest reading is that the thing cannot be
    fixed in code, and spending every remaining round on it is worse than saying so.
    """
    t0 = time.time()
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

    chosen = pick_agent(agent)
    count = candidates if candidates is not None else os.environ.get("SPOT_ON_CANDIDATES", 3)
    count = max(1, min(5, int(count)))
    image_access = image_mode(chosen)
    stuck = [s for s in stuck_problems(history, base) if s["rounds"] < GIVE_UP_AFTER]
    prompt = _iterate_prompt(run, n, code, report, extra, discarded,
                             rejected_changes(history, base), image_access, stuck)
    images = [d / "reference.png",
              d / "attempts" / "{:03d}.png".format(n),
              d / "attempts" / "{:03d}-diff.png".format(n)]

    drafts, refusals = gather_candidates(chosen, prompt, d, images, count, run["kind"], panel)
    if not drafts:
        # Usage limits, refusals and errors all come back as ordinary prose. Rendering
        # that as if it were code silently poisons the run, so stop and show it instead.
        raise RuntimeError("{} returned no usable {} code. It said: {}".format(
            AGENT_LABELS[chosen], run["kind"], refusals[0] if refusals else "nothing"))

    # Recorded one at a time: each attempt takes the next number in the run.
    # Recorded against the agent that actually wrote it, so the history shows which
    # model won a round rather than crediting the one the run was started with.
    records = [record_attempt(slug, c, source=who, changes=ch,
                              meta={"candidate_of": n, "candidates": len(drafts)})
               for c, ch, who in drafts]
    best = max(records, key=lambda r: r["match"])
    best["candidate_scores"] = [r["match"] for r in records]
    # What the round was pressed on, and whether it moved. This is the part the page
    # and the command line show: a fault that survives a round it was named in is the
    # reason a run stops climbing, and it used to be invisible.
    # Only what the prompt actually named, not everything that carried over: the
    # prompt asks for PRESS_LIMIT of them, so reporting all ten as "pressed" would
    # claim the round ignored things it was never asked about.
    before = {tuple(s["key"]) for s in stuck[:PRESS_LIMIT]}
    best["was_pressed"] = [stuck_phrase(k) for k in sorted(before, key=str)]
    best["still_stuck"] = [stuck_phrase(k) for k in sorted(before & problem_keys(best["report"]),
                                                           key=str)]
    best["given_up"] = [stuck_phrase(tuple(s["key"]))
                        for s in stuck_problems(history, base) if s["rounds"] >= GIVE_UP_AFTER]

    # A round that was asked for something and did not do it has spent the person's
    # time and their usage for nothing, so take one more swing before handing back.
    # Exactly one: insist is not passed down, so this cannot recurse, and a fault that
    # has already survived GIVE_UP_AFTER rounds is not pressed at all. The retry reads
    # the history again, so it presses on whatever is still wrong rather than repeating
    # this prompt, and it is recorded like any other attempt.
    if insist and best["still_stuck"]:
        again = run_iteration(slug, extra=extra, agent=agent, candidates=candidates,
                              insist=False, panel=panel)
        again["insisted_on"] = best["still_stuck"]
        if again["match"] >= best["match"]:
            again["round_seconds"] = round(time.time() - t0, 2)
            stamp_attempt(slug, again["n"], round_seconds=again["round_seconds"])
            return again
        best["insisting_did_not_help"] = True
    best["round_seconds"] = round(time.time() - t0, 2)
    stamp_attempt(slug, best["n"], round_seconds=best["round_seconds"])
    return best


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

def measure_stability(run, code, tmp_dir):
    """Screenshot the same target twice and score one against the other.

    A running page with an animation, a carousel or a live clock never matches
    itself. Without this the score looks like a gap in the build, and a loop
    chases motion it cannot fix. The number it returns is the ceiling for
    that page: nothing can score above it.
    """
    # Throw the first one away: a cold page is still fetching fonts, images and
    # lazy chunks, and measuring that would mask out real content for the whole run.
    render_code(code, run["kind"], run.get("css_width", run["width"]),
                run.get("css_height", run["height"]), tmp_dir / "stability-warmup.png",
                out_size=(run["width"], run["height"]), scale=run.get("scale", 1.0),
                ground=run.get("ground", "#FFFFFF"))
    a = render_code(code, run["kind"], run.get("css_width", run["width"]),
                    run.get("css_height", run["height"]), tmp_dir / "stability-a.png",
                    out_size=(run["width"], run["height"]), scale=run.get("scale", 1.0),
                    ground=run.get("ground", "#FFFFFF"))
    b = render_code(code, run["kind"], run.get("css_width", run["width"]),
                    run.get("css_height", run["height"]), tmp_dir / "stability-b.png",
                    out_size=(run["width"], run["height"]), scale=run.get("scale", 1.0),
                    ground=run.get("ground", "#FFFFFF"))
    raw, per_px, _ = score_images(a, b)
    moving = per_px > 64
    if moving.any():
        # Widen it a little: the edge of a moving element shimmers by a pixel or two.
        moving = _box_mean(moving.astype(np.float64), 9) > 0
    steady, _, _ = score_images(a, b, ignore=moving if moving.any() else None)
    ys, xs = np.where(moving)
    box = ([int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
           if moving.any() else None)
    return {"match": steady["match"], "raw_match": raw["match"],
            "pixels_changed": round(float((per_px > 64).mean() * 100), 2),
            "ignored_pct": round(float(moving.mean() * 100), 2),
            "box": box}, moving


def _unstable_mask(slug, run, code):
    """The parts of a running page that move, measured once and reused.

    Only running pages need it: pasted code renders the same every time.
    """
    if run["kind"] != "url":
        return None
    d = _run_dir(slug)
    mask_file = d / "unstable.png"
    if "stability" not in run:
        try:
            tmp = Path(tempfile.mkdtemp(prefix="spot-on-stability-"))
            try:
                stats, moving = measure_stability(run, code, tmp)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            # Excluding most of the page would leave nothing to score, and every
            # attempt would come back a meaningless 100.
            stats["unscoreable"] = stats["ignored_pct"] > 60
            if moving.any() and not stats["unscoreable"]:
                Image.fromarray((moving * 255).astype(np.uint8), mode="L").save(mask_file)
            run["stability"] = stats
        except Exception as e:
            run["stability"] = {"error": str(e)[:200]}
        _save_run(slug, run)
    if (run.get("stability") or {}).get("unscoreable"):
        return None
    if mask_file.exists():
        return np.asarray(Image.open(mask_file).convert("L")) > 127
    return None


def record_attempt(slug, code, source="manual", changes="", meta=None):
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
    ignore = _unstable_mask(slug, run, code)
    report, per_px, _ = score_images(ref_img, att_img, px_per_css=px_per_css, ignore=ignore,
                                     design_fonts=run.get("design_fonts"))
    diff_heatmap(per_px, d / "attempts" / "{:03d}-diff.png".format(n), ignore=ignore)
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
    record.update(meta or {})
    (d / "attempts" / "{:03d}.json".format(n)).write_text(
        json.dumps(record, indent=2), encoding="utf-8")

    # A running page is measured for stillness once, on its first attempt.
    record["stability"] = run.get("stability")

    # Re-read under a lock rather than trusting the copy loaded at the top: rendering
    # takes seconds, so that copy is stale by now, and two attempts finishing together
    # each wrote their own view of the best score. The later write won whether or not
    # it was the better attempt, leaving best_attempt pointing at the wrong one.
    with _run_lock(slug):
        latest = _load_run(slug)
        if report["match"] > latest.get("best_match", -1):
            latest["best_match"] = report["match"]
            latest["best_attempt"] = n
            _save_run(slug, latest)
    return record


def stamp_attempt(slug, n, **fields):
    """Add fields to a stored attempt after it was written.

    How long a round took is only known once it has finished, by which point the
    attempt it produced is already on disk. It is worth keeping, because it is what
    lets the page say how far through a round it is instead of spinning.
    """
    p = _run_dir(slug) / "attempts" / "{:03d}.json".format(n)
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    rec.update(fields)
    p.write_text(json.dumps(rec, indent=2), encoding="utf-8")


def capture_design(url, out_png, css_width=1440, css_height=900, scale=1.0):
    """Screenshot a live page to use as the design.

    Captured through the same renderer as every attempt, so both sit on the
    browser's virtual clock and an animation lands in the same place in each.
    """
    return render_code(_check_url(url), "url", css_width, css_height, out_png,
                       out_size=(round(css_width * scale), round(css_height * scale)), scale=scale)


_GENERIC_FAMILIES = {
    "inherit", "initial", "unset", "revert", "serif", "sans-serif", "monospace", "cursive",
    "fantasy", "system-ui", "-apple-system", "blinkmacsystemfont", "ui-monospace",
    "ui-sans-serif", "ui-serif", "ui-rounded", "emoji", "math", "fangsong", "var",
}


def read_page_fonts(url, limit=3):
    """The typefaces a live page actually asks for, read from its own source.

    No amount of looking at pixels will name a typeface, so the report can only say the
    letter shapes differ and leave the model to guess, and a wrong guess costs a round.
    When the design is a link the answer is simply available: the page says what it
    wants. The DOM is dumped after scripts have run, so a font a bundler injects at
    runtime counts too, which a plain fetch of the HTML would miss.
    """
    chrome = _find_chrome()
    if chrome is None:
        return []
    tmp = Path(tempfile.mkdtemp(prefix="spot-on-fonts-"))
    try:
        proc = _run_tree([str(chrome), "--headless=new", "--disable-gpu", "--no-first-run",
                          "--user-data-dir={}".format(tmp / "profile"),
                          "--virtual-time-budget=4000", "--dump-dom", _check_url(url)],
                         timeout=60)
        dom = (proc.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fonts_in_source(dom, limit)


def fonts_in_source(dom, limit=3):
    """Rank the typefaces named in a page's markup and styles."""
    counts = {}

    def note(name, weight):
        name = name.strip().strip("\"'").strip()
        if not name or name.lower() in _GENERIC_FAMILIES or len(name) > 40:
            return
        if name.startswith("--") or "(" in name:
            return
        counts[name] = counts.get(name, 0) + weight

    # A webfont the page loads on purpose outranks a name inside a stack, which is
    # mostly a fallback chain listing faces the page is not using.
    for m in re.finditer(r"fonts\.googleapis\.com/css2?\?([^\"'>]+)", dom):
        for fam in re.findall(r"family=([^&:]+)", m.group(1)):
            note(fam.replace("+", " "), 10)
    for m in re.finditer(r"@font-face[^}]*?font-family\s*:\s*([^;}]+)", dom, re.I):
        note(m.group(1), 6)
    for m in re.finditer(r"font-family\s*:\s*([^;}]+)", dom, re.I):
        note(m.group(1).split(",")[0], 1)   # first face only; the rest are fallbacks
    return [n for n, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]]


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


def create_run(name, kind, reference_bytes=None, reference_path=None, scale=1.0,
               reference_url=None, capture_width=1440, capture_height=900):
    slug = _slugify(name)
    d = _run_dir(slug)
    (d / "attempts").mkdir(parents=True, exist_ok=True)

    if reference_url:
        capture_design(reference_url, d / "captured-design.png",
                       int(capture_width), int(capture_height), scale)
        img = load_design(reference_path=d / "captured-design.png")
    else:
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
    run["materials"] = MATERIALS_DEFAULT
    if reference_url:
        run["design_url"] = reference_url
        # Read once, when the design is captured: the page is up now, and the answer
        # does not change between rounds.
        try:
            run["design_fonts"] = read_page_fonts(reference_url)
        except Exception:
            run["design_fonts"] = []
    _save_run(slug, run)
    return run


# Offered as the starting value, so the field is not an empty box nobody knows how to
# fill. Everything here is drawn by the page itself: nothing is fetched, so a run stays
# reproducible and nobody's licence is borrowed by accident.
MATERIALS_DEFAULT = (
    "Inline SVG for icons and for any curved or radial shape (gauges, rings, arcs, "
    "wifi and signal glyphs). CSS conic-gradient and radial-gradient for dials and "
    "glows, blur and rgba fills for translucent panels. Prefer drawing inline, so the "
    "page stays self-contained. The one exception is a real 3D object: WebGL renders "
    "here and three.js from a CDN works, so build it as one rather than faking it flat. "
    "Render a single frame and do not animate, because anything still moving between "
    "screenshots is excluded from the score rather than matched against the design."
)

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
  /* Both display:block. As inline spans the fill had no width or height at all, so
     every bar drew an empty track whatever the score was. */
  .bar-track { display: block; height: 8px; border-radius: 999px; background: rgba(34,55,43,0.12); overflow: hidden; }
  .bar-fill { display: block; height: 8px; border-radius: 999px; background: var(--deep); transition: width 180ms ease; }
  .code-summary { cursor: pointer; font-size: 13px; color: var(--deep); padding: 7px 0;
    list-style: none; display: flex; align-items: center; gap: 7px; }
  .code-summary::-webkit-details-marker { display: none; }
  .code-summary::before { content: ""; width: 0; height: 0; border-left: 5px solid var(--muted);
    border-top: 4px solid transparent; border-bottom: 4px solid transparent;
    transition: transform 150ms ease; }
  #code-fold[open] .code-summary::before { transform: rotate(90deg); }
  #code-fold[open] .code-summary { margin-bottom: 8px; }
  .comp-row { display: grid; grid-template-columns: 96px 1fr 52px; gap: 12px; align-items: center; min-height: 30px; }
  .comp-name { font-size: 13px; font-weight: 500; }
  .comp-val { text-align: right; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12.5px; font-weight: 500; color: var(--deep); font-variant-numeric: tabular-nums; }

  .region-cell { aspect-ratio: 1; border-radius: 3px; cursor: help; }
  /* One progress control, used light on the page and pale on the dark stage. A round
     takes minutes, and a spinner for minutes is indistinguishable from a hang. */
  .prog { display: block; height: 4px; border-radius: 999px; background: rgba(34,55,43,0.12); overflow: hidden; }
  .prog > i { display: block; height: 100%; width: 0; border-radius: 999px; background: var(--accent); transition: width 320ms linear; }
  .prog.over > i { background: repeating-linear-gradient(115deg, var(--accent) 0 9px, var(--accent-hover) 9px 18px); animation: progCrawl 1s linear infinite; }
  .prog.dark { background: rgba(255,255,255,0.16); }
  .prog.dark > i { background: #CFE3D4; }
  .prog.dark.over > i { background: repeating-linear-gradient(115deg, #CFE3D4 0 9px, #9FC2A9 9px 18px); }
  @keyframes progCrawl { to { background-position: 18px 0; } }
  .prog-note { font-size: 11px; color: var(--muted); }
  .el-score { display: flex; align-items: center; gap: 10px; padding: 5px 0; font-size: 12px; color: var(--deep); }
  .el-name { font-family: ui-monospace, Menlo, monospace; font-size: 11px; color: var(--muted); min-width: 172px; }
  .el-verdict { font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--faint); min-width: 78px; text-align: right; }
  @media (max-width: 720px) { .el-score { flex-wrap: wrap; } .el-name { min-width: 0; flex: 1 1 100%; } }
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
            <div style="display: flex; align-items: center; gap: 8px; margin-top: 10px;">
              <span class="mono" style="font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted); white-space: nowrap;">or a link</span>
              <input type="text" id="design-url" class="text-input mono" spellcheck="false" placeholder="https://yoursite.com/pricing" style="flex: 1; min-width: 0; font-size: 12px;" title="A page to copy: your production site, a staging build, or any page you have the right to match. It is screenshot through the same renderer as the attempts, so both sit on the same clock.">
              <select id="capture-size" class="text-input" style="width: 140px; font-size: 12px;" title="The page size to capture the link at. It becomes the size every attempt is screenshot at.">
                <option value="1440x900">1440 x 900</option>
                <option value="1280x800">1280 x 800</option>
                <option value="1024x768">1024 x 768</option>
                <option value="390x844">390 x 844 phone</option>
              </select>
              <button type="button" id="capture-design" class="btn-fill small">Capture</button>
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

    <div style="position: relative; display: grid; grid-template-columns: minmax(0,1fr) 1px minmax(0,1fr); align-items: stretch;">

      <div id="sec-attempt" style="grid-column: 1; grid-row: 2; padding: 26px 34px 28px 0; min-width: 0; position: relative;">
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
        <!-- Folded away by default. On a running page it is not used at all, and even on
             a snippet the loop writes the code rather than the person, so leading the
             section with it put the least-touched thing first. -->
        <details id="code-fold">
          <summary class="code-summary">The code, if you want to see or edit it</summary>
          <textarea id="code" class="code-area" rows="16" spellcheck="false" placeholder="Paste the page's HTML, or SVG or canvas code."></textarea>
        </details>
        <div class="hint">Screenshot by headless Chrome at the design's page size and display scale, so the score reflects the page and nothing else.</div>

        <div style="display: flex; align-items: center; gap: 10px; margin-top: 14px; flex-wrap: wrap;">
          <button type="button" id="render" class="btn-primary" disabled><span id="render-spin" hidden style="width: 11px; height: 11px; border-radius: 50%; border: 1.5px solid rgba(255,255,255,0.4); border-top-color: #FFFFFF; animation: mlSpin .7s linear infinite;"></span><span id="render-label">Screenshot and score</span></button>
          <button type="button" id="starter" class="btn-fill" disabled>Insert starter</button>
          <span id="status-line" style="margin-left: 4px; font-size: 12px; color: var(--muted);" aria-live="polite">Drop in a design to begin.</span>
        </div>
        <div id="prog-row" hidden style="display: flex; align-items: center; gap: 10px; margin-top: 10px; max-width: 620px;">
          <span class="prog" style="flex: 1;"><i id="prog-bar"></i></span>
          <span id="prog-note" class="prog-note mono" style="white-space: nowrap;"></span>
        </div>

        <div class="rule22" style="margin-top: 20px;"></div>
        <div id="no-agent" hidden style="padding-top: 14px; max-width: 580px;">
          <div style="font-size: 13px; font-weight: 500;">No AI is set up here</div>
          <div style="font-size: 12px; color: var(--muted); margin-top: 2px;">The loop can use Chrome's own built-in model (no account, no cost), Claude Code, the Codex CLI, the Gemini CLI, an <span class="mono">ANTHROPIC_API_KEY</span> or <span class="mono">OPENAI_API_KEY</span>, or a local Ollama. None of those is here, so copy the feedback packet below into whatever you do use, and paste the result back.</div>
        </div>
        <div id="url-loop-note" hidden style="padding-top: 14px; max-width: 580px;">
          <div style="font-size: 13px; font-weight: 500;">Letting the model iterate</div>
          <div style="font-size: 12px; color: var(--muted); margin-top: 2px;">A running page's code lives in your repo, so the loop runs in the session that edits it. Ask that session to use <span class="mono">/spot-on</span>: it scores the page after each change, reads the difference map, and its attempts land in this history.</div>
        </div>
        <div id="iterate-wrap" style="padding-top: 14px;">
          <div style="font-size: 13px; font-weight: 500;">Let an AI iterate</div>
          <div style="font-size: 12px; color: var(--muted); margin-top: 2px; max-width: 560px;">Each round shows the model the design, its own render and the difference map, hands it the report below, and scores whatever comes back.</div>
          <div id="agent-row" style="display: flex; align-items: center; gap: 8px; margin-top: 10px;">
            <span class="mono" style="font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted);">using</span>
            <select id="agent" class="text-input" style="height: 28px; font-size: 12px; width: 190px;" title="Whichever of these is installed or configured on this machine. Set SPOT_ON_MODEL to choose the model."></select>
            <span id="agent-note" class="hint" style="margin-top: 0;"></span>
          </div>
          <div style="display: flex; align-items: center; gap: 10px; margin-top: 12px; flex-wrap: wrap;">
            <button type="button" id="iterate" class="btn-fill" disabled><span id="iter-spin" hidden style="width: 11px; height: 11px; border-radius: 50%; border: 1.5px solid rgba(82,121,111,0.3); border-top-color: var(--accent); animation: mlSpin .7s linear infinite; display: inline-block; margin-right: 7px; vertical-align: -1px;"></span><span id="iter-label">Run 3 rounds</span></button>
            <select id="rounds" class="text-input" style="width: 104px; height: 32px;">
              <option value="1">1 round</option>
              <option value="3" selected>3 rounds</option>
              <option value="5">5 rounds</option>
            </select>
            <select id="candidates" class="text-input" style="width: 116px; height: 32px;" title="How many rewrites to ask for per round. They run at the same time and the highest scoring one is kept, which evens out the luck of a single draw. Each one costs as much as a round on its own.">
              <option value="1">1 try each</option>
              <option value="2">best of 2</option>
              <option value="3" selected>best of 3</option>
              <option value="5">best of 5</option>
            </select>
            <input type="text" id="iter-note" class="text-input" placeholder="Optional steer, for example the font is Inter, keep the card colours" style="flex: 1; min-width: 220px;">
            <button type="button" id="iter-stop" class="btn-ghost" hidden style="height: 32px;">Stop</button>
          </div>
          <details id="materials-fold" style="margin-top: 12px;">
            <summary class="code-summary">What it may build with</summary>
            <textarea id="materials" class="text-input mono" rows="3" spellcheck="false"
              style="width: 100%; font-size: 12px; line-height: 1.5; padding: 8px 10px;"
              placeholder="Inline SVG for icons and curved shapes, conic-gradient for dials..."></textarea>
            <div class="hint">Sent with every round. Left to guess, a model writes plain boxes, so a gauge comes out square and an icon comes out missing. Keep it to things the page can draw itself: anything fetched makes the run depend on the network.</div>
          </details>
        </div>
      </div>

      <div class="vrule" style="grid-column: 2; grid-row: 2;"></div>

      <div id="sec-compare" style="grid-column: 1 / -1; grid-row: 1; display: flex; flex-direction: column; padding: 11px 22px 20px; margin: 18px 0 8px; min-width: 0; background: linear-gradient(180deg, #52796F 0%, #3E5D53 40%, #2A4237 74%, #1F3029 100%); border-radius: 14px; color: #D6E1D8; -webkit-mask-image: radial-gradient(126% 118% at 50% 42%, #000 62%, rgba(0,0,0,0.55) 84%, rgba(0,0,0,0) 100%); mask-image: radial-gradient(126% 118% at 50% 42%, #000 62%, rgba(0,0,0,0.55) 84%, rgba(0,0,0,0) 100%);">
        <div style="display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 0 0 12px;">
          <div style="flex: 1; min-width: 0;">
            <h2 class="sec-title" style="color: #EDF2ED;">Comparison</h2>
            <div style="font-size: 13px; color: #E2EAE3; margin-top: 2px;">The design on the left, what was built on the right, at the same size.</div>
          </div>
          <span class="mono" style="font-size: 10px; color: #DCEBE0; letter-spacing: 0.16em; text-transform: uppercase;">live</span>
        </div>
        <div style="height: 1px; background: linear-gradient(90deg, rgba(255,255,255,0.13), rgba(255,255,255,0.13) 94%, transparent); margin-bottom: 12px;"></div>

        <div style="display: flex; align-items: center; gap: 7px; margin-bottom: 11px; flex-wrap: wrap;">
          <span class="mono" style="font-size: 10px; color: #DCEBE0; letter-spacing: 0.14em; text-transform: uppercase; margin-right: 3px;">on the right</span>
          <button type="button" class="dark-chip active" data-view="attempt">attempt</button>
          <button type="button" class="dark-chip" data-view="difference">difference</button>
          <button type="button" class="dark-chip" data-view="overlay">overlay</button>
        </div>

        <div style="display: grid; grid-template-columns: minmax(0,1fr) minmax(0,1fr); gap: 14px; flex: 1;">
          <div style="display: flex; flex-direction: column; min-width: 0;">
            <span class="mono" style="font-size: 10px; color: #DCEBE0; letter-spacing: 0.14em; text-transform: uppercase; margin-bottom: 6px;">design</span>
            <div style="position: relative; flex: 1; min-height: 420px; border-radius: 8px; overflow: hidden; background: rgba(10,18,13,0.30);">
              <img id="stage-design" alt="the design" hidden style="position: absolute; inset: 8px; width: calc(100% - 16px); height: calc(100% - 16px); object-fit: contain; object-position: top;">
            </div>
          </div>
          <div style="display: flex; flex-direction: column; min-width: 0;">
            <span id="stage-label" class="mono" style="font-size: 10px; color: #DCEBE0; letter-spacing: 0.14em; text-transform: uppercase; margin-bottom: 6px;">attempt</span>
            <div id="stage" style="position: relative; flex: 1; min-height: 420px; border-radius: 8px; overflow: hidden; background: rgba(10,18,13,0.30); display: flex; align-items: center; justify-content: center;">
              <img id="stage-base" alt="" hidden style="position: absolute; inset: 8px; width: calc(100% - 16px); height: calc(100% - 16px); object-fit: contain; object-position: top;">
              <img id="stage-over" alt="" hidden style="position: absolute; inset: 8px; width: calc(100% - 16px); height: calc(100% - 16px); object-fit: contain; object-position: top;">
              <span id="stage-empty" class="mono" style="font-size: 11px; color: #A9AFAB;">Nothing rendered yet.</span>
              <div id="stage-prog" hidden style="position: absolute; left: 14px; right: 14px; bottom: 14px; display: flex; align-items: center; gap: 10px;">
                <span class="prog dark" style="flex: 1;"><i id="stage-prog-bar"></i></span>
                <span id="stage-prog-note" class="mono" style="font-size: 10px; color: #DCEBE0; letter-spacing: 0.1em; white-space: nowrap;"></span>
              </div>
            </div>
          </div>
        </div>

        <div id="onion-row" hidden style="display: flex; align-items: center; gap: 10px; margin-top: 11px;">
          <span class="mono" style="font-size: 10px; color: #DCEBE0; letter-spacing: 0.14em; text-transform: uppercase;">design</span>
          <input type="range" id="onion" min="0" max="100" value="50" style="flex: 1; min-width: 0;">
          <span class="mono" style="font-size: 10px; color: #DCEBE0; letter-spacing: 0.14em; text-transform: uppercase;">page</span>
        </div>
        <div style="color: #E2EAE3; font-size: 11px; margin-top: 9px;">In the difference view, bright red is a big miss and black is an exact match. Antialiasing always leaves a faint outline.</div>
      </div>

      <div id="sec-score" style="grid-column: 3; grid-row: 2; padding: 26px 0 30px 30px; min-width: 0; position: relative;">
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
              <div id="score-best" class="mono" style="font-size: 11px; margin-top: 3px;"></div>
            </div>
            <div class="vrule" style="height: 108px;"></div>
            <div id="components" style="min-width: 0;"></div>
            <div class="vrule" style="height: 108px;"></div>
            <div title="The page divided into sixteen areas. The darker the square, the further that part of the page is from the design.">
              <div class="mono" style="font-size: 10px; color: var(--muted); letter-spacing: 0.14em; text-transform: uppercase; margin-bottom: 6px;">where the miss is</div>
              <div id="region-grid" style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 3px; width: 120px;"></div>
              <div style="font-size: 11px; color: var(--muted); margin-top: 7px; max-width: 150px; line-height: 1.35;">The page in sixteen areas. Darker is further from the design.</div>
            </div>
          </div>

          <div class="rule13" style="margin: 20px 0 8px;"></div>
          <div id="problems"></div>
          <div id="element-scores" style="margin-top: 16px;"></div>
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

// ---- progress
// A round takes minutes, and for minutes a spinner is indistinguishable from a hang.
// The bar fills against how long this run's own rounds have actually taken, so the
// estimate is this machine with this model rather than a number chosen here. It stops
// at 95% and starts crawling once it runs over, because a bar that sits full while
// nothing happens is worse than one that admits it does not know.
var PROG_FALLBACK = 150;   // seconds, until this run has timed a round of its own
var prog = { timer: null, t0: 0, budget: 0, label: "" };

function clock(s) {
  return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2);
}

function roundBudget() {
  var seen = [];
  for (var i = 0; i < state.attempts.length; i++) {
    var v = state.attempts[i].round_seconds;
    if (v) seen.push(v);
  }
  if (!seen.length) return PROG_FALLBACK;
  seen.sort(function (a, b) { return a - b; });
  return seen[Math.floor(seen.length / 2)];
}

function progPaint() {
  var s = Math.round((Date.now() - prog.t0) / 1000);
  var frac = Math.min(0.95, prog.budget ? (Date.now() - prog.t0) / (prog.budget * 1000) : 0);
  var over = frac >= 0.95;
  var note = prog.label + " " + clock(s) +
    (prog.budget ? (over ? ", longer than usual" : " of about " + clock(Math.round(prog.budget))) : "");
  [["prog-bar", "prog-note"], ["stage-prog-bar", "stage-prog-note"]].forEach(function (ids) {
    var bar = $(ids[0]);
    bar.style.width = (frac * 100).toFixed(1) + "%";
    bar.parentNode.classList.toggle("over", over);
    $(ids[1]).textContent = note;
  });
}

function progStart(label, budget) {
  prog.t0 = Date.now();
  prog.label = label;
  prog.budget = budget === undefined ? roundBudget() : budget;
  $("prog-row").hidden = false;
  $("stage-prog").hidden = false;
  progPaint();
  clearInterval(prog.timer);
  prog.timer = setInterval(progPaint, 250);
}

function progStop() {
  clearInterval(prog.timer);
  prog.timer = null;
  $("prog-row").hidden = true;
  $("stage-prog").hidden = true;
  $("prog-bar").style.width = "0";
  $("stage-prog-bar").style.width = "0";
}
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
  // Hidden entirely on a running page, where the code lives in a repo. On a snippet
  // run it stays folded until asked for: the loop writes the code, and the summary
  // line says it is there.
  $("code-fold").hidden = url;
  $("url-loop-note").hidden = !url;
  $("iterate-wrap").hidden = url;
  $("starter").textContent = url ? "Use localhost" : "Insert starter";
}
$("url-input").addEventListener("keydown", function (e) {
  if (e.key === "Enter") $("render").click();
});

// ---- reference and runs ----
// Chrome ships a small model (Gemini Nano) behind LanguageModel. It costs nothing
// and needs no account, so it is offered whenever the browser has it ready.
var BROWSER_AGENT = "chrome-builtin";
function browserModel() { return (typeof LanguageModel !== "undefined") ? LanguageModel : null; }
function browserAgentReady() {
  var lm = browserModel();
  if (!lm || !lm.availability) return Promise.resolve(null);
  return lm.availability({ expectedInputs: [{ type: "image" }] })
    .then(function (state) { return state === "unavailable" ? null : state; })
    .catch(function () { return null; });
}

function extractCode(text) {
  var fenced = /```(?:[a-zA-Z]+)?\s*([\s\S]*?)```/.exec(text || "");
  return (fenced ? fenced[1] : (text || "")).trim();
}

function runBrowserRound(note) {
  var lm = browserModel();
  if (!lm) return Promise.reject(new Error("this browser has no built-in model"));
  var payload;
  return api("/prompt?run=" + encodeURIComponent(state.run.slug) +
             (note ? "&instructions=" + encodeURIComponent(note) : ""))
    .then(function (d) {
      payload = d;
      return Promise.all(d.images.map(function (f) {
        return fetch(imgUrl(f)).then(function (r) { return r.blob(); });
      }));
    })
    .then(function (blobs) {
      return lm.create({
        expectedInputs: [{ type: "text", languages: ["en"] }, { type: "image" }],
        expectedOutputs: [{ type: "text", languages: ["en"] }],
        monitor: function (m) {
          m.addEventListener("downloadprogress", function (e) {
            setStatus("Downloading the browser model, " + Math.round(e.loaded * 100) + "%. This happens once.");
          });
        }
      }).then(function (session) {
        var content = [{ type: "text", value: payload.prompt }];
        blobs.forEach(function (b) { content.push({ type: "image", value: b }); });
        return session.prompt([{ role: "user", content: content }]).then(function (answer) {
          session.destroy && session.destroy();
          return answer;
        }, function (err) {
          session.destroy && session.destroy();
          throw err;
        });
      });
    })
    .then(function (answer) {
      var code = extractCode(answer);
      if (!code) throw new Error("the browser model returned no code");
      return api("/attempt", { run: state.run.slug, code: code, source: BROWSER_AGENT,
                               changes: "written by the browser's built-in model" });
    });
}

var PANEL_AGENT = "__panel__";

function loadAgents() {
  return Promise.all([api("/agents"), browserAgentReady()]).then(function (both) {
    var agents = both[0], browserState = both[1];
    var sel = $("agent");
    sel.innerHTML = "";
    var usable = agents.filter(function (a) { return a.available; });
    if (browserState) {
      usable.push({ id: BROWSER_AGENT, available: true,
                    label: "Chrome built-in" + (browserState === "available" ? "" : ", downloads once") });
    }
    usable.forEach(function (a) {
      var o = document.createElement("option");
      o.value = a.id;
      o.textContent = a.label;
      sel.appendChild(o);
    });
    // Only offered when there is more than one model to spread across; on a machine
    // with a single agent it would be the same thing under a different name.
    if (usable.length > 1) {
      var mix = document.createElement("option");
      mix.value = PANEL_AGENT;
      mix.textContent = "all of them, one rewrite each";
      sel.appendChild(mix);
    }
    state.agents = usable.length;
    $("agent-row").hidden = usable.length === 0;
    $("iterate").hidden = usable.length === 0;
    $("rounds").hidden = usable.length === 0;
    $("iter-note").hidden = usable.length === 0;
    $("candidates").hidden = usable.length === 0;
    $("agent-note").textContent = usable.length > 1 ? "others found: " +
      usable.slice(1).map(function (a) { return a.label; }).join(", ") : "";
    if (usable.length === 0) {
      $("no-agent").hidden = false;
    }
    return usable;
  });
}

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
    $("materials").value = run.materials || "";
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
    '<span class="meta-pill" title="What the model writes for this run">' + r.kind + '</span>' +
    (r.stability && r.stability.ignored_pct
      ? '<span class="meta-pill" style="background: rgba(38,58,92,0.12); color: #26456E;" title="This share of the page moves between screenshots, so it is excluded from the score and shown slate blue in the difference map. With it excluded, two screenshots of the page score ' + r.stability.match.toFixed(1) + ' against each other.">' + r.stability.ignored_pct.toFixed(1) + '% moving, excluded</span>'
      : '');
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

$("capture-design").addEventListener("click", function () {
  var url = $("design-url").value.trim();
  if (!url) return;
  var name = $("run-name").value.trim();
  if (!name) {
    try { name = new URL(url).hostname.replace(/^www\./, "") + new URL(url).pathname.replace(/\/$/, "").replace(/\//g, " "); }
    catch (e) { name = "captured design"; }
    $("run-name").value = name;
  }
  var size = $("capture-size").value.split("x");
  setStatus("Capturing " + url + "...");
  $("capture-design").disabled = true;
  api("/runs", { name: name, kind: $("run-kind").value, url: url,
                 scale: parseFloat($("run-scale").value),
                 capture_width: parseInt(size[0], 10), capture_height: parseInt(size[1], 10) })
    .then(function (run) { return loadRuns().then(function () { return openRun(run.slug); }); })
    .then(function () { setStatus("Design captured. Point the attempt at your own page and score it."); })
    .catch(function (e) { setStatus("Could not capture that page: " + e.message); })
    .then(function () { $("capture-design").disabled = false; });
});

var materialsSaveTimer = null;
$("materials").addEventListener("input", function () {
  if (!state.run) return;
  clearTimeout(materialsSaveTimer);
  var value = $("materials").value;
  materialsSaveTimer = setTimeout(function () {
    api("/materials", { run: state.run.slug, materials: value })
      .then(function (run) { state.run = run; })
      .catch(function () {});
  }, 600);
});

// A new run starts itself. Everything the three opening clicks did was forced: there
// is nothing to screenshot but the starter, and nothing to do with the score but
// iterate on it. One round runs, not three, so the first real attempt appears and then
// it hands back rather than spending minutes nobody asked for.
function autoStart() {
  var run = state.run;
  if (!run || isUrlKind() || state.attempts.length) return Promise.resolve();
  return api("/run?run=" + encodeURIComponent(run.slug))
    .then(function (full) {
      setCode(full.starter);
      return scoreCurrentCode();
    })
    .then(function () {
      if (!state.agents) {
        setStatus("Scored the starter. No AI is set up here, so copy the packet below.");
        return;
      }
      startRounds(1);
    })
    .catch(function () {});
}

$("create-run").addEventListener("click", function () {
  var name = $("run-name").value.trim();
  if (!name || !state.pending) return;
  setStatus("Creating run...");
  api("/runs", { name: name, kind: $("run-kind").value, scale: parseFloat($("run-scale").value), data_url: state.pending })
    .then(function (run) { state.pending = null; return loadRuns().then(function () { return openRun(run.slug); }); })
    .then(function () { return autoStart(); })
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

function scoreCurrentCode() {
  var code = getCode();
  if (!state.run || !code.trim()) { setStatus("Nothing to render yet."); return Promise.reject(); }
  busy(true);
  setStatus("Taking the screenshot...");
  // A screenshot and score is seconds, not minutes, so the bar is measured against
  // what the last render of this run actually took.
  var last = state.attempts.length
    ? state.attempts[state.attempts.length - 1].render_seconds : 0;
  progStart("screenshot", Math.max(3, last || 6));
  return api("/attempt", { run: state.run.slug, code: code })
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
      return rec;
    })
    .catch(function (e) { setStatus("Render failed: " + e.message); throw e; })
    .then(function (rec) { busy(false); progStop(); return rec; },
          function (e) { busy(false); progStop(); throw e; });
}

$("render").addEventListener("click", function () {
  scoreCurrentCode().catch(function () {});
});

// ---- iterate ----
function startRoundClock(round, total) {
  var tries = parseInt($("candidates").value, 10);
  setStatus("Round " + round + " of " + total + ". " +
    (tries > 1 ? tries + " rewrites are running at once; the best one is kept."
               : "One rewrite is running."));
  progStart("round " + round + " of " + total + ",");
}
function stopRoundClock() { progStop(); }

function iterateRounds(left, note) {
  if (left <= 0 || state.stopping) {
    stopRoundClock();
    $("iter-spin").hidden = true;
    $("iter-stop").hidden = true;
    $("iter-label").textContent = "Run " + $("rounds").value + " rounds";
    $("iterate").disabled = !state.attempts.length;
    busy(false);
    var best = state.run.best_match || 0;
    var beat = state.startBest === undefined || best > state.startBest + 0.05;
    setStatus(state.stopping ? "Stopped." : (beat
      ? "Finished. Best is now " + best.toFixed(1) + " (attempt " + state.run.best_attempt + ")."
      : "Finished. Nothing beat " + best.toFixed(1) + " (attempt " + state.run.best_attempt +
        "), which is still the best. Try a steer naming what is still off, or fix one thing by hand and screenshot again."));
    state.stopping = false;
    return;
  }
  $("iter-label").textContent = left + " to go";
  startRoundClock(state.rounds - left + 1, state.rounds);
  ($("agent").value === BROWSER_AGENT
    ? runBrowserRound(note)
    : api("/iterate", { run: state.run.slug, instructions: note,
                        agent: $("agent").value === PANEL_AGENT ? null : $("agent").value,
                        panel: $("agent").value === PANEL_AGENT,
                        candidates: parseInt($("candidates").value, 10) }))
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

function startRounds(rounds) {
  if (!state.run || !state.attempts.length) return;
  state.stopping = false;
  state.rounds = rounds;
  $("iter-spin").hidden = false;
  $("iter-stop").hidden = false;
  $("iterate").disabled = true;
  busy(true, "Iterating");
  state.startBest = state.run.best_match || 0;
  iterateRounds(state.rounds, $("iter-note").value.trim());
}

$("iterate").addEventListener("click", function () {
  startRounds(parseInt($("rounds").value, 10));
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

// The design keeps the left pane whatever is chosen, so the two are always side by
// side at the same size. The chips change the right one only.
var STAGE_LABELS = {attempt: "attempt", difference: "difference", overlay: "overlay"};

function renderStage() {
  var rec = current();
  var base = $("stage-base"), over = $("stage-over"), design = $("stage-design");
  $("stage-label").textContent = STAGE_LABELS[state.view] || "attempt";
  if (!state.run) {
    design.hidden = true; base.hidden = true; over.hidden = true;
    $("stage-empty").hidden = false; $("onion-row").hidden = true;
    return;
  }
  design.src = imgUrl("reference.png");
  design.hidden = false;
  var pad = function (n) { return ("00" + n).slice(-3); };
  $("onion-row").hidden = state.view !== "overlay";
  if (!rec) { base.hidden = true; over.hidden = true; $("stage-empty").hidden = false; return; }
  $("stage-empty").hidden = true;
  if (state.view === "difference") {
    base.src = imgUrl("attempts/" + pad(rec.n) + "-diff.png"); base.style.opacity = 1; base.hidden = false; over.hidden = true;
  } else if (state.view === "overlay") {
    base.src = imgUrl("reference.png"); base.style.opacity = 1; base.hidden = false;
    over.src = imgUrl("attempts/" + pad(rec.n) + ".png"); over.hidden = false;
    over.style.opacity = parseInt($("onion").value, 10) / 100;
  } else {
    base.src = imgUrl("attempts/" + pad(rec.n) + ".png"); base.style.opacity = 1; base.hidden = false; over.hidden = true;
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

  var bestEl = $("score-best");
  var best = state.run && state.run.best_match >= 0 ? state.run.best_match : null;
  if (best !== null && state.run.best_attempt !== rec.n) {
    bestEl.textContent = "best " + best.toFixed(1) + " (attempt " + state.run.best_attempt + "), this one was not kept";
    bestEl.style.color = "var(--muted)";
  } else if (best !== null) {
    bestEl.textContent = "best so far";
    bestEl.style.color = "var(--accent)";
  } else {
    bestEl.textContent = "";
  }

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

  // Named elements, worst first. A sixteen-cell heat grid says a corner is dark; this
  // says which thing in it is wrong, and whether it needs moving or rebuilding.
  var es = $("element-scores");
  es.innerHTML = "";
  var scored = rep.element_scores || [];
  if (scored.length) {
    var head = document.createElement("div");
    head.className = "mono";
    head.style.cssText = "font-size:10px;color:var(--muted);letter-spacing:0.14em;" +
      "text-transform:uppercase;margin-bottom:8px;";
    head.textContent = "weakest elements";
    es.appendChild(head);
    scored.forEach(function (e) {
      var row = document.createElement("div");
      row.className = "el-score";
      var verdict = e.as_built === null ? "not drawn"
        : (e.as_built - e.in_place >= 10 ? "placed wrong" : "built wrong");
      var name = document.createElement("span");
      name.className = "el-name";
      name.textContent = e.w + "x" + e.h + " " + e.kind + " at " + e.x + "," + e.y;
      name.title = e.where + " of the page";
      var track = document.createElement("span");
      track.className = "bar-track";
      track.style.flex = "1";
      track.innerHTML = '<span class="bar-fill" style="width:' +
        Math.max(0, Math.min(100, e.in_place)) + '%"></span>';
      var val = document.createElement("span");
      val.className = "comp-val";
      val.textContent = e.in_place.toFixed(1);
      val.title = e.as_built === null ? "nothing in the attempt was recognisable as it"
        : "scores " + e.as_built.toFixed(1) + " compared with what it was paired to";
      var tag = document.createElement("span");
      tag.className = "el-verdict";
      tag.textContent = verdict;
      row.appendChild(name);
      row.appendChild(track);
      row.appendChild(val);
      row.appendChild(tag);
      es.appendChild(row);
    });
    var note = document.createElement("div");
    note.style.cssText = "font-size:11px;color:var(--muted);margin-top:8px;line-height:1.45;";
    note.textContent = "Scored against the design in the element's own place. " +
      "Comparable with each other, not with the page total.";
    es.appendChild(note);
  }

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
      '<span class="meta-pill att-src" title="Who wrote this attempt"></span>' +
      '<span class="att-note"></span>';
    row.querySelector(".att-src").textContent = rec.source || "";
    row.querySelector(".att-note").textContent = rec.changes || "";
    if (state.run && rec.n === state.run.best_attempt) {
      var badge = document.createElement("span");
      badge.className = "best-badge";
      badge.textContent = "best";
      row.appendChild(badge);
    } else if (state.run && state.run.best_match > rec.match && rec.source !== "manual") {
      var skipped = document.createElement("span");
      skipped.className = "best-badge";
      skipped.style.cssText = "background: var(--fill); color: var(--muted);";
      skipped.textContent = "not kept";
      skipped.title = "Scored below the best attempt, so the next round started from the best instead";
      row.appendChild(skipped);
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
loadAgents();
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
            elif path == "/agents":
                self._send_json(200, available_agents())
            elif path == "/prompt":
                # The same round prompt the server loop uses, for an AI that runs in
                # the page (Chrome's built-in model) instead of on this machine.
                q = self._query()
                run = _load_run(q["run"])
                history = _attempts(q["run"])
                if not history:
                    raise ValueError("render a first attempt before iterating")
                base, discarded = iteration_base(history)
                d = _run_dir(q["run"])
                code = (d / "attempts" / "{:03d}.code".format(base["n"])).read_text(encoding="utf-8")
                self._send_json(200, {
                    "attempt": base["n"],
                    "match": base["match"],
                    "prompt": _iterate_prompt(run, base["n"], code, base["report"],
                                              q.get("instructions", ""), discarded,
                                              rejected_changes(history, base), "attached"),
                    "images": ["reference.png",
                               "attempts/{:03d}.png".format(base["n"]),
                               "attempts/{:03d}-diff.png".format(base["n"])],
                })
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
                                 scale=payload.get("scale", 1.0),
                                 reference_url=payload.get("url"),
                                 capture_width=payload.get("capture_width", 1440),
                                 capture_height=payload.get("capture_height", 900))
                run["starter"] = starter_code(run)
                self._send_json(200, run)
            elif path == "/runs/delete":
                shutil.rmtree(_run_dir(payload["run"]), ignore_errors=True)
                self._send_json(200, {"ok": True})
            elif path == "/attempt":
                self._send_json(200, record_attempt(payload["run"], payload["code"],
                                                    source=_clean_source(payload.get("source")),
                                                    changes=payload.get("changes", "")))
            elif path == "/iterate":
                self._send_json(200, run_iteration(payload["run"], payload.get("instructions", ""),
                                                   payload.get("agent") or None,
                                                   payload.get("candidates"),
                                                   insist=payload.get("insist", True),
                                                   panel=payload.get("panel", False)))
            elif path == "/materials":
                run = _load_run(payload["run"])
                run["materials"] = (payload.get("materials") or "")[:600]
                _save_run(payload["run"], run)
                self._send_json(200, run)
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
    ap.add_argument("design", help="the design: an image, or a link to a page to copy")
    ap.add_argument("target", help="a running page URL, or a .html / .svg / .js file")
    ap.add_argument("--kind", choices=KINDS, help="what the target is; guessed if omitted")
    ap.add_argument("--width", type=int, default=1440, help="page width when the design is a link")
    ap.add_argument("--height", type=int, default=900, help="page height when the design is a link")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="display scale the design was captured at: 1, 1.25, 1.5 or 2")
    ap.add_argument("--run", help="run name; defaults to the design's file name")
    ap.add_argument("--note", default="", help="one line on what changed since the last attempt")
    ap.add_argument("--json", action="store_true", help="print the record as JSON")
    a = ap.parse_args(argv)

    from urllib.parse import urlparse
    design_is_link = bool(re.match(r"^https?://", a.design))
    design = None if design_is_link else Path(a.design)
    kind = a.kind or _guess_kind(a.target)
    code = a.target if kind == "url" else Path(a.target).read_text(encoding="utf-8")
    default_name = (urlparse(a.design).netloc.replace(".", "-") if design_is_link else design.stem)
    slug = _slugify(a.run or default_name)
    sha = None if design_is_link else hashlib.sha256(design.read_bytes()).hexdigest()[:16]

    if (_run_dir(slug) / "run.json").exists():
        run = _load_run(slug)
        # A captured design is kept as it was; only a file is checked for swaps.
        if sha and run.get("design_sha") and run["design_sha"] != sha:
            raise SystemExit("run {!r} was started with a different design image; "
                             "pass --run with a new name".format(slug))
        if run["kind"] != kind:
            run["kind"] = kind
            _save_run(slug, run)
    else:
        run = create_run(a.run or default_name, kind, scale=a.scale,
                         reference_path=None if design_is_link else design,
                         reference_url=a.design if design_is_link else None,
                         capture_width=a.width, capture_height=a.height)
        if sha:
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


USAGE = """Spot On: score a web page against a design.

  python spot-on.py                 serve the page on http://{host}:{port}
  python spot-on.py 7266            serve it on another port
  python spot-on.py score ...       score one attempt from the command line

The design is an image file or a link to a page to copy. Run
`python spot-on.py score --help` for the scoring options."""


def main():
    global PORT
    args = sys.argv[1:]
    if args and args[0] == "score":
        cli_score(args[1:])
        return
    if args and args[0] in ("-h", "--help", "help"):
        print(USAGE.format(host=HOST, port=PORT))
        return
    if args:
        try:
            PORT = int(args[0])
        except ValueError:
            # Anything unrecognised used to be shrugged off and the server started
            # anyway, so a mistyped flag looked like a hang behind a buffered notice.
            raise SystemExit("not a port number: {!r}\n\n{}".format(
                args[0], USAGE.format(host=HOST, port=PORT)))
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("ready on http://{}:{}".format(HOST, PORT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
