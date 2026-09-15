"""README consistency: every number in it comes from a generated file or a fixture."""

import importlib.util
import re
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")

spec = importlib.util.spec_from_file_location("sync_readme", ROOT / "scripts" / "sync_readme.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


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
        rows = {}
        for line in (ROOT / "docs" / "demo-run.txt").read_text(encoding="utf-8").splitlines()[1:]:
            parts = line.split()
            if parts and parts[0].isdigit():
                rows[int(parts[0])] = {"match": float(parts[1]), "detail": float(parts[5])}
        m = re.search(r"detail went from (\d+(?:\.\d+)?) to (\d+(?:\.\d+)?)", README)
        self.assertEqual((float(m.group(1)), float(m.group(2))), (rows[2]["detail"], rows[3]["detail"]))
        m = re.search(r"It scored (\d+(?:\.\d+)?), down from (\d+(?:\.\d+)?)", README)
        self.assertEqual((float(m.group(1)), float(m.group(2))), (rows[4]["match"], rows[3]["match"]))
        self.assertLess(rows[4]["match"], rows[3]["match"])
        m = re.search(r"round 5 kept only the part that helped: (\d+(?:\.\d+)?)", README)
        self.assertEqual(float(m.group(1)), rows[5]["match"])
        self.assertEqual(max(r["match"] for r in rows.values()), rows[5]["match"])

    def test_earlier_run_plateau_is_recorded(self):
        # The 58.1 comes from the run before the typeface hint existed; its table is
        # kept in docs/demo-run-before-typeface-hint.txt so the claim stays checkable.
        before = (ROOT / "docs" / "demo-run-before-typeface-hint.txt").read_text(encoding="utf-8")
        best = max(float(l.split()[1]) for l in before.splitlines()[1:] if l.split() and l.split()[0].isdigit())
        m = re.search(r"stalled at (\d+(?:\.\d+)?)", README)
        self.assertEqual(float(m.group(1)), best)

    def test_coverage_cap_example(self):
        m = re.search(r"leaves out a fifth of the design can reach at most (\d+)%", README)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), round((0.6 + 0.4 * 0.8) * 100))


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
