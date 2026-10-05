"""Can the findings tie-break ever fire, and on which run?

The tie-break only acts when two candidates in a round land within REPAIR_TOLERANCE
of the run's best and differ in how many findings they leave outstanding. Twice now a
trial of it was spent on a subject where that could not happen: once the candidates
were 12 to 24 points apart, once the seed had no findings at all. So this counts the
opportunities that already exist in the runs on this machine, before anything is
built or run.

An opportunity is a round where at least two candidates sit inside the tolerance of
that round's best AND their penalties differ. Anything else is a round where the
tie-break would choose exactly what fidelity alone chooses.

    python scripts/tiebreak_survey.py        # writes scripts/tiebreak-survey.json
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


def rounds_of(run_dir):
    """Attempts grouped by the round that produced them, via candidate_of."""
    by_round = {}
    for path in sorted((run_dir / "attempts").glob("*.json"), key=lambda p: int(p.stem)):
        try:
            a = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        a["n"] = int(path.stem)
        key = a.get("candidate_of")
        if key is None:
            continue
        by_round.setdefault(key, []).append(a)
    return by_round


def main():
    out = []
    for run_dir in sorted(p for p in (ROOT / "runs").iterdir() if p.is_dir()):
        if not (run_dir / "attempts").is_dir():
            continue
        best_ever = 0.0
        chances = 0
        rounds = 0
        detail = []
        for base_n, cands in sorted(rounds_of(run_dir).items()):
            if len(cands) < 2:
                continue
            rounds += 1
            scores = [so.rank_of(c) for c in cands]
            top = max(scores)
            best_ever = max(best_ever, top)
            band = [c for c in cands if so.rank_of(c) >= top - so.REPAIR_TOLERANCE]
            penalties = set(round(so._penalty_of(c), 4) for c in band)
            if len(band) >= 2 and len(penalties) > 1:
                chances += 1
                winner = max(band, key=so.rank_of)
                repaired = min(band, key=lambda c: (so._penalty_of(c), -so.rank_of(c)))
                detail.append({
                    "base": base_n,
                    "candidates": [{"n": c["n"], "match": c["match"],
                                    "penalty": round(so._penalty_of(c), 3)} for c in band],
                    "would_change_pick": winner["n"] != repaired["n"],
                    "fidelity_given_up": round(so.rank_of(winner) - so.rank_of(repaired), 3),
                })
        if rounds:
            out.append({"run": run_dir.name, "rounds_with_candidates": rounds,
                        "rounds_the_tiebreak_could_act_on": chances,
                        "rounds_it_would_change_the_pick":
                            sum(1 for d in detail if d["would_change_pick"]),
                        "best_match": round(best_ever, 1), "detail": detail})

    out.sort(key=lambda r: -r["rounds_the_tiebreak_could_act_on"])
    path = ROOT / "scripts" / "tiebreak-survey.json"
    path.write_text(json.dumps({"tolerance": so.REPAIR_TOLERANCE, "runs": out}, indent=1),
                    encoding="utf-8")

    print("tolerance {}".format(so.REPAIR_TOLERANCE))
    print("{:<34} {:>6} {:>8} {:>8} {:>7}".format(
        "run", "rounds", "could", "would", "best"))
    for r in out:
        print("{:<34} {:>6} {:>8} {:>8} {:>7}".format(
            r["run"][:34], r["rounds_with_candidates"],
            r["rounds_the_tiebreak_could_act_on"],
            r["rounds_it_would_change_the_pick"], r["best_match"]))
    total = sum(r["rounds_the_tiebreak_could_act_on"] for r in out)
    changed = sum(r["rounds_it_would_change_the_pick"] for r in out)
    seen = sum(r["rounds_with_candidates"] for r in out)
    print()
    print("across {} rounds with more than one candidate, the tie-break could act on "
          "{} and would change the pick on {}".format(seen, total, changed))
    print("wrote", path)


if __name__ == "__main__":
    main()
