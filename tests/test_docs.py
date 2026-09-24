"""README consistency: every number in it comes from a generated file or a fixture."""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
WORDS = {"six": 6, "fourteen": 14}

spec = importlib.util.spec_from_file_location("sync_readme", ROOT / "scripts" / "sync_readme.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)

_tool = importlib.util.spec_from_file_location("spot_on_docs", ROOT / "spot-on.py")
TOOL = importlib.util.module_from_spec(_tool)
_tool.loader.exec_module(TOOL)

SWEEP = json.loads((ROOT / "scripts" / "match-sweep.json").read_text(encoding="utf-8"))
TRIAL = json.loads((ROOT / "scripts" / "match-trial.json").read_text(encoding="utf-8"))


class GeneratedTables(unittest.TestCase):
    def test_calibration_table_matches_generated_file(self):
        self.assertTrue(sync.block("calibration") in README,
                        "README calibration table is stale: run scripts/calibrate.py then scripts/sync_readme.py")

    def test_demo_table_matches_generated_file(self):
        self.assertTrue(sync.block("demo") in README,
                        "README demo table is stale: run scripts/sync_readme.py after scripts/demo.py")


class EchoedNumbers(unittest.TestCase):
    def test_worked_example_shift_sentence_matches_the_report(self):
        tool_spec = importlib.util.spec_from_file_location("so", ROOT / "spot-on.py")
        so = importlib.util.module_from_spec(tool_spec)
        tool_spec.loader.exec_module(so)
        fx = ROOT / "tests" / "fixtures"
        r, _, _ = so.score_images(Image.open(fx / "pricing-design.png"),
                                  Image.open(fx / "pricing-first-attempt.png"))
        v = r["offsets"]["vertical"]
        m = re.search(r"content sits about (\d+)px higher than the design from roughly (\d+)px down", README)
        self.assertIsNotNone(m, "worked example sentence changed; update this check with it")
        self.assertEqual(int(m.group(1)), abs(v["css_shift"]))
        self.assertEqual(int(m.group(2)), v["from_css_y"])

    def test_worked_example_round_numbers_match_the_demo_file(self):
        # Keyed to what the table says rather than to which row it lands on: with
        # several rewrites per round, the attempt numbers are no longer the round
        # numbers, and the old version indexed rows 2 and 3 directly.
        rows, spreads = {}, {}
        for line in (ROOT / "docs" / "demo-run.txt").read_text(encoding="utf-8").splitlines()[1:]:
            parts = line.split()
            if parts and parts[0].isdigit():
                rows[int(parts[0])] = float(parts[1])
                spreads[int(parts[0])] = parts[7]
        best_n = max(rows, key=lambda n: rows[n])

        m = re.search(r"scored (\d+(?:\.\d+)?), below the best at (\d+(?:\.\d+)?)", README)
        self.assertIsNotNone(m, "worked example sentence changed; update this check with it")
        worse, before = float(m.group(1)), float(m.group(2))
        self.assertIn(worse, rows.values(), "the discarded score is not in the table")
        self.assertEqual(before, rows[best_n], "the run's best score is quoted wrong")
        self.assertLess(worse, before)

        m = re.search(r"attempt (\d+) reached the best score of the run", README)
        self.assertIsNotNone(m, "worked example sentence changed; update this check with it")
        self.assertEqual(best_n, int(m.group(1)))

        # The spread quoted for best-of-three has to be a round that really happened.
        m = re.search(r"drew (\d+\.\d+), (\d+\.\d+) and (\d+\.\d+) from one prompt", README)
        self.assertIsNotNone(m, "worked example sentence changed; update this check with it")
        self.assertIn("/".join(m.groups()), spreads.values())

    def test_matcher_table_matches_the_trial_output(self):
        trial = TRIAL
        for label, how in (("appearance, with position breaking ties", "content"),
                           ("position and size", "geometry"),
                           ("nothing: each element against whatever is in its rectangle",
                            "overlay")):
            m = re.search(r"\|\s*" + re.escape(label) + r"\s*\|\s*(\d+\.\d+)%", README)
            self.assertIsNotNone(m, "matcher table row missing: " + label)
            self.assertEqual(float(m.group(1)), trial["totals"][how]["accuracy"], label)
        m = re.search(r"(\w+) mutations of one page, (\d+) decisions", README)
        self.assertIsNotNone(m, "the trial's shape is quoted differently now")
        self.assertEqual(WORDS.get(m.group(1), -1), len(trial["scenarios"]))
        self.assertEqual(int(m.group(2)), trial["totals"]["content"]["of"])

    def test_the_reword_fix_matches_the_trial_output(self):
        rows = {s["scenario"]: s["matchers"]["content"]["accuracy"]
                for s in TRIAL["scenarios"]}
        m = re.search(r"reword case from (\d+\.\d+)% to (\d+)% and the total from "
                      r"(\d+\.\d+)% to (\d+\.\d+)%", README)
        self.assertIsNotNone(m, "the reword fix is worded differently now")
        self.assertEqual(float(m.group(2)), rows["text-swap"])
        self.assertEqual(float(m.group(4)), TRIAL["totals"]["content"]["accuracy"])
        # The before figures come from the sweep row with the profile switched off.
        sweep = {(r["far_w"], r["gate"], r["layout"]): r for r in SWEEP["sweep"]}
        off = sweep[(TOOL.CONTENT_FAR_W, TOOL.CONTENT_GATE, 0.0)]
        self.assertEqual(float(m.group(1)), off["by_scenario"]["text-swap"])
        self.assertEqual(float(m.group(3)), off["known_pct"])

    def test_the_remaining_limit_matches_the_trial_output(self):
        rows = {s["scenario"]: s["matchers"]["content"]["accuracy"]
                for s in TRIAL["scenarios"]}
        m = re.search(r"Both remaining failures in the trial are that case "
                      r"\((\d+\.\d+)% and (\d+\.\d+)%; every other scenario is 100%\)", README)
        self.assertIsNotNone(m, "the remaining limit is worded differently now")
        below = {k: v for k, v in rows.items() if v < 100.0}
        self.assertTrue(all("swap" in k for k in below),
                        "a non-swap scenario now fails: " + ", ".join(sorted(below)))
        self.assertEqual(sorted(round(float(g), 1) for g in m.groups()),
                         sorted(round(v, 1) for v in below.values()))

    def test_the_unbounded_reach_numbers_match_the_sweep(self):
        sweep = SWEEP
        loose = max(sweep["sweep"], key=lambda r: (r["gate"], -r["far_w"]))
        m = re.search(r"on (\w+) real runs it reached (\d+)px for a pair and made (\d+) "
                      r"pairings more than a quarter of a page apart", README)
        self.assertIsNotNone(m, "the unbounded-reach sentence changed")
        self.assertEqual(WORDS.get(m.group(1), -1), SWEEP["real_runs"])
        self.assertEqual(int(m.group(2)), int(loose["furthest_px"]))
        self.assertEqual(int(m.group(3)), loose["cross_page_pairs"])

    def test_the_chosen_setting_is_the_best_that_never_crosses_the_page(self):
        sweep = SWEEP
        clean = [r for r in sweep["sweep"] if r["cross_page_pairs"] == 0]
        self.assertTrue(clean, "the sweep found no setting without cross-page pairs")
        best = max(clean, key=lambda r: (r["known_pct"], r["real_paired_pct"]))
        self.assertEqual(TOOL.CONTENT_FAR_W, best["far_w"])
        self.assertEqual(TOOL.CONTENT_GATE, best["gate"])

    def test_coverage_cap_example(self):
        m = re.search(r"leaves out a fifth of the design can reach at most (\d+)%", README)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), round((0.6 + 0.4 * 0.8) * 100))


class ThePageScript(unittest.TestCase):
    """The whole interface is one inline script, and nothing else parses it.

    A stray bracket in it does not fail an import or a test: the server serves the page,
    the browser stops at the error, and every control is dead with no message anywhere.
    Node is not a dependency of the tool, so this is skipped when it is missing.
    """

    def script(self):
        page = TOOL.PAGE_HTML
        start = page.index("<script>") + len("<script>")
        return page[start:page.index("</script>", start)]

    def test_it_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(self.script())
            path = f.name
        try:
            done = subprocess.run([node, "--check", path], capture_output=True)
            self.assertEqual(done.returncode, 0,
                             done.stderr.decode("utf-8", "replace")[-900:])
        finally:
            os.unlink(path)

    def test_every_element_it_reaches_for_exists(self):
        # $("thing") on an id the markup does not have returns null, and the next line
        # throws. That kills the rest of the script, so one typo can disable the page.
        page, script = TOOL.PAGE_HTML, self.script()
        ids = set(re.findall(r'\bid="([A-Za-z0-9\-]+)"', page))
        wanted = set(re.findall(r'\$\("([A-Za-z0-9\-]+)"\)', script))
        made = set(re.findall(r'\.id = "([A-Za-z0-9\-]+)"', script))
        self.assertEqual(sorted(wanted - ids - made), [])


class HouseRules(unittest.TestCase):
    def test_assets_carry_no_embedded_provenance_manifest(self):
        # The signature SVG once shipped with a C2PA manifest naming the tool that
        # produced it. Re-exporting an asset can bring one back.
        for path in (ROOT / "assets").rglob("*"):
            if path.is_file():
                data = path.read_bytes().lower()
                self.assertNotIn(b"c2pa", data, path.name)
                self.assertNotIn(b"jumbf", data, path.name)

    def test_no_em_dashes(self):
        self.assertNotIn("\u2014", README)


if __name__ == "__main__":
    unittest.main()
