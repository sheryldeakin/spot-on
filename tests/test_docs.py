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
TOOL_SOURCE = (ROOT / "spot-on.py").read_text(encoding="utf-8")
WORDS = {"six": 6, "fourteen": 14}

spec = importlib.util.spec_from_file_location("sync_readme", ROOT / "scripts" / "sync_readme.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)

_tool = importlib.util.spec_from_file_location("spot_on_docs", ROOT / "spot-on.py")
TOOL = importlib.util.module_from_spec(_tool)
_tool.loader.exec_module(TOOL)

SWEEP = json.loads((ROOT / "scripts" / "match-sweep.json").read_text(encoding="utf-8"))
TRIAL = json.loads((ROOT / "scripts" / "match-trial.json").read_text(encoding="utf-8"))
ICONS = json.loads((ROOT / "scripts" / "icon-trial.json").read_text(encoding="utf-8"))
ICONS_REAL = json.loads((ROOT / "scripts" / "icon-real.json").read_text(encoding="utf-8"))
FONTS = json.loads((ROOT / "scripts" / "font-changes.json").read_text(encoding="utf-8"))
INK = json.loads((ROOT / "scripts" / "ink-colour-survey.json").read_text(encoding="utf-8"))
SCRATCH = json.loads((ROOT / "scripts" / "from-scratch.json").read_text(encoding="utf-8"))
UNNAMED = json.loads((ROOT / "scripts" / "unmeasured-survey.json").read_text(encoding="utf-8"))


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

    def test_the_colour_change_numbers_match_their_sources(self):
        fit = json.loads((ROOT / "scripts" / "colour-fit.json").read_text(encoding="utf-8"))
        m = re.search(r"across (\d+) saved attempts, by \*\*([-+]?\d+\.\d+) on average\*\*, "
                      r"never more than (\d+\.\d+) in either direction", README)
        self.assertIsNotNone(m, "the colour-change sentence is worded differently now")
        self.assertEqual(int(m.group(1)), fit["match"]["attempts"])
        self.assertEqual(float(m.group(2)), fit["match"]["mean"])
        self.assertGreaterEqual(float(m.group(3)),
                                max(abs(fit["match"]["min"]), abs(fit["match"]["max"])))
        # The two calibration figures are quoted from the generated table, both ends.
        table = (ROOT / "docs" / "calibration.txt").read_text(encoding="utf-8")
        m = re.search(r"wrong hue went from 37\.2 to \*\*(\d+\.\d+)\*\*", README)
        self.assertIsNotNone(m, "the hue example is worded differently now")
        row = re.search(r"^hue\s+\S+\s+\S+\s+\S+\s+(\S+)", table, re.M)
        self.assertEqual(m.group(1), row.group(1))
        m = re.search(r"a shade too deep went from 80\.3 to \*\*(\d+\.\d+)\*\*", README)
        self.assertIsNotNone(m, "the gradient example is worded differently now")
        row = re.search(r"^deeper\s+\S+\s+\S+\s+\S+\s+(\S+)", table, re.M)
        self.assertEqual(m.group(1), row.group(1))

    def test_the_glyph_asymmetry_numbers_match_the_icon_trial(self):
        m = re.search(r"Each of the (\d+) distinct drawings in the set was rendered again"
                      r".*?`scripts/icon_trial\.py`, (\d+) arms", README, re.S)
        self.assertIsNotNone(m, "the icon trial sentence is worded differently now")
        self.assertEqual(int(m.group(1)), ICONS["icons"])
        self.assertEqual(int(m.group(2)), len(ICONS["arms"]))
        self.assertEqual(ICONS["sanity"]["top1_or_twin_pct"], 100.0)

        m = re.search(r"at 32px a drawing that \*\*is\*\* in the set scores at least "
                      r"(\d\.\d+) against itself (\d+\.\d+)% to (\d+\.\d+)% of the time",
                      README)
        self.assertIsNotNone(m, "the absence-evidence sentence changed")
        self.assertEqual(float(m.group(1)), ICONS["present_gate"])
        key = "{:.2f}/{:.2f}".format(ICONS["present_gate"], 0.0)
        at32 = [a["by_score"][key]["named_pct"] for a in ICONS["arms"] if a["size"] >= 32]
        self.assertEqual(float(m.group(2)), min(at32))
        self.assertEqual(float(m.group(3)), max(at32))

        m = re.search(r"(\d+\.\d+)% to (\d+\.\d+)% of glyphs that are \*\*not\*\* in the set "
                      r"reach it", README)
        self.assertIsNotNone(m, "the false-presence sentence changed")
        at24 = [a["by_score"][key]["absent_named_pct"] for a in ICONS["arms"] if a["size"] >= 24]
        self.assertEqual(float(m.group(1)), min(at24))
        self.assertEqual(float(m.group(2)), max(at24))

    def test_the_name_gate_numbers_match_the_icon_trial(self):
        m = re.search(r"prints a name only at (\d\.\d+) with (\d\.\d+) clear of the nearest "
                      r"different drawing, where the false-name rate falls to (\d+\.\d+)% to "
                      r"(\d+\.\d+)% and the names it does print are right (\d+\.\d+)% to "
                      r"(\d+\.\d+)% of the time", README)
        self.assertIsNotNone(m, "the name-gate sentence changed")
        self.assertEqual(float(m.group(1)), TOOL.ICON_NAME)
        self.assertEqual(float(m.group(2)), TOOL.ICON_MARGIN)
        self.assertEqual(float(m.group(1)), ICONS["name_gate"])
        self.assertEqual(float(m.group(2)), ICONS["name_margin"])
        key = "{:.2f}/{:.2f}".format(TOOL.ICON_NAME, TOOL.ICON_MARGIN)
        arms = [a["by_score"][key] for a in ICONS["arms"] if a["size"] >= 24]
        self.assertEqual(float(m.group(3)), min(a["absent_named_pct"] for a in arms))
        self.assertEqual(float(m.group(4)), max(a["absent_named_pct"] for a in arms))
        self.assertEqual(float(m.group(5)), min(a["right_pct"] for a in arms))
        self.assertEqual(float(m.group(6)), max(a["right_pct"] for a in arms))

    def test_the_text_colour_numbers_match_their_source(self):
        m = re.search(r"Across (\d+) text runs on best attempts, (\d+) differ by more "
                      r"than 10", README)
        self.assertIsNotNone(m, "the text-colour sentence changed")
        self.assertEqual(int(m.group(1)), INK["text_elements"])
        self.assertEqual(int(m.group(2)), INK["over_10"])
        m = re.search(r"(\d+) are mostly lightness, (\d+) are mostly hue or chroma, "
                      r"(\d+) are mixed", README)
        self.assertIsNotNone(m, "the lightness split sentence changed")
        self.assertEqual(int(m.group(1)), INK["over_10_mostly_lightness"])
        self.assertEqual(int(m.group(2)), INK["over_10_mostly_colour"])
        self.assertEqual(int(m.group(3)), INK["over_10_mixed"])
        # The split is the whole reason the finding is built on chroma.
        self.assertGreater(INK["over_10_mostly_colour"], INK["over_10_mostly_lightness"])

    def test_the_unnamed_property_costs_match_their_source(self):
        """What a border, a shadow and the wrong case cost, each read off the pair
        of pages that differ only in that one thing."""
        rows = {r["property"]: r for r in UNNAMED["rows"]}
        for name, pattern in (
                ("border added", r"a border drawn around one cost ([\d.]+) points"),
                ("drop shadow added", r"a drop shadow cost ([\d.]+) while"),
                ("letter case", r"Continue costs ([\d.]+) points")):
            m = re.search(pattern, README)
            self.assertIsNotNone(m, "the sentence for " + name + " changed")
            self.assertAlmostEqual(float(m.group(1)), 100.0 - rows[name]["match"],
                                   places=1, msg=name)
            # And the claim that it is named now is the survey's to make.
            self.assertTrue(rows[name]["named"], name)
        m = re.search(r"(\w+) of twelve went unnamed", README)
        self.assertIsNotNone(m, "the unnamed count sentence changed")
        self.assertEqual(len(UNNAMED["rows"]), 12)

    def test_the_from_scratch_rounds_match_their_source(self):
        """Which round each thing first reaches a prompt in. A sentence saying the
        colour line is there on round one is the claim the whole probe exists to
        make, so it is checked against the run rather than remembered."""
        m = re.search(r"builds, over (\d+) rounds", README)
        self.assertIsNotNone(m, "the from-scratch sentence changed")
        self.assertEqual(int(m.group(1)), len(SCRATCH["rounds"]))
        m = re.search(r"colour line are all in round (\d+)", README)
        self.assertIsNotNone(m, "the round-one sentence changed")
        first = SCRATCH["first_seen"]
        for name in ("needs section", "glyph verdict", "colour line"):
            self.assertEqual(int(m.group(1)), first[name], name)
        m = re.search(r"arrives in round (\d+), which is when", README)
        self.assertIsNotNone(m, "the dispute-question sentence changed")
        self.assertEqual(int(m.group(1)), first["dispute question"])
        m = re.search(r"dispute is heard in round (\d+)", README)
        self.assertIsNotNone(m, "the dispute-heard sentence changed")
        self.assertEqual(int(m.group(1)), first["dispute heard"])
        # The whole point is that nobody rescored anything, so the probe has to have
        # run on the scorer the tool currently ships.
        self.assertEqual(SCRATCH["scorer_version"], TOOL.SCORER_VERSION,
                         "the scorer has moved since the from-scratch run: "
                         "re-run scripts/from_scratch.py, because evidence "
                         "about what reaches a round is about one scorer")

    def test_the_font_change_numbers_match_their_source(self):
        """These were read off a top-six list printed in a throwaway command and two
        of the four were wrong, which the committed script caught at once. Hence the
        rule: no number without a script, and the echo checked in the same edit."""
        m = re.search(r"across (\d+) attempts here whose change mentions the font, "
                      r"(\d+) cut the share of wrong letters by a third or more, "
                      r"(\w+) went from every line wrong to none, and the best single "
                      r"change was worth (\d+\.\d+) points of match", README)
        self.assertIsNotNone(m, "the font-change sentence changed")
        self.assertEqual(int(m.group(1)), FONTS["font_changes"])
        self.assertEqual(int(m.group(2)), FONTS["cut_weak_share_by_0_3_or_more"])
        words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
        said = m.group(3)
        self.assertEqual(words.get(said, said if not said.isdigit() else int(said)),
                         FONTS["went_from_every_line_wrong_to_none"])
        self.assertEqual(float(m.group(4)), FONTS["best_match_change"])

    def test_the_tool_quotes_the_same_font_numbers_as_the_readme(self):
        # The claim lives in three places: the README, a docstring, and the sentence
        # the person reads in the needs panel. Anything written twice drifts.
        src = TOOL_SOURCE
        for value in (str(FONTS["font_changes"]),
                      str(FONTS["cut_weak_share_by_0_3_or_more"])):
            self.assertIn(value, src, "the tool no longer quotes " + value)
        self.assertNotIn("134", src, "a stale font count is still in the tool")
        self.assertNotIn("134", README, "a stale font count is still in the README")

    def test_the_real_design_survey_numbers_match_their_source(self):
        m = re.search(r"Over every icon-sized box in the (\d+) distinct designs on this "
                      r"machine \((\d+) boxes, `scripts/icon_real\.py`\), the set has nothing "
                      r"of that shape for (\d+) of them and prints a name for (\d+)\.", README)
        self.assertIsNotNone(m, "the real-design survey sentence changed")
        self.assertEqual(int(m.group(1)), ICONS_REAL["designs"])
        self.assertEqual(int(m.group(2)), ICONS_REAL["totals"]["boxes"])
        self.assertEqual(int(m.group(3)), ICONS_REAL["totals"]["absent"])
        self.assertEqual(int(m.group(4)), ICONS_REAL["totals"]["named"])
        # The claim the sentence rests on: most of the page is not in the set.
        self.assertGreater(ICONS_REAL["totals"]["absent"], ICONS_REAL["totals"]["named"] * 10)

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

    def test_no_stray_control_characters_in_any_tracked_file(self):
        r"""A backslash escape that got eaten on the way into a file.

        Content written into a file through another script's string literals gets
        decoded on the way: source meant as backslash-b arrives as a single 0x08
        byte. It parses, it runs, and the regex it belongs to quietly stops matching.
        Six times in one session, twice while writing the warning about it, so the
        warning is not the control.

        Not the shell, which was the first diagnosis and was wrong: a quoted heredoc
        passes content through untouched, and the decoding happens in the literal
        the content was sitting in. Writing the patch script with an editor tool
        does not help, because the content is still inside python literals.

        What this sees: the escapes that land as a control character, `\a \b \f \v
        \0`. What it does NOT see: a corrupted `\t`, `\n` or `\r`, because those
        bytes are legitimate in a text file. One real damaged path had a newline AND
        a BEL in it, and only the BEL was visible here. A net under part of a class,
        not coverage of it.
        """
        allowed = {9, 10, 13}        # tab, newline, carriage return
        binary = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".npz", ".woff",
                  ".woff2", ".ttf", ".otf", ".pdf", ".zip"}
        out = subprocess.run(["git", "ls-files"], cwd=str(ROOT),
                             capture_output=True, text=True)
        offenders = []
        for name in out.stdout.split("\n"):
            name = name.strip()
            if not name:
                continue
            path = ROOT / name
            if path.suffix.lower() in binary or not path.is_file():
                continue
            found = sorted(set(c for c in path.read_bytes() if c < 32 and c not in allowed))
            if found:
                offenders.append("{}: {}".format(name, [hex(c) for c in found]))
        self.assertEqual(offenders, [],
                         "control characters in tracked text, almost certainly a "
                         "backslash escape eaten by a shell heredoc")

    def test_no_em_dashes(self):
        self.assertNotIn("\u2014", README)


if __name__ == "__main__":
    unittest.main()
