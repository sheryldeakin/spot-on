"""Did changing the font stack ever actually help? The runs say yes, sometimes decisively.

Written because searching for a typeface was left out of the material search on the
reasoning that a lookalike font is the same bad substitution as a lookalike icon. That
was an analogy from one measurement to a case it never covered, and the runs disagree.

Every attempt whose `changes` line mentions the font is compared with the attempt
before it in the same run: how far the share of text lines with the wrong letter
shapes moved, and how far the match moved. An icon is binary, present or absent, while
a font applies to every line at once, so the two cases are not alike.

    python scripts/font_changes.py      # writes scripts/font-changes.json
"""
import json
import re
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOUCHED = re.compile(r"font|typeface|typograph", re.I)


def attempts(run_dir):
    out = []
    for path in sorted((run_dir / "attempts").glob("*.json"),
                       key=lambda p: int(p.stem)):
        try:
            a = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        glyph = ((a.get("report") or {}).get("elements") or {}).get("glyph") or {}
        out.append({"n": int(path.stem), "match": a.get("match"),
                    "weak_share": glyph.get("weak_share"),
                    "changes": a.get("changes") or ""})
    return out


def main():
    pairs = []
    for run_dir in sorted(p for p in (ROOT / "runs").iterdir() if p.is_dir()):
        seq = attempts(run_dir)
        for before, after in zip(seq, seq[1:]):
            if not TOUCHED.search(after["changes"]):
                continue
            if before["match"] is None or after["match"] is None:
                continue
            pairs.append({
                "run": run_dir.name, "n": after["n"],
                "match_change": round(after["match"] - before["match"], 3),
                "weak_change": (None if before["weak_share"] is None
                                or after["weak_share"] is None
                                else round(after["weak_share"] - before["weak_share"], 4)),
                "weak_before": before["weak_share"], "weak_after": after["weak_share"]})

    measured = [p for p in pairs if p["weak_change"] is not None]
    big = [p for p in measured if p["weak_change"] <= -0.3]
    cleared = [p for p in measured if p["weak_before"] == 1.0 and p["weak_after"] == 0.0]
    deltas = [p["match_change"] for p in pairs]

    out = {
        "font_changes": len(pairs),
        "with_a_letter_measure": len(measured),
        "cut_weak_share_by_0_3_or_more": len(big),
        "went_from_every_line_wrong_to_none": len(cleared),
        "best_match_change": max(deltas) if deltas else None,
        "median_match_change": round(statistics.median(deltas), 3) if deltas else None,
        "mean_match_change": round(sum(deltas) / len(deltas), 3) if deltas else None,
        "pairs": pairs,
    }
    path = ROOT / "scripts" / "font-changes.json"
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print("{} font changes, {} cut the wrong-letter share by 0.3 or more, "
          "{} went from every line wrong to none, best match change {:+.1f}".format(
              out["font_changes"], out["cut_weak_share_by_0_3_or_more"],
              out["went_from_every_line_wrong_to_none"], out["best_match_change"]))
    print("wrote", path)


if __name__ == "__main__":
    main()
