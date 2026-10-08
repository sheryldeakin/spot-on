"""Does any test actually notice when a threshold moves?

Every detector here rests on a number somebody chose. The project's rule is to prove
a check can fail before trusting it, by breaking the thing on purpose and watching it
go red, and that is done by hand. On 2026-10-05 it caught a test asserting a constant
against itself, which passed happily with the guard removed, and it caught that only
because the mutation happened to be run. Two other thresholds were never mutated at
all and turned out to be guarded by nothing.

So this does it mechanically. For each declared threshold, move the number, run the
test class that is supposed to guard it, and require that class to go red. A constant
whose tests stay green has no guard, whatever the suite's total says.

Constants with no declared guard are listed rather than failed, so the number is
visible and can be driven up, and a new one cannot hide.

TWO WAYS THIS LIES, both found by acting on its first run and then checking.

A mutation that does not actually disable the guard proves nothing. `CASE_CHANGE`
was moved to 0.01 and the gate is `abs(change) < CASE_CHANGE`, so identical pages
still failed it and the threshold still worked: the report said "no guard" about a
guard that was working. Pick a value that turns the thing OFF.

And one threshold can MASK another. Every face change that `CASE_FLAT` rejects is
also below `CASE_CHANGE`, so moving either alone left the suite green and both read
as pointless, which is what the backlog concluded and it was wrong. Four thresholds
looked redundant and all four were load-bearing: without `CASE_CHANGE` two identical
pages report a case change. So "no guard" here means "no test distinguishes this
one", and the fix is a case that only it rejects, not a deletion.

    python scripts/check_guards.py            # declared guards only
    python scripts/check_guards.py --list     # also print what is uncovered
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "spot-on.py"

# constant -> (mutated value, the test class that should notice)
# The value is chosen to disable the guard rather than to nudge it: a threshold set
# where it can never bite is the failure being guarded against.
GUARDS = {
    "RULE_RIDGE": ("0.0", "LinesNothingElseCanSee"),
    "RULE_STEP": ("0.5", "LinesNothingElseCanSee"),
    "RULE_SHARE": ("0.01", "LinesNothingElseCanSee"),
    "RULE_CORNER": ("400", "LinesNothingElseCanSee"),
    "RULE_THICK": ("200", "LinesNothingElseCanSee"),
    "SHADOW_DEPTH": ("0.0", "ASoftShadowNothingElseCanSee"),
    "SHADOW_SPREAD": ("0", "ASoftShadowNothingElseCanSee"),
    "SHADOW_REACH": ("2", "ASoftShadowNothingElseCanSee"),
    "CASE_MIN_BAND": ("3", "CapitalsAreNotAWiderBox"),
    "CASE_FLAT": ("0.10", "CapitalsAreNotAWiderBox"),
    # 0.0 and not 0.01. The gate is `abs(change) < CASE_CHANGE`, so at 0.01 two
    # IDENTICAL renders still fail it and the threshold still works. A mutation that
    # leaves the guard functioning says "no test noticed" and means nothing, which is
    # how this one read as unguarded for a day. A value has to actually disable it.
    "CASE_CHANGE": ("0.0", "CapitalsAreNotAWiderBox"),
    "CASE_MARKS": ("99", "CapitalsAreNotAWiderBox"),
    "INK_CHROMA": ("0.0", "TextTheWrongColour"),
    "DISPUTE_AFTER": ("1", "WhenTheRoundsSayTheReportIsWrong"),
    "ICON_NAME": ("0.0", "WhatTheSetKnowsAboutOneWell"),
    "ICON_PRESENT": ("0.0", "WhatTheSetKnowsAboutOneWell"),
    # ---- declared 2026-10-08, working through the ones that had no guard ----
    "INK_EDGE": ("0.0", "ComponentsIsolateTheirError"),
    "INK_ABOVE": ("0", "ComponentsIsolateTheirError"),
    "COLOUR_FALLOFF": ("10000.0", "ColourIsMeasuredPerceptually"),
    "DELTA_E_VISIBLE": ("0.0", "ColourIsMeasuredPerceptually"),
    "CONTENT_ACCEPT": ("0.0", "PairingElementsByAppearance"),
    "CONTENT_FAR_W": ("0.0", "PairingElementsByAppearance"),
    "CONTENT_GATE": ("9999.0", "PairingElementsByAppearance"),
    "LAYOUT_TRUST": ("0.0", "PairingElementsByAppearance"),
    "ELEMENT_BEHIND": ("0", "ElementsAreScoredOnTheirOwn"),
    "ELEMENT_BUILT": ("0", "ElementsAreScoredOnTheirOwn"),
    "ELEMENT_MOVED": ("999", "ElementFeedback"),
    "FILL_DELTA": ("0.0", "PanelFillIsMeasured"),
    "HOLLOW_INK_GAP": ("0.0", "AnEmptyContainerHasToBeEmpty"),
    "RESCORE_WITHIN": ("0.0", "AllTheContendersAreOnOneScorer"),
    "FINDING_PENALTY": ("0.0", "WhatTheReportAskedForAndDidNotGet"),
    "RESIDUAL_SCALE": ("0.001", "WhatTheReportAskedForAndDidNotGet"),
    "REPAIR_TOLERANCE": ("0.0", "BothSelectionPathsSayWhatTheyDid"),
    "RULE_SAME": ("0", "LinesNothingElseCanSee"),
    "INK_SHARE": ("1.0", "TextTheWrongColour"),
    "INK_MIN_WIDTH": ("0", "TextTheWrongColour"),
    "UNEXPLAINED_BELOW": ("0.0", "ReportWatchesItself"),
    "PLATEAU_ROUNDS": ("999", "ThingsThePersonWantsFixed"),
    "PLATEAU_GAIN": ("9999.0", "ThingsThePersonWantsFixed"),
    "WORKLIST_SAME": ("0.0", "TheRoundsKeepAListBetweenThem"),
    "GIVE_UP_AFTER": ("999", "PressingUnfixedProblems"),
    "PRESS_LIMIT": ("999", "PressingUnfixedProblems"),
    "DISPUTE_WORDS": ("5", "WhenTheRoundsSayTheReportIsWrong"),
    "NEEDS_ARTWORK_SHARE": ("0.0", "WhatThePageNeedsFromThePerson"),
    "NEEDS_FONT_WEAK": ("0.0", "WhatThePageNeedsFromThePerson"),
    "NEEDS_FONT_ROUNDS": ("0", "WhatThePageNeedsFromThePerson"),
    "ICON_MARGIN": ("0.0", "WhatTheSetKnowsAboutOneWell"),
    "ICON_FAMILY": ("99", "AGlyphFamilyWhenTheVariantIsACoinToss"),
}

# Numbers that are not thresholds: an identity, a port, a size, a count of things to
# print, a limit the operating system sets. Moving one of these is not a weakened
# guard, so they are not expected to have a test that goes red. Each one is excused
# on purpose, because the point of the listing is that a NEW constant lands on it.
NOT_A_THRESHOLD = {
    "PORT", "SCORER_VERSION", "SSIM_WINDOW", "DELTA_E_MAX",
    "RULE_FOUND", "INK_FOUND", "HOLLOW_NAMED", "ELEMENT_SCORES",
    "RULE_REACH", "SHADOW_FAR", "CASE_TOP", "CASE_INK",
    # Limits the operating system sets, not choices: cmd.exe dies at 8191 and
    # CreateProcess at 32767, and these sit under them.
    "ARG_SAFE_CHARS", "CMD_SHIM_LIMIT", "CREATE_PROCESS_LIMIT",
    # An identity, bumped on purpose when a question changes.
    "NEEDS_VERSION",
    # Sizes and counts. Moving one changes how much is shown or how big a buffer
    # is, not whether a fault is reported.
    "ICON_NORM", "FIND_LIMIT", "FIND_TIMEOUT", "ASKS_MAX", "WORKLIST_MAX",
    "WORKLIST_WORDS", "ELEMENT_TAIL", "ELEMENT_TAIL_MIN", "INSIST_CANDIDATES",
    # Floors on how much evidence there has to be before a measurement is taken at
    # all. Arguably thresholds, but moving one makes a measurement noisier rather
    # than making a finding appear or vanish, and no test can hold that still.
    "INK_MIN_PIXELS", "ELEMENT_MIN", "ICON_READABLE",
}

CONST = re.compile(r"^([A-Z][A-Z0-9_]{2,})\s*=\s*(-?\d+(?:\.\d+)?)\s*(?:#.*)?$", re.M)


def constants(src):
    return {m.group(1): m.group(2) for m in CONST.finditer(src)}


def no_bytecode():
    """Stop any cached bytecode being read for the tool while it is being mutated.

    This is the bug that made the checker lie, and it took the premise check above
    to find it. Python invalidates a .pyc on the source's mtime IN WHOLE SECONDS
    plus its size. Two mutations a second apart that happen to leave the file the
    same length therefore look identical to the cache, so the second run executes
    the first one's bytecode: the threshold under test is back at its real value,
    the tests pass for the ordinary reason, and the report says "no guard" about a
    guard that works. INK_CHROMA was reported unguarded three runs in a row that
    way, while the same mutation applied by hand failed four tests.
    """
    for stale in (ROOT / "__pycache__").glob("spot-on.*.pyc"):
        try:
            stale.unlink()
        except OSError:
            pass
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def loaded_value(name):
    """What a fresh import of the tool actually sees for this constant.

    The checker has to verify its own premise, which is the lesson it exists to
    teach. It writes a mutation and then spawns a test run, and if the write has
    not landed where the subprocess reads it, the tests pass for the ordinary
    reason and the report says "no guard" about a guard that is working. That
    happened: INK_CHROMA was reported unguarded three runs in a row while the same
    mutation, applied by hand, failed four tests.
    """
    code = ("import importlib.util,sys;"
            "s=importlib.util.spec_from_file_location('t','spot-on.py');"
            "m=importlib.util.module_from_spec(s);sys.modules['t']=m;"
            "s.loader.exec_module(m);print(getattr(m,%r))" % name)
    done = subprocess.run([sys.executable, "-B", "-c", code], cwd=str(ROOT),
                          capture_output=True, text=True, env=no_bytecode())
    return done.stdout.strip()


def run(target):
    """True when the test class passes."""
    done = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "tests.test_spot_on." + target],
        cwd=str(ROOT), capture_output=True, text=True, env=no_bytecode())
    return done.returncode == 0


def main():
    # newline="" both ways, so the file goes back byte for byte: this edits the tool
    # in place and a mangled restore would be a far worse bug than the one it hunts.
    with open(TOOL, encoding="utf-8", newline="") as fh:
        src = fh.read()
    found = constants(src)
    backup = Path(tempfile.mkdtemp(prefix="guards-")) / "spot-on.py"
    shutil.copy2(TOOL, backup)

    rows, unguarded = [], []
    try:
        for name, (value, target) in sorted(GUARDS.items()):
            if name not in found:
                rows.append({"constant": name, "target": target,
                             "result": "gone from the tool"})
                unguarded.append(name)
                continue
            # Anchored to the start of a line, because several of these names also
            # appear in the page's own JavaScript: `var PLATEAU_ROUNDS = 3,
            # PLATEAU_GAIN = 0.1;`. A bare substring replace matched both and the
            # count assertion killed the whole run halfway through, leaving no
            # report at all.
            at_line_start = re.compile(
                r"^" + re.escape(name) + r"(\s*=\s*)" + re.escape(found[name])
                + r"(?=\s|$)", re.M)
            hits = len(at_line_start.findall(src))
            if hits != 1:
                rows.append({"constant": name, "target": target,
                             "result": "defined {} times at a line start".format(hits)})
                unguarded.append(name)
                print("  {:<16} -> {:<6} {:<34} CANNOT MUTATE ({} definitions)".format(
                    name, value, target, hits))
                continue
            mutated = at_line_start.sub(
                lambda m: name + m.group(1) + value, src, count=1)
            with open(TOOL, "w", encoding="utf-8", newline="") as fh:
                fh.write(mutated)
            # Check the mutation is what the next process will actually read,
            # before drawing any conclusion from whether the tests pass.
            landed = loaded_value(name)
            if landed not in (value, str(float(value)) if "." in value else value):
                try:
                    float_ok = abs(float(landed) - float(value)) < 1e-9
                except (TypeError, ValueError):
                    float_ok = False
                if not float_ok:
                    with open(TOOL, "w", encoding="utf-8", newline="") as fh:
                        fh.write(src)
                    rows.append({"constant": name, "target": target,
                                 "result": "mutation did not land",
                                 "wanted": value, "loaded": landed})
                    unguarded.append(name)
                    print("  {:<16} -> {:<6} {:<34} MUTATION DID NOT LAND "
                          "(the tool still reads {})".format(
                              name, value, target, landed or "nothing"))
                    continue
            still_green = run(target)
            with open(TOOL, "w", encoding="utf-8", newline="") as fh:
                fh.write(src)
            rows.append({"constant": name, "target": target, "was": found[name],
                         "moved_to": value,
                         "result": "NO GUARD" if still_green else "caught"})
            if still_green:
                unguarded.append(name)
            print("  {:<16} -> {:<6} {:<34} {}".format(
                name, value, target,
                "NOT CAUGHT" if still_green else "caught"), flush=True)
    finally:
        shutil.copy2(backup, TOOL)
        shutil.rmtree(backup.parent, ignore_errors=True)

    covered = set(GUARDS) | NOT_A_THRESHOLD
    missing = sorted(n for n in found if n not in covered)
    out = {"declared": len(GUARDS), "caught": len(GUARDS) - len(unguarded),
           "unguarded": sorted(unguarded), "constants": len(found),
           "undeclared": missing, "rows": rows}
    (ROOT / "scripts" / "guard-coverage.json").write_text(
        json.dumps(out, indent=1), encoding="utf-8")

    print()
    print("{} of {} declared thresholds are caught by a test".format(
        out["caught"], out["declared"]))
    if unguarded:
        print("NO GUARD: {}".format(", ".join(unguarded)))
    print("{} numeric constants in the tool, {} neither declared nor excused".format(
        out["constants"], len(missing)))
    if "--list" in sys.argv and missing:
        for name in missing:
            print("   {:<22} {}".format(name, found[name]))
    return 1 if unguarded else 0


if __name__ == "__main__":
    sys.exit(main())
