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

    python scripts/check_guards.py            # declared guards only
    python scripts/check_guards.py --list     # also print what is uncovered
"""
import json
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
    "CASE_CHANGE": ("0.01", "CapitalsAreNotAWiderBox"),
    "CASE_MARKS": ("99", "CapitalsAreNotAWiderBox"),
    "INK_CHROMA": ("0.0", "TextTheWrongColour"),
    "DISPUTE_AFTER": ("1", "WhenTheRoundsSayTheReportIsWrong"),
    "ICON_NAME": ("0.0", "WhatTheSetKnowsAboutOneWell"),
    "ICON_PRESENT": ("0.0", "WhatTheSetKnowsAboutOneWell"),
}

# Numbers that are not thresholds: an identity, a port, a size, a count of things to
# print. Moving one of these is not a weakened guard, so they are not expected to
# have a test that goes red.
NOT_A_THRESHOLD = {"PORT", "SCORER_VERSION", "SSIM_WINDOW", "DELTA_E_MAX",
                   "RULE_FOUND", "INK_FOUND", "HOLLOW_NAMED", "ELEMENT_SCORES",
                   "RULE_REACH", "SHADOW_FAR", "CASE_TOP", "CASE_INK"}

CONST = re.compile(r"^([A-Z][A-Z0-9_]{2,})\s*=\s*(-?\d+(?:\.\d+)?)\s*(?:#.*)?$", re.M)


def constants(src):
    return {m.group(1): m.group(2) for m in CONST.finditer(src)}


def run(target):
    """True when the test class passes."""
    done = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_spot_on." + target],
        cwd=str(ROOT), capture_output=True, text=True)
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
            old = "{} = {}".format(name, found[name])
            assert src.count(old) == 1, old
            with open(TOOL, "w", encoding="utf-8", newline="") as fh:
                fh.write(src.replace(old, "{} = {}".format(name, value), 1))
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
