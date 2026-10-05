"""What rounds actually put in `blocked`, and whether the filter reads them right.

The field is meant to carry what stopped the round: a refused command, a library
that would not load, a file that was not there. Rounds also use it for the material
the design needs and nobody has, which the needs list already owns and states better,
with what the material is worth and how to hand it over. Said in both places, the
same impossible job is put in front of every later round as if it were work.

Narrowing the schema wording moved this from every use to two of six. This reads
every `blocked` line every round on this machine has ever written and checks the
filter against them, so the claim is a count off the runs rather than a guess.

    python scripts/blocked_lines.py        # writes scripts/blocked-lines.json
"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("spot_on", ROOT / "spot-on.py")
so = importlib.util.module_from_spec(spec)
sys.modules["spot_on"] = so
spec.loader.exec_module(so)

# Read off the lines by hand once, which is the only way to have an answer to score
# against. A line is a material gap when the thing stopping the round is something
# nobody on this machine has, rather than something that failed.
BY_HAND = {
    "globe and cityscape artwork and the design's typeface are not available as assets": True,
    "the design's typeface is not installed, so item 1 cannot be fixed in code.": True,
    "the globe artwork is a photograph-like render and cannot be reproduced by code.": True,
}


def main():
    rows = []
    for path in sorted((ROOT / "runs").glob("*/attempts/*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for line in (rec.get("blocked") or []):
            text = str(line).strip()
            if not text:
                continue
            rows.append({"run": path.parent.parent.name, "n": rec.get("n"),
                         "text": text,
                         "gap": bool(so._is_material_gap(text)),
                         "by_hand": BY_HAND.get(text.lower())})

    judged = [r for r in rows if r["by_hand"] is not None or not r["gap"]]
    # Anything not read by hand is taken to be a real blocker, which is the
    # conservative reading: the filter is wrong if it drops one of those.
    right = sum(1 for r in rows
                if r["gap"] == (r["by_hand"] if r["by_hand"] is not None else False))
    out = {"runs_scanned": len(set(r["run"] for r in rows)),
           "attempt_files": len(list((ROOT / "runs").glob("*/attempts/*.json"))),
           "blocked_lines": len(rows),
           "material_gaps": sum(1 for r in rows if r["gap"]),
           "real_blockers": sum(1 for r in rows if not r["gap"]),
           "agreed_with_hand": right, "judged": len(rows), "rows": rows}
    path = ROOT / "scripts" / "blocked-lines.json"
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")

    print("{} blocked lines across {} attempt files".format(
        out["blocked_lines"], out["attempt_files"]))
    print()
    for r in rows:
        print("  {:<6} [{}#{}] {}".format(
            "DROP" if r["gap"] else "keep", r["run"], r["n"], r["text"][:96]))
    print()
    print("{} material gaps the needs list already owns, {} real blockers, "
          "{} of {} agreeing with the hand reading".format(
              out["material_gaps"], out["real_blockers"],
              out["agreed_with_hand"], out["judged"]))
    print("wrote", path)


if __name__ == "__main__":
    main()
