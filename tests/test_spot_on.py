"""Tests for spot-on.py.

Tier 1 (scoring, guards, geometry) runs anywhere with numpy and pillow.
Tier 2 (the HTTP server and real screenshots) needs Chrome or Edge and is
skipped without one.

    python -m unittest discover -s tests -v
"""

import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent / "spot-on.py"


def load_tool(env=None):
    """Import spot-on.py fresh, optionally under a modified environment."""
    saved = dict(os.environ)
    try:
        if env:
            os.environ.update(env)
        spec = importlib.util.spec_from_file_location("spot_on_under_test", TOOL)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.environ.clear()
        os.environ.update(saved)


so = load_tool()

W, H = 400, 300


def scene(circle=(200, 150, 80, "#52796F"), square=(30, 30, 60, "#C2703F")):
    """Draw the calibration scene: a circle and a square on white."""
    img = Image.new("RGB", (W, H), "#FFFFFF")
    d = ImageDraw.Draw(img)
    if circle:
        cx, cy, r, fill = circle
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)
    if square:
        x, y, s, fill = square
        d.rectangle([x, y, x + s - 1, y + s - 1], fill=fill)
    return img


DESIGN = scene()
CASES = {
    "exact": scene(),
    "close": scene(circle=(203, 152, 78, "#55796C"), square=(32, 30, 58, "#C07038")),
    "half": scene(square=None),
    "hue": scene(circle=(200, 150, 80, "#2F6FA8")),
    "shift": scene(circle=(240, 150, 80, "#52796F"), square=(70, 30, 60, "#C2703F")),
    "wrong": scene(circle=(230, 160, 60, "#3355AA"), square=None),
    "blank": scene(circle=None, square=None),
}


def _png_bytes(img):
    import io
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def score(name):
    report, _, _ = so.score_images(DESIGN, CASES[name])
    return report


class ScoreOrdering(unittest.TestCase):
    """The score has to agree with the eye, or the loop learns the wrong lesson."""

    @classmethod
    def setUpClass(cls):
        cls.r = {name: score(name) for name in CASES}

    def m(self, name):
        return self.r[name]["match"]

    def test_exact_copy_scores_high(self):
        self.assertGreater(self.m("exact"), 95)

    def test_blank_scores_lowest(self):
        self.assertEqual(min(CASES, key=self.m), "blank")

    def test_near_miss_beats_leaving_an_element_out(self):
        # Regression: averaged over the empty page, dropping the square once
        # scored above drawing both shapes three pixels off. The loop would
        # have learned to delete the hard parts.
        self.assertGreater(self.m("close"), self.m("half"))

    def test_order_matches_what_the_eye_sees(self):
        self.assertGreater(self.m("exact"), self.m("close"))
        self.assertGreater(self.m("half"), self.m("shift"))
        self.assertGreater(self.m("shift"), self.m("wrong"))
        self.assertGreater(self.m("wrong"), self.m("blank"))


class ComponentsIsolateTheirError(unittest.TestCase):
    """Each part must fail for its own reason, so the report can name the mistake."""

    def test_wrong_colour_shows_up_in_colour_only(self):
        c = score("hue")["components"]
        self.assertLess(c["colour"], 50)
        self.assertGreater(c["shape"], 95)

    def test_displacement_shows_up_in_shape_not_colour(self):
        c = score("shift")["components"]
        self.assertLess(c["shape"], 60)
        self.assertGreater(c["colour"], 95)

    def test_antialiasing_does_not_read_as_missing_detail(self):
        # Regression: unblurred edge correlation put a near-identical copy at 54.6.
        self.assertGreater(score("exact")["components"]["detail"], 90)

    def test_offset_is_not_punished_through_colour(self):
        # Regression: per-pixel colour over the drawn area measured position.
        self.assertGreater(score("close")["components"]["colour"], 90)

    def test_missing_element_is_the_first_problem(self):
        r = score("half")
        self.assertLess(r["components"]["coverage"], 90)
        self.assertIn("nothing drawn near it", r["problems"][0])
        self.assertIn("top, left", r["problems"][0])

    def test_small_offset_counts_as_covered(self):
        self.assertGreater(score("close")["components"]["coverage"], 99)


def page(top_label=True, content_top=90):
    """A web-like page: optional small label, a heading bar, three cards."""
    img = Image.new("RGB", (600, 400), "#FFFFFF")
    d = ImageDraw.Draw(img)
    if top_label:
        d.rectangle([260, 40, 340, 52], fill="#5B7A6E")
    d.rectangle([150, content_top, 450, content_top + 24], fill="#1E2A24")
    for i in range(3):
        x = 40 + i * 180
        d.rectangle([x, content_top + 60, x + 160, content_top + 250], outline="#CBD3CE", width=2,
                    fill="#1F3A30" if i == 1 else "#FFFFFF")
        d.rectangle([x + 20, content_top + 90, x + 90, content_top + 104], fill="#6A746F")
    return img


class ShiftDiagnosis(unittest.TestCase):
    """One missing element pushes a page up; the report must say that, not 'all wrong'."""

    def test_missing_top_element_is_reported_as_a_shift(self):
        design = page(top_label=True, content_top=90)
        attempt = page(top_label=False, content_top=60)
        r, _, _ = so.score_images(design, attempt)
        v = r["offsets"]["vertical"]
        self.assertIsNotNone(v)
        self.assertAlmostEqual(v["shift"], 30, delta=3)
        self.assertIn("30px higher", r["problems"][0].replace("29px", "30px").replace("31px", "30px"))

    def test_shift_is_reported_in_css_pixels(self):
        design = page(top_label=True, content_top=90)
        attempt = page(top_label=False, content_top=60)
        r, _, _ = so.score_images(design, attempt, px_per_css=2.0)
        self.assertAlmostEqual(r["offsets"]["vertical"]["css_shift"], 15, delta=2)

    def test_shift_found_on_a_real_rendered_page(self):
        design, attempt = HERE / "fixtures" / "pricing-design.png", HERE / "fixtures" / "pricing-first-attempt.png"
        r, _, _ = so.score_images(Image.open(design), Image.open(attempt))
        v = r["offsets"]["vertical"]
        self.assertIsNotNone(v, "the missing PRICING label shifts the page up about 37px")
        self.assertAlmostEqual(v["shift"], 37, delta=4)
        self.assertIn("higher", r["problems"][0])

    def test_no_shift_invented_for_aligned_attempts(self):
        for name in ("exact", "close", "hue", "half"):
            off = score(name)["offsets"]
            self.assertIsNone(off["vertical"], name)
            self.assertIsNone(off["horizontal"], name)


class TypefaceHint(unittest.TestCase):
    def test_wrong_font_is_named_when_everything_else_is_close(self):
        # From the pricing demo: the design uses Segoe UI, the attempt Arial. Colour
        # 98.6 and coverage 95.4, structure 44, and the old report never mentioned
        # type, so the model spent rounds moving spacing instead.
        fx = HERE / "fixtures"
        r, _, _ = so.score_images(Image.open(fx / "pricing-design.png"),
                                  Image.open(fx / "pricing-wrong-font.png"))
        self.assertTrue(any("font family" in p for p in r["problems"]), r["problems"])

    def test_no_font_hint_for_shapes_with_the_wrong_colour(self):
        self.assertFalse(any("font family" in p for p in score("hue")["problems"]))


class IterationGuards(unittest.TestCase):
    LIMIT = "You've reached your usage limit. Switch to another model to continue."

    def test_prose_is_never_accepted_as_code(self):
        # Regression: a usage-limit message was rendered and scored as an attempt.
        for kind in so.KINDS:
            self.assertFalse(so._looks_like(self.LIMIT, kind), kind)

    def test_real_code_is_accepted(self):
        self.assertTrue(so._looks_like('<svg xmlns="http://www.w3.org/2000/svg"></svg>', "svg"))
        self.assertTrue(so._looks_like("<main><h1>Hi</h1></main>", "html"))
        self.assertTrue(so._looks_like("ctx.fillRect(0, 0, W, H);", "canvas"))
        self.assertTrue(so._looks_like("http://localhost:5173/pricing", "url"))

    def test_error_payload_surfaces_its_message(self):
        out = json.dumps({"type": "result", "is_error": True, "result": self.LIMIT})
        code, _ = so._parse_iteration(out)
        self.assertEqual(code, self.LIMIT)

    def test_claude_output_is_decoded_as_utf8(self):
        # Regression: text=True alone used the Windows codepage, so "10px→20px"
        # came back as mojibake.
        seen = {}

        class Done(Exception):
            pass

        def fake_run(cmd, **kw):
            seen.update(kw)
            raise Done()

        tmp = Path(tempfile.mkdtemp(prefix="spot-on-enc-"))
        saved_runs, saved_run = so.RUNS_DIR, so.subprocess.run
        try:
            so.RUNS_DIR = tmp
            so.create_run("enc", "html", reference_bytes=_png_bytes(DESIGN))
            d = tmp / "enc" / "attempts"
            (d / "001.code").write_text("<div></div>", encoding="utf-8")
            (d / "001.json").write_text(json.dumps({"n": 1, "match": 10.0, "report": score("blank")}),
                                        encoding="utf-8")
            so.subprocess.run = fake_run
            with self.assertRaises(Done):
                so.run_iteration("enc")
        finally:
            so.subprocess.run, so.RUNS_DIR = saved_run, saved_runs
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(seen.get("encoding"), "utf-8")

    def test_structured_output_is_preferred(self):
        out = json.dumps({"structured_output": {"code": "<svg></svg>", "changes": "x"},
                          "result": "ignored"})
        self.assertEqual(so._parse_iteration(out), ("<svg></svg>", "x"))


class IterationBase(unittest.TestCase):
    def test_next_round_builds_on_the_best_attempt(self):
        # Regression: the loop built on a round that had scored 9.5 points lower.
        history = [{"n": 1, "match": 19.9}, {"n": 2, "match": 56.5}, {"n": 3, "match": 47.0}]
        base, discarded = so.iteration_base(history)
        self.assertEqual(base["n"], 2)
        self.assertEqual(discarded["n"], 3)

    def test_latest_is_used_when_it_is_the_best(self):
        base, discarded = so.iteration_base([{"n": 1, "match": 20.0}, {"n": 2, "match": 60.0}])
        self.assertEqual(base["n"], 2)
        self.assertIsNone(discarded)

    def test_prompt_tells_the_model_what_was_discarded(self):
        run = {"name": "x", "kind": "html", "width": 10, "height": 10}
        report = score("close")
        discarded = {"n": 3, "match": 47.0, "changes": "tightened card rhythm"}
        text = so._iterate_prompt(run, 2, "<div></div>", report, "", discarded)
        self.assertIn("discarded", text)
        self.assertIn("tightened card rhythm", text)
        self.assertIn("attempts/002.png", text)


class BrowserLookup(unittest.TestCase):
    def test_unset_override_is_not_the_current_directory(self):
        # Regression: an empty SPOT_ON_CHROME became Path("."), which exists, so
        # the tool tried to execute the working directory.
        mod = load_tool({"SPOT_ON_CHROME": ""})
        self.assertNotIn(Path("."), mod.CHROME_CANDIDATES)
        self.assertTrue(all(str(c) not in ("", ".") for c in mod.CHROME_CANDIDATES))


class PageGeometry(unittest.TestCase):
    def test_hidpi_capture_renders_at_css_width(self):
        img = Image.new("RGB", (2160, 1350))
        scale, css_w, css_h, w, h = so.page_geometry(img, 1.5)
        self.assertEqual((scale, css_w, css_h), (1.5, 1440, 900))
        self.assertEqual((w, h), (2160, 1350))

    def test_huge_design_is_shrunk_for_scoring_but_keeps_page_size(self):
        img = Image.new("RGB", (2880, 9000))
        scale, css_w, css_h, w, h = so.page_geometry(img, 2)
        self.assertEqual((css_w, css_h), (1440, 4500))
        self.assertLessEqual(w * h, so.MAX_PIXELS)
        self.assertAlmostEqual(w / h, 2880 / 9000, places=2)

    def test_unknown_scale_falls_back_to_one(self):
        self.assertEqual(so.page_geometry(Image.new("RGB", (100, 100)), 3.7)[0], 1.0)

    def test_url_without_scheme_is_rejected(self):
        with self.assertRaises(ValueError):
            so._check_url("localhost:5173/pricing")


def _chrome_available():
    try:
        so._find_chrome()
        return True
    except RuntimeError:
        return False


@unittest.skipUnless(_chrome_available(), "needs Chrome or Edge")
class ServerAndScreenshots(unittest.TestCase):
    """Tier 2: real HTTP server, real headless Chrome, a throwaway runs folder."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="spot-on-test-"))
        so.RUNS_DIR = cls.tmp
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        cls.port = sock.getsockname()[1]
        sock.close()
        so.PORT = cls.port
        cls.server = ThreadingHTTPServer(("127.0.0.1", cls.port), so.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:{}".format(cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def post(self, path, obj):
        req = urllib.request.Request(self.base + path, data=json.dumps(obj).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=180).read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read())

    def design_url(self, img):
        import base64, io
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    def test_svg_attempt_is_screenshot_and_scored(self):
        run = self.post("/runs", {"name": "svg", "kind": "svg", "data_url": self.design_url(DESIGN)})
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">'
               '<rect width="400" height="300" fill="#FFFFFF"/>'
               '<circle cx="200" cy="150" r="80" fill="#52796F"/>'
               '<rect x="30" y="30" width="60" height="60" fill="#C2703F"/></svg>')
        rec = self.post("/attempt", {"run": run["slug"], "code": svg})
        self.assertGreater(rec["match"], 90)
        self.assertTrue((self.tmp / run["slug"] / "attempts" / "001-diff.png").exists())

    def test_running_page_is_screenshot_at_css_size_and_scale(self):
        # The design is a 2x capture, so the page must be rendered 200x150 CSS
        # pixels wide at scale 2, and come back 400x300 like the design.
        run = self.post("/runs", {"name": "url", "kind": "url", "scale": 2,
                                  "data_url": self.design_url(DESIGN)})
        self.assertEqual((run["css_width"], run["css_height"], run["scale"]), (200, 150, 2.0))
        rec = self.post("/attempt", {"run": run["slug"], "code": self.base + "/"})
        self.assertIn("match", rec)
        with Image.open(self.tmp / run["slug"] / "attempts" / "001.png") as shot:
            self.assertEqual(shot.size, (400, 300))

    def test_running_page_is_not_iterated_by_the_server(self):
        run = self.post("/runs", {"name": "url-iter", "kind": "url",
                                  "data_url": self.design_url(DESIGN)})
        self.post("/attempt", {"run": run["slug"], "code": self.base + "/"})
        out = self.post("/iterate", {"run": run["slug"]})
        self.assertIn("session", out.get("error", ""))

    def test_page_script_parses(self):
        # Regression: an apostrophe in UI copy ("design's") closed a single-quoted
        # JavaScript string and would have left the whole page dead.
        node = shutil.which("node")
        if not node:
            self.skipTest("needs node to syntax-check the page script")
        import re, subprocess
        html = urllib.request.urlopen(self.base + "/", timeout=30).read().decode()
        script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
        path = self.tmp / "page.js"
        path.write_text(script, encoding="utf-8")
        proc = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[-600:])

    def test_page_script_is_served(self):
        html = urllib.request.urlopen(self.base + "/", timeout=30).read().decode()
        self.assertTrue("<title>Spot On</title>" in html, "title missing")
        self.assertTrue("127.0.0.1:{}".format(self.port) in html, "port not substituted")


if __name__ == "__main__":
    unittest.main()
