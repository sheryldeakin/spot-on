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


def _chrome_available():
    try:
        so._find_chrome()
        return True
    except RuntimeError:
        return False


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
    """The font is named only on evidence: text in the right place with wrong letters."""

    def test_wrong_font_is_named(self):
        # The design uses Segoe UI, this attempt Arial.
        fx = HERE / "fixtures"
        r, _, _ = so.score_images(Image.open(fx / "pricing-design.png"),
                                  Image.open(fx / "pricing-wrong-font.png"))
        self.assertTrue(any("font family or weight is wrong" in p for p in r["problems"]),
                        r["problems"])

    def test_right_font_with_small_misses_is_not_blamed_on_the_font(self):
        # Regression: the old rule fired whenever colour was fine and structure low,
        # so it kept telling the model to change a font that was already correct,
        # and the loop stalled making sweeping type changes that lowered the score.
        fx = HERE / "fixtures"
        r, _, _ = so.score_images(Image.open(fx / "pricing-design.png"),
                                  Image.open(fx / "pricing-right-font-near.png"))
        self.assertFalse(any("font" in p and "wrong" in p for p in r["problems"]), r["problems"])
        self.assertGreater(r["elements"]["glyph"]["median"], 0.85)

    def test_no_font_hint_for_shapes_with_the_wrong_colour(self):
        self.assertFalse(any("font family" in p for p in score("hue")["problems"]))


class ElementFeedback(unittest.TestCase):
    """Page-wide numbers stop helping when a page is close; name the element instead."""

    def test_taller_boxes_are_named_with_their_size_and_place(self):
        design = page(content_top=90)
        attempt = page(content_top=90)
        d = ImageDraw.Draw(attempt)
        for i in range(3):  # redraw the cards 12px taller
            x = 40 + i * 180
            d.rectangle([x, 150, x + 160, 340], fill="#FFFFFF", outline="#FFFFFF", width=1)
            d.rectangle([x, 150, x + 160, 352], outline="#CBD3CE", width=2,
                        fill="#1F3A30" if i == 1 else "#FFFFFF")
            d.rectangle([x + 20, 180, x + 90, 194], fill="#6A746F")
        r, _, _ = so.score_images(design, attempt)
        text = " ".join(r["problems"])
        self.assertIn("taller", text)
        self.assertRegex(text, r"boxes around y \d+px")

    def test_a_missing_element_is_named_by_position(self):
        design = page(top_label=True)
        attempt = page(top_label=False, content_top=90)  # same layout, label gone
        r, _, _ = so.score_images(design, attempt)
        self.assertTrue(any("Nothing in the attempt matches" in p for p in r["problems"]),
                        r["problems"])

    def test_identical_pages_produce_no_element_findings(self):
        r, _, _ = so.score_images(page(), page())
        self.assertEqual(r["elements"]["groups"], [])

    def test_a_page_wide_shift_is_not_repeated_on_every_element(self):
        r, _, _ = so.score_images(page(top_label=True, content_top=90),
                                  page(top_label=True, content_top=60))
        moved = [g for g in r["elements"]["groups"] if "position" in g[0]["aspects"]]
        self.assertLessEqual(len(moved), 1, [g[0] for g in moved])

    def test_thin_borders_do_not_become_findings(self):
        # Card outlines split into fragments that match nothing; they were reported
        # as missing elements.
        fx = HERE / "fixtures"
        r, _, _ = so.score_images(Image.open(fx / "pricing-design.png"),
                                  Image.open(fx / "pricing-right-font-near.png"))
        self.assertFalse(any("Nothing in the attempt matches" in p for p in r["problems"]),
                         r["problems"])


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

    def test_every_losing_change_is_listed_not_just_the_last(self):
        # Regression: a round re-tried "switch the font stack to Arial first", which
        # an earlier round had already proved lowered the score.
        history = [{"n": 1, "match": 19.9, "changes": "first pass"},
                   {"n": 2, "match": 73.6, "changes": "rebuilt the cards"},
                   {"n": 3, "match": 48.0, "changes": "font stack Arial first"},
                   {"n": 4, "match": 67.4, "changes": "line heights everywhere"}]
        base, _ = so.iteration_base(history)
        rejected = so.rejected_changes(history, base)
        self.assertEqual([r["n"] for r in rejected], [4, 3, 1])
        text = so._iterate_prompt({"name": "x", "kind": "html", "width": 10, "height": 10},
                                  2, "<div></div>", score("close"), "", None, rejected)
        self.assertIn("Arial first", text)
        self.assertIn("line heights everywhere", text)

    def test_prompt_tells_the_model_what_was_discarded(self):
        run = {"name": "x", "kind": "html", "width": 10, "height": 10}
        report = score("close")
        discarded = {"n": 3, "match": 47.0, "changes": "tightened card rhythm"}
        text = so._iterate_prompt(run, 2, "<div></div>", report, "", discarded)
        self.assertIn("discarded", text)
        self.assertIn("tightened card rhythm", text)
        self.assertIn("attempts/002.png", text)


class AgentChoice(unittest.TestCase):
    """The loop runs on whatever AI the machine has, not only on Claude Code."""

    def setUp(self):
        keys = ("SPOT_ON_AGENT", "SPOT_ON_MODEL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                "LOCALAPPDATA")
        self.saved = {k: os.environ.get(k) for k in keys}
        for k in keys:
            os.environ.pop(k, None)
        # The Gemini CLI is found at a fixed path, and Ollama by a request; keep both
        # out of these tests so they check the choice, not this machine.
        self.tmp = Path(tempfile.mkdtemp(prefix="spot-on-agents-"))
        os.environ["LOCALAPPDATA"] = str(self.tmp)
        self.which, self.urlopen = so.shutil.which, so.urllib.request.urlopen

        def no_server(*a, **k):
            raise OSError("no ollama")

        so.urllib.request.urlopen = no_server

    def tearDown(self):
        so.shutil.which = self.which
        so.urllib.request.urlopen = self.urlopen
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def only(self, *found):
        so.shutil.which = lambda name: ("/usr/bin/" + name) if name in found else None

    def test_cli_is_preferred_when_present(self):
        self.only("claude", "codex")
        self.assertEqual(so.pick_agent(), "claude")

    def test_falls_through_to_the_next_available_agent(self):
        self.only("codex")
        self.assertEqual(so.pick_agent(), "codex")

    def test_an_api_key_is_enough_without_any_cli(self):
        self.only()
        os.environ["OPENAI_API_KEY"] = "sk-test"
        self.assertEqual(so.pick_agent(), "openai-api")

    def test_explicit_choice_wins(self):
        self.only("claude", "codex")
        self.assertEqual(so.pick_agent("codex"), "codex")

    def test_explicit_choice_that_is_not_installed_says_so(self):
        self.only("claude")
        with self.assertRaises(ValueError):
            so.pick_agent("codex")

    def test_no_agent_anywhere_explains_the_options(self):
        self.only()
        with self.assertRaises(ValueError) as e:
            so.pick_agent()
        self.assertIn("feedback packet", str(e.exception))


class AgentRequests(unittest.TestCase):
    """Each provider is sent the three images and the prompt in its own shape."""

    def setUp(self):
        self.sent = {}
        self.saved_post = so._post_json
        self.saved_run = so.subprocess.run
        self.saved_which = so.shutil.which
        def fake_post(url, payload, headers, timeout=600):
            self.sent["call"] = {"url": url, "payload": payload, "headers": headers}
            return self.reply(url)

        so._post_json = fake_post
        self.tmp = Path(tempfile.mkdtemp(prefix="spot-on-agent-"))
        self.images = []
        for name in ("design.png", "attempt.png", "diff.png"):
            f = self.tmp / name
            DESIGN.save(f)
            self.images.append(f)

    def tearDown(self):
        so._post_json, so.subprocess.run = self.saved_post, self.saved_run
        so.shutil.which = self.saved_which
        shutil.rmtree(self.tmp, ignore_errors=True)

    def reply(self, url):
        if "anthropic" in url:
            return {"content": [{"type": "text", "text": "<svg>from anthropic</svg>"}]}
        if "openai" in url:
            return {"choices": [{"message": {"content": "<svg>from openai</svg>"}}]}
        return {"message": {"content": "<svg>from ollama</svg>"}}

    def test_anthropic_gets_images_then_the_prompt(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        try:
            out = so._run_api_agent("anthropic-api", "make it match", self.images)
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        content = self.sent["call"]["payload"]["messages"][0]["content"]
        self.assertEqual([c["type"] for c in content], ["image", "image", "image", "text"])
        self.assertEqual(self.sent["call"]["headers"]["anthropic-version"], "2023-06-01")
        self.assertIn("from anthropic", out)

    def test_openai_uses_image_url_blocks(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"
        try:
            out = so._run_api_agent("openai-api", "make it match", self.images)
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
        content = self.sent["call"]["payload"]["messages"][0]["content"]
        self.assertEqual([c["type"] for c in content], ["image_url", "image_url", "image_url", "text"])
        self.assertTrue(content[0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertIn("from openai", out)

    def test_ollama_sends_images_alongside_the_text(self):
        out = so._run_api_agent("ollama", "make it match", self.images)
        msg = self.sent["call"]["payload"]["messages"][0]
        self.assertEqual(len(msg["images"]), 3)
        self.assertIn("from ollama", out)

    def test_cli_agents_get_a_closed_stdin(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw

            class R:
                returncode = 0
                stdout = "```\n<svg>cli</svg>\n```"
                stderr = ""
            return R()

        so.shutil.which = lambda name: "/usr/bin/" + name
        so.subprocess.run = fake_run
        out = so._run_cli_agent("codex", "make it match", self.tmp)
        self.assertIn("exec", seen["cmd"])
        self.assertIsNotNone(seen["kw"].get("stdin"))
        self.assertEqual(seen["kw"].get("encoding"), "utf-8")
        self.assertIn("cli", out)


@unittest.skipUnless(_chrome_available(), "needs Chrome or Edge")
class PageStillness(unittest.TestCase):
    """A page that will not hold still has a ceiling, and the report has to say so."""

    STATIC = '<div style="width:200px;height:120px;background:#52796F"></div>'
    MOVING = ('<div id="b" style="width:200px;height:120px"></div><script>'
              'document.getElementById("b").style.background = '
              '"rgb(" + Math.floor(Math.random()*255) + ",40,40)";</script>')

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="spot-on-still-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_for(self, code):
        run = {"kind": "html", "width": 200, "height": 120, "css_width": 200,
               "css_height": 120, "scale": 1.0, "ground": "#FFFFFF"}
        return so.measure_stability(run, code, self.tmp)

    def test_a_still_page_scores_against_itself(self):
        self.assertGreater(self.run_for(self.STATIC)["match"], 99)

    def test_a_moving_page_does_not(self):
        moving = self.run_for(self.MOVING)
        self.assertLess(moving["match"], 99)
        self.assertGreater(moving["pixels_changed"], 0)

    def test_the_ceiling_is_stated_in_the_report(self):
        run = {"name": "x", "kind": "url", "width": 10, "height": 10,
               "stability": {"match": 77.3, "pixels_changed": 3.27}}
        text = so.feedback_text(run, 1, score("close"))
        self.assertIn("does not hold still", text)
        self.assertIn("77.3", text)

    def test_a_still_page_gets_no_ceiling_warning(self):
        run = {"name": "x", "kind": "url", "width": 10, "height": 10,
               "stability": {"match": 99.6, "pixels_changed": 0.01}}
        self.assertNotIn("hold still", so.feedback_text(run, 1, score("close")))


class BestOfN(unittest.TestCase):
    """Several rewrites per round, in parallel; the highest scoring one wins."""

    SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">'
           '<rect width="400" height="300" fill="#FFFFFF"/>{}</svg>')
    CIRCLE = '<circle cx="200" cy="150" r="{}" fill="#52796F"/>'

    def setUp(self):
        self.saved_agent, self.saved_runs = so.run_agent, so.RUNS_DIR
        self.tmp = Path(tempfile.mkdtemp(prefix="spot-on-bestof-"))
        so.RUNS_DIR = self.tmp

    def tearDown(self):
        so.run_agent, so.RUNS_DIR = self.saved_agent, self.saved_runs
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed_run(self):
        run = so.create_run("bestof", "svg", reference_bytes=_png_bytes(DESIGN))
        so.record_attempt("bestof", self.SVG.format(self.CIRCLE.format(20)))
        return run

    def answer(self, *bodies):
        seen = []

        def fake(agent, prompt, cwd, images):
            body = bodies[len(seen) % len(bodies)]
            seen.append(body)
            return "```\n" + body + "\n```"

        so.run_agent = fake
        return seen

    @unittest.skipUnless(_chrome_available(), "needs Chrome or Edge")
    def test_the_best_candidate_is_returned_and_all_are_kept(self):
        self.seed_run()
        # radius 80 matches the design; 20 and 140 do not.
        self.answer(self.SVG.format(self.CIRCLE.format(140)),
                    self.SVG.format(self.CIRCLE.format(80)),
                    self.SVG.format(self.CIRCLE.format(30)))
        best = so.run_iteration("bestof", candidates=3)
        self.assertEqual(len(best["candidate_scores"]), 3)
        self.assertEqual(best["match"], max(best["candidate_scores"]))
        self.assertEqual(len(so._attempts("bestof")), 4)  # the seed plus three candidates
        self.assertEqual(so._load_run("bestof")["best_attempt"], best["n"])
        self.assertEqual(best["candidate_of"], 1)

    @unittest.skipUnless(_chrome_available(), "needs Chrome or Edge")
    def test_one_refusal_does_not_sink_the_round(self):
        self.seed_run()
        self.answer("You have reached your usage limit.",
                    self.SVG.format(self.CIRCLE.format(80)))
        best = so.run_iteration("bestof", candidates=2)
        self.assertEqual(len(best["candidate_scores"]), 1)

    def test_every_candidate_refusing_is_an_error_that_quotes_it(self):
        self.seed_run()
        self.answer("You have reached your usage limit.")
        with self.assertRaises(RuntimeError) as e:
            so.run_iteration("bestof", candidates=2)
        self.assertIn("usage limit", str(e.exception))

    def test_count_is_clamped_and_defaults_to_three(self):
        seen = {"n": 0}

        def counting(agent, prompt, cwd, images, count, kind):
            seen["n"] = count
            return [], ["nothing"]

        saved = so.gather_candidates
        so.gather_candidates = counting
        self.seed_run()
        try:
            for asked, expected in ((None, 3), (1, 1), (99, 5)):
                with self.assertRaises(RuntimeError):
                    so.run_iteration("bestof", candidates=asked)
                self.assertEqual(seen["n"], expected)
        finally:
            so.gather_candidates = saved

    def test_candidates_run_at_the_same_time(self):
        import threading
        import time as _time
        active, peak, lock = [0], [0], threading.Lock()

        def slow(agent, prompt, cwd, images):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            _time.sleep(0.2)
            with lock:
                active[0] -= 1
            return "```\n" + self.SVG.format(self.CIRCLE.format(80)) + "\n```"

        so.run_agent = slow
        drafts, _ = so.gather_candidates("claude", "p", self.tmp, [], 3, "svg")
        self.assertEqual(len(drafts), 3)
        self.assertGreater(peak[0], 1, "candidates ran one after another")


class PromptShape(unittest.TestCase):
    def test_far_off_pages_are_told_to_fix_everything(self):
        text = so._iterate_prompt({"name": "x", "kind": "html", "width": 10, "height": 10},
                                  1, "<div></div>", score("wrong"), "")
        self.assertIn("fix everything the report names", text)

    def test_close_pages_are_held_to_three_changes(self):
        text = so._iterate_prompt({"name": "x", "kind": "html", "width": 10, "height": 10},
                                  1, "<div></div>", score("close"), "")
        self.assertIn("at most three things", text)

    def test_agents_that_cannot_open_files_are_told_the_images_are_attached(self):
        text = so._iterate_prompt({"name": "x", "kind": "html", "width": 10, "height": 10},
                                  1, "<div></div>", score("close"), "", None, (), False)
        self.assertIn("attached", text)


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

    def test_a_running_page_is_measured_for_stillness_once(self):
        run = self.post("/runs", {"name": "still", "kind": "url",
                                  "data_url": self.design_url(DESIGN)})
        rec = self.post("/attempt", {"run": run["slug"], "code": self.base + "/"})
        self.assertIn("stability", rec)
        self.assertGreater(rec["stability"]["match"], 95)  # the tool's own page holds still
        saved = json.loads((self.tmp / run["slug"] / "run.json").read_text(encoding="utf-8"))
        self.assertIn("stability", saved)

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

    def test_prompt_endpoint_serves_a_round_for_an_in_page_model(self):
        # Chrome's built-in model runs in the page, so the page asks for the same
        # prompt the server loop would have used.
        run = self.post("/runs", {"name": "browser", "kind": "svg",
                                  "data_url": self.design_url(DESIGN)})
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"></svg>'
        self.post("/attempt", {"run": run["slug"], "code": svg})
        out = json.loads(urllib.request.urlopen(
            self.base + "/prompt?run=" + run["slug"], timeout=30).read())
        self.assertEqual(out["attempt"], 1)
        self.assertEqual(out["images"],
                         ["reference.png", "attempts/001.png", "attempts/001-diff.png"])
        self.assertIn("attached", out["prompt"])
        self.assertNotIn("Read reference.png", out["prompt"])

    def test_an_attempt_can_record_who_wrote_it(self):
        run = self.post("/runs", {"name": "browser-src", "kind": "svg",
                                  "data_url": self.design_url(DESIGN)})
        rec = self.post("/attempt", {
            "run": run["slug"], "source": "chrome-builtin", "changes": "in-page model",
            "code": '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"></svg>'})
        self.assertEqual(rec["source"], "chrome-builtin")
        self.assertEqual(rec["changes"], "in-page model")

    def test_page_script_is_served(self):
        html = urllib.request.urlopen(self.base + "/", timeout=30).read().decode()
        self.assertTrue("<title>Spot On</title>" in html, "title missing")
        self.assertTrue("127.0.0.1:{}".format(self.port) in html, "port not substituted")


if __name__ == "__main__":
    unittest.main()
