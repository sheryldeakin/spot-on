"""Tests for spot-on.py.

Tier 1 (scoring, guards, geometry) runs anywhere with numpy and pillow.
Tier 2 (the HTTP server and real screenshots) needs Chrome or Edge and is
skipped without one.

    python -m unittest discover -s tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

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
        # Narrowed to the one sentence this guards, the same one the wrong-font test
        # above asserts is present. A measured font-size difference is a different
        # claim: the glyph check here confirms the family is right, so "right family,
        # wrong size" is a useful decomposition rather than a relapse into blaming the
        # typeface. The added weak_share check makes the guard stricter, not looser.
        self.assertFalse(any("font family or weight is wrong" in p for p in r["problems"]),
                         r["problems"])
        self.assertGreater(r["elements"]["glyph"]["median"], 0.85)
        self.assertEqual(r["elements"]["glyph"]["weak_share"], 0.0)

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

        def fake_run(cmd, timeout=None, **kw):
            seen.update(kw)
            raise Done()

        tmp = Path(tempfile.mkdtemp(prefix="spot-on-enc-"))
        saved_runs, saved_run = so.RUNS_DIR, so._run_tree
        try:
            so.RUNS_DIR = tmp
            so.create_run("enc", "html", reference_bytes=_png_bytes(DESIGN))
            d = tmp / "enc" / "attempts"
            (d / "001.code").write_text("<div></div>", encoding="utf-8")
            (d / "001.json").write_text(json.dumps({"n": 1, "match": 10.0, "report": score("blank")}),
                                        encoding="utf-8")
            so._run_tree = fake_run
            with self.assertRaises(Done):
                so.run_iteration("enc")
        finally:
            so._run_tree, so.RUNS_DIR = saved_run, saved_runs
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
        self.saved_run = so._run_tree
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
        so._post_json, so._run_tree = self.saved_post, self.saved_run
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

        def fake_run(cmd, timeout=None, **kw):
            seen["cmd"], seen["kw"] = cmd, kw

            class R:
                returncode = 0
                stdout = "```\n<svg>cli</svg>\n```"
                stderr = ""
            return R()

        so.shutil.which = lambda name: "/usr/bin/" + name
        so._run_tree = fake_run
        out = so._run_cli_agent("codex", "make it match", self.tmp)
        self.assertIn("exec", seen["cmd"])
        self.assertIsNotNone(seen["kw"].get("stdin"))
        self.assertEqual(seen["kw"].get("encoding"), "utf-8")
        self.assertIn("cli", out)


@unittest.skipUnless(_chrome_available(), "needs Chrome or Edge")
class PageStillness(unittest.TestCase):
    """A page that will not hold still has a ceiling, and the report has to say so."""

    STATIC = '<div style="width:200px;height:120px;background:#52796F"></div>'
    # Both moving fixtures paint per-pixel noise rather than one random colour. With a
    # single random channel two renders could land close enough that nothing crossed the
    # per-pixel threshold, so the page read as perfectly still and the test failed on a
    # bad draw. Noise differs on essentially every pixel every time, which makes "this
    # moves" a property of the fixture instead of a lucky roll.
    _NOISE = ('var x=document.getElementById("{id}").getContext("2d"),'
              'd=x.createImageData({w},{h});'
              'for(var i=0;i<d.data.length;i+=4){{'
              'd.data[i]=Math.random()*255;d.data[i+1]=Math.random()*255;'
              'd.data[i+2]=Math.random()*255;d.data[i+3]=255;}}'
              'x.putImageData(d,0,0);')
    # A still layout with one element that changes every render, like a carousel.
    MOVING = ('<div style="width:200px;height:120px;background:#52796F">'
              '<canvas id="b" width="60" height="40" style="margin:10px"></canvas></div>'
              '<script>' + _NOISE.format(id="b", w=60, h=40) + '</script>')
    ALL_MOVING = ('<canvas id="c" width="200" height="120"></canvas>'
                  '<script>' + _NOISE.format(id="c", w=200, h=120) + '</script>')

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="spot-on-still-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_for(self, code):
        run = {"kind": "html", "width": 200, "height": 120, "css_width": 200,
               "css_height": 120, "scale": 1.0, "ground": "#FFFFFF"}
        stats, mask = so.measure_stability(run, code, self.tmp)
        return stats

    def test_the_first_capture_is_thrown_away(self):
        # Regression: measuring a cold page masked out the 3D avatar that had not
        # finished loading, and excluded it from the score for the whole run.
        shots = []
        saved = so.render_code

        def counting(code, kind, w, h, out, **kw):
            shots.append(Path(out).name)
            return saved(code, kind, w, h, out, **kw)

        so.render_code = counting
        try:
            self.run_for(self.STATIC)
        finally:
            so.render_code = saved
        self.assertEqual(len(shots), 3, shots)
        self.assertIn("stability-warmup.png", shots)

    def test_a_still_page_scores_against_itself(self):
        self.assertGreater(self.run_for(self.STATIC)["match"], 99)

    def test_a_moving_element_is_found_and_excluded(self):
        moving = self.run_for(self.MOVING)
        self.assertLess(moving["raw_match"], 99)       # it does not match itself
        self.assertGreater(moving["match"], 99)        # until the moving part is excluded
        self.assertGreater(moving["ignored_pct"], 0)
        self.assertLess(moving["ignored_pct"], 60)
        self.assertFalse(moving.get("unscoreable"))

    def test_the_excluded_area_is_stated_in_the_report(self):
        run = {"name": "x", "kind": "url", "width": 10, "height": 10,
               "stability": {"match": 100.0, "raw_match": 80.3, "ignored_pct": 3.6}}
        text = so.feedback_text(run, 1, score("close"))
        self.assertIn("3.6% of this page moves", text)
        self.assertIn("excluded from the score", text)

    def test_a_still_page_gets_no_warning(self):
        run = {"name": "x", "kind": "url", "width": 10, "height": 10,
               "stability": {"match": 99.6, "raw_match": 99.6, "ignored_pct": 0.0}}
        self.assertNotIn("moves between screenshots",
                         so.feedback_text(run, 1, score("close")))

    def test_a_page_that_moves_everywhere_is_called_unscoreable(self):
        # Excluding nearly everything would leave nothing to compare, and every
        # attempt would come back a meaningless 100.
        stats = self.run_for(self.ALL_MOVING)
        self.assertGreater(stats["ignored_pct"], 60)
        run = {"name": "x", "kind": "url", "width": 10, "height": 10,
               "stability": dict(stats, unscoreable=True)}
        text = so.feedback_text(run, 1, score("close"))
        self.assertIn("too much to score", text)


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

        def counting(agent, prompt, cwd, images, count, kind, panel=False):
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

    def test_an_agent_that_is_sent_the_images_is_told_they_are_attached(self):
        text = so._iterate_prompt({"name": "x", "kind": "html", "width": 10, "height": 10},
                                  1, "<div></div>", score("close"), "", None, (), "attached")
        self.assertIn("attached", text)
        self.assertNotIn("Read reference.png", text)

    def test_an_agent_with_no_images_is_told_so_and_not_sent_looking(self):
        # Regression: every CLI was told to read the three images. Gemini's headless
        # mode auto-denies that permission and returned nothing at all, every round,
        # while the report still talked about a difference map it could not open.
        text = so._iterate_prompt({"name": "x", "kind": "html", "width": 10, "height": 10},
                                  1, "<div></div>", score("close"), "", None, (), "none")
        self.assertIn("cannot see the page", text)
        self.assertNotIn("Read reference.png", text)
        self.assertNotIn("difference map", text)

    def test_image_access_is_stated_per_agent(self):
        self.assertEqual(so.image_mode("claude"), "read")
        self.assertEqual(so.image_mode("gemini"), "none")
        self.assertEqual(so.image_mode("anthropic-api"), "attached")


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


def typeset(stroke=2, bar_h=26, gaps=(24, 24), bg="#F5F8FF"):
    """Rows of vertical strokes: a stand-in for text whose weight can be varied.

    Solid bars cannot express weight, and horizontal stripes break the row-band
    reading that measures height. Strokes of a given thickness on a fixed pitch change
    how much of the box is ink while leaving the box itself the same size.
    """
    img = Image.new("RGB", (360, 320), bg)
    d = ImageDraw.Draw(img)
    y = 40
    for gap in (0,) + tuple(gaps):
        y += gap
        for x in range(40, 300, 8):
            d.rectangle([x, y, x + stroke - 1, y + bar_h - 1], fill="#1B2A3A")
        y += bar_h
    return img


def stack(bg="#F5F8FF", gaps=(24, 24), bar_h=26, top=40, indent=0):
    """A page-like scene: a column of dark bars on a tinted ground.

    Bars stand in for text. They give the element matcher something to match and the
    gaps between them something to measure.
    """
    img = Image.new("RGB", (360, 320), bg)
    d = ImageDraw.Draw(img)
    y = top
    for i, gap in enumerate((0,) + tuple(gaps)):
        y += gap
        x0 = 40 + (indent if i % 2 else 0)
        d.rectangle([x0, y, x0 + 260, y + bar_h - 1], fill="#1B2A3A")
        y += bar_h
    return img


class ReportNamesColourAndSpacing(unittest.TestCase):
    """The two things the report could never say, found by judging real pages."""

    def problems(self, design, attempt):
        return so.score_images(design, attempt)[0]["problems"]

    def test_a_wrong_page_colour_is_reported_even_when_elements_also_miss(self):
        # Regression: component problems were reached through a loop that broke as soon
        # as there were element lines, and a real page always has element lines. A page
        # could score 64.5 on colour and be told nothing about colour, round after round.
        problems = self.problems(stack(), stack(bg="#D2E0F6", gaps=(24, 30)))
        self.assertTrue(any("page behind the content" in p for p in problems), problems)

    def test_a_matching_page_colour_is_not_reported(self):
        self.assertFalse(any("page behind the content" in p
                             for p in self.problems(stack(), stack(gaps=(24, 30)))))

    def test_the_colour_named_is_where_it_is_worst_not_at_the_border(self):
        # A gradient can match at the edge the ground is read from and be far off in the
        # middle. Naming the two border colours would send the next round chasing nothing.
        design = stack()
        attempt = stack()
        px = attempt.load()
        for y in range(160, 320):          # only the lower half is wrong
            for x in range(360):
                if px[x, y] == (245, 248, 255):
                    px[x, y] = (200, 216, 244)
        report = so.score_images(design, attempt)[0]
        self.assertLess(report["components"]["colour"], 95)
        self.assertIn((report["raw"]["background_where"] or "").split(",")[0],
                      ("lower middle", "bottom"))
        self.assertNotEqual(report["raw"]["background_reference_hex"],
                            report["raw"]["background_attempt_hex"])

    def test_a_gap_that_is_too_big_is_named_with_both_numbers(self):
        # Indented, so these read as separate blocks rather than lines of one, which
        # would be reported as line-height instead.
        report = so.score_images(stack(gaps=(20, 20), indent=40),
                                 stack(gaps=(20, 60), indent=40))[0]
        spacing = report["elements"]["spacing"]
        self.assertTrue(spacing, "the 40px wider gap was not found")
        s = spacing[0]
        # The measured gap runs between detected edges, a little tighter than the drawn
        # one, so the difference is what is checked rather than the absolute pixels.
        self.assertAlmostEqual(s["diff"], 40, delta=5)
        self.assertGreater(s["attempt"], s["design"])
        line = [p for p in report["problems"] if "gap above" in p][0]
        self.assertIn("is {}px".format(s["attempt"]), line)
        self.assertIn("the design has {}px".format(s["design"]), line)
        self.assertIn("too big", line)

    def test_bigger_text_is_reported_as_font_size(self):
        problems = self.problems(typeset(bar_h=20), typeset(bar_h=32))
        line = [p for p in problems if "font-size" in p]
        self.assertTrue(line, problems)
        self.assertIn("px tall", line[0])

    def test_heavier_text_is_reported_as_font_weight(self):
        # Same boxes on the same pitch, thicker strokes inside them: weight, not size.
        problems = self.problems(typeset(stroke=2), typeset(stroke=5))
        line = [p for p in problems if "font-weight" in p]
        self.assertTrue(line, problems)
        self.assertIn("% of its box is ink", line[0])
        self.assertIn("heavier", line[0])

    def test_type_is_silent_when_the_type_matches(self):
        # The typeface hint used to fire on a page whose font was already right and
        # cost a round every time it did. Silence here is the whole point.
        problems = self.problems(typeset(gaps=(24, 24)), typeset(gaps=(24, 40)))
        self.assertFalse([p for p in problems if "font-size" in p or "font-weight" in p],
                         problems)

    def test_lines_of_one_block_are_reported_as_line_height(self):
        problems = self.problems(stack(gaps=(8, 8), bar_h=22), stack(gaps=(18, 18), bar_h=22))
        self.assertTrue([p for p in problems if "line-height" in p], problems)

    def test_a_gap_far_too_large_is_not_called_line_height(self):
        # A design gap of 8px that became 90px is not a line height eleven times too
        # big; it is something inserted. Saying line-height would set the wrong property.
        report = so.score_images(stack(gaps=(8, 8), bar_h=22), stack(gaps=(90, 8), bar_h=22))[0]
        self.assertTrue(report["elements"]["spacing"])
        self.assertFalse(any(s.get("leading") and s["attempt"] > 60
                             for s in report["elements"]["spacing"]))

    def test_a_page_wide_shift_is_not_reported_as_a_spacing_problem(self):
        # A gap is the distance between two elements, so shifting everything down
        # together leaves every gap identical. Reporting those would be noise on top
        # of the one shift sentence that already explains it.
        design = stack(gaps=(20, 20), top=40)
        shifted = stack(gaps=(20, 20), top=70)
        spacing = so.score_images(design, shifted)[0]["elements"]["spacing"]
        self.assertEqual(spacing, [])


class LongPromptsSurviveWindowsShims(unittest.TestCase):
    """A prompt carrying a real page is longer than cmd.exe will pass as an argument."""

    def setUp(self):
        self.saved_run, self.saved_which = so._run_tree, so.shutil.which
        self.tmp = Path(tempfile.mkdtemp(prefix="spot-on-arg-"))
        self.seen = {}

        def fake(cmd, timeout=None, **kw):
            self.seen["cmd"] = cmd

            class R:
                returncode = 0
                stdout = "```\n<svg>ok</svg>\n```"
                stderr = ""
            return R()

        so._run_tree = fake

    def tearDown(self):
        so._run_tree, so.shutil.which = self.saved_run, self.saved_which
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_long_prompt_to_a_cmd_shim_goes_through_a_file(self):
        # Regression: codex and gemini are installed as .cmd shims, so their command
        # line runs through cmd.exe and truncates at 8191 characters. A real rebuild
        # died with "The command line is too long"; a trivial prompt did not, which is
        # why it only appeared when the panel was run on an actual page.
        so.shutil.which = lambda name: "C:\\npm\\codex.CMD"
        long_prompt = "do the thing\n" + ("filler " * 2000)
        so._run_cli_agent("codex", long_prompt, self.tmp)
        passed = " ".join(str(c) for c in self.seen["cmd"])
        self.assertLess(len(passed), so.ARG_SAFE_CHARS)
        self.assertIn("prompt.txt", passed)
        self.assertEqual((self.tmp / "prompt.txt").read_text(encoding="utf-8"), long_prompt)

    def test_a_short_prompt_is_still_passed_directly(self):
        so.shutil.which = lambda name: "C:\\npm\\codex.CMD"
        so._run_cli_agent("codex", "make it match", self.tmp)
        self.assertIn("make it match", " ".join(str(c) for c in self.seen["cmd"]))
        self.assertFalse((self.tmp / "prompt.txt").exists())

    def test_gemini_is_asked_to_answer_without_tools(self):
        # Regression: it reached for a tool unprompted, headless mode auto-denied the
        # permission, and it returned nothing at all with the reason only on stderr.
        # Every round silently came back a draft short.
        so.shutil.which = lambda name: "C:\bin\agy.exe"
        so._run_cli_agent("gemini", "make it match", self.tmp)
        sent = " ".join(str(c) for c in self.seen["cmd"])
        self.assertIn("Do not use any tools", sent)
        self.assertIn("make it match", sent)

    def test_the_others_are_not_told_that(self):
        so.shutil.which = lambda name: "C:\bin\claude.exe"
        so._run_cli_agent("claude", "make it match", self.tmp)
        self.assertNotIn("Do not use any tools", " ".join(str(c) for c in self.seen["cmd"]))

    def test_a_real_executable_keeps_the_whole_prompt(self):
        # claude.exe is not a shim, so it is not subject to the cmd.exe limit.
        so.shutil.which = lambda name: "C:\\bin\\claude.exe"
        long_prompt = "do the thing\n" + ("filler " * 2000)
        so._run_cli_agent("claude", long_prompt, self.tmp)
        self.assertIn(long_prompt, self.seen["cmd"])
        self.assertFalse((self.tmp / "prompt.txt").exists())


class PanelOfModels(unittest.TestCase):
    """A round can draw from several models instead of sampling one three times."""

    def setUp(self):
        self.saved = so._agent_available
        so._agent_available = lambda a: a in ("claude", "codex", "gemini")

    def tearDown(self):
        so._agent_available = self.saved

    def panel(self, count, env):
        keep = os.environ.get("SPOT_ON_PANEL")
        if env is None:
            os.environ.pop("SPOT_ON_PANEL", None)
        else:
            os.environ["SPOT_ON_PANEL"] = env
        try:
            return so.panel_agents("claude", count)
        finally:
            os.environ.pop("SPOT_ON_PANEL", None)
            if keep is not None:
                os.environ["SPOT_ON_PANEL"] = keep

    def test_off_by_default_so_nothing_changes(self):
        self.assertEqual(self.panel(3, None), ["claude"] * 3)

    def test_it_spreads_across_the_models_the_machine_has(self):
        self.assertEqual(self.panel(3, "1"), ["claude", "codex", "gemini"])

    def test_the_page_can_ask_for_it_without_an_environment_variable(self):
        # The picker sends a flag; nobody should have to know an env var exists.
        self.assertEqual(so.panel_agents("claude", 3, panel=True),
                         ["claude", "codex", "gemini"])
        self.assertEqual(so.panel_agents("claude", 3, panel=False), ["claude"] * 3)

    def test_the_preferred_agent_goes_first(self):
        self.assertEqual(self.panel(1, "1"), ["claude"])

    def test_it_wraps_when_fewer_models_than_draws(self):
        so._agent_available = lambda a: a == "codex"
        self.assertEqual(self.panel(3, "1"), ["claude", "codex", "claude"])

    def test_one_agent_machine_behaves_as_before(self):
        so._agent_available = lambda a: False
        self.assertEqual(self.panel(3, "1"), ["claude"] * 3)


class FontFromTheSource(unittest.TestCase):
    """Pixels cannot name a typeface, but a live design's own markup can."""

    DOM = ('<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;700">'
           "<style>body{font-family:'Poppins',Inter,sans-serif} .a{font-family:Georgia,serif}"
           " .b{font-family:var(--theme-font)} .c{font-family:sans-serif}</style>")

    def test_a_loaded_webfont_outranks_a_fallback_stack(self):
        self.assertEqual(so.fonts_in_source(self.DOM)[0], "Poppins")

    def test_generic_families_and_variables_are_not_typefaces(self):
        found = so.fonts_in_source(self.DOM)
        for junk in ("sans-serif", "serif", "var(--theme-font)", "--theme-font"):
            self.assertNotIn(junk, found)

    def test_nothing_is_invented_when_the_page_names_nothing(self):
        self.assertEqual(so.fonts_in_source("<p>no styles here</p>"), [])

    def test_the_name_is_given_only_when_the_typeface_is_wrong(self):
        fx = HERE / "fixtures"
        design = Image.open(fx / "pricing-design.png")

        def named(attempt, fonts):
            r, _, _ = so.score_images(design, Image.open(fx / attempt), design_fonts=fonts)
            return any("own source asks for" in p for p in r["problems"])

        self.assertTrue(named("pricing-wrong-font.png", ["Poppins"]))
        self.assertFalse(named("pricing-wrong-font.png", None))
        # Already correct: naming a font here is what used to cost a whole round.
        self.assertFalse(named("pricing-right-font-near.png", ["Poppins"]))


def typed(font="arial.ttf", underline=False, bold_word=None, size=22,
          text="Understand how we protect your data"):
    """Real text from a real font file, so bold, italic and underline are genuine."""
    img = Image.new("RGB", (520, 70), "#FFFFFF")
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype("C:/Windows/Fonts/" + font, size)
    d.text((20, 20), text, font=f, fill="#1B2A3A")
    if bold_word is not None:
        fb = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", size)
        words = text.split()
        before = " ".join(words[:bold_word]) + (" " if bold_word else "")
        x = 20 + d.textlength(before, font=f)
        d.rectangle([x - 1, 18, x + d.textlength(words[bold_word], font=f) + 1, 18 + size + 8],
                    fill="#FFFFFF")
        d.text((x, 20), words[bold_word], font=fb, fill="#1B2A3A")
    if underline:
        d.rectangle([20, 20 + size + 4, 20 + d.textlength(text, font=f), 20 + size + 5],
                    fill="#1B2A3A")
    return img


@unittest.skipUnless(Path("C:/Windows/Fonts/ariali.ttf").exists(), "needs the Arial family")
class EmphasisIsMeasured(unittest.TestCase):
    """Bold, italic and underline are design instructions the report walked past."""

    def finds(self, design, attempt):
        return so.score_images(design, attempt)[0]["elements"]["emphasis"]

    def props(self, design, attempt):
        return {f["prop"] for f in self.finds(design, attempt)}

    def test_an_underline_that_is_missing_is_named(self):
        self.assertIn("underline", self.props(typed(underline=True), typed()))

    def test_an_underline_that_should_not_be_there_is_named(self):
        self.assertIn("underline", self.props(typed(), typed(underline=True)))

    def test_italic_against_upright_is_named(self):
        self.assertIn("italic", self.props(typed("ariali.ttf"), typed()))

    def test_one_word_left_unbolded_is_named(self):
        # The case that started this: most of the line matches and one word in the
        # design is set heavier. A whole-line weight check cannot see it.
        self.assertIn("part-weight", self.props(typed(bold_word=3), typed()))

    def test_identical_text_says_nothing(self):
        self.assertEqual(self.finds(typed(), typed()), [])

    def test_a_whole_line_in_bold_is_not_called_a_part(self):
        # That is the weight of the line, which font-weight already reports; calling it
        # a span would send the next round hunting a word that is not there.
        self.assertNotIn("part-weight", self.props(typed("arialbd.ttf"), typed()))

    def shape(self, kind):
        """Not text: a filled pill, a solid circle, a wrapped paragraph."""
        img = Image.new("RGB", (520, 90), "#FFFFFF")
        d = ImageDraw.Draw(img)
        if kind == "pill":
            d.rounded_rectangle([20, 20, 200, 62], radius=21, fill="#1B2A3A")
        elif kind == "circle":
            d.ellipse([20, 18, 78, 76], fill="#1B2A3A")
        else:
            f = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 18)
            for i in range(3):
                d.text((20, 18 + i * 24), "wrapped line of body copy here", font=f,
                       fill="#1B2A3A")
        return img

    def test_a_solid_shape_is_not_reported_as_underlined(self):
        # Regression: the attempt drew a plain dark circle where the design had a logo,
        # and the circle's fully inked bottom rows scored a perfect underline.
        self.assertEqual(self.finds(typed(), self.shape("circle")), [])
        self.assertEqual(self.finds(self.shape("circle"), typed()), [])

    def test_a_pill_button_is_not_reported_as_italic(self):
        # Regression: a rounded button's ends tilt the measured stroke angle to about
        # -8.6 degrees, which read as italic. Setting font-style on a button is exactly
        # the wrong instruction.
        self.assertNotIn("italic", self.props(self.shape("pill"), typed()))

    def test_a_wrapped_paragraph_is_not_compared_with_a_single_line(self):
        # Regression: a 597x21 one-line design box matched a 604x66 three-line attempt
        # box, and the design's only line was compared against the attempt's last one,
        # producing an underline and an italic that were neither.
        self.assertEqual(self.finds(typed(), self.shape("paragraph")), [])

    def test_an_underline_is_not_mistaken_for_italic(self):
        # A long horizontal rule dominates the gradients, so underlined text read as
        # slanted until the rule was dropped before measuring the angle.
        self.assertNotIn("italic", self.props(typed(underline=True), typed()))


class RowSpacing(unittest.TestCase):
    """A row of links is spaced along the other axis, and nothing measured it."""

    def row(self, gap, n=5, y=60, w=48):
        img = Image.new("RGB", (460, 200), "#F5F8FF")
        d = ImageDraw.Draw(img)
        x = 40
        for _ in range(n):
            d.rectangle([x, y, x + w, y + 22], fill="#1B2A3A")
            x += w + gap
        return img

    def finds(self, a, b):
        return so.score_images(a, b)[0]["elements"]["row_spacing"]

    def test_a_row_spread_too_wide_is_reported_once(self):
        found = self.finds(self.row(20), self.row(46))
        self.assertTrue(found, "the wider row spacing was not noticed")
        self.assertGreater(found[0]["attempt"], found[0]["design"])
        problems = so.score_images(self.row(20), self.row(46))[0]["problems"]
        self.assertEqual(sum(1 for p in problems if "items in the row" in p), 1)
        self.assertIn("more spread out", [p for p in problems if "items in the row" in p][0])

    def test_a_row_packed_too_tightly_says_so(self):
        found = self.finds(self.row(46), self.row(20))
        self.assertTrue(found)
        self.assertLess(found[0]["attempt"], found[0]["design"])

    def test_a_row_that_matches_is_not_reported(self):
        self.assertEqual(self.finds(self.row(20), self.row(20)), [])

    def test_two_items_are_not_a_row(self):
        # Two things side by side say nothing about a container's spacing rule.
        self.assertEqual(self.finds(self.row(20, n=2), self.row(60, n=2)), [])


class ReportWatchesItself(unittest.TestCase):
    """A component scoring badly with no sentence attached is a blind spot, and it
    should say so rather than let the silence pass."""

    REPORT = {"components": {"structure": 87.0, "shape": 87.2, "colour": 64.5,
                             "detail": 80.8, "coverage": 99.6}}

    def test_the_colour_blind_spot_would_have_been_caught(self):
        # Colour scored 64.5 for six rounds while every sentence was about text, and
        # nothing was watching for the absence. This is that watch.
        said = so._unexplained(self.REPORT, {"element", "type"})
        self.assertTrue(said)
        self.assertIn("colour at 64.5", said[0])

    def test_it_is_silent_once_the_fault_is_named(self):
        self.assertEqual(so._unexplained(self.REPORT, {"colour", "element"}), [])

    def test_it_is_silent_when_nothing_scores_badly(self):
        good = {"components": {"structure": 95.0, "shape": 93.0, "colour": 99.0,
                               "detail": 91.0, "coverage": 100.0}}
        self.assertEqual(so._unexplained(good, set()), [])

    def test_missing_elements_count_as_explaining_low_coverage(self):
        low = {"components": {"coverage": 60.0}}
        self.assertEqual(so._unexplained(low, {"coverage"}), [])
        self.assertTrue(so._unexplained(low, {"type"}))

    def test_a_real_report_carries_the_check(self):
        # The wiring, not just the helper: a report built the normal way runs it.
        problems = so.score_images(stack(), stack(bg="#D2E0F6", gaps=(24, 30)))[0]["problems"]
        self.assertIsInstance(problems, list)
        self.assertFalse(any("nothing above to explain it: colour" in p for p in problems),
                         "colour is named here, so it must not be reported as unexplained")


class AlignmentIsOneContainer(unittest.TestCase):
    """Left edges that line up in the design are one fix, not one fix per element."""

    def column(self, offsets):
        img = Image.new("RGB", (360, 320), "#F5F8FF")
        d = ImageDraw.Draw(img)
        for i, dx in enumerate(offsets):
            y = 40 + i * 50
            d.rectangle([40 + dx, y, 40 + dx + 200, y + 26], fill="#1B2A3A")
        return img

    def finds(self, design, attempt):
        return so.score_images(design, attempt)[0]["elements"]["alignment"]

    def test_a_ragged_column_is_reported_once(self):
        found = self.finds(self.column([0, 0, 0, 0]), self.column([0, 26, 0, 22]))
        self.assertTrue(found, "ragged left edges were not noticed")
        self.assertEqual(found[0]["kind"], "ragged")
        problems = so.score_images(self.column([0, 0, 0, 0]),
                                   self.column([0, 26, 0, 22]))[0]["problems"]
        self.assertEqual(sum(1 for p in problems if "share a left edge" in p), 1)

    def test_a_whole_column_moved_is_reported_as_one_container(self):
        found = self.finds(self.column([0, 0, 0, 0]), self.column([20, 20, 20, 20]))
        self.assertTrue(found)
        self.assertEqual(found[0]["kind"], "shifted")

    def test_a_column_that_lines_up_is_not_reported(self):
        self.assertEqual(self.finds(self.column([0, 0, 0, 0]), self.column([0, 0, 0, 0])), [])

    def test_a_column_ragged_in_the_design_too_is_not_reported(self):
        # Only edges the design actually aligns are worth aligning.
        self.assertEqual(self.finds(self.column([0, 30, 0, 30]), self.column([0, 26, 0, 22])), [])


class PressingUnfixedProblems(unittest.TestCase):
    """A fault the round was asked to fix and did not is the reason a run stalls."""

    def record(self, n, match, **report):
        base = {"components": {"coverage": 100, "colour": 100}, "offsets": {},
                "elements": {}, "raw": {}}
        base.update(report)
        return {"n": n, "match": match, "report": base}

    def test_a_fault_present_in_every_earlier_best_is_counted(self):
        shift = {"offsets": {"vertical": {"css_shift": -8, "from_css_y": 50, "where": "top"}}}
        history = [self.record(1, 10.0, **shift), self.record(2, 20.0, **shift),
                   self.record(3, 30.0, **shift)]
        stuck = so.stuck_problems(history, history[-1])
        self.assertEqual([s["key"] for s in stuck], [["shift", "vertical"]])
        self.assertEqual(stuck[0]["rounds"], 2)

    def test_a_fault_that_was_fixed_is_not_counted(self):
        shift = {"offsets": {"vertical": {"css_shift": -8, "from_css_y": 50, "where": "top"}}}
        history = [self.record(1, 10.0, **shift), self.record(2, 20.0)]
        self.assertEqual(so.stuck_problems(history, history[-1]), [])

    def test_only_the_climb_counts_not_discarded_attempts(self):
        # Rounds that scored worse are thrown away, so a fault they happened to have
        # says nothing about whether the run is stuck on it.
        shift = {"offsets": {"vertical": {"css_shift": -8, "from_css_y": 50, "where": "top"}}}
        history = [self.record(1, 30.0), self.record(2, 5.0, **shift),
                   self.record(3, 40.0, **shift)]
        self.assertEqual(so.stuck_problems(history, history[-1]), [])

    def test_the_prompt_names_what_was_asked_for_and_skipped(self):
        run = {"kind": "html", "width": 10, "height": 10, "ground": "#FFFFFF"}
        text = so._iterate_prompt(run, 1, "<div></div>", score("close"), "",
                                  stuck=[{"key": ["colour", "background"], "rounds": 2}])
        self.assertIn("still not fixed", text)
        self.assertIn("the page background colour", text)
        self.assertIn("asked for 2 rounds ago", text)

    def test_the_prompt_allows_saying_it_cannot_be_fixed(self):
        # Without this the loop presses forever on a typeface that is not installed.
        run = {"kind": "html", "width": 10, "height": 10, "ground": "#FFFFFF"}
        text = so._iterate_prompt(run, 1, "<div></div>", score("close"), "",
                                  stuck=[{"key": ["type", "family"], "rounds": 1}])
        self.assertIn("cannot be fixed in code", text)

    def test_nothing_is_said_when_nothing_is_stuck(self):
        run = {"kind": "html", "width": 10, "height": 10, "ground": "#FFFFFF"}
        self.assertNotIn("still not fixed",
                         so._iterate_prompt(run, 1, "<div></div>", score("close"), ""))

    def test_a_fault_pressed_too_long_stops_being_pressed(self):
        shift = {"offsets": {"vertical": {"css_shift": -8, "from_css_y": 50, "where": "top"}}}
        history = [self.record(i, i * 10.0, **shift) for i in range(1, 7)]
        stuck = so.stuck_problems(history, history[-1])
        self.assertGreaterEqual(stuck[0]["rounds"], so.GIVE_UP_AFTER)
        # run_iteration filters on exactly this, so a fault that cannot be fixed in code
        # stops consuming every remaining round.
        self.assertEqual([s for s in stuck if s["rounds"] < so.GIVE_UP_AFTER], [])


@unittest.skipUnless(_chrome_available(), "needs Chrome or Edge")
class InsistingOnce(unittest.TestCase):
    """A round that ignored what it was asked for gets one more go, and only one."""

    SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">'
           '<rect width="400" height="300" fill="#FFFFFF"/>'
           '<circle cx="200" cy="150" r="{}" fill="#52796F"/></svg>')

    def setUp(self):
        self.saved_agent, self.saved_runs = so.run_agent, so.RUNS_DIR
        self.tmp = Path(tempfile.mkdtemp(prefix="spot-on-insist-"))
        so.RUNS_DIR = self.tmp
        self.calls = []

    def tearDown(self):
        so.run_agent, so.RUNS_DIR = self.saved_agent, self.saved_runs
        shutil.rmtree(self.tmp, ignore_errors=True)

    def answer_with(self, radius):
        def fake(agent, prompt, cwd, images):
            self.calls.append(prompt)
            return "```\n" + self.SVG.format(radius) + "\n```"
        so.run_agent = fake

    def seed(self):
        so.create_run("insist", "svg", reference_bytes=_png_bytes(DESIGN))
        so.record_attempt("insist", self.SVG.format(20))
        so.record_attempt("insist", self.SVG.format(22))

    def test_it_does_not_press_twice(self):
        # The stand-in never fixes anything, so without a cap this would not return.
        self.seed()
        self.answer_with(24)
        so.run_iteration("insist", candidates=1)
        self.assertLessEqual(len(self.calls), 2)


@contextlib.contextmanager
def _argv(args):
    saved = sys.argv
    sys.argv = args
    try:
        yield
    finally:
        sys.argv = saved


class DefectRegressions(unittest.TestCase):
    """One test per defect found in the review and trials of 2026-09-16."""

    def setUp(self):
        self.saved_runs = so.RUNS_DIR
        self.tmp = Path(tempfile.mkdtemp(prefix="spot-on-defect-"))
        so.RUNS_DIR = self.tmp

    def tearDown(self):
        so.RUNS_DIR = self.saved_runs
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_attempt_numbers_claimed_at_once_are_all_different(self):
        # Regression: the number was the file count plus one, so two attempts recorded
        # at the same moment took the same number and the second overwrote the first.
        so._save_run("race", {"slug": "race", "kind": "svg"})
        seen, lock = [], threading.Lock()

        def claim():
            n = so._next_n("race")
            with lock:
                seen.append(n)

        threads = [threading.Thread(target=claim) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(seen), list(range(1, 13)))

    def test_an_invented_source_is_not_recorded(self):
        # Regression: the attempt endpoint accepted any string for source and the page
        # built that pill with innerHTML, so a page in the browser could store markup.
        self.assertEqual(so._clean_source('<img src=x onerror=alert(1)>'), "manual")
        self.assertEqual(so._clean_source(None), "manual")
        self.assertEqual(so._clean_source("claude"), "claude")
        self.assertEqual(so._clean_source("session"), "session")

    def test_the_history_row_never_builds_the_source_as_markup(self):
        self.assertNotIn("+ rec.source +", so.PAGE_HTML)
        self.assertIn('.att-src").textContent = rec.source', so.PAGE_HTML)

    def test_help_prints_usage_instead_of_starting_the_server(self):
        # Regression: any flag fell through to "ignoring invalid port argument" and then
        # served forever, which reads as a hang as soon as stdout is buffered.
        for flag in ("-h", "--help", "help"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), _argv(["spot-on.py", flag]):
                so.main()
            self.assertIn("python spot-on.py score", out.getvalue())

    def test_an_unreadable_port_stops_instead_of_serving(self):
        with _argv(["spot-on.py", "--porcelain"]):
            with self.assertRaises(SystemExit):
                so.main()

    def test_a_timed_out_command_is_killed_rather_than_waited_out(self):
        # Regression: only the top process was killed, so Chrome's children and agent
        # CLIs outlived their timeout, holding the temp profile open and still spending.
        started = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            so._run_tree([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
        self.assertLess(time.time() - started, 15)


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

    def test_a_link_can_be_the_design(self):
        # Paste a page to copy (production, staging, anything you may match) and it
        # is captured through the same renderer as every attempt.
        # A static asset, not the tool's own page: that page lists the runs, so
        # creating one changes it between the two captures.
        target = self.base + "/signature.svg"
        run = self.post("/runs", {"name": "from a link", "kind": "url",
                                  "url": target, "capture_width": 800,
                                  "capture_height": 600})
        self.assertEqual(run["design_url"], target)
        self.assertEqual((run["css_width"], run["css_height"]), (800, 600))
        with Image.open(self.tmp / run["slug"] / "reference.png") as ref:
            self.assertEqual(ref.size, (800, 600))
        rec = self.post("/attempt", {"run": run["slug"], "code": target})
        self.assertGreater(rec["match"], 95)  # the same page against itself

    def test_the_command_line_takes_a_link_as_the_design(self):
        saved = so.RUNS_DIR
        try:
            so.cli_score([self.base + "/signature.svg", self.base + "/signature.svg",
                          "--run", "cli-link", "--json"])
        except SystemExit as e:
            self.fail("cli refused a link: {}".format(e))
        finally:
            so.RUNS_DIR = saved
        run = json.loads((self.tmp / "cli-link" / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run["design_url"], self.base + "/signature.svg")

    def test_page_script_is_served(self):
        html = urllib.request.urlopen(self.base + "/", timeout=30).read().decode()
        self.assertTrue("<title>Spot On</title>" in html, "title missing")
        self.assertTrue("127.0.0.1:{}".format(self.port) in html, "port not substituted")


if __name__ == "__main__":
    unittest.main()
